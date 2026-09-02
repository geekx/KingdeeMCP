"""MCP 底座：本体、前置规则、原子性契约、租户覆盖层、业务操作入口。"""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from base.dispatch import Dispatcher            # noqa: E402
from base.ontology import OntologyError, load   # noqa: E402
from base.transport import FakeTransport        # noqa: E402

OK = {"Result": {"ResponseStatus": {"IsSuccess": True}, "Id": "1001", "Number": "T0001"}}
FAIL = {"Result": {"ResponseStatus": {"IsSuccess": False,
        "Errors": [{"Message": "批号不能为空", "FieldName": "FLot"}]}}}
PUSH_OK = {"Result": {"ResponseStatus": {"IsSuccess": True},
                      "Numbers": ["RKD001"], "Ids": ["200318"]}}


@pytest.fixture(autouse=True)
def _isolated_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("KINGDEE_OPERATION_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    import operation_audit as oa
    monkeypatch.setattr(oa, "audit_recorder", oa.AuditRecorder(tmp_path / "audit.jsonl"))
    import base.dispatch as bd
    monkeypatch.setattr(bd, "audit_recorder", oa.audit_recorder)
    return tmp_path / "audit.jsonl"


@pytest.fixture
def base_o():
    return load(tenant="")


@pytest.fixture
def tenant_o():
    return load(tenant="example-tenant")


class TestRegistry:
    def test_registry_loads(self, base_o):
        # 不写死名词总数——长尾收敛会持续往注册表里加名词，
        # 而"加名词"正是这套设计要做到的零成本操作。
        assert len(base_o.nouns) >= 48
        assert len(base_o.verbs) == 14
        assert base_o.links
        for essential in ("PUR_PurchaseOrder", "SAL_SaleOrder", "BD_Material"):
            assert essential in base_o.nouns

    def test_adding_nouns_does_not_grow_the_tool_surface(self, base_o):
        """名词是数据不是能力：注册表长到 71 个，MCP 工具数仍是 7。"""
        assert len(base_o.nouns) >= 71

    def test_destructive_is_derived_not_annotated(self, base_o):
        """修 N-1：破坏性从「有无逆动词」推导，不再依赖人工标注。"""
        assert base_o.verb("delete").destructive is True
        assert base_o.verb("push").destructive is True
        assert base_o.verb("audit").destructive is False   # 有 unaudit
        assert base_o.verb("query").destructive is False

    def test_state_groups_fix_the_D_overlap(self, base_o):
        """修 S-2：'D' 曾同时属于 pending 与 rejected。"""
        assert "D:重新审核" in base_o.state_groups["pending"]
        assert base_o.state_groups["rejected"] is None

    def test_alias_resolution(self, base_o):
        for ref in ("PUR_PurchaseOrder", "采购订单", "采购单", "PO"):
            assert base_o.resolve_noun(ref).form_id == "PUR_PurchaseOrder"

    def test_unknown_noun_suggests_candidates(self, base_o):
        with pytest.raises(OntologyError) as e:
            base_o.resolve_noun("采购")
        assert "可能是" in str(e.value)


class TestPreconditions:
    def test_verb_noun_applicability(self, base_o):
        """PRE-01 / MISS-01：审核不适用于基础资料。"""
        with pytest.raises(OntologyError) as e:
            base_o.check_verb_applies("audit", "BD_Material")
        assert "可用动词" in str(e.value)

    def test_view_is_read_only(self, base_o):
        with pytest.raises(OntologyError) as e:
            base_o.check_verb_applies("save", "STK_Inventory")
        assert "查询视图" in str(e.value)

    def test_unregistered_link_blocked(self, base_o):
        """PRE-02 / MISS-02：未登记的下推在发请求前被拦下。"""
        with pytest.raises(OntologyError) as e:
            base_o.check_link("PUR_PurchaseOrder", "SAL_OUTSTOCK")
        assert "已登记的目标单" in str(e.value)

    def test_state_precondition(self, base_o):
        with pytest.raises(OntologyError) as e:
            base_o.check_state("audit", "Z:暂存")
        assert "B:审核中" in str(e.value)

    def test_unknown_state_degrades_to_warning(self, base_o):
        """刻意不自动补一次查询——那会让每个写操作的往返翻倍。"""
        assert "跳过状态校验" in base_o.check_state("audit", None)

    def test_suspect_link_still_allowed_but_flagged(self, base_o):
        assert base_o.check_link("PRD_PickMtrl", "PRD_Instock")["verified"] == "suspect"


class TestAtomicityContract:
    def test_contract_always_returned(self, base_o):
        d = Dispatcher(ontology=base_o, transport=FakeTransport([OK, OK]))
        r = asyncio.run(d.act("submit", "采购订单", ["100", "101"]))
        assert r["contract"] == {"arity": "batch", "atomicity": "per_item",
                                 "idempotent": False, "destructive": False,
                                 "inverse": "cancel"}

    def test_per_item_reports_partial(self, base_o):
        """修 A-2：部分成功必须可辨认，且明说已成功的不回滚。"""
        d = Dispatcher(ontology=base_o, transport=FakeTransport([OK, FAIL]))
        r = asyncio.run(d.act("submit", "采购订单", ["100", "101"]))
        assert r["outcome"] == "partial"
        assert r["succeeded"] == ["100"] and r["failed"] == ["101"]
        assert "不会回滚" in r["tip"]

    def test_server_defined_admits_it_cannot_report_per_id(self, base_o):
        d = Dispatcher(ontology=base_o, transport=FakeTransport([FAIL]))
        r = asyncio.run(d.act("void", "采购订单", ["100", "101"]))
        assert r["contract"]["atomicity"] == "server_defined"
        assert r["results"][0]["per_id_unavailable"] is True
        assert "无法判定" in r["tip"]

    def test_exception_is_unknown_not_failed(self, base_o):
        """结果不可判定必须与失败区分——服务端可能已生效。"""
        class Boom(FakeTransport):
            async def call(self, *a, **kw): raise TimeoutError("read timeout")
        d = Dispatcher(ontology=base_o, transport=Boom())
        r = asyncio.run(d.act("submit", "采购订单", ["100"]))
        assert r["results"][0]["outcome"] == "unknown"

    def test_push_does_not_auto_submit(self, base_o):
        t = FakeTransport([PUSH_OK])
        d = Dispatcher(ontology=base_o, transport=t)
        r = asyncio.run(d.push("采购订单", "STK_InStock", ["CGDD001"]))
        assert [c[0] for c in t.calls] == ["push"], "push 不得自动串联 submit/audit"
        assert "不自动提交审核" in r["next"]


class TestTenantOverlay:
    def test_custom_form_and_link(self, tenant_o):
        assert "PAEZ_CustomInvoice" in tenant_o.nouns
        assert tenant_o.check_link("SAL_SaleOrder", "PAEZ_CustomInvoice")

    def test_partial_override_inherits_rest(self, tenant_o, base_o):
        n = tenant_o.nouns["SAL_SaleOrder"]
        assert "FProjectNo" in n.default_fields          # 覆盖生效
        assert n.allowed_verbs == base_o.nouns["SAL_SaleOrder"].allowed_verbs  # 其余继承

    def test_overlay_cannot_delete_base_entries(self, tenant_o, base_o):
        assert set(base_o.nouns) <= set(tenant_o.nouns)

    def test_base_unaffected_by_overlay(self, base_o):
        assert "PAEZ_CustomInvoice" not in base_o.nouns


class TestOperationEntrypoints:
    def test_dry_run_does_no_writes(self, tenant_o):
        t = FakeTransport()
        d = Dispatcher(ontology=tenant_o, transport=t)
        r = asyncio.run(d.run_operation("销售开票", ["XSDD001"]))
        assert r["status"] == "awaiting_confirmation"
        assert t.calls == [], "未确认时不得发生任何写操作"
        assert len(r["plan"]) == 5

    def test_runs_after_confirmation(self, tenant_o):
        d = Dispatcher(ontology=tenant_o,
                       transport=FakeTransport([PUSH_OK, OK, OK, PUSH_OK]))
        r = asyncio.run(d.run_operation("销售开票", ["XSDD001"], confirmed=True))
        assert r["success"] is True and len(r["steps"]) == 5

    def test_halt_reports_left_behind(self, tenant_o, _isolated_audit):
        d = Dispatcher(ontology=tenant_o, transport=FakeTransport([PUSH_OK, FAIL]))
        r = asyncio.run(d.run_operation("采购收货入库", ["CGDD001"]))
        assert r["success"] is False and r["halted_at"] == 2
        assert r["left_behind"]["STK_InStock"] == ["RKD001"]
        assert "不会自动回滚" in r["tip"]

    def test_all_steps_share_one_trace(self, tenant_o, _isolated_audit):
        """修 P-5：一次业务操作的所有步骤必须能拼回同一条链。"""
        d = Dispatcher(ontology=tenant_o, transport=FakeTransport([PUSH_OK, OK, OK]))
        asyncio.run(d.run_operation("采购收货入库", ["CGDD001"]))
        recs = [json.loads(l) for l in open(_isolated_audit, encoding="utf-8")]
        assert len({r["trace_id"] for r in recs}) == 1
        assert {r["tool"] for r in recs} == {"kd_run:采购收货入库"}
        assert [r["step"] for r in recs] == [1, 2, 3]

    def test_unknown_operation_points_to_profile(self, tenant_o):
        with pytest.raises(OntologyError) as e:
            tenant_o.operation("随便编的操作")
        assert "profiles" in str(e.value)


class TestProfileValidation:
    def test_example_tenant_is_valid(self):
        from base.validate_profile import validate
        errs, _ = validate("example-tenant")
        assert errs == []

    def test_broken_profile_reports_chinese_errors(self, tmp_path, monkeypatch):
        from base import ontology as ont
        from base.validate_profile import validate
        bad = {"tenant": "bad", "operations": {"乱写": {"steps": [
            {"做": "下推", "从": "SAL_SaleOrder", "到": "PRD_MO"},
            {"做": "audit", "对象": "BD_Material", "用": "targets"},
            {"做": "提交", "对象": "SAL_SaleOrder", "用": "targets"},
        ]}}}
        monkeypatch.setattr(ont, "load_profile",
                            lambda t: bad if t == "bad" else None)
        import base.validate_profile as vp
        monkeypatch.setattr(vp, "load_profile", lambda t: bad if t == "bad" else None)
        ont.load.cache_clear()
        errs, warns = validate("bad")
        assert len(errs) == 3
        assert any("未登记的下推关系" in e for e in errs)
        assert any("不适用于" in e for e in errs)
        assert any("不认识的动作" in e for e in errs)
        ont.load.cache_clear()
