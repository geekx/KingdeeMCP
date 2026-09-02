"""传输层抽象 —— 让底座可以脱离 kingdee_mcp.server 独立测试。

「接口要稳健和独立」：底座的本体/校验/审计逻辑不应该依赖 7331 行的大模块。
这里定义一个极小的协议，默认实现懒加载地复用已加固过的 _post_raw/_post
（含 A-3 的会话重放修复），测试时注入 FakeTransport 即可。
"""
from __future__ import annotations

from typing import Any, Protocol


class Transport(Protocol):
    async def call(self, endpoint: str, form_id: str, payload: dict) -> Any: ...
    async def query(self, form_id: str, fields: str, filter_string: str, top: int) -> Any: ...
    async def system_query(self, endpoint: str, form_id: str, fields: str,
                           filter_string: str, top: int) -> Any: ...
    async def view(self, form_id: str, bill_id: str) -> Any: ...
    async def report(self, form_id: str, payload: dict) -> Any: ...
    async def metadata(self, form_id: str) -> Any: ...
    async def fields(self, form_id: str) -> Any: ...
    async def validate(self, form_id: str, model: dict) -> Any: ...
    async def template(self, form_id: str) -> Any: ...


class KingdeeTransport:
    """默认实现：复用 kingdee_mcp.server 的传输层（懒导入，避免启动即连库）。"""

    async def call(self, endpoint: str, form_id: str, payload: dict) -> Any:
        from kingdee_mcp import server as srv
        if endpoint == "execute":
            op = payload.pop("_op_number")
            return await srv._post_raw(endpoint, form_id, payload, op_number=op)
        return await srv._post_raw(endpoint, form_id, payload)

    async def query(self, form_id: str, fields: str, filter_string: str, top: int) -> Any:
        from kingdee_mcp import server as srv
        return await srv._post("query", srv._query_payload(
            form_id, fields, filter_string, "", 0, top))

    async def system_query(self, endpoint: str, form_id: str, fields: str,
                           filter_string: str, top: int) -> Any:
        from kingdee_mcp import server as srv
        return await srv._post_system(endpoint, form_id, fields, filter_string, 0, top)

    async def view(self, form_id: str, bill_id: str) -> Any:
        from kingdee_mcp import server as srv
        return await srv._post_raw("view", form_id, {"Id": bill_id})

    async def report(self, form_id: str, payload: dict) -> Any:
        from kingdee_mcp import server as srv
        return await srv._post("report", (form_id, payload))

    async def metadata(self, form_id: str) -> Any:
        from kingdee_mcp import server as srv
        return await srv._query_metadata(form_id)

    async def fields(self, form_id: str) -> Any:
        """实时字段清单。委托 legacy 的 MetadataValidator——
        它已经把金蝶元数据的分录/多语言/必填等结构解析好了，
        在底座里重写一遍只会多一处会漂移的实现。"""
        from kingdee_mcp import server as srv
        v = await srv._get_metadata_validator(form_id)
        if not v:
            return None
        return {
            "count": len(v.fields),
            "required": sorted(v.get_required_fields()),
            "fields": [
                {"name": name, "is_entry": d.is_entry, "must_input": d.must_input,
                 "children": sorted(c.name for c in getattr(d, "children", []) or [])}
                for name, d in sorted(v.fields.items())
            ],
        }

    async def template(self, form_id: str) -> Any:
        """已验证的 model 骨架。直接读 legacy 的 BILL_TEMPLATES，
        不在底座里复制一份——两份模板迟早会不一致。"""
        from kingdee_mcp import server as srv
        return srv.BILL_TEMPLATES.get(form_id)

    async def validate(self, form_id: str, model: dict) -> Any:
        """保存前校验。同样委托 legacy 实现，保持单一事实来源。"""
        from kingdee_mcp import server as srv
        import json as _json
        raw = await srv.kingdee_validate_bill(
            srv.SaveInput(form_id=form_id, model=model))
        return _json.loads(raw)


class FakeTransport:
    """测试替身：记录调用、按预设脚本返回。"""

    def __init__(self, script: list[Any] | None = None):
        self.calls: list[tuple] = []
        self.script = list(script or [])

    def _next(self, default: Any) -> Any:
        return self.script.pop(0) if self.script else default

    async def call(self, endpoint: str, form_id: str, payload: dict) -> Any:
        self.calls.append((endpoint, form_id, dict(payload)))
        return self._next({"Result": {"ResponseStatus": {"IsSuccess": True},
                                      "Id": "1001", "Number": "TEST0001"}})

    async def query(self, form_id: str, fields: str, filter_string: str, top: int) -> Any:
        self.calls.append(("query", form_id, {"filter": filter_string, "top": top,
                                              "fields": fields}))
        return self._next([])

    async def system_query(self, endpoint: str, form_id: str, fields: str,
                           filter_string: str, top: int) -> Any:
        self.calls.append((f"system:{endpoint}", form_id, {"filter": filter_string,
                                                           "fields": fields}))
        return self._next([])

    async def view(self, form_id: str, bill_id: str) -> Any:
        self.calls.append(("view", form_id, {"Id": bill_id}))
        return self._next({"Result": {"Result": {"Id": bill_id}}})

    async def report(self, form_id: str, payload: dict) -> Any:
        self.calls.append(("report", form_id, dict(payload)))
        return self._next({"Result": {"Rows": []}})

    async def metadata(self, form_id: str) -> Any:
        self.calls.append(("metadata", form_id, {}))
        return self._next({"Result": {"NeedReturnData": {}}})

    async def fields(self, form_id: str) -> Any:
        self.calls.append(("fields", form_id, {}))
        return self._next({"count": 0, "required": [], "fields": []})

    async def template(self, form_id: str) -> Any:
        self.calls.append(("template", form_id, {}))
        return self._next(None)

    async def validate(self, form_id: str, model: dict) -> Any:
        self.calls.append(("validate", form_id, dict(model)))
        return self._next({"ok": True, "missing_required": [], "entry_issues": []})
