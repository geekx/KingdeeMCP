"""约束层覆盖率断言 —— 让"漏覆盖"变成 CI 失败而不是静默失效。

审计发现 R-1：harness 规则用硬编码工具名匹配，24 个写动词只认 3 个（覆盖率 25%），
而且新增写工具时规则不会报错，只是悄悄不覆盖它。这组测试把这个缺口钉住。
"""
import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kingdee_ontology.harness.rules import HARNESS_RULES, OpNode, validate_operation_chain  # noqa: E402
from kingdee_ontology.harness.tools import (  # noqa: E402
    NEXT_ACTION_VOCAB, WRITE_TOOL_VERBS, parse_next_action,
)

SERVER = ROOT / "src" / "kingdee_mcp" / "server.py"


def _write_tools_in_server() -> set[str]:
    """从 server.py 抽出所有 readOnlyHint=False 的工具名。"""
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "tool"):
                continue
            kw = {k.arg: k.value for k in dec.keywords if k.arg}
            name = (kw["name"].value if isinstance(kw.get("name"), ast.Constant)
                    else node.name)
            if isinstance(kw.get("annotations"), ast.Dict):
                for k, v in zip(kw["annotations"].keys, kw["annotations"].values):
                    if (isinstance(k, ast.Constant) and k.value == "readOnlyHint"
                            and isinstance(v, ast.Constant) and v.value is False):
                        names.add(name)
    return names


class TestCoverage:
    def test_every_write_tool_is_registered(self):
        """新增写工具必须登记进 harness/tools.py，否则不受任何操作链约束。"""
        missing = _write_tools_in_server() - set(WRITE_TOOL_VERBS)
        assert not missing, (
            f"以下写工具未登记到 harness/tools.py:WRITE_TOOL_VERBS：{sorted(missing)}。"
            f"未登记 = 不受 RULE-001/002/003/005 约束（审计 R-1）。")

    def test_no_stale_registrations(self):
        stale = set(WRITE_TOOL_VERBS) - _write_tools_in_server()
        assert not stale, f"登记表里有 server.py 中已不存在的工具：{sorted(stale)}"

    def test_coverage_is_total(self):
        """覆盖率必须是 100%。不写死数字——新增写工具时应由上面两条断言指出缺口，
        而不是因为一个魔法数字对不上而失败。"""
        assert set(WRITE_TOOL_VERBS) == _write_tools_in_server()
        assert len(WRITE_TOOL_VERBS) >= 24


class TestNextActionVocabulary:
    def test_server_emits_only_valid_next_action(self):
        """修 R-2：next_action 必须是动词，不能是工具名。"""
        src = SERVER.read_text(encoding="utf-8")
        emitted = set(re.findall(r'"next_action"\]\s*=\s*"([^"]+)"', src))
        emitted |= set(re.findall(r'"next_action":\s*"([^"]+)"', src))
        bad = {v for v in emitted if not parse_next_action(v)}
        assert not bad, (
            f"server.py 发出了不在词表内的 next_action：{sorted(bad)}。"
            f"合法取值：{sorted(NEXT_ACTION_VOCAB)}。"
            f"塞工具名会让 RULE-001 解析失败并恒定误报。")

    @pytest.mark.parametrize("value,expected", [
        ("submit", ("submit",)),
        ("submit+audit", ("submit", "audit")),
        ("kingdee_submit_bills + kingdee_audit_bills", ()),   # 越界
        ("kingdee_audit_production_orders", ()),              # 越界
        (None, ()),
    ])
    def test_parse(self, value, expected):
        assert parse_next_action(value) == expected


def _node(tool, params=None, result=None, ts=0.0):
    return OpNode(tool, params or {}, result or {"success": True}, ts)


class TestRuleContract:
    def test_check_returns_passed_not_violated(self):
        """修 R-4：check 返回 True 表示通过。文档曾与实现相反。"""
        for rule in HARNESS_RULES:
            passed, msg = rule.check([])
            assert passed is True and msg == "", f"{rule.id} 对空链路应判通过"


class TestRule001:
    def test_covers_previously_missed_tools(self):
        """save_production_order 等 18 个工具此前完全不受约束。"""
        chain = [_node("kingdee_save_production_order",
                       result={"success": True, "next_action": "submit", "fid": "500"}, ts=1)]
        v = validate_operation_chain(chain)
        assert any(x["rule_id"] == "RULE-001" for x in v)

    def test_out_of_vocab_next_action_is_reported_as_such(self):
        chain = [_node("kingdee_push_production_pick",
                       result={"success": True, "fid": "1",
                               "next_action": "kingdee_submit_bills + kingdee_audit_bills"}, ts=1)]
        msg = next(x["message"] for x in validate_operation_chain(chain)
                   if x["rule_id"] == "RULE-001")
        assert "词表越界" in msg

    def test_untrackable_node_does_not_abort_the_rule(self):
        """修 R-3：原实现用 return 提前退出，其后节点全部漏检。"""
        chain = [
            _node("kingdee_save_bill", result={"success": True, "next_action": "submit"}, ts=1),
            _node("kingdee_save_bill",
                  result={"success": True, "next_action": "submit", "fid": "777"}, ts=2),
        ]
        v = validate_operation_chain(chain)
        assert any("777" in x["message"] for x in v), "第一个无 fid 的节点不应让规则提前退出"

    def test_followup_on_other_bill_is_not_silently_accepted(self):
        """修 R-5：对无关单据的 submit 不再被算作后续动作。

        但它也不判为 RULE-001 错误——因为无法区分"操作了别的单"与
        "同一张单换了标识体系"（push 给 FBillNo，submit 要 FID）。
        如实降级为 RULE-006 警告：这条链不可审计。
        """
        chain = [
            _node("kingdee_save_bill",
                  result={"success": True, "next_action": "submit", "fid": "100"}, ts=1),
            _node("kingdee_submit_bills", params={"bill_ids": ["999"]},
                  result={"success": True}, ts=2),
        ]
        v = validate_operation_chain(chain)
        assert any(x["rule_id"] == "RULE-006" for x in v)
        assert not [x for x in v if x["rule_id"] == "RULE-001"]

    def test_missing_followup_entirely_is_an_error(self):
        """完全没有后续动作时，仍然是 RULE-001 错误。"""
        chain = [_node("kingdee_save_bill",
                       result={"success": True, "next_action": "submit", "fid": "100"}, ts=1)]
        assert any(x["rule_id"] == "RULE-001" for x in validate_operation_chain(chain))

    def test_passes_when_followup_matches(self):
        chain = [
            _node("kingdee_save_bill",
                  result={"success": True, "next_action": "submit", "fid": "100"}, ts=1),
            _node("kingdee_submit_bills", params={"bill_ids": ["100"]},
                  result={"success": True}, ts=2),
        ]
        assert not [x for x in validate_operation_chain(chain) if x["rule_id"] == "RULE-001"]


class TestRule002:
    def test_covers_all_push_variants(self):
        for tool in ("kingdee_push_stock_transfer", "kingdee_push_production_pick",
                     "kingdee_push_production_stock_in"):
            chain = [_node(tool, result={"success": True, "target_bill_nos": ["T1"]}, ts=1)]
            v = validate_operation_chain(chain)
            assert any(x["rule_id"] == "RULE-002" for x in v), f"{tool} 未被 RULE-002 覆盖"

    def test_unbindable_followup_downgrades_to_warning(self):
        """push 返回 FBillNo、submit/audit 用 FID —— 真实链路的常态。

        旧实现"出现过就算通过"是漏报；简单收紧成错误则是误报。
        正确答案是 RULE-006 警告：无法证实 ≠ 没发生，该修的是身份解析缺失。
        """
        chain = [
            _node("kingdee_push_bill",
                  result={"success": True, "target_bill_nos": ["CGRKD001"]}, ts=1),
            _node("kingdee_submit_bills", params={"bill_ids": ["300001"]},
                  result={"success": True}, ts=2),
            _node("kingdee_audit_bills", params={"bill_ids": ["300001"]},
                  result={"success": True}, ts=3),
        ]
        v = validate_operation_chain(chain)
        assert not [x for x in v if x["severity"] == "error"]
        assert any(x["rule_id"] == "RULE-006" for x in v)

    def test_push_with_no_followup_at_all_is_an_error(self):
        chain = [_node("kingdee_push_bill",
                       result={"success": True, "target_bill_nos": ["T1"]}, ts=1)]
        assert any(x["rule_id"] == "RULE-002" for x in validate_operation_chain(chain))

    def test_bindable_followup_passes_cleanly(self):
        """push 同时返回 target_fids 时，绑定成立，连警告都不该有。"""
        chain = [
            _node("kingdee_push_bill",
                  result={"success": True, "target_bill_nos": ["CGRKD001"],
                          "target_fids": ["300001"]}, ts=1),
            _node("kingdee_submit_bills", params={"bill_ids": ["300001"]},
                  result={"success": True}, ts=2),
            _node("kingdee_audit_bills", params={"bill_ids": ["300001"]},
                  result={"success": True}, ts=3),
        ]
        assert validate_operation_chain(chain) == []


class TestRule003:
    def test_diagnostic_alone_is_not_recovery(self):
        chain = [
            _node("kingdee_save_bill", result={"success": False, "errors": [{"m": "x"}]}, ts=1),
            _node("kingdee_view_bill", params={"id": "1"}, result={"success": True}, ts=2),
        ]
        assert any(x["rule_id"] == "RULE-003" for x in validate_operation_chain(chain))

    def test_identical_retry_is_not_recovery(self):
        p = {"form_id": "X", "model": {}}
        chain = [
            _node("kingdee_save_bill", params=p, result={"success": False, "errors": [1]}, ts=1),
            _node("kingdee_save_bill", params=p, result={"success": False, "errors": [1]}, ts=2),
        ]
        assert any(x["rule_id"] == "RULE-003" for x in validate_operation_chain(chain))

    def test_changed_params_counts_as_recovery(self):
        chain = [
            _node("kingdee_save_bill", params={"model": {"a": 1}},
                  result={"success": False, "errors": [1]}, ts=1),
            _node("kingdee_save_bill", params={"model": {"a": 2}},
                  result={"success": True, "fid": "1"}, ts=2),
        ]
        assert not [x for x in validate_operation_chain(chain) if x["rule_id"] == "RULE-003"]


class TestRule005:
    def test_composite_halt_without_compensation_flagged(self):
        """对症 A-1：复合操作停在中途，留下无人认领的草稿。"""
        chain = [_node("kingdee_create_and_audit",
                       result={"success": False, "halted_at": "submit", "fid": "100"}, ts=1)]
        v = validate_operation_chain(chain)
        msg = next(x["message"] for x in v if x["rule_id"] == "RULE-005")
        assert "无人认领的中间态" in msg and "100" in msg

    def test_compensation_satisfies_the_rule(self):
        chain = [
            _node("kingdee_create_and_audit",
                  result={"success": False, "halted_at": "submit", "fid": "100"}, ts=1),
            _node("kingdee_delete_bills", params={"bill_ids": ["100"]},
                  result={"success": True}, ts=2),
        ]
        assert not [x for x in validate_operation_chain(chain) if x["rule_id"] == "RULE-005"]

    def test_continuing_the_lifecycle_also_satisfies(self):
        chain = [
            _node("kingdee_create_and_audit",
                  result={"success": False, "halted_at": "audit", "fid": "100"}, ts=1),
            _node("kingdee_audit_bills", params={"bill_ids": ["100"]},
                  result={"success": True}, ts=2),
        ]
        assert not [x for x in validate_operation_chain(chain) if x["rule_id"] == "RULE-005"]

    def test_nothing_produced_needs_no_compensation(self):
        chain = [_node("kingdee_create_and_audit",
                       result={"success": False, "halted_at": "save"}, ts=1)]
        assert not [x for x in validate_operation_chain(chain) if x["rule_id"] == "RULE-005"]
