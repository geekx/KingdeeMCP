"""底座 kd_query 与 legacy 专用查询工具的等价性（长尾收敛）。

40 个 legacy 专用查询工具各自硬编码一个 form_id 与一套默认字段集，
每个都要占一份 inputSchema。把它们登记进 base/registry.yml 之后，
全部由 kd_query 一个工具承载 —— 新增名词不再增加 token。

这组测试守住三件事：
  1. 新登记的名词，其 default_fields 与对应 legacy 工具**逐字一致**；
  2. 已知的 13 处分歧被锁定，不允许再增加（审计 F-1）；
  3. kd_query 在字段留空时确实使用注册表的默认字段集。
"""
import ast
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kingdee_ontology.base.dispatch import Dispatcher     # noqa: E402
from kingdee_ontology.base.ontology import load           # noqa: E402
from kingdee_ontology.base.transport import FakeTransport  # noqa: E402

SERVER = ROOT / "src" / "kingdee_mcp" / "server.py"

# 已知的两套事实来源分歧（审计 F-1）。静态分析无法判定哪边正确，
# 需在真实账套验证；此处锁定集合，防止无声增加。
KNOWN_DIVERGENT = {
    "BD_Material", "PRD_Instock", "PRD_MO", "PRD_PickMtrl",
    "PUR_PurchaseOrder", "PUR_Requisition",
    "QIS_InspectBill", "SAL_Quotation", "SAL_SaleOrder", "STK_Inventory",
    "STK_TransferApply", "STK_TransferDirect", "SVM_InquiryBill", "SVM_QuoteBill",
}


def _legacy_query_defaults() -> dict[str, dict]:
    """从 server.py 抽出「form_id → (工具名, 默认字段集)」。

    默认字段集有两处来源：函数体内的 IfExp 兜底值，或 Input 模型的
    field_keys 默认值。只取其一会漏 —— 早期版本的抽取只看函数体，
    把模型级默认误报成"不一致"，这里两处都读。
    """
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    model_defaults: dict[str, str] = {}
    for n in tree.body:
        if not isinstance(n, ast.ClassDef):
            continue
        for st in n.body:
            tgt = getattr(st, "target", None)
            if isinstance(st, ast.AnnAssign) and isinstance(tgt, ast.Name) \
                    and tgt.id == "field_keys":
                for kw in getattr(st.value, "keywords", []):
                    if kw.arg == "default" and isinstance(kw.value, ast.Constant):
                        model_defaults[n.name] = kw.value.value

    def body_default(node):
        for x in ast.walk(node):
            if isinstance(x, ast.IfExp):
                try:
                    v = ast.literal_eval(x.orelse)
                    if isinstance(v, str) and "," in v:
                        return v
                except Exception:
                    pass
        for x in ast.walk(node):
            if isinstance(x, ast.Call) and isinstance(x.func, ast.Name) \
                    and x.func.id == "_query_payload" and len(x.args) > 1:
                try:
                    v = ast.literal_eval(x.args[1])
                    if isinstance(v, str) and "," in v:
                        return v
                except Exception:
                    pass
        return None

    out: dict[str, dict] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if not any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                   and d.func.attr == "tool" for d in node.decorator_list):
            continue
        forms = {x.args[0].value for x in ast.walk(node)
                 if isinstance(x, ast.Call) and isinstance(x.func, ast.Name)
                 and x.func.id == "_query_payload" and x.args
                 and isinstance(x.args[0], ast.Constant)}
        if len(forms) != 1:
            continue
        fid = forms.pop()
        inp = (ast.unparse(node.args.args[0].annotation)
               if node.args.args and node.args.args[0].annotation else "")
        fields = body_default(node) or model_defaults.get(inp)
        if not fields:
            continue
        # 同一 form_id 可能有多个查询工具（如 query_purchase_orders 与
        # query_purchase_order_progress）。只有带显式默认字段集的才作数，
        # 且先到先得——否则抽取结果随 AST 遍历顺序漂移，比对就不可复现。
        out.setdefault(fid, {"tool": node.name, "fields": fields})
    return out


@pytest.fixture(scope="module")
def legacy():
    return _legacy_query_defaults()


@pytest.fixture(scope="module")
def onto():
    load.cache_clear()
    return load(tenant="")


class TestCoverage:
    def test_every_legacy_query_form_is_registered(self, legacy, onto):
        """每个 legacy 查询工具的 form_id 都要在注册表里，否则 kd_query 表达不了它。"""
        missing = sorted(set(legacy) - set(onto.nouns))
        assert not missing, (
            f"以下 form_id 有 legacy 查询工具但未登记到 base/registry.yml：{missing}。"
            f"未登记 = 该查询无法通过 kd_query 表达，只能继续依赖专用工具。")

    def test_registry_covers_more_than_the_original_catalog(self, onto):
        assert len(onto.nouns) >= 71, "长尾名词收敛后注册表应有 71+ 个名词"


class TestFieldParity:
    def test_non_divergent_forms_match_exactly(self, legacy, onto):
        """除已知分歧外，注册表默认字段集必须与 legacy 逐字一致。"""
        bad = []
        for fid, info in sorted(legacy.items()):
            if fid in KNOWN_DIVERGENT or fid not in onto.nouns:
                continue
            reg = onto.nouns[fid].default_fields
            if reg != info["fields"]:
                bad.append(f"{fid}（{info['tool']}）\n  registry: {reg}\n  legacy  : {info['fields']}")
        assert not bad, "注册表与 legacy 的默认字段集出现新的分歧：\n" + "\n".join(bad)

    def test_divergence_set_is_locked(self, legacy, onto):
        """审计 F-1：两套事实来源的分歧集合被锁定，只许减少不许增加。"""
        actual = {fid for fid, info in legacy.items()
                  if fid in onto.nouns and onto.nouns[fid].default_fields != info["fields"]}
        added = sorted(actual - KNOWN_DIVERGENT)
        assert not added, (
            f"新增了未登记的字段集分歧：{added}。"
            f"要么对齐两边，要么在确认后加入 KNOWN_DIVERGENT 并说明理由。")
        # 修好了也要更新常量，避免这层保护随时间失效
        fixed = sorted(KNOWN_DIVERGENT - actual)
        assert not fixed, (
            f"这些分歧已经消失，请从 KNOWN_DIVERGENT 中移除：{fixed}")


class TestKdQueryUsesRegistryDefaults:
    @pytest.mark.parametrize("ref,expect_form", [
        ("采购订单", "PUR_PurchaseOrder"),
        ("成本中心", "CB_CostCenter"),
        ("操作日志", "BOS_OperateLog"),
        ("MRP运算结果", "PLAN_MRPResult"),
    ])
    def test_alias_resolution_and_default_fields(self, onto, ref, expect_form):
        t = FakeTransport([[]])
        d = Dispatcher(ontology=onto, transport=t)
        asyncio.run(d.query(ref))
        assert t.calls[0][1] == expect_form

    def test_view_nouns_reject_write_verbs(self, onto):
        """长尾里有大量查询视图（成本分析、日志），它们不该接受写动词。"""
        from kingdee_ontology.base.ontology import OntologyError
        for view in ("BOS_OperateLog", "STK_CostTrend", "PLAN_MRPResult"):
            with pytest.raises(OntologyError):
                onto.check_verb_applies("audit", view)
