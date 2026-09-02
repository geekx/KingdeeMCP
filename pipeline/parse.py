"""解析 —— 金蝶原始响应 → 规范行。

这层此前散在三处：dispatch.py 的 _rows_of / _flatten_props、objects.py 的
属性拆分。散着写的后果是同一种响应形状在不同入口被解析成不同结果，
而且没人记得哪里还有第三份。这里收拢成单一实现。

金蝶 WebAPI 的返回形状很不统一：
  ExecuteBillQuery  → 二维数组（无字段名！按 FieldKeys 顺序对位）
  View              → 嵌套对象
  报表              → {Result:{Rows:[...]}}
  业务异常          → HTTP 200 但正文是纯文本
所以解析必须**由调用方告诉它期望什么形状**，不能靠猜。
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from pipeline.lineage import Lineage, Origin

# 金蝶 ExecuteBillQuery 返回的是位置数组，字段名要靠请求时的 FieldKeys 对位。
# 位数对不上就是真的对不上——宁可报错，也不要错位赋值：
# 错位比缺失危险得多，它会让"供应商名称"变成"金额"而没人发现。
class FieldCountMismatch(ValueError):
    pass


def rows_from_query(result: Any, field_keys: str) -> list[dict]:
    """ExecuteBillQuery 的二维数组 → 具名行。"""
    cols = [c.strip() for c in (field_keys or "").split(",") if c.strip()]
    raw = _unwrap_rows(result)
    out: list[dict] = []
    for i, row in enumerate(raw):
        if isinstance(row, dict):
            out.append(row)                      # 有些端点已经是具名的
            continue
        if not isinstance(row, (list, tuple)):
            continue
        if cols and len(row) != len(cols):
            raise FieldCountMismatch(
                f"第 {i} 行有 {len(row)} 个值，但 FieldKeys 声明了 {len(cols)} 个字段"
                f"（{cols[:4]}…）。位置数组按顺序对位，位数不符会导致**错位赋值**，"
                f"故在此中断而不是猜。检查请求的 FieldKeys 与响应是否一致。")
        out.append(dict(zip(cols, row)) if cols else {f"c{j}": v for j, v in enumerate(row)})
    return out


def _unwrap_rows(result: Any) -> list:
    if isinstance(result, list):
        return result
    if not isinstance(result, dict):
        return []
    for k in ("Result", "data", "Rows"):
        v = result.get(k)
        if isinstance(v, list):
            return v
    inner = result.get("Result")
    if isinstance(inner, dict):
        for k in ("Result", "Rows", "data"):
            v = inner.get(k)
            if isinstance(v, list):
                return v
    return []


def flatten_view(data: Any, lineage: Optional[Lineage] = None) -> dict:
    """View 的嵌套结构 → 平铺属性。

    只做一层保守展开：顶层标量直接取，嵌套对象取其 FName/FNumber。
    取不到就不编——宁可属性少，也不要伪造值。
    """
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
            if lineage:
                lineage.record(k, Origin.WEBAPI, source=k)
        elif isinstance(v, dict):
            for sub in ("FName", "FNumber", "Name", "Number"):
                if sub in v and isinstance(v[sub], (str, int, float)):
                    col = f"{k}.{sub}"
                    out[col] = v[sub]
                    if lineage:
                        lineage.record(col, Origin.WEBAPI, source=f"{k}.{sub}",
                                       note="嵌套对象取其名称/编码字段")
    return out


def split_field_keys(field_keys: str) -> list[dict]:
    """把 FieldKeys 串拆成列定义。`FSupplierId.FName` 这种是关联字段。"""
    out: list[dict] = []
    for raw in (field_keys or "").split(","):
        f = raw.strip()
        if f:
            out.append({"name": f, "is_lookup": "." in f, "base": f.split(".")[0]})
    return out


def is_business_error(result: Any) -> Optional[str]:
    """金蝶在业务异常时会 HTTP 200 但正文是纯文本，_safe_json 会包成 kd_error。"""
    if isinstance(result, dict) and result.get("kd_error"):
        return str(result.get("message", ""))[:500]
    return None
