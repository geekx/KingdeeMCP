"""线 —— 每个字段是怎么来的。

数据加工层的第一件事不是加工，是**说清来源**。一个属性值可能来自：
金蝶 WebAPI 的某个字段、SQL 表的某一列、由别的字段推导、或者压根没取到。
不记来源，下游看到一个值就只能当它是真的；出错时也无从回溯是哪一步弄坏的。

三条约束：
  1. 每个产出列都必须有来源，没有来源的列不允许存在（宁可少一列）；
  2. 推导列必须记下它依赖哪些列，否则改上游时不知道会影响谁；
  3. 取不到的列显式标 MISSING，与"取到了但是空值"区分——
     这两者在 ERP 里意义完全不同（没这个字段 vs 这个字段是空的）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Origin(str, Enum):
    WEBAPI = "webapi"          # 金蝶 WebAPI 返回的字段
    SQL = "sql"                # SQL Server 目录/表的列
    DERIVED = "derived"        # 由其它列推导
    REGISTRY = "registry"      # 来自本体注册表（如规范状态码）
    MISSING = "missing"        # 应该有但没取到


@dataclass(frozen=True)
class ColumnLineage:
    column: str
    origin: Origin
    source: str = ""                      # 原始字段名 / 表.列 / 规则名
    depends_on: tuple[str, ...] = ()      # 推导列的上游
    note: str = ""

    def to_dict(self) -> dict:
        return {"column": self.column, "origin": self.origin.value,
                "source": self.source, "depends_on": list(self.depends_on),
                "note": self.note}


@dataclass
class Lineage:
    """一次加工的完整血缘。"""
    noun: str
    columns: dict[str, ColumnLineage] = field(default_factory=dict)

    def record(self, column: str, origin: Origin, source: str = "",
               depends_on: tuple[str, ...] = (), note: str = "") -> None:
        self.columns[column] = ColumnLineage(column, origin, source, depends_on, note)

    def missing(self) -> list[str]:
        return sorted(c for c, l in self.columns.items() if l.origin is Origin.MISSING)

    def derived(self) -> list[str]:
        return sorted(c for c, l in self.columns.items() if l.origin is Origin.DERIVED)

    def explain(self, column: str) -> Optional[dict]:
        l = self.columns.get(column)
        return l.to_dict() if l else None

    def to_dict(self) -> dict:
        return {"noun": self.noun,
                "columns": {c: l.to_dict() for c, l in sorted(self.columns.items())},
                "missing": self.missing(), "derived": self.derived()}
