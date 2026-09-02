#!/usr/bin/env python3
"""从 src/kingdee_mcp/server.py 静态抽取 Ontology 实例层。

只做 AST 解析，不导入 server 模块（避免触发登录/网络）。
产出 docs/ontology/model/*.yml 的 instances 段，保证「实例」永远与代码同步。

用法：
    python3 tools/ontology/extract_ontology.py            # 打印摘要
    python3 tools/ontology/extract_ontology.py --write    # 写回 model/*.instances.yml
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "src" / "kingdee_mcp" / "server.py"
OUT_DIR = ROOT / "docs" / "ontology" / "model"

# 动词分类：MCP 工具名前缀/词根 → 本体动词
VERB_OF_TOOL = {
    "query": "Query", "view": "Read", "discover": "Discover", "describe": "Discover",
    "list": "Discover", "get": "Read", "validate": "Validate", "refresh": "Refresh",
    "save": "Save", "submit": "Submit", "audit": "Audit", "unaudit": "Unaudit",
    "delete": "Delete", "cancel": "Cancel", "void": "Void", "close": "Close",
    "unclose": "Unclose", "forbid": "Forbid", "enable": "Enable", "push": "Push",
    "create": "Composite", "workflow": "Approve", "usage": "Introspect",
}

# 与状态迁移相关的写动词（原子性审计重点）
WRITE_VERBS = {
    "Save", "Submit", "Audit", "Unaudit", "Delete", "Cancel", "Void",
    "Close", "Unclose", "Forbid", "Enable", "Push", "Composite", "Approve", "Refresh",
}


def _load_tree() -> ast.Module:
    return ast.parse(SERVER.read_text(encoding="utf-8"))


def extract_tools(tree: ast.Module) -> list[dict]:
    """抽取所有 @mcp.tool 装饰的工具及其 annotations。"""
    rows: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "tool"):
                continue
            kw = {k.arg: k.value for k in dec.keywords if k.arg}
            name = kw["name"].value if isinstance(kw.get("name"), ast.Constant) else node.name
            ann: dict = {}
            if isinstance(kw.get("annotations"), ast.Dict):
                for k, v in zip(kw["annotations"].keys, kw["annotations"].values):
                    if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                        ann[k.value] = v.value
            body_src = ast.unparse(node)
            rows.append({
                "tool": name,
                "line": node.lineno,
                "input_model": (ast.unparse(node.args.args[0].annotation)
                                if node.args.args and node.args.args[0].annotation else None),
                "title": ann.get("title"),
                "read_only": ann.get("readOnlyHint"),
                "destructive": ann.get("destructiveHint"),
                "idempotent": ann.get("idempotentHint"),
                "verb": _verb_of(name),
                # 该工具体内实际打到的 WebAPI 端点（原子性判定的关键证据）
                "endpoints": sorted(_endpoints_called(body_src)),
                "per_id_loop": "for bill_id in params.bill_ids" in body_src
                               or "for fid in target_fids" in body_src,
                "ids_joined": '",".join' in body_src,
                "src": body_src,
                # _result_status(result, "<label>") 里的标签才是 DOC_LIFECYCLE 的键，
                # 传输端点名（execute/cancel_assign）并不是。
                "op_labels": sorted(_result_status_labels(body_src)),
            })
    rows.sort(key=lambda r: r["line"])
    return rows


def _result_status_labels(body_src: str) -> set[str]:
    """抽出函数体内 _result_status(..., "<label>") 与 _run_execute_action 的 op_label。"""
    found: set[str] = set()
    try:
        fn = ast.parse(body_src).body[0]
    except SyntaxError:
        return found
    for n in ast.walk(fn):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)):
            continue
        if n.func.id == "_result_status" and len(n.args) >= 2 \
                and isinstance(n.args[1], ast.Constant):
            found.add(n.args[1].value)
        if n.func.id == "_run_execute_action" and len(n.args) >= 3 \
                and isinstance(n.args[2], ast.Constant):
            found.add(n.args[2].value)
    return found


def _endpoints_called(body_src: str) -> set[str]:
    """找出函数体内 _post_raw(<ep>, ...) 的第一个字面量实参。"""
    found: set[str] = set()
    try:
        fn = ast.parse(body_src).body[0]
    except SyntaxError:
        return found
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id in ("_post_raw", "_post") and n.args
                and isinstance(n.args[0], ast.Constant)):
            found.add(n.args[0].value)
    # _run_execute_action(..., endpoint=...) 间接调用
    if "_run_execute_action" in body_src:
        found.add("cancel_assign" if "cancel_assign" in body_src else "execute")
    return found


def _verb_of(tool: str) -> str:
    parts = tool.replace("kingdee_", "").split("_")
    for p in parts:
        if p in VERB_OF_TOOL:
            return VERB_OF_TOOL[p]
    return "Unknown"


def extract_const(tree: ast.Module, name: str):
    for n in tree.body:
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) \
                and n.targets[0].id == name:
            try:
                return ast.literal_eval(n.value)
            except Exception:
                return None
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) \
                and n.target.id == name:
            try:
                return ast.literal_eval(n.value)
            except Exception:
                return None
    return None


def extract_links(tree: ast.Module) -> list[dict]:
    """抽取硬编码的下推链接（source_form → target_form）。"""
    links: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if "push" not in _endpoints_called(ast.unparse(node)):
            continue
        target = source = None
        for n in ast.walk(node):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                    and n.func.id == "_post_raw" and len(n.args) >= 2 \
                    and isinstance(n.args[0], ast.Constant) and n.args[0].value == "push" \
                    and isinstance(n.args[1], ast.Constant):
                source = n.args[1].value
            if isinstance(n, ast.Dict):
                for k, v in zip(n.keys, n.values):
                    if isinstance(k, ast.Constant) and k.value == "TargetFormId" \
                            and isinstance(v, ast.Constant):
                        target = v.value
        if source or target:
            links.append({
                "tool": node.name, "line": node.lineno,
                "source_form": source or "<param:form_id>",
                "target_form": target or "<param:target_form_id>",
                "hardcoded": bool(source and target),
            })
    return links


def build(tree: ast.Module) -> dict:
    tools = extract_tools(tree)
    return {
        "nouns": extract_const(tree, "FORM_CATALOG") or {},
        "verbs": tools,
        "states": extract_const(tree, "DOC_LIFECYCLE") or {},
        "links": extract_links(tree),
        "rules": {
            "known_error_patterns": extract_const(tree, "KNOWN_ERROR_PATTERNS") or [],
            "known_error_next_actions": extract_const(tree, "KNOWN_ERROR_NEXT_ACTIONS") or {},
        },
    }


def summarize(m: dict) -> str:
    tools = m["verbs"]
    writes = [t for t in tools if t["verb"] in WRITE_VERBS]
    lines = [
        f"名词 Noun (FORM_CATALOG)      : {len(m['nouns'])}",
        f"动词 Verb (MCP tools)         : {len(tools)}  (写 {len(writes)} / 读 {len(tools) - len(writes)})",
        f"状态 State (DOC_LIFECYCLE)    : {len(m['states'])}  覆盖动词 {sorted(m['states'])}",
        f"链接 Link (push 关系)         : {len(m['links'])}  其中硬编码 "
        f"{sum(1 for l in m['links'] if l['hardcoded'])}",
        f"规则 Rule (错误模式)          : {len(m['rules']['known_error_patterns'])}",
        "",
        "— 动词分布 —",
    ]
    dist: dict[str, int] = {}
    for t in tools:
        dist[t["verb"]] = dist.get(t["verb"], 0) + 1
    for v, c in sorted(dist.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {v:<12} {c}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="写出 JSON 实例快照")
    args = ap.parse_args()

    tree = _load_tree()
    model = build(tree)
    print(summarize(model))

    if args.write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out = OUT_DIR / "instances.snapshot.json"
        slim = dict(model)
        slim["verbs"] = [{k: v for k, v in t.items() if k != "src"} for t in model["verbs"]]
        out.write_text(json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出 {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
