"""连通性探测：候选名词怎么选，结果怎么归类，什么时候该停。

这一层的正确性不在"能连上就报 ok"——那是最容易的部分。真正容易错的是
分类边界：Kingdee 拒绝一次查询（权限不足/表单不存在）和探测本身跑不通
（登录失败/网络问题）必须分成两种完全不同的处理——前者继续测下一个，
后者立刻停，不然会把一次网络故障重复超时探测好几次。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kingdee_ontology.base.dispatch import Dispatcher              # noqa: E402
from kingdee_ontology.base.ontology import load                    # noqa: E402
from kingdee_ontology.base.probe import (                          # noqa: E402
    _legacy_catalog_forms, default_candidates, probe_connection,
)
from kingdee_ontology.base.transport import FakeTransport          # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("KINGDEE_OPERATION_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    import kingdee_ontology.operation_audit as oa
    monkeypatch.setattr(oa, "audit_recorder", oa.AuditRecorder(tmp_path / "audit.jsonl"))
    import kingdee_ontology.base.dispatch as bd
    monkeypatch.setattr(bd, "audit_recorder", oa.audit_recorder)


@pytest.fixture
def base_o():
    return load(tenant="")


@pytest.fixture
def tenant_o():
    return load(tenant="example-tenant")


KD_NO_PERM = {"kd_error": True, "message": "该用户无权限访问此单据类型"}
KD_OTHER_ERR = {"kd_error": True, "message": "表单标识不存在或已被二次开发删除"}


class TestLegacyCatalogReuse:
    """探测候选优先从 kingdee_mcp 那 97 个专用工具背后的人工维护目录里选——
    这些表单久经使用、行为摸得清楚，用它们做默认探测比按出边数量硬排更不
    容易撞见冷门/边缘表单触发的怪问题。这是复用，不是重新维护一份清单。
    """

    def test_returns_the_real_form_catalog(self):
        legacy = _legacy_catalog_forms()
        assert len(legacy) > 0
        assert "BD_Material" in legacy   # legacy 目录里最基础的一个

    def test_falls_back_gracefully_when_legacy_module_unavailable(self, monkeypatch):
        """kingdee_mcp.server 导入失败时不该让候选选择跟着炸——退化成没有
        这一个排序信号，而不是让整个探测流程用不了。"""
        monkeypatch.setitem(sys.modules, "kingdee_mcp.server", None)
        assert _legacy_catalog_forms() == frozenset()

    def test_legacy_members_ranked_ahead_of_higher_link_count_non_members(
            self, base_o, monkeypatch):
        """核心断言：即使某个非 legacy 名词出边更多，legacy 目录里的也该排前面。"""
        import kingdee_ontology.base.probe as probe_mod
        # 挑一个真实存在、出边多的非 legacy 候选，制造"出边数量会让它排前面"
        # 的情形，再验证补丁后的 legacy 集合确实把它压到后面。
        out_links: dict[str, int] = {}
        for lk in base_o.links:
            out_links[lk["from"]] = out_links.get(lk["from"], 0) + 1
        candidates = [n for n in base_o.nouns.values() if "query" in n.allowed_verbs]
        by_links = sorted(candidates, key=lambda n: -out_links.get(n.form_id, 0))
        high_link_noun = by_links[0].form_id
        low_link_noun = next(n.form_id for n in reversed(by_links)
                             if n.form_id != high_link_noun)

        monkeypatch.setattr(probe_mod, "_legacy_catalog_forms",
                            lambda: frozenset({low_link_noun}))
        picked = default_candidates(base_o, limit=len(candidates))
        assert picked.index(low_link_noun) < picked.index(high_link_noun), (
            "出边少但在 legacy 目录里的名词，应该排在出边多但不在目录里的前面")

    def test_tenant_entry_points_still_outrank_legacy(self, tenant_o, monkeypatch):
        """优先级第一位始终是"用户真的会用到的"——legacy 目录只是第二位。"""
        import kingdee_ontology.base.probe as probe_mod
        # 把 SAL_SaleOrder（真实的操作入口）排除在 legacy 目录之外，
        # 确认它仍然排在 legacy 目录里的任何名词前面。
        monkeypatch.setattr(probe_mod, "_legacy_catalog_forms",
                            lambda: frozenset({"BD_Material"}))
        picked = default_candidates(tenant_o, limit=10)
        assert picked.index("SAL_SaleOrder") < picked.index("BD_Material")


class TestDefaultCandidates:
    def test_prioritizes_tenant_operation_entry_points(self, tenant_o):
        """示例租户的操作起点是 SAL_SaleOrder / PUR_PurchaseOrder——这些该排最前面。"""
        picked = default_candidates(tenant_o, limit=10)
        assert "SAL_SaleOrder" in picked
        assert "PUR_PurchaseOrder" in picked
        idx = {f: i for i, f in enumerate(picked)}
        # 操作入口应该比补齐用的其它类别名词更靠前
        non_entry = [f for f in picked if f not in ("SAL_SaleOrder", "PUR_PurchaseOrder")]
        if non_entry:
            assert max(idx[f] for f in ("SAL_SaleOrder", "PUR_PurchaseOrder")) < \
                   min(idx[f] for f in non_entry)

    def test_respects_limit(self, base_o):
        assert len(default_candidates(base_o, limit=3)) <= 3
        assert len(default_candidates(base_o, limit=3)) > 0

    def test_no_duplicates(self, base_o):
        picked = default_candidates(base_o, limit=20)
        assert len(picked) == len(set(picked))

    def test_only_query_capable_nouns(self, base_o):
        picked = default_candidates(base_o, limit=20)
        for form_id in picked:
            assert "query" in base_o.nouns[form_id].allowed_verbs

    def test_deterministic(self, base_o):
        """同一本体两次调用必须给出同一份候选——探测报告不该每次都不一样。"""
        assert default_candidates(base_o, limit=10) == default_candidates(base_o, limit=10)


class TestProbeConnection:
    @pytest.mark.asyncio
    async def test_all_ok(self, base_o):
        d = Dispatcher(ontology=base_o, transport=FakeTransport([[], [], []]))
        out = await probe_connection(d, nouns=["销售订单", "采购订单", "物料"])
        assert out["ok"] == 3
        assert out["no_permission"] == 0
        assert out["stopped_early"] is None
        assert all(r["outcome"] == "ok" for r in out["probed"])

    @pytest.mark.asyncio
    async def test_classifies_permission_error(self, base_o):
        d = Dispatcher(ontology=base_o, transport=FakeTransport([KD_NO_PERM]))
        out = await probe_connection(d, nouns=["销售订单"])
        assert out["probed"][0]["outcome"] == "no_permission"
        assert out["no_permission"] == 1
        assert out["stopped_early"] is None, "权限不足是探测想知道的答案之一，不该中断"

    @pytest.mark.asyncio
    async def test_unrecognized_business_error_is_conservative(self, base_o):
        """没命中任何权限关键词时归到 business_error，不是猜成权限问题。"""
        d = Dispatcher(ontology=base_o, transport=FakeTransport([KD_OTHER_ERR]))
        out = await probe_connection(d, nouns=["销售订单"])
        assert out["probed"][0]["outcome"] == "business_error"

    @pytest.mark.asyncio
    async def test_continues_past_business_errors(self, base_o):
        """业务错误不中断——下一个候选照样测。"""
        d = Dispatcher(ontology=base_o,
                       transport=FakeTransport([KD_NO_PERM, [], KD_OTHER_ERR]))
        out = await probe_connection(d, nouns=["销售订单", "采购订单", "物料"])
        assert len(out["probed"]) == 3
        assert [r["outcome"] for r in out["probed"]] == \
               ["no_permission", "ok", "business_error"]

    @pytest.mark.asyncio
    async def test_stops_on_transport_level_failure(self, base_o):
        """登录失败/网络故障这类"探测本身跑不通"的情况，立刻停，
        不把剩下的候选也各超时一遍。"""
        class Boom(FakeTransport):
            async def query(self, *a, **kw):
                raise RuntimeError("金蝶登录失败: 密码错误")

        d = Dispatcher(ontology=base_o, transport=Boom())
        out = await probe_connection(d, nouns=["销售订单", "采购订单", "物料"])
        assert len(out["probed"]) == 1
        assert out["probed"][0]["outcome"] == "blocked"
        assert out["stopped_early"] is not None
        assert out["stopped_early"]["at"] == "SAL_SaleOrder"
        assert set(out["stopped_early"]["untested"]) == {"采购订单", "物料"}

    @pytest.mark.asyncio
    async def test_first_failure_only_stops_remaining_not_earlier_results(self, base_o):
        """前面已经拿到的结果不能因为后面失败就被丢掉。"""
        class BoomOnThird(FakeTransport):
            def __init__(self):
                super().__init__()
                self.n = 0

            async def query(self, *a, **kw):
                self.n += 1
                if self.n == 3:
                    raise RuntimeError("ConnectError: 连接超时")
                return []

        d = Dispatcher(ontology=base_o, transport=BoomOnThird())
        out = await probe_connection(d, nouns=["销售订单", "采购订单", "物料", "客户"])
        assert [r["outcome"] for r in out["probed"]] == ["ok", "ok", "blocked"]

    @pytest.mark.asyncio
    async def test_explicit_nouns_override_default_candidates(self, base_o):
        d = Dispatcher(ontology=base_o, transport=FakeTransport([[]]))
        out = await probe_connection(d, nouns=["物料"])
        assert out["candidates"] == ["物料"]
        assert len(out["probed"]) == 1
        assert out["probed"][0]["noun"] == "BD_Material"

    @pytest.mark.asyncio
    async def test_unknown_noun_is_skipped_not_fatal(self, base_o):
        d = Dispatcher(ontology=base_o, transport=FakeTransport([[]]))
        out = await probe_connection(d, nouns=["这不是一个真的单据类型", "销售订单"])
        assert len(out["probed"]) == 1
        assert out["probed"][0]["noun"] == "SAL_SaleOrder"

    @pytest.mark.asyncio
    async def test_note_explains_heuristic_nature(self, base_o):
        d = Dispatcher(ontology=base_o, transport=FakeTransport([[]]))
        out = await probe_connection(d, nouns=["销售订单"])
        assert "启发式" in out["note"]

    @pytest.mark.asyncio
    async def test_defaults_when_no_nouns_given(self, base_o):
        """不传 nouns 时落到 default_candidates，不是报错或空转。"""
        d = Dispatcher(ontology=base_o, transport=FakeTransport([[] for _ in range(10)]))
        out = await probe_connection(d, limit=3)
        assert len(out["candidates"]) <= 3
        assert len(out["candidates"]) > 0
