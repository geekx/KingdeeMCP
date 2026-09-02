#!/usr/bin/env python3
"""统计 legacy 只读工具被底座覆盖的比例。

    python3 tools/ontology/measure_convergence.py [--json]

判定依据是每个工具**实际打到的端点与 form_id**，不是工具名。
未覆盖的会列出原因——刻意不覆盖和还没覆盖是两回事，要分清。
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

SERVER = ROOT / "src" / "kingdee_mcp" / "server.py"

# 刻意不收敛的，及理由
# 每条理由要能被单独读懂——这份表会被序列化进 --json 输出，
# 写"同上"在那里就是一句空话。
_SQL_REASON = (
    "SQL Server 目录探查：数据来自数据库系统表而非金蝶 WebAPI，"
    "需要另一套数据库凭据（KINGDEE_SQL_*），且属于可选功能。"
    "把它折叠进 kd_describe 会让同一个工具横跨两个数据源、两套权限模型，"
    "调用方无从判断某次失败是账套问题还是数据库问题。保持独立更清楚。"
)
DELIBERATE = {
    "kingdee_discover_tables": _SQL_REASON + " 本工具按关键字搜数据库表名。",
    "kingdee_discover_columns": _SQL_REASON + " 本工具按关键字搜数据库列名。",
    "kingdee_describe_table": _SQL_REASON + " 本工具读单张表的列定义与索引。",
    "kingdee_discover_metadata_candidates": _SQL_REASON
        + " 本工具从数据库结构反推某单据可能对应的表，是 SQL 侧的启发式推断。",
}


def _tools() -> list[dict]:
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        dec = [d for d in node.decorator_list
               if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
               and d.func.attr == "tool"]
        if not dec:
            continue
        kw = {k.arg: k.value for k in dec[0].keywords if k.arg}
        name = (kw["name"].value if isinstance(kw.get("name"), ast.Constant) else node.name)
        ann = {}
        if isinstance(kw.get("annotations"), ast.Dict):
            for k, v in zip(kw["annotations"].keys, kw["annotations"].values):
                if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                    ann[k.value] = v.value
        if ann.get("readOnlyHint") is not True:
            continue

        forms = {x.args[0].value for x in ast.walk(node)
                 if isinstance(x, ast.Call) and isinstance(x.func, ast.Name)
                 and x.func.id == "_query_payload" and x.args
                 and isinstance(x.args[0], ast.Constant)}
        forms |= {st.value.value for st in ast.walk(node)
                  if isinstance(st, ast.Assign) and isinstance(st.value, ast.Constant)
                  and isinstance(st.value.value, str)
                  and any(getattr(t, "id", "") == "form_id" for t in st.targets)}
        systems = {x.args[1].value for x in ast.walk(node)
                   if isinstance(x, ast.Call) and isinstance(x.func, ast.Name)
                   and x.func.id == "_post_system" and len(x.args) >= 2
                   and isinstance(x.args[1], ast.Constant)}
        eps = {x.args[0].value for x in ast.walk(node)
               if isinstance(x, ast.Call) and isinstance(x.func, ast.Name)
               and x.func.id in ("_post", "_post_raw") and x.args
               and isinstance(x.args[0], ast.Constant)}
        out.append({"tool": name, "forms": sorted(forms | systems),
                    "eps": sorted(eps), "src": ast.unparse(node)})
    return out


def classify(t: dict, nouns: set[str]) -> tuple[str, str]:
    """→ (承载它的底座工具, 原因)。承载工具为空表示未覆盖。"""
    if t["tool"] in DELIBERATE:
        return "", DELIBERATE[t["tool"]]
    if t["tool"] in ("kingdee_usage_report", "kingdee_usage_stats"):
        return "kd_audit(usage)", ""
    if t["tool"] == "kingdee_validate_bill":
        return "kd_act(dry_run)", ""
    if t["tool"] == "kingdee_get_bill_template":
        return "kd_describe(template)", ""
    if t["tool"] in ("kingdee_get_fields", "kingdee_list_forms"):
        return "kd_describe(fields)", ""
    if "view" in t["eps"]:
        return "kd_read", ""
    if "report" in t["eps"]:
        return "kd_report", ""
    if t["forms"]:
        missing = [f for f in t["forms"] if f not in nouns]
        if missing:
            return "", f"form_id 未登记到 base/registry.yml：{missing}"
        return "kd_query", ""
    if "params.form_id" in t["src"] and "_query_payload" in t["src"]:
        return "kd_query", ""
    if "for fid in form_ids" in t["src"]:
        return "kd_query(多名词)", ""
    if "partner_type" in t["src"]:
        # 用参数在两个名词间二选一（客户/供应商），底座直接传名词即可
        return "kd_query", ""
    return "", "端点或 form_id 无法静态判定，需人工确认"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv[1:])

    from base.ontology import load
    load.cache_clear()
    nouns = set(load(tenant="").nouns)

    tools = _tools()
    covered, uncovered = {}, {}
    for t in tools:
        by, why = classify(t, nouns)
        (covered if by else uncovered)[t["tool"]] = by or why

    total = len(tools)
    n_cov = len(covered)
    n_deliberate = sum(1 for k in uncovered if k in DELIBERATE)
    result = {
        "readonly_tools": total, "covered": n_cov,
        "coverage_pct": round(n_cov * 100 / total),
        "uncovered": len(uncovered), "uncovered_deliberate": n_deliberate,
        "by_base_tool": dict(Counter(covered.values()).most_common()),
        "uncovered_detail": uncovered, "registry_nouns": len(nouns),
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"legacy 只读工具 {total} 个 → 底座可表达 {n_cov} 个"
          f"（{result['coverage_pct']}%），注册表名词 {len(nouns)} 个\n")
    print("按承载工具：")
    for k, v in result["by_base_tool"].items():
        print(f"  {k:24} {v}")
    if uncovered:
        print(f"\n未收敛 {len(uncovered)} 个（其中刻意不收 {n_deliberate} 个）：")
        for k, v in sorted(uncovered.items()):
            mark = "刻意" if k in DELIBERATE else "待办"
            print(f"  [{mark}] {k}\n         {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
