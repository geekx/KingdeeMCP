#!/usr/bin/env python3
"""实测 MCP 工具面的 token 成本 —— 每次会话开口前就要付的固定开销。

    python3 tools/ontology/measure_tool_surface.py           # 原 97 工具结构
    python3 tools/ontology/measure_tool_surface.py --base    # 7 工具底座
    python3 tools/ontology/measure_tool_surface.py --both    # 对比

token 用简单启发式估算（中文 ≈1 token/字，其余 ≈1 token/4 字符）。
绝对值会有出入，但两种结构用同一把尺子量，比例是可靠的。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("KINGDEE_PASSWORD", "-")


def _est_tokens(blob: str) -> int:
    cjk = sum(1 for c in blob if "一" <= c <= "鿿")
    return cjk + (len(blob) - cjk) // 4


async def _surface(mcp) -> dict:
    tools = await mcp.list_tools()
    payload = [{"name": t.name, "description": t.description or "",
                "inputSchema": t.inputSchema,
                "annotations": t.annotations.model_dump() if t.annotations else None}
               for t in tools]
    blob = json.dumps(payload, ensure_ascii=False)
    desc = sum(len(t["description"]) for t in payload)
    schema = sum(len(json.dumps(t["inputSchema"], ensure_ascii=False)) for t in payload)
    return {"tools": len(tools), "bytes": len(blob.encode("utf-8")),
            "chars": len(blob), "est_tokens": _est_tokens(blob),
            "description_share": round(desc * 100 / len(blob)),
            "schema_share": round(schema * 100 / len(blob)),
            "names": [t.name for t in tools],
            "largest": [(t["name"], len(json.dumps(t, ensure_ascii=False)))
                        for t in sorted(payload,
                                        key=lambda x: -len(json.dumps(x, ensure_ascii=False)))[:5]]}


def _report(label: str, s: dict) -> None:
    print(f"── {label} ──")
    print(f"  工具数        : {s['tools']}")
    print(f"  tools/list    : {s['bytes']:,} 字节 / {s['chars']:,} 字符")
    print(f"  估算 token    : ~{s['est_tokens']:,}"
          f"   （200k 上下文的 {s['est_tokens']*100//200000}%）")
    print(f"  其中 schema   : {s['schema_share']}%   description: {s['description_share']}%")
    if s["tools"] > 10:
        print("  最占位的 5 个 :")
        for n, c in s["largest"]:
            print(f"      {n:<40} {c:>6} 字符")
    else:
        print(f"  工具          : {s['names']}")
    print()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", action="store_true", help="只量底座")
    ap.add_argument("--both", action="store_true", help="对比两者")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv[1:])

    out: dict[str, dict] = {}
    if not args.base or args.both:
        from kingdee_mcp.server import mcp as legacy
        out["legacy"] = asyncio.run(_surface(legacy))
    if args.base or args.both:
        from kingdee_ontology.base.server import mcp as base_mcp
        out["base"] = asyncio.run(_surface(base_mcp))

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    for k, label in (("legacy", "原结构（专用工具）"), ("base", "MCP 底座（通用动词）")):
        if k in out:
            _report(label, out[k])
    if len(out) == 2:
        a, b = out["legacy"]["est_tokens"], out["base"]["est_tokens"]
        print(f"══ 对比 ══\n  {a:,} → {b:,} token，降低 {100 - b * 100 // a}%"
              f"（每次会话省下 ~{a - b:,} token）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
