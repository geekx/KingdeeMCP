"""表 —— 一次加工的产出：带 schema、血缘与出处的行集。

不是随便一个 list[dict]。一张表必须能回答三个问题：
  这些行是什么对象？（noun）
  每一列从哪来？（lineage）
  这批数据是什么时候、用什么条件取的？（provenance）

第三条尤其重要：ERP 数据是活的，一批查询结果只在取的那一刻成立。
不记出处的话，下游把陈旧数据当现状用，而且没人能发现。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from pipeline.lineage import Lineage


@dataclass
class Provenance:
    """这批数据怎么来的。"""
    source: str                     # webapi:query / webapi:view / sql / fixture
    noun: str
    filter_string: str = ""
    field_keys: str = ""
    fetched_at: str = ""
    tenant: str = ""
    row_count: int = 0
    truncated: bool = False         # 命中 top 上限 —— 说明还有更多没取到

    def __post_init__(self):
        if not self.fetched_at:
            self.fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def age_seconds(self, now: Optional[datetime] = None) -> float:
        t = datetime.fromisoformat(self.fetched_at)
        return ((now or datetime.now(timezone.utc)) - t).total_seconds()


@dataclass
class Dataset:
    noun: str
    rows: list[dict]
    lineage: Lineage
    provenance: Provenance

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self) -> Iterator[dict]:
        return iter(self.rows)

    @property
    def columns(self) -> list[str]:
        seen: dict[str, None] = {}
        for r in self.rows:
            for k in r:
                seen.setdefault(k, None)
        return list(seen)

    def select(self, *cols: str) -> list[dict]:
        return [{c: r.get(c) for c in cols} for r in self.rows]

    def where(self, **eq: Any) -> "Dataset":
        rows = [r for r in self.rows if all(r.get(k) == v for k, v in eq.items())]
        p = Provenance(**{**asdict(self.provenance), "row_count": len(rows)})
        return Dataset(self.noun, rows, self.lineage, p)

    def by_state(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rows:
            out[str(r.get("_state"))] = out.get(str(r.get("_state")), 0) + 1
        return out

    def quality(self) -> dict:
        """数据质量：缺列、空值率、状态取不到的比例。

        `_state is None` 单列出来，因为它不是"空值"而是"归一失败"——
        对象层拿它判动作可用性，取不到就只能标 unverified。
        """
        n = len(self.rows) or 1
        null_rate = {}
        for c in self.columns:
            if c.startswith("_"):
                continue
            miss = sum(1 for r in self.rows if r.get(c) in (None, ""))
            if miss:
                null_rate[c] = round(miss / n, 3)
        unresolved = sum(1 for r in self.rows if r.get("_state") is None)
        return {
            "rows": len(self.rows),
            "columns": len(self.columns),
            "missing_columns": self.lineage.missing(),
            "null_rate": dict(sorted(null_rate.items(), key=lambda kv: -kv[1])[:10]),
            "state_unresolved": unresolved,
            "state_unresolved_rate": round(unresolved / n, 3),
            "truncated": self.provenance.truncated,
            "age_seconds": round(self.provenance.age_seconds()),
        }

    def to_dict(self) -> dict:
        return {"noun": self.noun, "rows": self.rows,
                "lineage": self.lineage.to_dict(),
                "provenance": asdict(self.provenance),
                "quality": self.quality()}

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return p
