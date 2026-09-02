"""审计发现 A-3 / A-4 / P-3 的回归测试。

对应 docs/ontology/00-atomicity-audit.md。
"""
import asyncio

import pytest

import kingdee_mcp.server as srv


class _Resp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


# ── A-3：会话失效判定不得对任意含 "session" 的正文误命中 ──────────────
class TestSessionExpiryDetection:
    @pytest.mark.parametrize("text", [
        '{"Result":{"SessionId":"abc"}}',                 # 正常返回里带 SessionId
        '{"rows":[{"FOperateName":"session 登录"}]}',      # 操作日志查询结果
        '{"Result":{"ResponseStatus":{"IsSuccess":true}}}',
        '会话记录查询成功',                                 # 含"会话"二字但不是失效
    ])
    def test_normal_body_not_treated_as_expired(self, text):
        expired, _ = srv._session_expired(_Resp(200, text))
        assert expired is False, f"误判为会话失效：{text}"

    @pytest.mark.parametrize("text", [
        "会话信息已丢失，请重新登录",
        "会话超时",
        "登录已失效",
        "用户未登录",
        "Session timeout",
        "invalid session",
    ])
    def test_real_expiry_detected(self, text):
        expired, safe = srv._session_expired(_Resp(200, text))
        assert expired is True, f"未识别出会话失效：{text}"
        assert safe is False, "HTTP 200 的会话失效，结果不可判定，不得标记为可重放"

    def test_401_is_safe_to_replay(self):
        expired, safe = srv._session_expired(_Resp(401, ""))
        assert (expired, safe) == (True, True), "401 在鉴权层被拒，未进入业务处理，重放安全"


# ── A-3：写操作在不可判定时必须拒绝自动重放 ───────────────────────────
class TestWriteNotBlindlyReplayed:
    def _patch(self, monkeypatch, responses):
        """让 _post_raw 用假的 httpx client 跑完整流程，记录实际发出的请求数。"""
        sent = []

        class _Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, **kw):
                sent.append(url)
                return responses[min(len(sent) - 1, len(responses) - 1)]

        monkeypatch.setattr(srv.httpx, "AsyncClient", lambda **kw: _Client())
        monkeypatch.setattr(srv, "_session_id", "stale-session")

        async def _fake_login():
            srv._session_id = "fresh-session"
            return srv._session_id
        monkeypatch.setattr(srv, "_login", _fake_login)
        return sent

    def test_save_is_not_replayed_on_ambiguous_session(self, monkeypatch):
        sent = self._patch(monkeypatch, [_Resp(200, "会话信息已丢失，请重新登录")])
        with pytest.raises(srv.SessionAmbiguousError) as ei:
            asyncio.run(srv._post_raw("save", "PUR_PurchaseOrder", {"FID": 0}))
        assert len(sent) == 1, "写请求在结果不可判定时被重发了——会造成重复建单"
        assert "查证" in str(ei.value)

    def test_save_is_replayed_on_401(self, monkeypatch):
        ok = _Resp(200, '{"Result":{"ResponseStatus":{"IsSuccess":true}}}')
        ok.raise_for_status = lambda: None
        ok.json = lambda: {"Result": {"ResponseStatus": {"IsSuccess": True}}}
        sent = self._patch(monkeypatch, [_Resp(401, ""), ok])
        asyncio.run(srv._post_raw("save", "PUR_PurchaseOrder", {"FID": 0}))
        assert len(sent) == 2, "401 后应重新登录并重放一次"


# ── A-4：并发登录必须真的走锁，且不重复登录 ───────────────────────────
class TestLoginLock:
    def test_lock_is_actually_used(self, monkeypatch):
        calls = []

        async def _fake_locked():
            calls.append(1)
            await asyncio.sleep(0.01)
            srv._session_id = f"sid-{len(calls)}"
            return srv._session_id

        monkeypatch.setattr(srv, "_login_locked", _fake_locked)
        monkeypatch.setattr(srv, "_session_id", None)
        monkeypatch.setattr(srv, "PASSWORD", "x")
        monkeypatch.setattr(srv, "_session_lock", None)

        async def _run():
            return await asyncio.gather(*[srv._login() for _ in range(5)])

        results = asyncio.run(_run())
        assert len(calls) == 1, f"5 个并发登录触发了 {len(calls)} 次真实登录，锁没生效"
        assert len(set(results)) == 1, "并发登录返回了不一致的 SessionId"


# ── P-3：error_type 不再恒为空 ────────────────────────────────────────
class TestErrorTypeRecorded:
    def test_error_type_is_populated(self, monkeypatch):
        logged = {}
        monkeypatch.setattr(srv, "log_tool_usage",
                            lambda **kw: logged.update(kw))

        class _Boom:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **kw): raise ConnectionResetError("boom")

        monkeypatch.setattr(srv.httpx, "AsyncClient", lambda **kw: _Boom())
        monkeypatch.setattr(srv, "_session_id", "sid")

        with pytest.raises(ConnectionResetError):
            asyncio.run(srv._post_raw("save", "PUR_PurchaseOrder", {"FID": 0}))
        assert logged.get("error_type") == "ConnectionResetError", (
            f"error_type 仍未记录，实际={logged.get('error_type')!r}")
