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

from base.dispatch import Dispatcher            # noqa: E402
from base.objects import ObjectModel            # noqa: E402
from base.ontology import OntologyError, load   # noqa: E402
from base.transport import FakeTransport        # noqa: E402


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
        load.cache_clear()
        d = Dispatcher(ontology=load(tenant=""), transport=FakeTransport())
        with pytest.raises(OntologyError):
            d.navigate("采购订单", "销售出库单", "X")


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
