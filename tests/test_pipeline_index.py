"""数据加工层（线/表/解析/标准）与 Funnel 索引层。

断言的重点不是"能跑通"，而是**不能悄悄弄错数据**：
位置数组错位要中断而不是猜、标识符不许转数字、状态取不到要标 None 而不是编、
索引要如实承认自己是快照。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kingdee_ontology.base.ontology import load                                   # noqa: E402
from kingdee_ontology.indexlayer.store import ObjectIndex                         # noqa: E402
from kingdee_ontology.pipeline.lineage import Origin                              # noqa: E402
from kingdee_ontology.pipeline.parse import FieldCountMismatch, rows_from_query   # noqa: E402
from kingdee_ontology.pipeline.run import Pipeline, PipelineError                 # noqa: E402
from kingdee_ontology.pipeline.standardize import Standardizer                    # noqa: E402


@pytest.fixture
def onto():
    load.cache_clear()
    return load(tenant="")


@pytest.fixture
def pipe(onto):
    return Pipeline(onto, tenant="test")


class TestParse:
    def test_positional_rows_get_names(self):
        assert rows_from_query([["1", "A"]], "FID,FBillNo") == [{"FID": "1", "FBillNo": "A"}]

    def test_field_count_mismatch_aborts_instead_of_guessing(self):
        """位数对不上必须中断。错位会让『供应商』变成『金额』且无人察觉——
        比缺一列危险得多。"""
        with pytest.raises(FieldCountMismatch) as e:
            rows_from_query([["1", "A"]], "FID,FBillNo,FDate")
        assert "错位" in str(e.value)

    def test_already_named_rows_pass_through(self):
        assert rows_from_query([{"FID": "1"}], "FID,FBillNo") == [{"FID": "1"}]

    def test_business_error_body_is_detected(self, pipe):
        with pytest.raises(PipelineError) as e:
            pipe.from_query("采购订单", {"kd_error": True, "message": "过滤参数为空"})
        assert "业务异常" in str(e.value)


class TestStandardize:
    def test_identifiers_never_become_numbers(self, onto):
        """FID='0012' 转成 12 会丢前导零，且过滤式再也拼不对。"""
        s = Standardizer(onto)
        r = s.row("采购订单", {"FID": "0012", "FBillNo": "0001"})
        assert r["FID"] == "0012" and r["FBillNo"] == "0001"
        assert isinstance(r["_id"], str)

    def test_amounts_and_dates_are_normalised(self, onto):
        s = Standardizer(onto)
        r = s.row("采购订单", {"FAllAmount": "12345.60", "FDate": "2026-09-02T00:00:00"})
        assert r["FAllAmount"] == 12345.6 and r["FDate"] == "2026-09-02"

    def test_state_letter_becomes_canonical_code(self, onto):
        s = Standardizer(onto)
        assert s.row("采购订单", {"FDocumentStatus": "C"})["_state"] == "C:已审核"

    def test_unresolvable_state_is_none_not_a_guess(self, onto):
        s = Standardizer(onto)
        assert s.row("采购订单", {"FBillNo": "X"})["_state"] is None

    def test_field_aliases_fold_to_canonical(self, onto):
        """FCUSTID / FCustId 是同一语义的两种写法（审计 F-1）。"""
        s = Standardizer(onto)
        r = s.row("销售订单", {"FCUSTID.FName": "某客户"})
        assert "FCustId.FName" in r and r["FCustId.FName"] == "某客户"

    def test_id_and_no_stay_separate(self, onto):
        """内码与编号不通用——写操作要内码，下推要编号。合并会掩盖这点。"""
        s = Standardizer(onto)
        r = s.row("采购订单", {"FID": "100", "FBillNo": "CGDD001"})
        assert r["_id"] == "100" and r["_no"] == "CGDD001"


class TestLineage:
    def test_alias_folding_is_recorded_as_derived(self, pipe):
        ds = pipe.from_fixture("销售订单", [{"FCUSTID.FName": "客户甲"}])
        l = ds.lineage.explain("FCustId.FName")
        assert l["origin"] == Origin.DERIVED.value and l["depends_on"] == ["FCUSTID.FName"]

    def test_requested_but_absent_columns_are_marked_missing(self, pipe):
        """『字段不存在』与『字段是空的』意义完全不同，必须区分。

        注意这只在**具名行**路径上成立：位置数组少了值就是位数不符，
        已被 FieldCountMismatch 拦在前面——那是错位风险，不是缺列。
        """
        ds = pipe.from_query("采购订单", [{"FID": "1", "FBillNo": "A"}],
                             field_keys="FID,FBillNo,FNotThere")
        assert "FNotThere" in ds.lineage.missing()
        assert "可能不存在" in ds.lineage.explain("FNotThere")["note"]
        assert ds.quality()["missing_columns"] == ["FNotThere"]

    def test_state_column_cites_the_registry(self, pipe):
        ds = pipe.from_fixture("采购订单", [{"FDocumentStatus": "C"}])
        assert ds.lineage.explain("_state")["origin"] == Origin.REGISTRY.value


class TestDataset:
    def test_provenance_records_how_it_was_fetched(self, pipe):
        ds = pipe.from_query("采购订单", [["1", "A"]], field_keys="FID,FBillNo",
                             filter_string="FDocumentStatus='C'", top=1)
        assert ds.provenance.source == "webapi:query"
        assert ds.provenance.filter_string == "FDocumentStatus='C'"
        assert ds.provenance.truncated is True, "取满 top 说明还有更多没取到"

    def test_quality_separates_unresolved_state_from_nulls(self, pipe):
        ds = pipe.from_fixture("采购订单", [{"FBillNo": "A"}, {"FDocumentStatus": "C"}])
        q = ds.quality()
        assert q["state_unresolved"] == 1 and q["rows"] == 2

    def test_by_state_groups(self, pipe):
        ds = pipe.from_fixture("采购订单",
                               [{"FDocumentStatus": "C"}, {"FDocumentStatus": "Z"}])
        assert ds.by_state() == {"C:已审核": 1, "Z:暂存": 1}


class TestIndex:
    @pytest.fixture
    def idx(self, tmp_path):
        return ObjectIndex(tmp_path / "o.db", tenant="t")

    def test_rejects_raw_dicts(self, idx):
        """绕过标准化的数据进了索引，字段名和状态码就又乱了。"""
        with pytest.raises(TypeError) as e:
            idx.upsert([{"FID": "1"}])
        assert "标准化" in str(e.value)

    def test_upsert_and_find_by_bill_no_across_types(self, idx, pipe):
        idx.upsert(pipe.from_fixture("采购订单",
                                     [{"FID": "1", "FBillNo": "CGDD001", "FDocumentStatus": "C"}]))
        idx.upsert(pipe.from_fixture("销售订单",
                                     [{"FID": "9", "FBillNo": "XSDD001", "FDocumentStatus": "Z"}]))
        hits = idx.find_by_no("CGDD001")
        assert len(hits) == 1 and hits[0]["noun"] == "PUR_PurchaseOrder"

    def test_rows_without_identity_are_skipped_not_invented(self, idx, pipe):
        r = idx.upsert(pipe.from_fixture("采购订单", [{"FDocumentStatus": "C"}]))
        assert r["indexed"] == 0 and r["skipped_no_id"] == 1

    def test_write_marks_objects_stale_and_search_admits_it(self, idx, pipe):
        idx.upsert(pipe.from_fixture("采购订单",
                                     [{"FID": "1", "FBillNo": "CGDD001", "FDocumentStatus": "C"}]))
        assert idx.mark_stale("PUR_PurchaseOrder", ["1"], "audit 执行后") == 1
        r = idx.search(noun="PUR_PurchaseOrder")
        assert r["stale_hits"] == 1
        assert "不是账套现状" in r["caveat"] and "回源" in r["caveat"]

    def test_coverage_reports_what_can_be_trusted(self, idx, pipe):
        idx.upsert(pipe.from_query("采购订单", [["1", "A"]], field_keys="FID,FBillNo", top=1))
        cov = idx.coverage()["by_noun"][0]
        assert cov["objects"] == 1 and cov["truncated"] is True

    def test_reupsert_clears_stale(self, idx, pipe):
        ds = pipe.from_fixture("采购订单", [{"FID": "1", "FBillNo": "A", "FDocumentStatus": "C"}])
        idx.upsert(ds); idx.mark_stale("PUR_PurchaseOrder", ["1"], "x")
        idx.upsert(ds)
        assert idx.search(noun="PUR_PurchaseOrder")["stale_hits"] == 0
