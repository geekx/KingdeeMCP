"""每日回溯 —— 从过程操作审计记录里提炼可执行的改进项。

    python3 -m wikiskill.retro                      # 回溯昨天+今天，更新知识库
    python3 -m wikiskill.retro --day 2026-09-02     # 指定日期
    python3 -m wikiskill.retro --report             # 只看当前可执行项，不写入

自优化的闭环：
    kd_act / kd_push / kd_run  →  operation_audit.jsonl
                                        ↓  本模块
                              wikiskill/knowledge.json（累积证据、涨置信度）
                                        ↓  达到 medium 以上
                    改 base/registry.yml · profiles/<租户>/profile.yml · 代码
                                        ↓
                              下次回溯该现象消失 → 条目自然沉底

关键克制：本模块**只提议，不自动改配置**。自动改 ERP 的操作定义是危险的；
知识条目要经人 adopt 才落地，adopt/reject 的决定也被记住，不会每天重提。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "ontology"))

from operation_audit import dangling_traces, load as load_audit  # noqa: E402
from wikiskill.knowledge import Entry, Knowledge  # noqa: E402

# 低于这个次数不成条目——避免把偶发噪声写进知识库
_MIN_OCCURRENCES = 2
_SLOW_MS = 5000


def _sig(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _day_of(rec: dict) -> str:
    return (rec.get("occurred_at") or "")[:10]


def _norm_error(msg: str) -> str:
    """把错误消息里的可变部分抹掉，让同类错误归到同一条知识上。"""
    import re
    s = re.sub(r"\d+", "N", msg or "")
    s = re.sub(r"[A-Z]{2,}\d*N+", "BILLNO", s)
    return s.strip()[:120]


# ── 提炼规则 ────────────────────────────────────────────────────
def derive(records: list[dict]) -> list[Entry]:
    out: list[Entry] = []
    if not records:
        return out

    # 规则 1：反复失败的 (名词, 动词, 错误) 组合
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for r in records:
        if r.get("outcome") not in ("failed", "partial"):
            continue
        msg = _norm_error((r.get("error") or {}).get("message", ""))
        buckets[(r.get("noun", "?"), r.get("verb", "?"), msg)].append(r)
    for (noun, verb, msg), rs in buckets.items():
        if len(rs) < _MIN_OCCURRENCES:
            continue
        field = next((((r.get("error") or {}).get("field") or "") for r in rs
                      if (r.get("error") or {}).get("field")), "")
        out.append(Entry(
            id=_sig("failure", noun, verb, msg), kind="failure_pattern",
            title=f"{noun} 的 {verb} 反复失败：{msg[:40]}",
            detail=f"{len(rs)} 次失败，错误「{msg}」"
                   + (f"，涉及字段 {field}" if field else ""),
            suggestion=(
                f"把这条错误加进 KNOWN_ERROR_PATTERNS，附上 reason/suggestion，"
                f"让调用方一次就能改对；"
                + (f"若 {field} 是必填而模板没给，补进 base/registry.yml 的 "
                   f"{noun}.default_fields 或租户 profile 的字段模板。" if field else
                   f"若这是前置状态问题，给 {verb} 补 requires_state。")),
            occurrences=len(rs),
            evidence=[{"trace_id": r["trace_id"], "object": r.get("object_no") or r.get("object_id"),
                       "at": r.get("occurred_at")} for r in rs[:5]]))

    # 规则 2：悬挂操作链（中间态没人清算）
    for d in dangling_traces(records):
        tool, step = d["tool"], d["halted_at_step"]
        rs = [r for r in records if r["trace_id"] == d["trace_id"]]
        out.append(Entry(
            id=_sig("dangling", tool, str(step)), kind="dangling",
            title=f"{tool} 反复停在第 {step} 步，留下中间态",
            detail=f"末状态 {d['last_state']}（{d['last_outcome']}），"
                   f"遗留对象 {d['left_objects']}",
            suggestion=(
                f"这些单据处于中间态且不会自动回滚。短期：人工处理 {d['left_objects']}；"
                f"长期：给该操作补一步『确认』把风险前移，或在第 {step} 步失败时"
                f"执行补偿动作（{'删除草稿' if step == 1 else '反审核后删除'}），"
                f"并把补偿记进 compensated_by。"),
            occurrences=1,
            evidence=[{"trace_id": d["trace_id"], "left": d["left_objects"],
                       "at": rs[0].get("occurred_at") if rs else ""}]))

    # 规则 3：结果不可判定（传输不稳，重试有重复建单风险）
    unknowns = [r for r in records if r.get("outcome") == "unknown"]
    if len(unknowns) >= _MIN_OCCURRENCES:
        by_ep: dict[str, int] = defaultdict(int)
        for r in unknowns:
            by_ep[r.get("endpoint") or "?"] += 1
        out.append(Entry(
            id=_sig("unknown", ",".join(sorted(by_ep))), kind="flaky",
            title=f"{len(unknowns)} 次写操作结果不可判定",
            detail=f"按端点分布 {dict(by_ep)}。这类失败**不等于没生效**，"
                   f"服务端可能已经建单。",
            suggestion=("排查网络/超时配置；在重试前必须先 kd_query 查证对象是否已存在。"
                        "长期方案是给 save/push 加客户端幂等键（审计 A-5），"
                        "让重试可以安全去重。"),
            occurrences=len(unknowns),
            evidence=[{"trace_id": r["trace_id"], "endpoint": r.get("endpoint"),
                       "at": r.get("occurred_at")} for r in unknowns[:5]]))

    # 规则 4：被前置规则拦下的未登记下推（说明注册表缺条目）
    blocked: dict[str, int] = defaultdict(int)
    for r in records:
        err = (r.get("error") or {}).get("message", "")
        if "未登记的下推关系" in err:
            blocked[err.split("。")[0]] += 1
    for msg, n in blocked.items():
        if n < _MIN_OCCURRENCES:
            continue
        out.append(Entry(
            id=_sig("unlinked", msg), kind="unlinked_push",
            title=f"反复尝试未登记的下推：{msg[:50]}",
            detail=f"{n} 次被 PRE-02 拦下。",
            suggestion=("要么这条转换关系在本账套确实存在——那就补进 "
                        "profiles/<租户>/profile.yml 的 links 段（业务人员可自行填写，"
                        "见 profiles/README.md）；要么是调用方用错了目标单，"
                        "需要在业务操作入口里固化正确的下推链路。"),
            occurrences=n,
            evidence=[{"message": msg}]))

    # 规则 5：慢操作
    slow = [r for r in records if (r.get("duration_ms") or 0) > _SLOW_MS]
    if len(slow) >= _MIN_OCCURRENCES:
        by_verb: dict[str, list[float]] = defaultdict(list)
        for r in slow:
            by_verb[f"{r.get('noun')}.{r.get('verb')}"].append(r["duration_ms"])
        worst = max(by_verb.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))
        out.append(Entry(
            id=_sig("slow", worst[0]), kind="slow",
            title=f"{worst[0]} 平均耗时 {sum(worst[1])/len(worst[1]):.0f}ms",
            detail=f"{len(slow)} 次超过 {_SLOW_MS}ms，最慢分布 "
                   f"{ {k: round(sum(v)/len(v)) for k, v in by_verb.items()} }",
            suggestion="收窄查询字段集或加过滤条件；批量动词考虑减小每批目标数。",
            occurrences=len(slow),
            evidence=[{"trace_id": r["trace_id"], "ms": r["duration_ms"]} for r in slow[:5]]))

    return out


# ── 回溯主流程 ──────────────────────────────────────────────────
def retro(audit_path: str, store: str, days: Iterable[str],
          write: bool = True) -> dict:
    all_recs = load_audit(audit_path)
    day_set = set(days)
    recs = [r for r in all_recs if _day_of(r) in day_set] if day_set else all_recs

    k = Knowledge(store)
    stats = {"new": 0, "reinforced": 0, "skipped": 0}
    day = max(day_set) if day_set else date.today().isoformat()
    for obs in derive(recs):
        _, action = k.merge(obs, day=day)
        stats[action] += 1
    if write:
        k.save()

    return {
        "day": day, "records_scanned": len(recs), "total_records": len(all_recs),
        "entries": stats, "knowledge_size": len(k.entries),
        "actionable": [
            {"id": e.id, "confidence": e.confidence, "occurrences": e.occurrences,
             "days": len(e.days), "title": e.title, "suggestion": e.suggestion}
            for e in sorted(k.actionable(), key=lambda x: -x.occurrences)],
    }


def render(result: dict) -> str:
    lines = [f"# 每日回溯 {result['day']}", "",
             f"扫描 {result['records_scanned']} 条操作记录"
             f"（知识库累计 {result['knowledge_size']} 条）：",
             f"新增 {result['entries']['new']} · "
             f"强化 {result['entries']['reinforced']} · "
             f"已否决跳过 {result['entries']['skipped']}", ""]
    act = result["actionable"]
    if not act:
        lines += ["今天没有达到 medium 置信度的可执行项。",
                  "（只出现一两次的观察会先积累证据，跨天反复出现才会浮上来。）"]
    else:
        lines.append(f"## 可执行项（{len(act)}）")
        for e in act:
            lines += ["", f"### [{e['confidence']}] {e['title']}",
                      f"- 累计 {e['occurrences']} 次，跨 {e['days']} 天　`{e['id']}`",
                      f"- 建议：{e['suggestion']}",
                      f"- 处置：`python3 -m wikiskill.retro --adopt {e['id']}` "
                      f"或 `--reject {e['id']} --note \"理由\"`"]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="从过程操作审计记录做每日回溯与自优化")
    ap.add_argument("--audit", default=os.environ.get(
        "KINGDEE_OPERATION_AUDIT_LOG", "operation_audit.jsonl"))
    ap.add_argument("--store", default="wikiskill/knowledge.json")
    ap.add_argument("--day", action="append", help="回溯指定日期，可多次；默认昨天+今天")
    ap.add_argument("--all", action="store_true", help="回溯全部历史记录")
    ap.add_argument("--report", action="store_true", help="只输出，不写入知识库")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--adopt", help="把某条知识标记为已采纳")
    ap.add_argument("--reject", help="把某条知识标记为不处理（此后不再刷屏）")
    ap.add_argument("--note", default="", help="配合 --adopt/--reject 的处置说明")
    args = ap.parse_args(argv[1:])

    if args.adopt or args.reject:
        k = Knowledge(args.store)
        eid = args.adopt or args.reject
        e = k.set_status(eid, "adopted" if args.adopt else "rejected", args.note)
        k.save()
        print(f"✓ {eid} → {e.status}" + (f"（{args.note}）" if args.note else ""))
        return 0

    if args.all:
        days: list[str] = []
    elif args.day:
        days = args.day
    else:
        today = date.today()
        days = [(today - timedelta(days=1)).isoformat(), today.isoformat()]

    result = retro(args.audit, args.store, days, write=not args.report)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
