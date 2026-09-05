"""连通性探测：候选名词怎么选，结果怎么归类，什么时候该停。

这一层的正确性不在"能连上就报 ok"——那是最容易的部分。真正容易错的是
分类边界：Kingdee 拒绝一次查询（权限不足/表单不存在）和探测本身跑不通
（登录失败/网络问题）必须分成两种完全不同的处理——前者继续测下一个，
后者立刻停，不然会把一次网络故障重复超时探测好几次。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kingdee_ontology.base.dispatch import Dispatcher              # noqa: E402
from kingdee_ontology.base.ontology import load                    # noqa: E402
from kingdee_ontology.base.probe import (                          # noqa: E402
    _legacy_catalog_forms, _looks_like_report_param_error,
    default_candidates, probe_connection,
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


class TestUnregisteredFormDiscovery:
    """本体解析不出来的候选，只有调用方明确点名（nouns=）时才值得绕开本体
    直接探测——default_candidates() 挑出来的永远已经在本体里，不会走这条路。

    这条路是"发现候选 → 提 WikiSkill 建议"的桥：金蝶认这个 form_id、
    没报业务错误，就说明账号能用、本体没登记，值得提一条建议；
    金蝶自己就拒绝了（表单不存在/无权限），不提，按普通结果归类。
    """

    def _d(self, script, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)   # 隔离 wikiskill/knowledge.json 的写入位置
        load.cache_clear()
        d = Dispatcher(ontology=load(tenant=""), transport=FakeTransport(script))
        load.cache_clear()
        return d

    @pytest.mark.asyncio
    async def test_default_candidates_never_trigger_raw_fallback(self, base_o, tmp_path,
                                                                  monkeypatch):
        """不传 nouns 时，候选都保证已在本体里——不该有任何一次走原始探测。"""
        d = self._d([[] for _ in range(10)], tmp_path, monkeypatch)
        out = await probe_connection(d, limit=5)
        assert not any(r.get("unregistered") for r in out["probed"])

    @pytest.mark.asyncio
    async def test_reachable_unregistered_form_is_reported_and_proposed(
            self, tmp_path, monkeypatch):
        """金蝶不报错＝认这个表单——报成 ok，且提一条 WikiSkill 建议。"""
        d = self._d([[]], tmp_path, monkeypatch)
        out = await probe_connection(d, nouns=["YL_CustomBill"])
        r = out["probed"][0]
        assert r["outcome"] == "ok"
        assert r["unregistered"] is True
        assert out["unregistered_found"] == 1

        kb = tmp_path / "wikiskill" / "knowledge.json"
        assert kb.exists(), "探测到能用的未登记表单，应该沉淀一条建议，不能白探测一次"
        entries = json.loads(kb.read_text(encoding="utf-8"))["entries"]
        e = next(e for e in entries if e["kind"] == "unregistered_form_reachable")
        assert "YL_CustomBill" in e["title"]
        assert "profile.yml" in e["suggestion"]
        assert "nouns" in e["suggestion"]

    @pytest.mark.asyncio
    async def test_rejected_unregistered_form_is_not_proposed(self, tmp_path, monkeypatch):
        """金蝶自己就说"没这个表单"——不是"能用但没登记"，不该提建议。"""
        d = self._d([{"kd_error": True, "message": "表单标识YL_Fake不存在"}],
                    tmp_path, monkeypatch)
        out = await probe_connection(d, nouns=["YL_Fake"])
        r = out["probed"][0]
        assert r["outcome"] == "business_error"
        assert r["unregistered"] is True
        assert out["unregistered_found"] == 0
        kb = tmp_path / "wikiskill" / "knowledge.json"
        assert not kb.exists(), "没查通的表单不该被当成'发现'提给知识库"

    @pytest.mark.asyncio
    async def test_permission_denied_unregistered_form_classified_normally(
            self, tmp_path, monkeypatch):
        """权限判断对未登记表单同样适用——复用同一套关键词启发式，不是两套逻辑。"""
        d = self._d([{"kd_error": True, "message": "该用户无权限访问此单据类型"}],
                    tmp_path, monkeypatch)
        out = await probe_connection(d, nouns=["YL_NoPerm"])
        assert out["probed"][0]["outcome"] == "no_permission"
        assert out["probed"][0]["unregistered"] is True

    @pytest.mark.asyncio
    async def test_registered_noun_is_unaffected(self, base_o, tmp_path, monkeypatch):
        """能正常解析的名词，走的还是原来的路，不该被打上 unregistered。"""
        d = self._d([[]], tmp_path, monkeypatch)
        out = await probe_connection(d, nouns=["销售订单"])
        assert "unregistered" not in out["probed"][0]

    @pytest.mark.asyncio
    async def test_transport_failure_during_fallback_still_stops_the_probe(
            self, tmp_path, monkeypatch):
        """原始探测本身跑不通（不是金蝶拒绝，是探测failed）——一样得停，
        不能因为走的是兜底路径就不遵守"一出问题就停"的规矩。"""
        class Boom(FakeTransport):
            async def query(self, *a, **kw):
                raise RuntimeError("金蝶登录失败: 密码错误")
        d = self._d([], tmp_path, monkeypatch)
        d.t = Boom()
        out = await probe_connection(d, nouns=["YL_Custom", "销售订单"])
        assert len(out["probed"]) == 1
        assert out["probed"][0]["outcome"] == "blocked"
        assert out["stopped_early"] is not None

    def test_knowledge_base_unavailable_does_not_raise(self, tmp_path, monkeypatch):
        """知识库写不进去，不该连累探测本身——和 navigate() 的 _propose_link_filter
        一个原则：直接测这个函数自己吞不吞错误，而不是绕一圈通过探测流程猜。"""
        monkeypatch.chdir(tmp_path)
        import kingdee_ontology.wikiskill.knowledge as kb_mod
        from kingdee_ontology.base.probe import _propose_unregistered_form

        class BrokenKnowledge:
            def __init__(self, *a, **kw):
                raise OSError("磁盘满了")
        monkeypatch.setattr(kb_mod, "Knowledge", BrokenKnowledge)
        _propose_unregistered_form("YL_CustomBill")   # 不该抛

    @pytest.mark.asyncio
    async def test_probe_still_reports_ok_when_knowledge_base_is_broken(
            self, tmp_path, monkeypatch):
        """即使提议这一步坏了，探测本身该给的结果一条不能少。"""
        d = self._d([[]], tmp_path, monkeypatch)
        import kingdee_ontology.wikiskill.knowledge as kb_mod

        class BrokenKnowledge:
            def __init__(self, *a, **kw):
                raise OSError("磁盘满了")
        monkeypatch.setattr(kb_mod, "Knowledge", BrokenKnowledge)
        out = await probe_connection(d, nouns=["YL_CustomBill"])
        assert out["probed"][0]["outcome"] == "ok"


class TestReportParamErrorHeuristic:
    """报表参数缺失异常的识别——两个关键词都出现才判定，避免"key"这种
    常见词单独出现时把普通业务错误误判成报表。"""

    def test_matches_the_observed_dotnet_shape(self):
        assert _looks_like_report_param_error("值不能为 null。\n参数名: key")

    def test_case_insensitive_on_the_latin_part(self):
        assert _looks_like_report_param_error("值不能为 null。参数名: KEY")

    def test_requires_both_markers_not_just_key(self):
        assert not _looks_like_report_param_error("值不能为 null。参数名: value")

    def test_requires_both_markers_not_just_the_chinese_one(self):
        assert not _looks_like_report_param_error("参数名对不上，检查一下配置")

    def test_unrelated_message_does_not_match(self):
        assert not _looks_like_report_param_error("该用户无权限访问此单据类型")


class TestReportProbing:
    """未登记表单探测里，report() 兜底探测报表类二开对象的分支——见模块
    docstring「一个结构性的盲区：报表类二开对象」。

    这条路只在 query() 已经判定为业务错误之后才会走：query() 成功（真的
    是账号能查的表单）或探测本身跑不通（异常）时不该触发。
    """

    def _d(self, script, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        load.cache_clear()
        d = Dispatcher(ontology=load(tenant=""), transport=FakeTransport(script))
        load.cache_clear()
        return d

    @pytest.mark.asyncio
    async def test_report_param_error_is_classified_as_possible_report(
            self, tmp_path, monkeypatch):
        """query() 报业务错误，report() 空参数试探出参数缺失异常——
        归类为 possible_report，且提一条 WikiSkill 建议。"""
        script = [
            {"kd_error": True, "message": "表单标识UZJK_SomeReport不存在"},
            {"kd_error": True, "message": "值不能为 null。\n参数名: key"},
        ]
        d = self._d(script, tmp_path, monkeypatch)
        out = await probe_connection(d, nouns=["UZJK_SomeReport"])
        r = out["probed"][0]
        assert r["outcome"] == "possible_report"
        assert r["unregistered"] is True
        assert out["possible_reports"] == 1
        assert out["unregistered_found"] == 0, "possible_report 不算'确认可用'"

        kb = tmp_path / "wikiskill" / "knowledge.json"
        assert kb.exists()
        entries = json.loads(kb.read_text(encoding="utf-8"))["entries"]
        e = next(e for e in entries if e["kind"] == "possible_report_unconfirmed")
        assert "UZJK_SomeReport" in e["title"]
        assert "FieldKeys" in e["suggestion"]

    @pytest.mark.asyncio
    async def test_detail_kept_is_the_query_error_not_the_report_probe(
            self, tmp_path, monkeypatch):
        """存下来的 detail 是 query() 那次的错误信息——report() 只是用来
        辅助分类，它自己的错误文本不该覆盖掉本来的结论。"""
        script = [
            {"kd_error": True, "message": "这是 query 报的原始错误信息"},
            {"kd_error": True, "message": "值不能为 null。参数名: key"},
        ]
        d = self._d(script, tmp_path, monkeypatch)
        out = await probe_connection(d, nouns=["UZJK_SomeReport"])
        assert "这是 query 报的原始错误信息" in out["probed"][0]["detail"]

    @pytest.mark.asyncio
    async def test_ordinary_business_error_without_report_signature_is_unaffected(
            self, tmp_path, monkeypatch):
        """report() 空参数没触发那个特定异常形状——不该被误判成报表，
        按原来的业务错误/权限分类走。"""
        script = [
            {"kd_error": True, "message": "该用户无权限访问此单据类型"},
            {"kd_error": True, "message": "服务器内部错误"},
        ]
        d = self._d(script, tmp_path, monkeypatch)
        out = await probe_connection(d, nouns=["YL_NoPerm"])
        assert out["probed"][0]["outcome"] == "no_permission"
        assert out["possible_reports"] == 0

    @pytest.mark.asyncio
    async def test_report_probe_is_skipped_when_query_already_succeeds(
            self, tmp_path, monkeypatch):
        """query() 就查通了——这是普通的未登记表单发现，不该额外发一次
        report() 探测请求。"""
        d = self._d([[]], tmp_path, monkeypatch)
        out = await probe_connection(d, nouns=["YL_CustomBill"])
        assert out["probed"][0]["outcome"] == "ok"
        assert out["possible_reports"] == 0
        assert not any(c[0] == "report" for c in d.t.calls)

    @pytest.mark.asyncio
    async def test_report_probe_exception_falls_back_to_original_classification(
            self, tmp_path, monkeypatch):
        """report() 兜底探测自己炸了（网络/序列化之类，不是我们要找的那个
        特定异常形状）——不该盖掉已经拿到的 query() 结论，也不该让整个
        探测崩掉。"""
        class ReportBoom(FakeTransport):
            async def report(self, *a, **kw):
                raise RuntimeError("ConnectError")
        d = self._d([{"kd_error": True, "message": "表单标识不存在"}], tmp_path, monkeypatch)
        d.t = ReportBoom(d.t.script)
        out = await probe_connection(d, nouns=["YL_Fake"])
        assert out["probed"][0]["outcome"] == "business_error"
        assert out["possible_reports"] == 0

    @pytest.mark.asyncio
    async def test_note_mentions_report_probing_only_when_found(
            self, tmp_path, monkeypatch):
        d_none = self._d([[]], tmp_path, monkeypatch)
        out_none = await probe_connection(d_none, nouns=["YL_CustomBill"])
        assert "possible_reports" not in out_none["note"]

        script = [
            {"kd_error": True, "message": "表单标识不存在"},
            {"kd_error": True, "message": "值不能为 null。参数名: key"},
        ]
        d_found = self._d(script, tmp_path, monkeypatch)
        out_found = await probe_connection(d_found, nouns=["UZJK_SomeReport"])
        assert "possible_reports" in out_found["note"]


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
    async def test_unresolvable_noun_does_not_crash_the_probe(self, base_o):
        """本体解析不出来不该让整个探测炸掉——不管走不走原始兜底探测，
        都得体面地产出一条结果，接着测下一个候选。"""
        d = Dispatcher(ontology=base_o, transport=FakeTransport([[], []]))
        out = await probe_connection(d, nouns=["这不是一个真的单据类型", "销售订单"])
        assert len(out["probed"]) == 2
        assert out["probed"][1]["noun"] == "SAL_SaleOrder"

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
