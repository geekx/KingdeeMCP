"""只读长尾收敛：系统对象、多名词、详情、报表、实时字段、预演校验。

覆盖第二批收敛（29 个无单一 form_id 的只读工具）中可被底座表达的部分。
"""
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from base.dispatch import Dispatcher            # noqa: E402
from base.ontology import OntologyError, load   # noqa: E402
from base.transport import FakeTransport        # noqa: E402


@pytest.fixture
def onto():
    load.cache_clear()
    return load(tenant="")


class TestSystemObjects:
    """用户/角色/权限等走专用端点，调用方不必知道这个区别。"""

    @pytest.mark.parametrize("ref,form,ep", [
        ("用户", "BD_User", "user"),
        ("角色", "BD_Role", "role"),
        ("权限", "SYS_Permission", "permission"),
        ("编码规则", "SYS_NumberRule", "number_rule"),
        ("序列规则", "SYS_SequenceRule", "sequence"),
        ("系统参数", "SYS_SystemConfig", "sysconfig"),
    ])
    def test_routed_to_dedicated_endpoint(self, onto, ref, form, ep):
        t = FakeTransport([[{"x": 1}]])
        d = Dispatcher(ontology=onto, transport=t)
        r = asyncio.run(d.query(ref))
        assert r["noun"] == form and r["category"] == "system"
        assert t.calls[0][0] == f"system:{ep}", "系统对象不能走通用单据查询端点"

    def test_default_fields_come_from_registry(self, onto):
        t = FakeTransport([[]])
        asyncio.run(Dispatcher(ontology=onto, transport=t).query("用户"))
        assert "FUserID" in t.calls[0][2]["fields"]

    def test_system_objects_reject_write_verbs(self, onto):
        with pytest.raises(OntologyError) as e:
            onto.check_verb_applies("save", "BD_User")
        assert "系统对象" in str(e.value)


class TestMultiNounQuery:
    def test_comma_separated_nouns_are_merged(self, onto):
        t = FakeTransport([[{"a": 1}], [{"b": 1}, {"b": 2}]])
        r = asyncio.run(Dispatcher(ontology=onto, transport=t).query("销售出库单,采购入库单"))
        assert r["nouns"] == ["销售出库单", "采购入库单"]
        assert r["total"] == 3
        assert [c[1] for c in t.calls] == ["SAL_OUTSTOCK", "STK_InStock"]

    def test_one_bad_noun_does_not_kill_the_rest(self, onto):
        """一个名词写错不该让整次查询失败——其余结果照常返回，错的那个带错误信息。"""
        t = FakeTransport([[{"a": 1}]])
        r = asyncio.run(Dispatcher(ontology=onto, transport=t).query("采购订单,不存在的单据"))
        assert r["groups"][0]["count"] == 1
        assert "error" in r["groups"][1]


class TestRead:
    def test_read_uses_view_endpoint(self, onto):
        t = FakeTransport()
        r = asyncio.run(Dispatcher(ontology=onto, transport=t).read("采购订单", "100231"))
        assert t.calls[0][0] == "view"
        assert r["bill_id"] == "100231" and r["noun"] == "PUR_PurchaseOrder"

    def test_read_rejected_on_query_views(self, onto):
        """查询视图没有单条详情可看。"""
        with pytest.raises(OntologyError):
            onto.check_verb_applies("read", "STK_Inventory")


class TestReport:
    def test_report_uses_its_own_endpoint(self, onto):
        t = FakeTransport([{"Result": {"Rows": [{"r": 1}]}}])
        r = asyncio.run(Dispatcher(ontology=onto, transport=t).report(
            "STK_StockSumReport", {"FilterString": "x"}))
        assert t.calls[0][0] == "report"
        assert r["count"] == 1

    def test_unknown_report_id_passes_through(self, onto):
        """报表标识不在名词表里是正常的——报表不是单据，不强求登记。"""
        t = FakeTransport([{"Result": {"Rows": []}}])
        r = asyncio.run(Dispatcher(ontology=onto, transport=t).report("SomeCustomReport", {}))
        assert r["noun"] == "SomeCustomReport"


class TestLiveFields:
    def test_fields_hits_metadata_not_registry(self, onto):
        t = FakeTransport([{"count": 3, "required": ["FSupplierId"], "fields": []}])
        r = asyncio.run(Dispatcher(ontology=onto, transport=t).fields("采购订单"))
        assert t.calls[0][0] == "fields"
        assert r["required"] == ["FSupplierId"]

    def test_missing_metadata_gives_actionable_error(self, onto):
        t = FakeTransport([None])
        with pytest.raises(OntologyError) as e:
            asyncio.run(Dispatcher(ontology=onto, transport=t).fields("采购订单"))
        assert "kd_describe" in str(e.value)


class TestDryRun:
    def test_dry_run_save_does_not_write(self, onto):
        t = FakeTransport([{"ok": False, "missing_required": ["FSupplierId"]}])
        r = asyncio.run(Dispatcher(ontology=onto, transport=t).act(
            "save", "采购订单", [], model={"FDate": "2026-09-02"}, dry_run=True))
        assert r["dry_run"] is True
        assert r["missing_required"] == ["FSupplierId"]
        assert [c[0] for c in t.calls] == ["validate"], "dry_run 不得发生任何写操作"

    def test_dry_run_rejected_for_other_verbs(self, onto):
        """没有预演接口的动词不能假装支持 dry_run。"""
        with pytest.raises(OntologyError) as e:
            asyncio.run(Dispatcher(ontology=onto, transport=FakeTransport()).act(
                "audit", "采购订单", ["1"], dry_run=True))
        assert "只支持 verb='save'" in str(e.value)


class TestToolSurfaceStillSmall:
    def test_nine_tools_cover_the_converged_readonly_surface(self):
        """收敛了 40+13 个只读工具后，底座仍只有 9 个工具。"""
        import os
        os.environ.setdefault("KINGDEE_PASSWORD", "-")
        sys.path.insert(0, str(ROOT / "src"))
        from base.server import mcp
        tools = asyncio.run(mcp.list_tools())
        assert len(tools) == 9
        assert {t.name for t in tools} == {
            "kd_describe", "kd_query", "kd_act", "kd_push", "kd_read",
            "kd_report", "kd_run", "kd_audit", "kd_check_profile"}


@pytest.fixture(scope="module")
def cov():
    sys.path.insert(0, str(ROOT / "tools" / "ontology"))
    import measure_convergence as mc
    from base.ontology import load as _load
    _load.cache_clear()
    nouns = set(_load(tenant="").nouns)
    tools = mc._tools()
    covered, uncovered = {}, {}
    for t in tools:
        by, why = mc.classify(t, nouns)
        (covered if by else uncovered)[t["tool"]] = by or why
    return {"total": len(tools), "covered": covered, "uncovered": uncovered,
            "deliberate": mc.DELIBERATE}


class TestConvergenceCoverage:
    """收敛覆盖率不许回退，且"刻意不收"必须有明文理由。"""

    def test_coverage_does_not_regress(self, cov):
        pct = len(cov["covered"]) * 100 // cov["total"]
        assert pct >= 94, (
            f"只读工具收敛率跌到 {pct}%（{len(cov['covered'])}/{cov['total']}）。"
            f"未覆盖：{sorted(cov['uncovered'])}")

    def test_every_uncovered_tool_is_deliberate(self, cov):
        """未收敛的必须是刻意排除且写明理由——'还没做'和'不打算做'不能混为一谈。"""
        accidental = sorted(set(cov["uncovered"]) - set(cov["deliberate"]))
        assert not accidental, (
            f"这些工具既没被底座覆盖，也没登记在 DELIBERATE 里：{accidental}。"
            f"要么补进注册表，要么写明为什么不收。")

    def test_deliberate_exclusions_have_reasons(self, cov):
        for tool, reason in cov["deliberate"].items():
            assert len(reason) >= 4, f"{tool} 的排除理由太敷衍：{reason!r}"
