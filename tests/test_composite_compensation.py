"""复合工具中间态可清算性（审计 A-1 / P-1 / P-5）。

不测"审计器是否满意"，测**真实行为**：
中途失败后，遗留对象是否被结构化列出、是否落进过程审计、是否能被悬挂链检测查到。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "ontology"))

import kingdee_mcp.server as srv  # noqa: E402
from kingdee_ontology.operation_audit import AuditRecorder, dangling_traces, load  # noqa: E402

SAVE_OK = {"Result": {"ResponseStatus": {"IsSuccess": True},
                      "Id": "100231", "Number": "CGDD000231"}}
SUBMIT_FAIL = {"Result": {"ResponseStatus": {"IsSuccess": False, "Errors": [
    {"Message": "必录字段 FLot 未填写", "FieldName": "FLot"}]}}}
PUSH_OK = {"Result": {"ResponseStatus": {"IsSuccess": True},
                      "Numbers": ["RKD000318"], "Ids": ["200318"]}}


@pytest.fixture
def audit_log(tmp_path, monkeypatch):
    """把过程审计重定向到临时文件，并让 server 内部的懒导入拿到同一个记录器。"""
    path = tmp_path / "operation_audit.jsonl"
    monkeypatch.setenv("KINGDEE_OPERATION_AUDIT_LOG", str(path))
    import kingdee_ontology.operation_audit as oa
    monkeypatch.setattr(oa, "audit_recorder", AuditRecorder(path))
    return path


@pytest.fixture
def fake_post(monkeypatch):
    """按端点脚本化 _post_raw 的返回。"""
    def _install(script: dict):
        calls = []

        async def _fake(ep, form_id, model, **kw):
            calls.append((ep, form_id))
            r = script.get(ep)
            if isinstance(r, Exception):
                raise r
            return r
        monkeypatch.setattr(srv, "_post_raw", _fake)
        return calls
    return _install


class TestCreateAndAuditHalt:
    def test_left_objects_are_listed_structurally(self, fake_post, audit_log):
        """草稿已生成、submit 失败 —— 遗留对象必须被明确列出，而不是藏在一段文字里。"""
        fake_post({"save": SAVE_OK, "submit": SUBMIT_FAIL})
        out = json.loads(asyncio.run(srv.kingdee_create_and_audit(
            srv.CreateAndAuditInput(form_id="PUR_PurchaseOrder", model={"FDate": "2026-09-02"}))))

        assert out["halted_at"] == "submit"
        pc = out["pending_compensation"]
        assert pc["form_id"] == "PUR_PurchaseOrder"
        assert "100231" in pc["left_objects"] and "CGDD000231" in pc["left_objects"]
        assert pc["suggested_actions"] == ["kingdee_delete_bills"]
        assert "不会自动回滚" in pc["note"]

    def test_halt_is_written_to_operation_audit(self, fake_post, audit_log):
        """中间态必须落审计——否则它只存在于这一次的返回体里，事后无从查起。

        关键：**成功的前序步骤也要记**。"遗留了什么"来自成功的 Save，
        只记失败那一步的话，悬挂链检测根本判不出来。
        """
        fake_post({"save": SAVE_OK, "submit": SUBMIT_FAIL})
        asyncio.run(srv.kingdee_create_and_audit(
            srv.CreateAndAuditInput(form_id="PUR_PurchaseOrder", model={})))

        recs = load(audit_log)
        assert len(recs) == 2, "Save 成功 + Submit 失败，两步都要留痕"
        assert [r["verb"] for r in recs] == ["Save", "Submit"]
        assert [r["outcome"] for r in recs] == ["success", "failed"]
        assert recs[0]["state_to"] == "Z:暂存" and recs[1]["state_to"] is None
        assert all(r["noun"] == "PUR_PurchaseOrder" for r in recs)
        assert len({r["trace_id"] for r in recs}) == 1, "同一次操作必须共享 trace_id"
        assert [r["step"] for r in recs] == [1, 2]

    def test_dangling_detection_finds_it(self, fake_post, audit_log):
        """落了审计还不够——必须能被悬挂链检测查出来，才算真的可清算。"""
        fake_post({"save": SAVE_OK, "submit": SUBMIT_FAIL})
        asyncio.run(srv.kingdee_create_and_audit(
            srv.CreateAndAuditInput(form_id="PUR_PurchaseOrder", model={})))

        d = dangling_traces(load(audit_log))
        assert len(d) == 1
        assert any("100231" in o or "CGDD000231" in o for o in d[0]["left_objects"])

    def test_success_path_leaves_no_pending_compensation(self, fake_post, audit_log):
        ok = {"Result": {"ResponseStatus": {"IsSuccess": True}, "Id": "1", "Number": "N1"}}
        fake_post({"save": ok, "submit": ok, "audit": ok})
        out = json.loads(asyncio.run(srv.kingdee_create_and_audit(
            srv.CreateAndAuditInput(form_id="PUR_PurchaseOrder", model={}))))
        assert out["success"] is True
        assert "pending_compensation" not in out
        assert load(audit_log) == [], "成功路径不该产生悬挂记录"

    def test_save_failure_needs_no_compensation(self, fake_post, audit_log):
        """第一步就失败，什么都没生成 —— 不该谎报有遗留对象。"""
        fake_post({"save": {"Result": {"ResponseStatus": {
            "IsSuccess": False, "Errors": [{"Message": "客户不能为空"}]}}}})
        out = json.loads(asyncio.run(srv.kingdee_create_and_audit(
            srv.CreateAndAuditInput(form_id="SAL_SaleOrder", model={}))))
        assert out["halted_at"] == "save"
        assert out["pending_compensation"]["left_objects"] == []
        assert "无需补偿" in out["pending_compensation"]["note"]


class TestPushAndAuditHalt:
    def test_target_drafts_are_reported(self, fake_post, audit_log):
        fake_post({"push": PUSH_OK, "submit": SUBMIT_FAIL})
        out = json.loads(asyncio.run(srv.kingdee_push_and_audit(
            srv.PushAndAuditInput(form_id="PUR_PurchaseOrder", target_form_id="STK_InStock",
                                  source_bill_nos=["CGDD000231"]))))
        assert out["halted_at"] == "submit"
        assert "RKD000318" in out["pending_compensation"]["left_objects"]

    def test_contract_declares_non_atomicity(self, fake_post, audit_log):
        """修 A-2：复合动词的契约取最弱一环，必须如实声明不可回滚。"""
        fake_post({"push": PUSH_OK, "submit": SUBMIT_FAIL})
        out = json.loads(asyncio.run(srv.kingdee_push_and_audit(
            srv.PushAndAuditInput(form_id="PUR_PurchaseOrder", target_form_id="STK_InStock",
                                  source_bill_nos=["CGDD000231"]))))
        c = out["contract"]
        assert c["atomicity"] == "none" and c["destructive"] is True
        assert "不会回滚" in c["atomicity_note"]


class TestBatchContract:
    def test_per_item_tools_expose_contract(self, fake_post):
        fake_post({"submit": SUBMIT_FAIL})
        out = json.loads(asyncio.run(srv.kingdee_submit_bills(
            srv.BillIdsInput(form_id="PUR_PurchaseOrder", bill_ids=["1", "2"]))))
        assert out["contract"]["atomicity"] == "per_item"
        assert "不会回滚" in out["contract"]["atomicity_note"]

    def test_execute_family_exposes_contract_and_batch_note(self, fake_post, monkeypatch):
        fake_post({"execute": {"Result": {"ResponseStatus": {"IsSuccess": True}}}})
        out = json.loads(asyncio.run(srv.kingdee_void_bills(
            srv.ExecuteActionInput(form_id="SAL_SaleOrder", bill_ids=["1", "2"],
                                   operation="Cancel"))))
        assert out["contract"]["atomicity"] == "server_defined"
        assert out["contract"]["destructive"] is True     # 作废无逆动词
        assert "无法判定" in out["batch_note"]

    def test_close_is_not_destructive_because_it_has_an_inverse(self, fake_post):
        fake_post({"execute": {"Result": {"ResponseStatus": {"IsSuccess": True}}}})
        out = json.loads(asyncio.run(srv.kingdee_close_bill(
            srv.ExecuteActionInput(form_id="SAL_SaleOrder", bill_ids=["1"],
                                   operation="BillClose"))))
        assert out["contract"]["inverse"] == "unclose"
        assert out["contract"]["destructive"] is False


class TestLifecycleStates:
    @pytest.mark.parametrize("op,to_state", [
        ("void", "已作废"), ("close", "已关闭"), ("unclose", "已审核"),
        ("forbid", "已禁用"), ("enable", "已启用"), ("cancel_assign", "创建"),
    ])
    def test_execute_verbs_have_state_definitions(self, op, to_state):
        """修 S-3/AT-04：此前这些动词在 DOC_LIFECYCLE 中缺席，
        _result_status 一律返回 next_action=None，等同于宣告"流程已完成"。"""
        assert op in srv.DOC_LIFECYCLE
        assert srv.DOC_LIFECYCLE[op]["to"] == to_state


class TestSavePreprocessingParity:
    """修 A-6：kingdee_save_bill 与复合工具的 Save 必须走同一套预处理。"""

    def test_both_paths_apply_field_autofix_and_ordering(self, fake_post, audit_log,
                                                         monkeypatch):
        seen: list[dict] = []

        class _Validator:
            def validate_and_fix(self, model):
                fixed = dict(model)
                if "FSalesOrgId" in fixed:                    # 故意写错的字段名
                    fixed["FSaleOrgId"] = fixed.pop("FSalesOrgId")
                return fixed, [{"from": "FSalesOrgId", "to": "FSaleOrgId"}]

        async def _fake_validator(form_id, force=False):
            return _Validator()
        monkeypatch.setattr(srv, "_get_metadata_validator", _fake_validator)

        async def _fake_post(ep, form_id, model, **kw):
            if ep == "save":
                seen.append(dict(model))
                return SAVE_OK
            return {"Result": {"ResponseStatus": {"IsSuccess": True}}}
        monkeypatch.setattr(srv, "_post_raw", _fake_post)

        # 分录键在前、财务键在后：预处理应把非 ENTRY 键调到 ENTRY 之前
        raw = {"FQUOTATIONENTRY": [{"FQty": 1}], "FSalesOrgId": {"FNumber": "100"},
               "FQUOTATIONFIN": {"FSettleCurrId": {"FNumber": "PRE001"}}}

        asyncio.run(srv.kingdee_save_bill(
            srv.SaveInput(form_id="SAL_Quotation", model=dict(raw))))
        asyncio.run(srv.kingdee_create_and_audit(
            srv.CreateAndAuditInput(form_id="SAL_Quotation", model=dict(raw))))

        assert len(seen) == 2
        for m in seen:
            assert "FSaleOrgId" in m and "FSalesOrgId" not in m, "字段自愈未生效"
            keys = list(m.keys())
            assert keys.index("FQUOTATIONFIN") < keys.index("FQUOTATIONENTRY"), \
                "字段顺序防御未生效（FIN 必须排在 ENTRY 之前）"
        assert seen[0] == seen[1], "两条路径必须产出完全相同的 model"
