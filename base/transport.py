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
        return await srv._post("query", {
            "FormId": form_id, "FieldKeys": fields,
            "FilterString": filter_string, "TopRowCount": top,
        })


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
        self.calls.append(("query", form_id, {"filter": filter_string, "top": top}))
        return self._next([])
