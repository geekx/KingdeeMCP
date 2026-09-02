"""legacy push 路径接入底座链接表（审计 L-1 / MISS-02）。

默认只警告不阻断——链接表只登记了 9 条，而金蝶实际支持的转换关系远不止，
硬拦会误伤正在用合法但未登记关系的调用方。
KINGDEE_STRICT_LINKS=1 时才在发请求前拒绝。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import kingdee_mcp.server as srv  # noqa: E402

PUSH_OK = {"Result": {"ResponseStatus": {"IsSuccess": True},
                      "Numbers": ["RKD001"], "Ids": ["200318"]}}


@pytest.fixture
def fake_push(monkeypatch):
    sent = []

    async def _fake(ep, form_id, model, **kw):
        sent.append((ep, form_id, dict(model)))
        return PUSH_OK
    monkeypatch.setattr(srv, "_post_raw", _fake)
    return sent


class TestLinkLookup:
    def test_registered_link(self):
        r = srv._check_push_link("PUR_PurchaseOrder", "STK_InStock")
        assert r["status"] == "registered"

    def test_unregistered_link_lists_known_targets(self):
        r = srv._check_push_link("PUR_PurchaseOrder", "SAL_OUTSTOCK")
        assert r["status"] == "unregistered"
        assert "STK_InStock" in r["known_targets_for_source"]
        assert "profiles" in r["hint"], "提示必须告诉人怎么补，而不只是说不合法"

    def test_suspect_link_is_flagged(self):
        r = srv._check_push_link("PRD_PickMtrl", "PRD_Instock")
        assert r["status"] == "registered" and r["verified"] == "suspect"
        assert "存疑" in r["warning"]

    def test_unknown_source_does_not_crash(self):
        r = srv._check_push_link("NOT_A_FORM", "ALSO_NOT")
        assert r["status"] == "unregistered"
        assert r["known_targets_for_source"] == []


class TestResponseAnnotation:
    def test_push_response_carries_link_check(self, fake_push):
        out = json.loads(asyncio.run(srv.kingdee_push_bill(
            srv.PushDownInput(form_id="PUR_PurchaseOrder", target_form_id="STK_InStock",
                              source_bill_nos=["CGDD001"]))))
        assert out["link_check"]["status"] == "registered"
        assert "link_warning" not in out

    def test_unregistered_push_warns_but_still_runs(self, fake_push):
        """默认不阻断：未登记 ≠ 不合法，只是无法校验。"""
        out = json.loads(asyncio.run(srv.kingdee_push_bill(
            srv.PushDownInput(form_id="PUR_PurchaseOrder", target_form_id="QIS_InspectBill",
                              source_bill_nos=["CGDD001"]))))
        assert out["link_check"]["status"] == "unregistered"
        assert "未登记" in out["link_warning"]
        assert len(fake_push) == 1, "默认模式下请求仍应发出"

    def test_suspect_push_warns(self, fake_push):
        out = json.loads(asyncio.run(srv.kingdee_push_production_stock_in(
            srv.ProductionStockInPushInput(bill_nos=["LLD001"]))))
        assert "存疑" in out["link_warning"]


class TestStrictMode:
    def test_strict_mode_blocks_before_sending(self, monkeypatch):
        sent = []

        async def _fake_client_post(*a, **kw):
            sent.append(a)
            raise AssertionError("严格模式下不应发出请求")

        monkeypatch.setattr(srv, "_STRICT_LINKS", True)
        monkeypatch.setattr(srv, "_session_id", "sid")

        class _C:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            post = staticmethod(_fake_client_post)
        monkeypatch.setattr(srv.httpx, "AsyncClient", lambda **kw: _C())

        with pytest.raises(RuntimeError) as e:
            asyncio.run(srv._post_raw("push", "PUR_PurchaseOrder",
                                      {"TargetFormId": "SAL_OUTSTOCK", "Numbers": ["X"]}))
        assert "STRICT_LINKS" in str(e.value)
        assert sent == [], "严格模式必须在发请求**之前**拦下"

    def test_strict_mode_allows_registered(self, monkeypatch):
        monkeypatch.setattr(srv, "_STRICT_LINKS", True)
        monkeypatch.setattr(srv, "_session_id", "sid")
        ok = type("R", (), {"status_code": 200, "text": "{}",
                            "raise_for_status": lambda self: None,
                            "json": lambda self: PUSH_OK})()

        class _C:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **kw): return ok
        monkeypatch.setattr(srv.httpx, "AsyncClient", lambda **kw: _C())
        r = asyncio.run(srv._post_raw("push", "PUR_PurchaseOrder",
                                      {"TargetFormId": "STK_InStock", "Numbers": ["X"]}))
        assert r["Result"]["ResponseStatus"]["IsSuccess"] is True


class TestChokePoint:
    def test_all_push_paths_go_through_one_check(self):
        """校验放在 _post_raw 的 push 分支——所有下推路径（含 3 个复合工具）
        的唯一咽喉点。这条测试守住"别再去 6 个函数里各加一遍"。"""
        import ast
        src = (ROOT / "src" / "kingdee_mcp" / "server.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        blocking = []
        for n in ast.walk(tree):
            if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)):
                body = ast.unparse(n)
                if "_STRICT_LINKS" in body:
                    blocking.append(n.name)
        assert blocking == ["_post_raw"], (
            f"阻断逻辑应只在 _post_raw 一处，实际出现在 {blocking}")
