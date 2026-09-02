"""对象层：把本体变成操作面（Palantir Ontology 式）。

核心断言不是"能返回数据"，而是**呈现的东西对使用者有没有用**：
状态能不能从属性反推出来、动作可用性对不对、不可用时说没说清为什么、
拿不准的地方有没有诚实地承认拿不准。
"""
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kingdee_ontology.base.dispatch import Dispatcher            # noqa: E402
from kingdee_ontology.base.objects import ObjectModel            # noqa: E402
from kingdee_ontology.base.ontology import OntologyError, load   # noqa: E402
from kingdee_ontology.base.transport import FakeTransport        # noqa: E402


@pytest.fixture
def m():
    load.cache_clear()
    return ObjectModel(load(tenant=""))


PO_AUDITED = {"FID": "100231", "FBillNo": "CGDD000231", "FDocumentStatus": "C",
              "FSupplierId.FName": "某供应商"}
PO_DRAFT = {"FID": "100232", "FBillNo": "CGDD000232", "FDocumentStatus": "Z"}


class TestObjectType:
    def test_identity_properties_are_picked(self, m):
        ot = m.object_type("采购订单")
        assert ot.id_property == "FID"
        assert ot.title_property == "FBillNo"

    def test_master_data_has_its_own_state_set(self, m):
        assert m.object_type("物料").states == ["ENABLED", "FORBIDDEN"]

    def test_view_has_no_states_and_no_write_actions(self, m):
        ot = m.object_type("即时库存")
        assert ot.states == []
        assert ot.actions == []

    def test_links_carry_direction(self, m):
        ot = m.object_type("采购入库单")
        incoming = [l for l in ot.links if l.direction == "incoming"]
        assert any(l.target_type == "PUR_PurchaseOrder" for l in incoming)


class TestActionAvailability:
    def test_state_resolved_from_properties(self, m):
        card = m.card("采购订单", PO_AUDITED)
        assert card["state"] == "C:已审核" and card["state_zh"] == "已审核"

    def test_audited_bill_cannot_be_audited_again(self, m):
        card = m.card("采购订单", PO_AUDITED)
        audit = next(a for a in card["actions"] if a["verb"] == "audit")
        assert audit["enabled"] is False
        assert "B:审核中" in audit["reason"] and "C:已审核" in audit["reason"], \
            "不可用时必须说清要求什么、当前是什么——灰掉却不解释比没有按钮更让人困惑"

    def test_audited_bill_can_be_unaudited_and_pushed(self, m):
        card = m.card("采购订单", PO_AUDITED)
        enabled = {a["verb"] for a in card["actions"] if a["enabled"]}
        assert {"unaudit", "push", "void", "close"} <= enabled

    def test_draft_can_be_submitted_and_deleted(self, m):
        card = m.card("采购订单", PO_DRAFT)
        enabled = {a["verb"] for a in card["actions"] if a["enabled"]}
        assert {"submit", "delete"} <= enabled
        assert "unaudit" not in enabled

    def test_unknown_state_is_marked_unverified_not_guessed(self, m):
        """状态取不到时不猜——标 unverified，让使用者知道这是未经核实的。"""
        card = m.card("采购订单", {"FID": "1", "FBillNo": "X"})
        audit = next(a for a in card["actions"] if a["verb"] == "audit")
        assert audit["enabled"] is True and audit.get("unverified") is True

    def test_destructive_actions_need_confirmation(self, m):
        card = m.card("采购订单", PO_AUDITED)
        by = {a["verb"]: a for a in card["actions"]}
        assert by["void"]["needs_confirmation"] is True      # 作废无逆动词
        assert by["close"]["needs_confirmation"] is False    # 有 unclose


class TestActionForms:
    def test_save_asks_for_a_model(self, m):
        save = next(a for a in m.object_type("采购订单").actions if a.verb == "save")
        assert [p["name"] for p in save.params] == ["model"]

    def test_push_asks_for_target_and_source_numbers(self, m):
        push = next(a for a in m.object_type("采购订单").actions if a.verb == "push")
        names = [p["name"] for p in push.params]
        assert "target" in names and "source_bill_nos" in names
        hint = next(p for p in push.params if p["name"] == "source_bill_nos")["hint"]
        assert "FBillNo" in hint, "必须提醒是编号不是内码——这两者不通用"

    def test_execute_family_offers_operation_override(self, m):
        close = next(a for a in m.object_type("采购订单").actions if a.verb == "close")
        assert any(p["name"] == "operation" for p in close.params), \
            "整单关闭的操作编码随表单而异，二开单必须能显式指定"


class TestNavigation:
    def test_navigate_admits_it_cannot_know_the_field(self, m):
        nav = m.navigate("采购订单", "采购入库单", "CGDD000231")
        assert nav["confirmed"] is False
        assert len(nav["candidate_filters"]) >= 3
        assert "随表单与二开而异" in nav["why"]
        assert "profiles" in nav["remember"], "要告诉人怎么把答案固化下来"

    def test_unregistered_link_is_blocked(self):
        """navigate 现在是协程（要探测下游），未登记的链接在 await 时被拦下。"""
        load.cache_clear()
        d = Dispatcher(ontology=load(tenant=""), transport=FakeTransport())
        with pytest.raises(OntologyError):
            asyncio.run(d.navigate("采购订单", "销售出库单", "X"))


class TestSearchAndCards:
    def test_search_by_alias(self, m):
        hits = {t["form_id"] for t in m.search_types("采购")}
        assert "PUR_PurchaseOrder" in hits

    def test_filter_by_category(self, m):
        cats = {t["category"] for t in m.search_types(category="system")}
        assert cats == {"system"}

    def test_type_card_and_instance_card_share_shape(self, m):
        t = m.card("采购订单")
        i = m.card("采购订单", PO_AUDITED)
        assert set(t) == set(i), "类型卡片与实例卡片必须同形状，使用者不该学两套结构"
        assert t["is_instance"] is False and i["is_instance"] is True


class TestDispatcherIntegration:
    def test_type_card_needs_no_network(self):
        load.cache_clear()
        t = FakeTransport()
        d = Dispatcher(ontology=load(tenant=""), transport=t)
        card = asyncio.run(d.object_card("采购订单"))
        assert t.calls == [], "类型卡片是纯本体推导，不该发请求"
        assert "hint" in card

    def test_instance_card_reads_then_builds(self):
        load.cache_clear()
        t = FakeTransport([{"Result": {"Result": PO_AUDITED}}])
        d = Dispatcher(ontology=load(tenant=""), transport=t)
        card = asyncio.run(d.object_card("采购订单", "100231"))
        assert t.calls[0][0] == "view"
        assert card["state"] == "C:已审核" and card["title"] == "CGDD000231"

    def test_not_found_explains_the_id_duality(self):
        load.cache_clear()
        t = FakeTransport([{"Result": {"Result": {}}}, []])
        d = Dispatcher(ontology=load(tenant=""), transport=t)
        with pytest.raises(OntologyError) as e:
            asyncio.run(d.object_card("采购订单", "NOPE"))
        assert "内码" in str(e.value) and "编号" in str(e.value)


class TestIdentify:
    """「这张单是什么单」——按编号前缀推断，返回候选而非断言。"""

    def test_known_prefix_yields_candidate_with_evidence(self, m):
        r = m.identify("CGDD000231")
        assert r["candidates"][0]["form_id"] == "PUR_PurchaseOrder"
        assert r["candidates"][0]["evidence"], "每条前缀都要能说出依据，不能是编的"
        assert "未经账套核实" in r["note"]

    def test_longer_prefix_wins_and_dedupes(self, m):
        """CGRKD 与 CGRK 都命中同一类型时，只列一次。"""
        assert [c["form_id"] for c in m.identify("CGRKD2026040015")["candidates"]] \
            == ["STK_InStock"]

    def test_unknown_prefix_does_not_pretend(self, m):
        r = m.identify("ZZZ001")
        assert r["candidates"] == []
        assert "不代表单号有问题" in r["note"], "认不出≠单号错，措辞不能误导"
        assert "bill_prefixes" in r["note"], "要告诉人怎么让系统记住"

    def test_empty_input_rejected(self, m):
        with pytest.raises(OntologyError):
            m.identify("")


class TestProbingNavigation:
    """导航从"给候选"升级为"替你试"，并把试出来的答案提给知识库。"""

    def _d(self, script, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        load.cache_clear()
        return Dispatcher(ontology=load(tenant=""), transport=FakeTransport(script))

    def test_probes_candidates_until_one_hits(self, tmp_path, monkeypatch):
        d = self._d([[], [{"FBillNo": "RKD001"}]], tmp_path, monkeypatch)
        r = asyncio.run(d.navigate("采购订单", "采购入库单", "CGDD000231"))
        assert r["confirmed_by_probe"] is True
        assert r["filter"].startswith("FSourceBillNo")
        assert r["count"] == 1
        assert len(r["tried"]) == 2, "第一个候选没命中，应继续试下一个"

    def test_reports_honestly_when_nothing_hits(self, tmp_path, monkeypatch):
        d = self._d([[], [], [], []], tmp_path, monkeypatch)
        r = asyncio.run(d.navigate("采购订单", "采购入库单", "CGDD000231"))
        assert r["confirmed_by_probe"] is False and r["count"] == 0
        assert "也可能" in r["note"], "查不到有两种解释，不能只说一种"

    def test_hit_proposes_a_link_filter_to_the_knowledge_base(self, tmp_path, monkeypatch):
        d = self._d([[{"FBillNo": "RKD001"}]], tmp_path, monkeypatch)
        asyncio.run(d.navigate("采购订单", "采购入库单", "CGDD000231"))
        import json
        # 这是运行期相对 cwd 写出的知识库，不是包内文件——包搬家不影响它
        kb = tmp_path / "wikiskill" / "knowledge.json"
        assert kb.exists(), "试出来的答案应该沉淀，否则下次还要再试一遍"
        e = json.loads(kb.read_text(encoding="utf-8"))["entries"][0]
        assert e["kind"] == "link_filter_learned"
        assert "profiles" in e["suggestion"] and "人眼核对" in e["suggestion"], \
            "只提议不自动改——探测结果不等于账套确认"

    def test_probe_can_be_switched_off(self, tmp_path, monkeypatch):
        d = self._d([], tmp_path, monkeypatch)
        r = asyncio.run(d.navigate("采购订单", "采购入库单", "X", try_candidates=False))
        assert "candidate_filters" in r and "tried" not in r


class TestApplicableOperations:
    def test_object_card_lists_tenant_operations(self):
        load.cache_clear()
        mm = ObjectModel(load(tenant="example-tenant"))
        ops = mm.card("销售订单")["operations"]
        assert {o["zh"] for o in ops} == {"销售开票", "关闭超期订单"}
        assert all(o["starts_here"] for o in ops)

    def test_downstream_object_knows_it_is_not_the_start(self):
        load.cache_clear()
        mm = ObjectModel(load(tenant="example-tenant"))
        ops = mm.card("客户开票申请单")["operations"]
        assert ops and ops[0]["starts_here"] is False

    def test_base_registry_has_no_operations(self, m):
        assert m.card("销售订单")["operations"] == []
