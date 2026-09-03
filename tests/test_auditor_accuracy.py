"""审计器自己得说真话。

一条**与代码相反**的审计发现，比不报还糟：它教人忽略审计输出。这已经发生过两次——

  F-1  抽取器只读函数体里的默认值，漏了 Pydantic 模型级默认值，
       于是报出 14 条并不存在的字段不一致（记在 04-audit-trail.md）。
  AT-07 无条件发出，文案写死「5 个 push 工具尚未接入，仍无法校验某条下推是否
       合法」。可它们早就接上了：_post_raw 的 push 分支是所有下推的唯一咽喉点，
       在那里查表、并在严格模式下阻断。

两次同一个病根：**断言了一件没去查的事**。所以这组测试不看措辞，只验一件事——
把代码里的接线拆掉，审计器的结论必须跟着变。结论不随代码变，就说明它在背书。
"""
import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "ontology"))

import audit_atomicity as aa  # noqa: E402
from extract_ontology import _load_tree, build  # noqa: E402


@pytest.fixture(scope="module")
def findings():
    return aa.audit(build(_load_tree()))


def _at07(fs):
    hits = [f for f in fs if f["id"] == "AT-07"]
    assert hits, "AT-07 没有发出任何结论"
    return hits


class TestAT07TellsTheTruth:
    def test_reports_the_chokepoint_as_wired(self, findings):
        """接线是真的：_post_raw 的 push 分支确实调了 _check_push_link。"""
        src = aa.SERVER_PY.read_text(encoding="utf-8")
        fn = next(f for f in ast.walk(ast.parse(src))
                  if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and f.name == "_post_raw")
        called = {getattr(n.func, "id", getattr(n.func, "attr", None))
                  for n in ast.walk(fn) if isinstance(n, ast.Call)}
        assert "_check_push_link" in called, "咽喉点没查表，那 AT-07 就该报 warning"
        assert all(f["level"] == "info" for f in _at07(findings)), \
            "接线是通的，AT-07 不该报 warning"

    def test_does_not_claim_validation_is_impossible(self, findings):
        """不许再说『无法校验某条下推是否合法』——那与代码相反。"""
        for f in _at07(findings):
            blob = f["title"] + f["detail"]
            assert "无法校验" not in blob, f"AT-07 又在说一件与代码相反的事：{blob[:120]}"
            assert "尚未接入" not in blob, f"AT-07 断言了没去查的事：{blob[:120]}"

    def test_says_default_is_advisory_not_blocking(self, findings):
        """真正剩下的事实是『默认只提示』，这个得说出来。"""
        blob = "".join(f["title"] + f["detail"] for f in _at07(findings))
        assert "KINGDEE_STRICT_LINKS" in blob, "没说清怎样才会真的阻断"

    def test_verdict_follows_the_code(self, tmp_path, monkeypatch):
        """把咽喉点的查表拆掉，AT-07 必须升级为 warning。

        这才是这组测试的重点：结论要随代码变。一个无论代码怎样都输出同一句话的
        检查项，不是审计，是背书。
        """
        src = aa.SERVER_PY.read_text(encoding="utf-8")
        broken = src.replace(
            '_chk = _check_push_link(form_id, str(data_obj.get("TargetFormId", "")))',
            '_chk = {"status": "skipped"}', 1)
        assert broken != src, "没找到咽喉点那行——测试的前提变了，先确认代码结构"

        fake = tmp_path / "server.py"
        fake.write_text(broken, encoding="utf-8")
        monkeypatch.setattr(aa, "SERVER_PY", fake)

        out = aa._audit_link_wiring(["A→B"])
        assert out and out[0]["level"] == "warning", \
            "拆掉咽喉点的查表后 AT-07 仍报 info —— 说明它根本没在看代码"
        assert "没有查表" in out[0]["detail"] or "未在咽喉点" in out[0]["title"]

    def test_verdict_follows_the_strict_switch(self, tmp_path, monkeypatch):
        """连开关都没有时，也不能报成『已接入』。"""
        src = aa.SERVER_PY.read_text(encoding="utf-8").replace("_STRICT_LINKS", "_X_OFF")
        fake = tmp_path / "server.py"
        fake.write_text(src, encoding="utf-8")
        monkeypatch.setattr(aa, "SERVER_PY", fake)
        out = aa._audit_link_wiring(["A→B"])
        assert out and out[0]["level"] == "warning"


class TestAuditStaysClean:
    def test_no_error_level_findings(self, findings):
        """CI 的硬门禁：不允许 error 级发现。"""
        errs = [f for f in findings if f["level"] == "error"]
        assert not errs, errs

    def test_every_finding_cites_evidence(self, findings):
        """每条发现都要指到具体位置——指不出来的，多半是凭印象写的。"""
        bad = [f["id"] for f in findings if not f.get("evidence")]
        assert not bad, f"这些发现没有证据出处：{bad}"
