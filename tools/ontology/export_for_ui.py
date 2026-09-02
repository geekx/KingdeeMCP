#!/usr/bin/env python3
"""把本体导出成 Ontology Explorer（界面形态）消费的 JSON。

界面不连账套——它操作的是**本体定义**：有哪些对象类型、它们的属性与状态机、
能施加哪些动作、动作在各状态下是否可用、对象之间怎么连。
所以这份导出必须是纯定义，不含任何实例数据。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from base.objects import ObjectModel          # noqa: E402
from base.ontology import load                # noqa: E402


def build(tenant: str = "") -> dict:
    load.cache_clear()
    o = load(tenant=tenant)
    m = ObjectModel(o)

    types = []
    for n in o.nouns.values():
        ot = m.object_type(n.form_id)
        d = ot.to_dict()
        # 动作里只留该类型特有的部分（参数 schema），契约与前置状态统一从
        # verbs 表取——同一事实只存一份。可用性由界面按 requires_state 现算，
        # 规则就一行（当前状态 ∈ requires_state），不值得把整张矩阵展开存一遍：
        # 581KB 里绝大部分是那张矩阵。
        d["actions"] = [{"verb": a.verb, "params": a.params} for a in ot.actions]
        types.append(d)

    return {
        "generated_by": "tools/ontology/export_for_ui.py",
        "tenant": tenant or "(底座，未叠加租户覆盖层)",
        "counts": {"types": len(types), "verbs": len(o.verbs),
                   "links": len(o.links), "states": len(o.states),
                   "operations": len(o.operations)},
        "states": o.states,
        "state_groups": o.state_groups,
        "verbs": {k: {"zh": v.zh, "kind": v.kind, "arity": v.arity,
                      "atomicity": v.atomicity, "idempotent": v.idempotent,
                      "inverse": v.inverse, "destructive": v.destructive,
                      "requires_state": list(v.requires_state), "to_state": v.to_state}
                  for k, v in o.verbs.items()},
        "links": o.links,
        "operations": {k: {"zh": op.zh, "desc": op.desc, "owner": op.owner,
                           "confirm": op.confirm, "steps": list(op.steps)}
                       for k, op in o.operations.items()},
        "types": types,
    }


def main(argv: list[str]) -> int:
    tenant = argv[1] if len(argv) > 1 else ""
    data = build(tenant)
    out = ROOT / "docs" / "ontology" / "ui" / "ontology.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    kb = out.stat().st_size / 1024
    print(f"导出 {out.relative_to(ROOT)}  {kb:.0f} KB")
    print(f"  {data['counts']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
