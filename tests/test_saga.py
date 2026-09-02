"""多扣扳机组 = Saga：守卫、逐步授权、逆序补偿。

像 12306 那条链（查询 → 付款状态 → 扣库存 → 出单），关键不是"按顺序打完"，
而是每步有守卫、有些步要人点头、失败要把已生效的按逆序退掉。

这组测试盯的是**出事时的行为**——顺利跑完谁都会，出事才见真章。
"""
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kingdee_ontology.base.dispatch import Dispatcher            # noqa: E402
from kingdee_ontology.base.ontology import OntologyError, load   # noqa: E402
from kingdee_ontology.base.transport import FakeTransport        # noqa: E402
from kingdee_ontology.saga.engine import SagaError, eval_condition, parse_steps  # noqa: E402
from kingdee_ontology.saga.model import RunState, RunStore, SagaRun              # noqa: E402

OK = {"Result": {"ResponseStatus": {"IsSuccess": True}, "Id": "1", "Number": "N1"}}
FAIL = {"Result": {"ResponseStatus": {"IsSuccess": False,
        "Errors": [{"Message": "必录字段未填"}]}}}
PUSH_OK = {"Result": {"ResponseStatus": {"IsSuccess": True},
                      "Numbers": ["FP001"], "Ids": ["9001"]}}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KINGDEE_OPERATION_AUDIT_LOG", str(tmp_path / "a.jsonl"))
    import kingdee_ontology.operation_audit as oa
    sys.path.insert(0, str(ROOT / "tools" / "ontology"))
    monkeypatch.setattr(oa, "audit_recorder", oa.AuditRecorder(tmp_path / "a.jsonl"))
    import kingdee_ontology.base.dispatch as bd
    monkeypatch.setattr(bd, "audit_recorder", oa.audit_recorder)
    load.cache_clear()
    yield
    load.cache_clear()


def _d(script, tenant="example-tenant"):
    return Dispatcher(ontology=load(tenant=tenant), transport=FakeTransport(script),
                      actor="tester")


class TestGuard:
    """守卫：写之前先验条件。没有守卫的顺序执行只是盲发。"""

    @pytest.mark.parametrize("rows,cond,ok", [
        ([{"FBaseQty": "5"}], "FBaseQty >= 1", True),
        ([{"FBaseQty": "0"}], "FBaseQty >= 1", False),
        ([{"FDocumentStatus": "C"}], "FDocumentStatus = 'C'", True),
        ([{"FDocumentStatus": "Z"}], "FDocumentStatus = 'C'", False),
        ([], "FBaseQty >= 1", False),
    ])
    def test_condition_eval(self, rows, cond, ok):
        assert eval_condition(cond, rows)[0] is ok

    def test_empty_rows_say_cannot_confirm_not_false(self):
        """查不到行 ≠ 条件为假。措辞要说清是『无法确认』。"""
        assert "无法确认" in eval_condition("FBaseQty >= 1", [])[1]

    def test_arbitrary_expressions_are_refused(self):
        with pytest.raises(SagaError) as e:
            eval_condition("__import__('os').system('rm -rf /')", [])
        assert "只支持" in str(e.value)

    def test_guard_failure_stops_before_any_write(self):
        """源单不是已审核 → 守卫拦下，一次写都不该发生。"""
        d = _d([[{"FDocumentStatus": "Z", "FID": "1", "FBillNo": "XSDD001"}]])
        r = asyncio.run(d.run_operation("销售开票", ["XSDD001"], confirmed=True))
        assert r["state"] in (RunState.HALTED.value, RunState.COMPENSATED.value)
        assert r["left_behind"] == [], "守卫拦下时不该产生任何遗留"
        writes = [c for c in d.t.calls if c[0] in ("push", "save", "submit", "audit")]
        assert writes == [], f"守卫失败后仍发生了写操作：{writes}"


class TestPerStepAuthorization:
    """逐步授权：开头点一次不等于全权委托。"""

    def _start(self, script):
        d = _d(script)
        r = asyncio.run(d.run_operation("销售开票", ["XSDD001"], confirmed=True))
        return d, r

    def test_stops_at_the_authorized_step(self):
        d, r = self._start([[{"FDocumentStatus": "C"}], PUSH_OK, OK])
        assert r["state"] == RunState.AWAITING_AUTH.value
        assert r["awaiting"]["role_required"] == "财务主管"
        assert "财务主管" in r["tip"]

    def test_earlier_steps_already_ran(self):
        d, r = self._start([[{"FDocumentStatus": "C"}], PUSH_OK, OK])
        assert any(c[0] == "push" for c in d.t.calls), "授权前的步骤应已执行"
        assert r["left_behind"], "停下来时已生成的对象要列出来"

    def test_authorization_must_be_named(self):
        d, r = self._start([[{"FDocumentStatus": "C"}], PUSH_OK, OK])
        with pytest.raises(SagaError) as e:
            d.saga.authorize(d.saga.store.get(r["run_id"]), by="")
        assert "记名" in str(e.value)

    def test_approve_then_resume(self):
        d, r = self._start([[{"FDocumentStatus": "C"}], PUSH_OK, OK])
        rid = r["run_id"]
        d.authorize_step(rid, by="李主管")
        d2 = _d([OK, PUSH_OK])
        r2 = asyncio.run(d2.run_operation("销售开票", [], confirmed=True, run_id=rid))
        assert r2["state"] == RunState.DONE.value
        run = d2.saga.store.get(rid)
        assert any(x.get("authorized_by") == "李主管" for x in run.results), \
            "谁批的必须留痕"

    def test_reject_halts_and_records_who(self):
        d, r = self._start([[{"FDocumentStatus": "C"}], PUSH_OK, OK])
        rep = d.authorize_step(r["run_id"], by="李主管", approve=False, reason="金额不对")
        assert rep["state"] == RunState.HALTED.value
        run = d.saga.store.get(r["run_id"])
        assert "李主管" in run.error and "金额不对" in run.error

    def test_cannot_authorize_a_step_that_needs_none(self):
        d, r = self._start([[{"FDocumentStatus": "C"}], PUSH_OK, OK])
        with pytest.raises(SagaError) as e:
            d.saga.authorize(d.saga.store.get(r["run_id"]), by="张三", step=0)
        assert "不需要授权" in str(e.value)


class TestCompensation:
    """逆序补偿：任一步失败，已生效的按倒序退掉。"""

    def test_failure_compensates_in_reverse(self):
        # 采购收货入库：push → submit → audit，audit 失败
        d = _d([PUSH_OK, OK, FAIL,
                OK, OK])          # 补偿：unaudit 不需要（audit 没成功）→ cancel → delete
        r = asyncio.run(d.run_operation("采购收货入库", ["CGDD001"], confirmed=True))
        assert r["state"] == RunState.COMPENSATED.value
        assert r["left_behind"] == [], "补偿干净后不该还有遗留"
        assert "补偿干净" in r["tip"]

    def test_compensation_runs_in_reverse_order(self):
        d = _d([PUSH_OK, OK, FAIL, OK, OK])
        asyncio.run(d.run_operation("采购收货入库", ["CGDD001"], confirmed=True))
        eps = [c[0] for c in d.t.calls]
        # 正向 push,submit,audit(失败) → 补偿按逆序：先 cancel(submit 的逆)，后 delete
        assert eps.index("cancel_assign") < eps.index("delete"), \
            f"补偿必须逆序执行，实际顺序：{eps}"

    def test_failed_compensation_is_the_loudest_state(self):
        d = _d([PUSH_OK, OK, FAIL, FAIL, FAIL])
        r = asyncio.run(d.run_operation("采购收货入库", ["CGDD001"], confirmed=True))
        assert r["state"] == RunState.COMPENSATION_FAILED.value
        assert "必须人工处理" in r["tip"], "补偿失败是最坏的终态，措辞必须够响"
        assert r["left_behind"], "没退掉的东西必须列出来"

    def test_irreversible_verbs_are_not_compensated_by_guessing(self):
        """本体说退不回来的动词（delete/void），失败时只报告遗留，**不猜**。

        补偿现在从本体的 compensation 字段继承——这不是猜，是声明。
        但 compensation 为 null 的动词就是真的退不回来，
        不许拿 inverse 顶替：inverse 退的是同一个对象的状态，
        compensation 清的是这一步的产物，两者不是一回事。
        """
        import kingdee_ontology.base.ontology as ont
        prof = {"tenant": "_t", "operations": {"删了再说": {"zh": "删了再说", "steps": [
            {"做": "delete", "对象": "PUR_PurchaseOrder", "用": "targets"},
            {"做": "submit", "对象": "PUR_PurchaseOrder", "用": "targets"},
        ]}}}
        ont.load.cache_clear()
        orig = ont.load_profile
        ont.load_profile = lambda t: prof if t == "_t" else orig(t)
        try:
            d = _d([OK, FAIL], tenant="_t")
            assert d.o.verbs["delete"].compensation is None, "前提：delete 本就退不回来"
            r = asyncio.run(d.run_operation("删了再说", ["CGDD001"], confirmed=True))
            assert r["state"] == RunState.HALTED.value, \
                "有步骤退不回来时不能自称『已补偿』"
            assert "人工处理" in r["tip"]
        finally:
            ont.load_profile = orig
            ont.load.cache_clear()

    def test_compensation_is_inherited_from_the_ontology(self):
        """profile 不写『补偿:』时从本体继承——只有一个事实来源。"""
        d = _d([])
        steps = parse_steps(d.o.operation("采购收货入库"), d.o)
        assert [s.compensate for s in steps] == ["delete", "cancel", "unaudit"]

    def test_profile_contradicting_the_ontology_is_rejected(self):
        """两处事实打架时不许静默采信任何一方。"""
        import kingdee_ontology.base.ontology as ont
        prof = {"tenant": "_t3", "operations": {"打架": {"zh": "打架", "steps": [
            {"做": "audit", "对象": "PUR_PurchaseOrder", "用": "targets", "补偿": "delete"},
        ]}}}
        ont.load.cache_clear()
        orig = ont.load_profile
        ont.load_profile = lambda t: prof if t == "_t3" else orig(t)
        try:
            with pytest.raises(SagaError) as e:
                _d([], tenant="_t3").saga.plan("打架", ["X"])
            assert "与本体矛盾" in str(e.value)
        finally:
            ont.load_profile = orig
            ont.load.cache_clear()


class TestPersistenceAndTriage:
    def test_run_survives_across_dispatchers(self):
        """授权发生在带外，可能换个会话才回来批——不落盘就丢了。"""
        d = _d([[{"FDocumentStatus": "C"}], PUSH_OK, OK])
        r = asyncio.run(d.run_operation("销售开票", ["XSDD001"], confirmed=True))
        assert _d([]).saga.store.get(r["run_id"]) is not None

    def test_list_surfaces_runs_waiting_on_a_human(self):
        d = _d([[{"FDocumentStatus": "C"}], PUSH_OK, OK])
        asyncio.run(d.run_operation("销售开票", ["XSDD001"], confirmed=True))
        lst = _d([]).saga_list()
        assert lst["count"] == 1
        assert lst["runs"][0]["state"] == RunState.AWAITING_AUTH.value
        assert "最容易被忘掉" in lst["note"]

    def test_unknown_run_id_is_actionable(self):
        with pytest.raises(OntologyError) as e:
            asyncio.run(_d([]).run_operation("销售开票", [], confirmed=True, run_id="nope"))
        assert "kd_saga" in str(e.value)


class TestPlanIsSafe:
    def test_unconfirmed_does_no_writes_and_shows_auth_gates(self):
        d = _d([])
        r = asyncio.run(d.run_operation("销售开票", ["XSDD001"]))
        assert r["status"] == "awaiting_confirmation"
        assert d.t.calls == [], "未确认时不得发生任何调用"
        assert "仍会各自停下来等人批" in r["tip"], "要说清整体确认不覆盖逐步授权"
        gated = [s for s in r["steps"] if s["needs_auth"]]
        assert len(gated) == 1 and gated[0]["authorize"] == "财务主管"

    def test_bad_compensation_verb_rejected_at_plan_time(self):
        import kingdee_ontology.base.ontology as ont
        prof = {"tenant": "_t2", "operations": {"坏的": {"zh": "坏的", "steps": [
            {"做": "submit", "对象": "PUR_PurchaseOrder", "用": "targets", "补偿": "不存在的动词"},
        ]}}}
        ont.load.cache_clear()
        orig = ont.load_profile
        ont.load_profile = lambda t: prof if t == "_t2" else orig(t)
        try:
            with pytest.raises(SagaError) as e:
                _d([], tenant="_t2").saga.plan("坏的", ["X"])
            assert "补偿动词" in str(e.value)
        finally:
            ont.load_profile = orig
            ont.load.cache_clear()


class TestSituationalAwareness:
    """【态】运行也是对象——态势要能被人用同一套机制感知，
    而不是另起一套只有工程师看得懂的枚举。"""

    def _await_auth(self):
        d = _d([[{"FDocumentStatus": "C"}], PUSH_OK, OK])
        r = asyncio.run(d.run_operation("销售开票", ["XSDD001"], confirmed=True))
        return d, r["run_id"]

    def test_run_is_a_registered_noun(self):
        o = load(tenant="")
        n = o.resolve_noun("操作运行")
        assert n.form_id == "SAGA_Run" and n.category == "view"
        assert sorted(n.allowed_verbs) == ["query", "read"], "运行由系统产生，不接受写动词"

    def test_run_states_live_in_the_registry(self):
        o = load(tenant="")
        codes = [c for c in o.states if c.startswith("SAGA:")]
        assert "SAGA:awaiting_auth" in codes and "SAGA:compensation_failed" in codes
        assert o.states["SAGA:compensated"]["terminal"] is True
        assert o.states["SAGA:awaiting_auth"]["terminal"] is False

    def test_run_renders_as_an_object_card(self):
        d, rid = self._await_auth()
        c = asyncio.run(d.object_card("操作运行", rid))
        assert c["state"] == "SAGA:awaiting_auth" and c["state_zh"] == "等待授权"
        assert "最容易被忘掉" in c["state_note"]

    def test_card_says_what_can_be_done_now(self):
        d, rid = self._await_auth()
        c = asyncio.run(d.object_card("操作运行", rid))
        assert {a["verb"] for a in c["actions"]} == {"authorize", "reject"}
        by = next(p for a in c["actions"] if a["verb"] == "authorize"
                  for p in a["params"] if p["name"] == "by")
        assert by["required"] is True and "记名" in by["hint"]

    def test_left_behind_is_deduped(self):
        """同一张单被多步『产出』，不该列成好几张。"""
        d, rid = self._await_auth()
        c = asyncio.run(d.object_card("操作运行", rid))
        keys = [(x["noun"], x["id"]) for x in c["left_behind"]]
        assert len(keys) == len(set(keys)), f"遗留对象重复列出：{keys}"

    def test_terminal_run_has_no_actions(self):
        d = _d([PUSH_OK, OK, OK])
        r = asyncio.run(d.run_operation("采购收货入库", ["CGDD001"], confirmed=True))
        c = asyncio.run(d.object_card("操作运行", r["run_id"]))
        assert c["terminal"] is True and c["actions"] == []

    def test_card_links_to_the_audit_trail(self):
        d, rid = self._await_auth()
        c = asyncio.run(d.object_card("操作运行", rid))
        assert any("kd_audit" in l["note"] for l in c["links"])


class TestRulesAreRegistered:
    """【规】Saga 的约束要登记在规则表里，和其它规则一样可见、可审。"""

    def test_saga_rules_are_in_the_registry(self):
        ids = {r["id"] for r in load(tenant="").rules}
        assert {"SAGA-01", "SAGA-02", "SAGA-03"} <= ids

    def test_each_rule_names_who_enforces_it(self):
        for r in load(tenant="").rules:
            if r["id"].startswith("SAGA"):
                assert ".saga." in r.get("enforced_by", ""), \
                    f"{r['id']} 没说清谁执行它"


class TestVerbSupportsCompensation:
    """【动】补偿是动词的属性，不是 profile 的发明。"""

    def test_compensation_is_distinct_from_inverse(self):
        o = load(tenant="")
        push = o.verbs["push"]
        assert push.inverse is None, "push 没有逆动词——不存在 unpush"
        assert push.compensation == "delete", "但它的补偿是删掉产物"
        assert push.compensation_target == "produced", "补偿作用在产物上，不是自己"

    def test_self_compensating_verbs_target_themselves(self):
        o = load(tenant="")
        assert o.verbs["audit"].compensation_target == "self"

    def test_irreversible_verbs_declare_it(self):
        o = load(tenant="")
        for v in ("delete", "void"):
            assert o.verbs[v].compensation is None
            assert o.verbs[v].compensation_target is None
