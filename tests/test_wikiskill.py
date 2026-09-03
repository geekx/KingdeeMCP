"""WikiSkill 自优化层：噪声抑制、证据累积、置信度上升、否决后不复活。"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "ontology"))

from kingdee_ontology.wikiskill.knowledge import Entry, Knowledge  # noqa: E402
from kingdee_ontology.wikiskill.retro import derive, retro         # noqa: E402


def _rec(day, trace, step, verb, outcome, noun="STK_InStock",
         msg="", field="", ms=100.0, object_no="RKD001"):
    return {"trace_id": trace, "step": step, "occurred_at": f"{day}T10:00:00.000+00:00",
            "actor": "u", "on_behalf_of": None, "tool": "kd_run:采购收货入库",
            "verb": verb, "endpoint": verb.lower(), "noun": noun,
            "object_id": None, "object_no": object_no,
            "state_from": None, "state_to": None, "outcome": outcome,
            "duration_ms": ms, "request_digest": "",
            "error": {"message": msg, "field": field} if msg else None,
            "compensated_by": None, "extra": {}}


def _write(path, recs):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
                    encoding="utf-8")


class TestDerivation:
    def test_single_failure_is_noise(self):
        """出现一次的失败不成条目——否则知识库会被偶发噪声淹没。"""
        assert derive([_rec("2026-09-02", "t1", 1, "Submit", "failed", msg="批号不能为空")]) == []

    def test_repeated_failure_becomes_entry(self):
        recs = [_rec("2026-09-02", f"t{i}", 1, "Submit", "failed",
                     msg="批号不能为空", field="FLot") for i in range(3)]
        es = derive(recs)
        fp = [e for e in es if e.kind == "failure_pattern"]
        assert len(fp) == 1
        assert "FLot" in fp[0].suggestion, "建议必须具体到哪个字段"

    def test_error_messages_are_normalised(self):
        """含单据号/数字的同类错误必须归到同一条知识上。"""
        recs = [_rec("2026-09-02", "t1", 1, "Submit", "failed", msg="单据 CGDD000231 已审核"),
                _rec("2026-09-02", "t2", 1, "Submit", "failed", msg="单据 CGDD000999 已审核")]
        fp = [e for e in derive(recs) if e.kind == "failure_pattern"]
        assert len(fp) == 1 and fp[0].occurrences == 2

    def test_unknown_outcome_warns_about_duplicate_creation(self):
        recs = [_rec("2026-09-02", f"t{i}", 1, "Save", "unknown") for i in range(2)]
        e = [x for x in derive(recs) if x.kind == "flaky"][0]
        assert "查证" in e.suggestion and "幂等键" in e.suggestion

    def test_blocked_push_suggests_profile_edit(self):
        recs = [_rec("2026-09-02", f"t{i}", 1, "Push", "failed",
                     msg="未登记的下推关系 SAL_SaleOrder → PRD_MO。已登记：[]")
                for i in range(2)]
        e = [x for x in derive(recs) if x.kind == "unlinked_push"][0]
        assert "profile.yml" in e.suggestion

    def test_dangling_entry_names_the_left_objects(self):
        recs = [_rec("2026-09-02", "t1", 1, "Push", "success"),
                _rec("2026-09-02", "t1", 2, "Submit", "failed", msg="批号不能为空")]
        e = [x for x in derive(recs) if x.kind == "dangling"][0]
        assert "STK_InStock:RKD001" in e.detail


class TestAccumulation:
    def test_confidence_rises_across_days(self, tmp_path):
        audit, store = tmp_path / "a.jsonl", tmp_path / "k.json"
        day1 = [_rec("2026-09-02", f"t{i}", 1, "Submit", "failed",
                     msg="批号不能为空", field="FLot") for i in range(3)]
        _write(audit, day1)
        r1 = retro(str(audit), str(store), ["2026-09-02"])
        assert r1["actionable"] == [], "单日出现不应立即变成可执行项"

        day2 = [_rec("2026-09-03", f"u{i}", 1, "Submit", "failed",
                     msg="批号不能为空", field="FLot") for i in range(3)]
        _write(audit, day1 + day2)
        r2 = retro(str(audit), str(store), ["2026-09-03"])
        act = [a for a in r2["actionable"] if "批号" in a["title"]]
        assert len(act) == 1
        assert act[0]["confidence"] == "medium" and act[0]["occurrences"] == 6
        assert act[0]["days"] == 2

    def test_id_is_stable_across_runs(self, tmp_path):
        audit, store = tmp_path / "a.jsonl", tmp_path / "k.json"
        recs = [_rec("2026-09-02", f"t{i}", 1, "Submit", "failed",
                     msg="批号不能为空") for i in range(3)]
        _write(audit, recs)
        retro(str(audit), str(store), ["2026-09-02"])
        ids1 = set(json.loads(store.read_text(encoding="utf-8"))["entries"][0]["id"])
        retro(str(audit), str(store), ["2026-09-02"])
        data = json.loads(store.read_text(encoding="utf-8"))
        assert set(data["entries"][0]["id"]) == ids1
        assert len(data["entries"]) <= 2, "同一现象不得每次回溯都新建条目"

    def test_rejected_entry_does_not_resurface(self, tmp_path):
        store = tmp_path / "k.json"
        k = Knowledge(store)
        e = Entry(id="abc", kind="failure_pattern", title="t", detail="d",
                  suggestion="s", occurrences=5)
        k.merge(e, day="2026-09-02")
        k.set_status("abc", "rejected", note="业务上就是这样，不改")
        k.save()

        k2 = Knowledge(store)
        _, action = k2.merge(Entry(id="abc", kind="failure_pattern", title="t",
                                   detail="d", suggestion="s", occurrences=5),
                             day="2026-09-03")
        assert action == "skipped"
        assert k2.entries["abc"].occurrences == 10, "计数仍累积，只是不再刷屏"
        assert k2.actionable() == []

    def test_evidence_is_capped(self, tmp_path):
        k = Knowledge(tmp_path / "k.json")
        for d in ("2026-09-02", "2026-09-03", "2026-09-04"):
            k.merge(Entry(id="x", kind="flaky", title="t", detail="d", suggestion="s",
                          occurrences=1, evidence=[{"i": i} for i in range(5)]), day=d)
        assert len(k.entries["x"].evidence) == 5, "证据不得无限膨胀"
        assert len(k.entries["x"].days) == 3
