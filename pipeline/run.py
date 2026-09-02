"""管道 —— 源 → 解析 → 标准 → 表，全程记血缘与出处。

这一层不认识网络，只认识"给我原始响应"。所以它既能跑真账套（由
Dispatcher 的 transport 供数），也能跑 fixture——**同一条管道**，
不是两套代码。测试跑的就是生产那条路径。
"""
from __future__ import annotations

from typing import Any, Optional

from pipeline.dataset import Dataset, Provenance
from pipeline.lineage import Lineage, Origin
from pipeline.parse import (FieldCountMismatch, flatten_view, is_business_error,
                            rows_from_query)
from pipeline.standardize import Standardizer


class PipelineError(RuntimeError):
    pass


class Pipeline:
    def __init__(self, ontology, tenant: str = ""):
        self.o = ontology
        self.std = Standardizer(ontology)
        self.tenant = tenant

    def from_query(self, noun: str, raw: Any, field_keys: str = "",
                   filter_string: str = "", top: Optional[int] = None) -> Dataset:
        n = self.o.resolve_noun(noun)
        err = is_business_error(raw)
        if err:
            raise PipelineError(
                f"{n.form_id} 查询返回业务异常（HTTP 200 但正文不是 JSON）：{err}")
        fk = field_keys or n.default_fields
        try:
            named = rows_from_query(raw, fk)
        except FieldCountMismatch as e:
            raise PipelineError(
                f"{n.form_id} 的响应与 FieldKeys 对不上：{e} "
                f"—— 位置数组错位会让值悄悄串列，故中断。") from e
        rows, lin = self.std.rows(n.form_id, named)
        self._mark_missing(lin, fk, rows)
        prov = Provenance(source="webapi:query", noun=n.form_id,
                          filter_string=filter_string, field_keys=fk,
                          tenant=self.tenant, row_count=len(rows),
                          truncated=bool(top) and len(rows) >= top)
        return Dataset(n.form_id, rows, lin, prov)

    def from_view(self, noun: str, raw: Any) -> Dataset:
        n = self.o.resolve_noun(noun)
        err = is_business_error(raw)
        if err:
            raise PipelineError(f"{n.form_id} 查看详情返回业务异常：{err}")
        data = raw.get("Result", {}).get("Result", raw) if isinstance(raw, dict) else raw
        lin = Lineage(n.form_id)
        flat = flatten_view(data, lin)
        rows, lin2 = self.std.rows(n.form_id, [flat] if flat else [])
        for c, l in lin.columns.items():
            lin2.columns.setdefault(c, l)
        prov = Provenance(source="webapi:view", noun=n.form_id,
                          tenant=self.tenant, row_count=len(rows))
        return Dataset(n.form_id, rows, lin2, prov)

    def from_fixture(self, noun: str, rows: list[dict], note: str = "") -> Dataset:
        """离线数据走同一条标准化路径，测试与生产不分叉。"""
        n = self.o.resolve_noun(noun)
        std, lin = self.std.rows(n.form_id, rows)
        prov = Provenance(source="fixture", noun=n.form_id, tenant=self.tenant,
                          filter_string=note, row_count=len(std))
        return Dataset(n.form_id, std, lin, prov)

    def _mark_missing(self, lin: Lineage, field_keys: str, rows: list[dict]) -> None:
        """请求了但一行都没返回的列，标 MISSING。

        与"取到了但是空值"区分：前者说明这个字段在本账套可能不存在
        （二开删了/改名了），后者只是这批单据没填。两者的处置完全不同。
        """
        asked = {c.strip() for c in (field_keys or "").split(",") if c.strip()}
        present = {k for r in rows for k in r}
        for c in sorted(asked - present):
            canon = self.std.canonical_name(c)
            if canon not in present:
                lin.record(c, Origin.MISSING, source=c,
                           note="请求了但响应里一行都没有——该字段在本账套可能不存在")
