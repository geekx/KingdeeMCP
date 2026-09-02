"""AIP Logic 判断层。

这一层存在的理由是**同一个问题只有一份实现**。所以这里的测试分两类：

  1. 判断本身对不对；
  2. 别处有没有偷偷再实现一遍——收拢的价值在于不分叉，
     而分叉是悄悄发生的，只能由机器守。
"""
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kingdee_ontology.aip import BLOCK, INFO, WARN, Decision, Facts, Reason, can, evaluate  # noqa: E402
from kingdee_ontology.aip.logic import REGISTRY  # noqa: E402
from kingdee_ontology.base.ontology import OntologyError, load  # noqa: E402


@pytest.fixture(scope="module")
def o():
    load.cache_clear()
    ont = load()
    yield ont
    load.cache_clear()


@pytest.fixture(scope="module")
def tenant_o():
    load.cache_clear()
    ont = load(tenant="example-tenant")
    yield ont
    load.cache_clear()


class TestDecisionType:
    def test_unknown_is_not_allowed(self, o):
        """「不知道」不等于「可以」——这是旧 availability() 的错法。"""
        d = can(o, "audit", "SAL_SaleOrder")          # 没给状态
        assert d.undetermined
        assert d.allowed is False
        assert not d.blocks, "缺事实不是『明确不行』"

    def test_block_is_not_allowed(self, o):
        d = can(o, "audit", "BD_Material")
        assert d.blocks and d.allowed is False

    def test_clean_decision_is_allowed(self, o):
        d = can(o, "submit", "SAL_SaleOrder", state="Z:暂存")
        assert d.allowed is True, d.why()

    def test_severity_is_validated(self):
        with pytest.raises(ValueError):
            Reason(rule="X", severity="很严重", message="")

    def test_reasons_carry_a_fix(self, o):
        """说了「不行」却不说「那该怎么办」，调用方只能猜。"""
        d = can(o, "audit", "BD_Material")
        assert all(r.fix for r in d.blocks), [r.to_dict() for r in d.blocks]

    def test_merge_is_associative(self):
        a = Decision((Reason("A", BLOCK, "a"),))
        b = Decision(undetermined=(Reason("B", BLOCK, "b"),))
        assert (a + b).blocks == a.blocks
        assert (a + b).undetermined == b.undetermined


class TestNoShortCircuit:
    def test_all_reasons_come_back_at_once(self, o):
        """不短路。短路省几微秒，代价是调用方改一个、再调一次、撞下一个——
        对 agent 那是一整轮重新思考。"""
        d = can(o, "audit", "BD_Material")            # 动词不适用 + 状态未知
        rules = {r.rule for r in d.reasons + d.undetermined}
        assert {"PRE-01", "PRE-03"} <= rules, rules

    def test_irrelevant_functions_do_not_report_missing(self, o):
        """问「审核能不能做」不该被告知缺『下推目标』。"""
        d = can(o, "audit", "SAL_SaleOrder", state="B:审核中")
        assert not any(r.rule == "PRE-02" for r in d.undetermined), d.to_dict()


class TestLogicFunctions:
    def test_verb_applies(self, o):
        assert can(o, "audit", "BD_Material").blocks
        assert not can(o, "save", "BD_Material").blocks

    def test_link_registered(self, o):
        d = evaluate(Facts(ontology=o, noun="SAL_SaleOrder", target="PRD_MO"),
                     only=["AIP-02"])
        assert d.blocks and "registry.yml" in d.blocks[0].fix

    def test_irreversible_warns_but_does_not_block(self, o):
        d = evaluate(Facts(ontology=o, verb="delete"), only=["AIP-04"])
        assert d.warnings and not d.blocks
        assert d.needs_confirmation

    def test_reversible_verb_is_silent(self, o):
        assert not evaluate(Facts(ontology=o, verb="audit"), only=["AIP-04"]).reasons

    def test_operation_code_is_advisory_not_blocking(self, o):
        """二开单最常见的一类『参数都对却报错』，提醒但不拦。"""
        d = evaluate(Facts(ontology=o, verb="close", noun="SAL_SaleOrder"),
                     only=["AIP-05"])
        assert d.reasons and not d.blocks
        assert d.reasons[0].severity == INFO

    def test_operation_code_silent_when_given(self, o):
        d = evaluate(Facts(ontology=o, verb="close", noun="SAL_SaleOrder",
                           params={"operation": "YLBillClose"}), only=["AIP-05"])
        assert not d.reasons

    def test_step_compensation_inherited_from_ontology(self, o):
        """继承来的补偿算数——对它报『没声明』会逼人在 profile 里重抄一遍，
        而重抄正是第二个事实来源的来源。"""
        d = evaluate(Facts(ontology=o, step={"做": "下推", "从": "A", "到": "B"}),
                     only=["AIP-06"])
        assert not d.warnings, d.to_dict()
        assert d.reasons and d.reasons[0].severity == INFO

    def test_irreversible_step_is_flagged(self, o):
        d = evaluate(Facts(ontology=o, step={"做": "delete", "对象": "SAL_SaleOrder"}),
                     only=["AIP-06"])
        assert d.warnings, "delete 没有补偿，必须说出来"

    def test_confirm_and_check_steps_need_no_compensation(self, o):
        for kind in ("确认", "检查"):
            d = evaluate(Facts(ontology=o, step={"做": kind}), only=["AIP-06"])
            assert not d.reasons, kind


class TestSingleSourceOfTruth:
    """判断收拢之后，别处不能再各写一份。"""

    def test_registry_pointers_resolve(self, o):
        """`decided_by` 指到哪，哪就得真有个函数。

        这类指针会随重构悄悄失效——文档说"由 X 执行"，X 早改名了，
        而没有任何测试会失败。所以由机器守。
        """
        bad = []
        for r in o.rules:
            ref = r.get("decided_by")
            if not ref:
                continue
            mod, _, fn = ref.rpartition(".")
            try:
                assert callable(getattr(importlib.import_module(mod), fn))
            except (ImportError, AttributeError, AssertionError):
                bad.append(f"{r['id']} → {ref}")
        assert not bad, f"registry.yml 的 decided_by 指空了：{bad}"

    def test_every_registered_function_is_claimed_by_a_rule(self, o):
        """反过来也要成立：逻辑函数必须挂在某条规则下。

        没有规则背书的判断，等于在代码里偷偷加了一条没人知道的业务约束。
        """
        claimed = {r.get("decided_by", "").rpartition(".")[2] for r in o.rules}
        orphan = [lf.id for lf in REGISTRY.values() if lf.fn.__name__ not in claimed]
        assert not orphan, f"这些逻辑函数没有对应的规则条目：{orphan}"

    def test_card_availability_agrees_with_decision(self, o):
        """对象卡上「可用」的动作，真去执行不能被前置条件拦下。

        今天成立是因为 _actions_for 只从 allowed_verbs 造动作。这条测试守的是
        以后：一旦有人为了"多给几个按钮"放宽那层过滤，卡片就会开始展示
        执行时必被 PRE-01 拦下的动作，而没有别的测试会失败。
        """
        from kingdee_ontology.base.objects import ObjectModel
        m = ObjectModel(o)
        mismatch = []
        for form_id in list(o.nouns)[:40]:
            card = m.card(form_id)
            for a in card["actions"]:
                if not a["enabled"]:
                    continue
                try:
                    o.check_verb_applies(a["verb"], form_id)
                except OntologyError as e:
                    mismatch.append(f"{form_id}.{a['verb']}: {e}")
        assert not mismatch, mismatch

    def test_needs_operation_code_table_has_one_home(self):
        """这张表原来埋在 objects.py 里，只在渲染卡片时用得到。"""
        import kingdee_ontology.base.objects as objects
        from kingdee_ontology.aip.logic import NEEDS_OPERATION_CODE
        assert objects._NEEDS_OPERATION is NEEDS_OPERATION_CODE


class TestPurity:
    """纯函数——这是"能做成毫秒级独立服务"的前提，不是洁癖。"""

    def test_no_io_imports_in_logic_layer(self):
        src = (ROOT / "src" / "kingdee_ontology" / "aip" / "logic.py").read_text(encoding="utf-8")
        for banned in ("import httpx", "import requests", "urllib", "open(",
                       "subprocess", "import os"):
            assert banned not in src, f"判断层不该碰 I/O，但出现了 {banned!r}"

    def test_decision_is_deterministic(self, o):
        a = can(o, "audit", "SAL_SaleOrder", state="Z:暂存").to_dict()
        b = can(o, "audit", "SAL_SaleOrder", state="Z:暂存").to_dict()
        assert a == b

    def test_logic_layer_does_not_import_base(self):
        """判断层不依赖 base，才能被单独打包、单独运行。"""
        src = (ROOT / "src" / "kingdee_ontology" / "aip" / "logic.py").read_text(encoding="utf-8")
        assert "kingdee_ontology.base" not in src, "判断层不能依赖 base——它要能被单独打包运行"


class TestTenantOverlay:
    def test_tenant_verbs_are_judged_too(self, tenant_o):
        """租户新增的单据同样受判断层管——覆盖层不是判断的后门。"""
        d = can(tenant_o, "audit", "BD_Material")
        assert d.blocks
