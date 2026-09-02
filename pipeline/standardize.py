"""标准 —— 把各账套长得不一样的行，归一成对象层能用的属性。

四件事，每件都对应一个真实踩过的坑：

  状态码   FDocumentStatus='C' → 'C:已审核'。此前中文名与字母码是两套互不
           映射的词表（审计 S-1），对象层判可用性时只能靠猜。
  标识     FID 与 FBillNo 不通用：写操作要内码，下推要编号，系统又没有
           二者的解析动词（审计 L-3）。归一成 _id / _no 两个显式字段，
           **不合并成一个**——合并会掩盖它们本来就是两种东西。
  字段名   同一语义在不同账套下字段名不同（FCustId / FCUSTID，
           FOutStockId / FSendStockId），见审计 F-1。按别名表折算。
  值类型   金蝶返回的数字常是字符串，日期格式不一。转换失败**保留原值并
           记一条 note**，不静默丢弃——丢一个金额比留一个怪值危险得多。
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Optional

from pipeline.lineage import Lineage, Origin

# 同一语义、不同账套的字段名。key 是规范名，值是见过的写法。
# 依据：base/registry.yml 里 13 处 legacy_fields 分歧（审计 F-1）。
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "FCustId": ("FCUSTID", "FCustID", "FCustomerId"),
    "FDate": ("FBillDate",),
    "FOutStockId": ("FSendStockId", "FStockOutId"),
    "FInStockId": ("FReceiveStockId", "FStockInId"),
    "FBaseUnitId": ("FUnitId",),
    "FSaleOrgId": ("FSalesOrgId",),
    "FSalesManId": ("FSalerId", "FSalesmanId"),
}
_ALIAS_TO_CANON = {a: canon for canon, al in FIELD_ALIASES.items() for a in al}

_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")

# 标识符**永远不转数字**。FID='0012' 转成 12 会丢前导零；
# 大内码转 int 还可能碰精度；更要命的是转完之后 FID='100231' 这样的
# 过滤式拼不出来，而这个错误只在真正去查的时候才暴露。
_ID_SUFFIXES = ("id", "no", "number", "code", "billno")


def _is_identifier(field: str) -> bool:
    base = field.split(".")[0].lower()
    return base.endswith(_ID_SUFFIXES) or base in ("fid", "fbillno", "fnumber")
_DATE_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d")


class Standardizer:
    """需要本体（状态定义）才能归一状态码，所以持有一个 Ontology。"""

    def __init__(self, ontology, aliases: Optional[dict] = None):
        self.o = ontology
        self.alias_to_canon = dict(_ALIAS_TO_CANON)
        for canon, al in (aliases or {}).items():
            for a in al:
                self.alias_to_canon[a] = canon

    # ── 单列 ──────────────────────────────────────────────────
    def canonical_name(self, field: str) -> str:
        """把别名折算成规范字段名。带 `.` 的关联字段只折算前半段。"""
        if "." in field:
            base, rest = field.split(".", 1)
            return f"{self.alias_to_canon.get(base, base)}.{rest}"
        return self.alias_to_canon.get(field, field)

    def coerce(self, value: Any, field: str = "") -> tuple[Any, str]:
        """值类型归一。返回 (值, 说明)；说明非空表示做了转换或转换失败。"""
        if value is None or isinstance(value, (int, float, bool)):
            return value, ""
        if not isinstance(value, str):
            return value, ""
        s = value.strip()
        if not s:
            return "", ""
        if field and _is_identifier(field):
            return s, ""            # 标识符保持字符串，见 _is_identifier 的说明
        if _NUM_RE.match(s):
            return (int(s) if "." not in s else float(s)), "字符串数字已转数值"
        for fmt in _DATE_FORMATS:
            try:
                d = datetime.strptime(s[:19] if "T" in s or " " in s else s, fmt)
                return d.date().isoformat(), "日期已归一为 ISO"
            except ValueError:
                continue
        return s, ""

    # ── 整行 ──────────────────────────────────────────────────
    def row(self, noun: str, raw: dict, lineage: Optional[Lineage] = None) -> dict:
        """一行原始数据 → 规范属性。不猜、不丢、不静默。"""
        n = self.o.resolve_noun(noun)
        out: dict[str, Any] = {}
        notes: list[str] = []

        for k, v in raw.items():
            canon = self.canonical_name(k)
            val, note = self.coerce(v, canon)
            out[canon] = val
            if lineage:
                if canon != k:
                    lineage.record(canon, Origin.DERIVED, source=k, depends_on=(k,),
                                   note=f"字段别名折算：{k} → {canon}")
                else:
                    lineage.record(canon, Origin.WEBAPI, source=k, note=note)
            if note:
                notes.append(f"{canon}: {note}")

        # 标识：内码与编号显式分开，**不合并**
        out["_id"] = _first(out, ("FID", "FMaterialId", "FSupplierId", "FUserID",
                                 "FRoleID", "FDetailId"))
        out["_no"] = _first(out, ("FBillNo", "FNumber", "FMoBillNo", "FConfigKey"))
        if lineage:
            lineage.record("_id", Origin.DERIVED, source="内码字段",
                           depends_on=("FID",), note="内码与编号不通用，故分列不合并")
            lineage.record("_no", Origin.DERIVED, source="编号字段",
                           depends_on=("FBillNo",), note="下推用编号，写操作用内码")

        # 状态：字母码 → 规范码
        state = self.state_of(out)
        out["_state"] = state
        if lineage:
            lineage.record("_state", Origin.REGISTRY,
                           source="registry.yml:states",
                           depends_on=("FDocumentStatus",),
                           note="取不到时为 None——不猜" if state is None else "")
        out["_noun"] = n.form_id
        if notes:
            out["_notes"] = notes
        return out

    def state_of(self, props: dict) -> Optional[str]:
        """从属性值反推规范状态码。取不到返回 None，**不猜**。"""
        for code, meta in self.o.states.items():
            fld, val = meta.get("field"), meta.get("value")
            if not fld or val is None:
                continue
            actual = props.get(fld) or props.get(fld.upper()) or props.get(fld.lower())
            if actual is not None and str(actual) == str(val):
                return code
        return None

    def rows(self, noun: str, raws: list[dict]) -> tuple[list[dict], Lineage]:
        lin = Lineage(self.o.resolve_noun(noun).form_id)
        return [self.row(noun, r, lin) for r in raws], lin


def _first(d: dict, keys: tuple[str, ...]) -> Optional[Any]:
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None
