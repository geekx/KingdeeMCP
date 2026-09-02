#!/usr/bin/env python3
"""操作原子化审计器 —— 对 server.py + harness/rules.py 做静态一致性检查。

审计维度（对应 Ontology 五元）：
  AT-01 动词  复合动词：一次 MCP 调用打多个写端点 = 无补偿 Saga
  AT-02 动词  批量语义分裂：逐条循环 vs Ids 逗号拼接，原子性级别不一致
  AT-03 动词  注解自洽：同一底层端点的工具 destructiveHint 必须一致；readOnly 不得非幂等
  AT-04 状态  写动词必须在 DOC_LIFECYCLE 中有 from→to 定义
  AT-05 规则  harness 规则硬编码的工具名必须覆盖全部写动词
  AT-06 规则  next_action 词表必须落在 harness 规则可解析的取值域内
  AT-07 链接  硬编码下推链接的源/目标必须在 FORM_CATALOG（名词表）中登记

退出码：0 = 无 error 级发现；1 = 存在 error 级发现。
用法：python3 tools/ontology/audit_atomicity.py [--json]
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_ontology import (  # noqa: E402
    ROOT, WRITE_VERBS, _load_tree, build,
)

RULES_PY = ROOT / "harness" / "rules.py"
HARNESS_TOOLS_PY = ROOT / "harness" / "tools.py"
SERVER_PY = ROOT / "src" / "kingdee_mcp" / "server.py"

WRITE_ENDPOINTS = {"save", "submit", "audit", "unaudit", "delete",
                   "push", "execute", "cancel_assign"}


def _is_branch_dispatch(fn_src: str) -> bool:
    """判断多端点是否落在同一个 if/elif 的互斥分支里（派发，非顺序 Saga）。"""
    try:
        fn = ast.parse(fn_src).body[0]
    except SyntaxError:
        return False

    def eps(node) -> set[str]:
        found = set()
        for n in ast.walk(node):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id in ("_post_raw", "_post") and n.args
                    and isinstance(n.args[0], ast.Constant)):
                found.add(n.args[0].value)
        return found

    total = eps(fn)
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        branches, cur = [], node
        while True:
            branches.append(cur.body)
            if len(cur.orelse) == 1 and isinstance(cur.orelse[0], ast.If):
                cur = cur.orelse[0]
            else:
                if cur.orelse:
                    branches.append(cur.orelse)
                break
        seen: set[str] = set()
        for b in branches:
            be: set[str] = set()
            for stmt in b:
                be |= eps(stmt)
            if be & seen:          # 同一端点出现在多个分支 → 不是纯派发
                seen = set()
                break
            seen |= be
        if seen == total and len(seen) > 1:
            return True
    return False


def _finding(fid, level, title, evidence, detail):
    return {"id": fid, "level": level, "title": title,
            "evidence": evidence, "detail": detail}


def audit(model: dict) -> list[dict]:
    out: list[dict] = []
    tools = model["verbs"]
    by_name = {t["tool"]: t for t in tools}
    writes = [t for t in tools if t["read_only"] is False]

    # ── AT-01 复合动词 ────────────────────────────────────────────
    for t in writes:
        weps = [e for e in t["endpoints"] if e in WRITE_ENDPOINTS]
        if len(weps) <= 1:
            continue
        if _is_branch_dispatch(t["src"]):
            out.append(_finding(
                "AT-01", "warning",
                f"动词过载：{t['tool']} 用一个工具名派发到 {len(weps)} 个互斥的写端点",
                f"src/kingdee_mcp/server.py:{t['line']}",
                f"端点 {weps} 落在互斥分支（非顺序 Saga，无中间态风险），但一个工具名承载多个"
                f"语义不同的动词，注解只能取其一，调用方无法按动词做差异化确认。"))
            continue
        # 复合工具本质上不可能是原子的（底层 API 没有事务）。可整改的是
        # **中间态是否可见、可清算**：有没有结构化的待补偿事项、有没有落审计。
        records = ("_record_halt(" in t["src"]
                   or "pending_compensation" in t["src"])
        if records:
            out.append(_finding(
                "AT-01", "warning",
                f"复合动词不可回滚（中间态已可清算）：{t['tool']} 顺序横跨 {len(weps)} 个写端点",
                f"src/kingdee_mcp/server.py:{t['line']}",
                f"端点 {weps}。底层 API 无事务，无法做到原子；但中途失败时会返回结构化的 "
                f"pending_compensation（遗留对象 + 建议补偿动作）并落一条过程操作审计记录，"
                f"可由 kd_audit(scope='dangling') 与 wikiskill 每日回溯查出。"
                f"**补偿仍需人或后续动作执行，不会自动回滚。**"))
        else:
            out.append(_finding(
                "AT-01", "error",
                f"复合动词无补偿：{t['tool']} 单次调用顺序横跨 {len(weps)} 个写端点",
                f"src/kingdee_mcp/server.py:{t['line']}",
                f"端点 {weps}。中途失败时前序端点的副作用已落库且不回滚，"
                f"系统停在中间态，仅靠返回体的 recovery_hint 文本指望调用方补偿。"))

    # ── AT-02 批量语义分裂 ────────────────────────────────────────
    # 存在两种原子性级别本身不是缺陷——它反映了底层 API 的事实。
    # 真正的危害是**调用方无从区分**：两组工具的入参形状一模一样（bill_ids: List[str]），
    # 失败后却要采取完全不同的处置。所以判定依据是「契约是否随结果暴露给调用方」。
    loops = sorted(t["tool"] for t in writes if t["per_id_loop"])
    joins = sorted(t["tool"] for t in writes
                   if "execute" in t["endpoints"] or "cancel_assign" in t["endpoints"])
    if loops and joins:
        # 走共用实现的工具（execute 系全部委托给 _run_execute_action），
        # 契约在那个 helper 里注入，函数体内看不到——跟进委托再判定。
        server_src = SERVER_PY.read_text(encoding="utf-8")
        helper_exposes = "_contract(" in server_src.split("def _run_execute_action")[-1][:2000]

        def _has_contract(t: dict) -> bool:
            src_t = t["src"]
            if ("_contract(" in src_t or '"contract"' in src_t
                    or "'contract'" in src_t):
                return True
            return helper_exposes and "_run_execute_action" in t["src"]

        batch = set(loops) | set(joins)
        exposed = {t["tool"] for t in writes if t["tool"] in batch and _has_contract(t)}
        silent = sorted(batch - exposed)      # 注意括号：| 与 - 同优先级、自左向右
        if silent:
            out.append(_finding(
                "AT-02", "error", "批量语义分裂且契约不可见",
                "server.py:_run_execute_action / kingdee_submit_bills 等",
                f"逐条循环（部分成功、可报告 per-id 结果）：{loops}；"
                f"Ids 逗号拼接单次提交（原子性由服务端决定、无 per-id 结果）：{joins}。"
                f"其中 {silent} 未在返回体中暴露 atomicity 契约，"
                f"调用方无法从签名或结果区分二者，失败后不知道哪些 id 已生效。"))
        else:
            out.append(_finding(
                "AT-02", "info", "批量语义仍分两级，但契约已随结果暴露",
                "server.py:VERB_CONTRACT / _contract()",
                f"per_item：{loops}；server_defined：{joins}。"
                f"两组工具的入参形状仍然相同，但返回体带 contract.atomicity 与"
                f"atomicity_note，调用方可据此决定重试策略。"
                f"彻底消除需统一批量语义或拆分入参类型。"))

    # ── AT-03 注解自洽 ────────────────────────────────────────────
    # execute / cancel_assign 是**多路复用**端点：靠 opNumber 承载彼此不同的操作
    # （作废 / 整单关闭 / 禁用…），破坏性本就应当不同，按端点分组会误报。
    MULTIPLEXED = {"execute", "cancel_assign"}
    by_ep: dict[str, set] = {}
    for t in writes:
        for e in t["endpoints"]:
            if e in MULTIPLEXED:
                continue
            by_ep.setdefault(e, set()).add((t["tool"], t["destructive"]))
    for ep, pairs in sorted(by_ep.items()):
        flags = {d for _, d in pairs}
        if len(flags) > 1:
            out.append(_finding(
                "AT-03", "warning", f"注解冲突：端点 `{ep}` 上 destructiveHint 取值不一致",
                ", ".join(f"{n}={d}" for n, d in sorted(pairs)),
                "同一底层不可逆操作被标注为不同破坏性，下游 Agent 的确认策略会随入口而异。"))
    for t in tools:
        if t["read_only"] is True and t["idempotent"] is False:
            out.append(_finding(
                "AT-03", "warning", f"注解自相矛盾：{t['tool']} readOnly=True 但 idempotent=False",
                f"src/kingdee_mcp/server.py:{t['line']}",
                "只读操作按定义可安全重复；标为非幂等意味着它有副作用，两者不能同时成立。"))

    # ── AT-04 状态覆盖 ────────────────────────────────────────────
    # DOC_LIFECYCLE 的键是 _result_status 的 op 标签，不是传输端点名 ——
    # 按端点校验会把 execute/cancel_assign 这类多路复用端点误报为"无状态定义"。
    covered = set(model["states"])
    uncovered = sorted({lbl for t in writes for lbl in t.get("op_labels", [])
                        if lbl not in covered})
    if uncovered:
        out.append(_finding(
            "AT-04", "error",
            f"状态未定义：{len(uncovered)} 个写动作的 op 标签不在 DOC_LIFECYCLE 中",
            f"server.py:DOC_LIFECYCLE（已覆盖 {sorted(covered)}）",
            f"未定义标签 {uncovered}。_result_status() 对其 lifecycle 取空字典，"
            f"next_action 一律返回 None，等价于向调用方宣告『流程已完成』，"
            f"掩盖了作废/关闭/禁用后的真实状态。"))

    # ── AT-05 规则覆盖 ────────────────────────────────────────────
    # 覆盖率的判定依据随实现走：
    #   旧实现把工具名硬编码在 rules.py 的匹配式里 → 从那里正则提取；
    #   新实现集中登记在 harness/tools.py:WRITE_TOOL_VERBS → 从登记表读。
    # 这条检查本身曾因只认前者而误报（见 docs/ontology/04-audit-trail.md
    # 「审计器自身的可信度」一节自陈的盲区），此处一并修正。
    registered: set[str] = set()
    source = "harness/rules.py（工具名硬编码匹配）"
    if HARNESS_TOOLS_PY.exists():
        tools_src = HARNESS_TOOLS_PY.read_text(encoding="utf-8")
        m = re.search(r"WRITE_TOOL_VERBS[^=]*=\s*\{(.*?)\n\}", tools_src, re.S)
        if m:
            registered = set(re.findall(r'"(kingdee_[a-z_]+)"', m.group(1)))
            source = "harness/tools.py:WRITE_TOOL_VERBS（集中登记表）"
    if not registered:
        rules_src = RULES_PY.read_text(encoding="utf-8") if RULES_PY.exists() else ""
        registered = set(re.findall(r'"(kingdee_[a-z_]+)"', rules_src))

    missed = sorted(t["tool"] for t in writes if t["tool"] not in registered)
    if missed:
        out.append(_finding(
            "AT-05", "error",
            f"约束层覆盖缺口：{len(missed)}/{len(writes)} 个写动词未登记，不受操作链约束",
            source,
            f"未覆盖：{missed}。这些工具产生的操作链不受生命周期完整性/"
            f"下推链完整性/中间态补偿约束。"))

    # ── AT-06 next_action 词表 ────────────────────────────────────
    src = SERVER_PY.read_text(encoding="utf-8")
    emitted = set(re.findall(r'"next_action"\]\s*=\s*"([^"]+)"', src))
    emitted |= {m for m in re.findall(r'"next_action":\s*"([^"]+)"', src)}
    # harness RULE-001 的推导式：next_action.replace('+','_').split('_')[0]
    bad = sorted(v for v in emitted
                 if f"kingdee_{v.replace('+', '_').split('_')[0]}_bills" not in
                 {"kingdee_submit_bills", "kingdee_audit_bills", "kingdee_delete_bills"})
    if bad:
        out.append(_finding(
            "AT-06", "error", "next_action 词表越界，导致 harness 规则恒误报",
            "harness/rules.py:_check_complete_lifecycle / server.py 各 push 工具",
            f"越界取值 {bad}。RULE-001 用 "
            f"`kingdee_{{next_action.replace('+','_').split('_')[0]}}_bills` 反推后继工具名，"
            f"这些取值反推出的工具不存在，规则永远找不到后继操作 → 恒判定操作链不完整。"))

    # ── AT-07 链接登记 ────────────────────────────────────────────
    nouns = set(model["nouns"])
    for lk in model["links"]:
        if not lk["hardcoded"]:
            continue
        missing = [f for f in (lk["source_form"], lk["target_form"]) if f not in nouns]
        if missing:
            out.append(_finding(
                "AT-07", "warning", f"链接端点未在名词表登记：{lk['tool']}",
                f"src/kingdee_mcp/server.py:{lk['line']}",
                f"{lk['source_form']} → {lk['target_form']}，未登记于 FORM_CATALOG 的是 {missing}。"))
    if model["links"]:
        hard = [f"{l['source_form']}→{l['target_form']}" for l in model["links"] if l["hardcoded"]]
        registry = ROOT / "base" / "registry.yml"
        if registry.exists():
            out.append(_finding(
                "AT-07", "info", "legacy 路径的下推链接仍散落在函数体内",
                "server.py 各 push 工具函数体内",
                f"硬编码链接 {hard}。底座已有集中登记表 base/registry.yml:links "
                f"并由 PRE-02 在发请求前校验，但 legacy 的 5 个 push 工具尚未接入，"
                f"仍无法校验『某条下推是否合法』。"))
        else:
            out.append(_finding(
                "AT-07", "info", "下推链接无集中登记表",
                "server.py 各 push 工具函数体内",
                f"硬编码链接 {hard}，加上自由参数形式的入口，"
                f"系统内不存在可校验『某条下推是否合法』的链接表。"))

    order = {"error": 0, "warning": 1, "info": 2}
    out.sort(key=lambda f: (order[f["level"]], f["id"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    findings = audit(build(_load_tree()))
    if args.json:
        print(json.dumps(findings, ensure_ascii=False, indent=2))
    else:
        n_err = sum(1 for f in findings if f["level"] == "error")
        print(f"操作原子化审计：{len(findings)} 项发现（error {n_err}）\n")
        for f in findings:
            print(f"[{f['level'].upper():7}] {f['id']}  {f['title']}")
            print(f"          证据: {f['evidence']}")
            print(f"          说明: {f['detail']}\n")
    return 1 if any(f["level"] == "error" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
