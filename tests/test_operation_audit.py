"""operation_audit 自测：验证 trace 串联、悬挂链检测、写失败不静默。"""
import json

from kingdee_ontology.operation_audit import AuditRecorder, dangling_traces, load  # noqa: E402


def test_trace_links_composite_steps(tmp_path):
    rec = AuditRecorder(tmp_path / "a.jsonl")
    with rec.operation("kingdee_create_and_audit", actor="demo") as op:
        op.step(verb="Save", noun="PUR_PurchaseOrder", endpoint="save",
                object_id="100001", object_no="CGDD000001",
                state_from=None, state_to="Z:暂存")
        op.step(verb="Submit", noun="PUR_PurchaseOrder", endpoint="submit",
                object_id="100001", state_from="Z:暂存", state_to="B:审核中")
        op.step(verb="Audit", noun="PUR_PurchaseOrder", endpoint="audit",
                object_id="100001", state_from="B:审核中", state_to="C:已审核")
    rows = load(tmp_path / "a.jsonl")
    assert len(rows) == 3
    assert len({r["trace_id"] for r in rows}) == 1, "复合动词的各步必须共享 trace_id"
    assert [r["step"] for r in rows] == [1, 2, 3]
    assert op.halted_at is None
    assert dangling_traces(rows) == [], "走到终态的链不应被判为悬挂"


def test_dangling_trace_detected(tmp_path):
    rec = AuditRecorder(tmp_path / "b.jsonl")
    with rec.operation("kingdee_create_and_audit", actor="demo") as op:
        op.step(verb="Save", noun="PUR_PurchaseOrder", endpoint="save",
                object_id="100002", object_no="CGDD000002", state_to="Z:暂存")
        op.step(verb="Submit", noun="PUR_PurchaseOrder", endpoint="submit",
                object_id="100002", state_from="Z:暂存", state_to=None,
                outcome="failed", error={"code": "KD001", "message": "必录字段未填"})
    d = dangling_traces(load(tmp_path / "b.jsonl"))
    assert len(d) == 1
    assert d[0]["halted_at_step"] == 2
    assert d[0]["left_objects"] == ["PUR_PurchaseOrder:CGDD000002"]
    assert op.halted_at == 2


def test_exception_leaves_unknown_record(tmp_path):
    rec = AuditRecorder(tmp_path / "c.jsonl")
    try:
        with rec.operation("kingdee_push_bill", actor="demo") as op:
            op.step(verb="Push", noun="PUR_PurchaseOrder", endpoint="push",
                    object_no="CGDD000003", state_to=None, outcome="unknown")
            raise TimeoutError("read timeout")
    except TimeoutError:
        pass
    rows = load(tmp_path / "c.jsonl")
    assert rows[-1]["outcome"] == "unknown", "异常逃逸必须留痕，因为服务端可能已生效"
    assert rows[-1]["error"]["code"] == "TimeoutError"


def test_outcome_domain_is_closed(tmp_path):
    rec = AuditRecorder(tmp_path / "d.jsonl")
    with rec.operation("x", actor="demo") as op:
        try:
            op.step(verb="Save", noun="N", outcome="ok")  # 非法取值
        except ValueError as e:
            assert "outcome" in str(e)
        else:
            raise AssertionError("非法 outcome 必须被拒绝")


if __name__ == "__main__":
    import tempfile
    for fn in (test_trace_links_composite_steps, test_dangling_trace_detected,
               test_exception_leaves_unknown_record, test_outcome_domain_is_closed):
        with tempfile.TemporaryDirectory() as d:
            fn(Path(d))
        print(f"  ok  {fn.__name__}")
    print("全部通过")
