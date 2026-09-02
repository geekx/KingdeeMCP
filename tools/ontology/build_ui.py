#!/usr/bin/env python3
"""把本体数据注入 UI 模板，生成可发布的单文件页面。

    python3 tools/ontology/export_for_ui.py     # 先导出数据
    python3 tools/ontology/build_ui.py          # 再注入模板

分成模板与成品两个文件，是为了让改样式和改数据互不干扰：
_shell.html 手改，explorer.html 永远是生成物（手改会被下次生成覆盖）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "docs" / "ontology" / "ui"
PLACEHOLDER = "__ONTOLOGY_JSON__"


def main() -> int:
    shell, data, out = UI / "_shell.html", UI / "ontology.json", UI / "explorer.html"
    for f in (shell, data):
        if not f.exists():
            print(f"✗ 缺少 {f.relative_to(ROOT)}"
                  + ("；先跑 python3 tools/ontology/export_for_ui.py" if f is data else ""))
            return 1
    tpl = shell.read_text(encoding="utf-8")
    if PLACEHOLDER not in tpl:
        print(f"✗ {shell.name} 里找不到占位符 {PLACEHOLDER}")
        return 1
    html = tpl.replace(PLACEHOLDER, data.read_text(encoding="utf-8"))
    out.write_text(html, encoding="utf-8")
    print(f"✓ {out.relative_to(ROOT)}  {len(html) / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
