"""
Harness 约束层 - 操作链完整性规则

定义金蝶 MCP 操作之间的依赖关系和约束规则。
这些规则通过 check_harness.py 自动化检查，在 CI 中强制阻断不合规的操作链。

核心原则：
- 所有写操作必须走完完整生命周期（Save→Submit→Audit）
- next_action 不为 null 时，AI 不应终止工作
- Push 操作后必须紧跟 Submit + Audit
- 错误返回后必须有真正的修正动作（诊断和原样重试都不算）
- 复合操作中途失败后，已生成的单据必须被续做或补偿

⚠️ 这一层是**事后检查**（CI 里读操作历史），不在运行期拦截。
   真正的前置拦截见 base/ontology.py 的 PRE-01..03。

规则按**动词**匹配（harness/tools.py 的登记表），不再硬编码工具名 ——
后者会随新增工具静默失效，正是审计发现 R-1 的成因。
"""

from dataclasses import dataclass, field
from typing import Optional

from kingdee_ontology.harness.tools import (
    COMPOSITE_TOOLS, NEXT_ACTION_VOCAB, TERMINAL_VERBS,
    is_write, parse_next_action, verbs_of,
)


@dataclass
class HarnessRule:
    """单条约束规则。

    ⚠️ check 的返回值是 (passed, message)：**True 表示通过**，不是"违反"。
    历史上这里的 docstring 写的是 (violated: bool, ...)，与全部 4 个实现相反
    （审计发现 R-4），照文档新增规则会全盘写反且不报错。现已统一为 passed。
    """
    id: str                     # 规则唯一标识，如 "RULE-001"
    name: str                   # 人类可读名称
    description: str            # 规则说明
    severity: str               # "error" | "warning" | "info"
    check: callable             # (nodes) -> (passed: bool, message: str)


# ─────────────────────────────────────────────
# 操作节点定义
# ─────────────────────────────────────────────

class OpNode:
    """操作链中的单个节点"""
    def __init__(self, tool: str, params: dict, result: dict, timestamp: float = 0):
        self.tool = tool
        self.params = params
        self.result = result
        self.timestamp = timestamp

    @property
    def is_success(self) -> bool:
        return self.result.get("success", False)

    @property
    def next_action(self) -> Optional[str]:
        return self.result.get("next_action")

    @property
    def fid(self) -> Optional[str]:
        return self.result.get("fid") or self.result.get("bill_id")

    @property
    def bill_ids(self) -> list:
        ids = self.result.get("bill_ids") or self.result.get("ids") or []
        return ids if isinstance(ids, list) else [ids]

    @property
    def bill_nos(self) -> list:
        return self.result.get("target_bill_nos") or self.result.get("bill_nos") or []

    def __repr__(self):
        return f"OpNode({self.tool}, success={self.is_success}, next={self.next_action})"


# ─────────────────────────────────────────────
# 约束规则定义
# ─────────────────────────────────────────────

def _check_complete_lifecycle(nodes: list[OpNode], **kwargs) -> tuple[bool, str]:
    """RULE-001: 写操作必须走完完整生命周期。

    修复三处审计发现：
      R-1  不再硬编码 3 个工具名，改用 harness.tools 的动词登记表 ——
           覆盖全部 24 个写工具，且新增工具漏登记会被 CI 断言拦下。
      R-2  next_action 经 parse_next_action 解析。塞了工具名的越界取值
           （如 "kingdee_submit_bills + kingdee_audit_bills"）会被明确报为
           词表越界，而不是去找一个不存在的 kingdee_kingdee_bills 然后恒定误报。
      R-3  单个节点无法追踪时用 continue 跳过，不再 return 提前终止整条规则
           （原实现让首个无 fid 的节点之后的所有节点全部漏检）。
    """
    problems: list[str] = []
    for node in nodes:
        if not is_write(node.tool) or not node.is_success or not node.next_action:
            continue

        expected = parse_next_action(node.next_action)
        if not expected:
            problems.append(
                f"[RULE-001] next_action 词表越界: {node.tool} 返回 "
                f"next_action={node.next_action!r}，不在合法取值域 "
                f"{sorted(NEXT_ACTION_VOCAB)} 内。规则无法据此判定后继操作。"
                f"建议: 返回动词（如 'submit' / 'submit+audit'）而不是工具名。")
            continue

        fid = node.fid or (node.bill_ids[0] if node.bill_ids else None)
        targets = set(node.bill_nos) | ({str(fid)} if fid else set())
        if not targets:
            continue  # R-3：无法追踪就跳过这个节点，继续检查后面的

        done = _verbs_applied_after(nodes, node, targets)
        missing = [v for v in expected if v not in done]
        if missing and _unbindable(nodes, node, targets, missing):
            continue  # 动词做了，只是标识对不上——由 RULE-006 以警告报出
        if missing:
            problems.append(
                f"[RULE-001] 操作链不完整: {node.tool} 返回 "
                f"next_action={node.next_action!r}，但未检测到针对 "
                f"{sorted(targets)} 的后续动词 {missing}。"
                f"单据可能停在中间状态。"
                f"建议: 继续执行 {missing}，或显式说明为何接受该中间态。")

    return (not problems), "\n".join(problems)


def _touched_by(node: OpNode) -> set[str]:
    """一个节点作用于哪些单据标识（FID 与单据编号混在一起，见下方说明）。"""
    touched = set(map(str, node.params.get("bill_ids") or []))
    touched |= set(map(str, node.bill_ids))
    touched |= set(node.bill_nos)
    touched |= set(map(str, node.result.get("target_fids") or []))
    for key in ("bill_id", "Ids"):
        val = node.params.get(key)
        if isinstance(val, str):
            touched |= {x.strip() for x in val.split(",") if x.strip()}
    return touched


def _unbindable(nodes: list[OpNode], after: OpNode, targets: set[str],
                verbs: list[str]) -> list[str]:
    """链路里存在这些动词、但没有一个能绑定到 targets 的情况。

    这不一定是操作链不完整，更可能是**名词身份双轨**：push 返回单据编号
    (FBillNo)，而 submit/audit 只接受内码 (FID)，系统又没有提供二者之间的
    解析动词（审计 L-3）。此时断言"链不完整"是误报——我们只能如实说
    "无法绑定"，把真正的缺陷指出来，而不是替它下结论。
    """
    found: list[str] = []
    for later in nodes:
        if later.timestamp <= after.timestamp:
            continue
        vs = set(verbs_of(later.tool)) & set(verbs)
        if vs and not (_touched_by(later) & targets):
            found.extend(sorted(vs))
    return sorted(set(found))


def _verbs_applied_after(nodes: list[OpNode], after: OpNode,
                         targets: set[str]) -> set[str]:
    """找出在 after 之后、作用于 targets 中任一单据的动词集合。

    修 R-5：动词必须**绑定到具体单据**。原实现只要链路后面出现过任意一次
    submit / audit 就算通过，对无关单据的操作即可骗过规则。
    """
    applied: set[str] = set()
    for later in nodes:
        if later.timestamp <= after.timestamp:
            continue
        if not _touched_by(later) & targets:
            continue
        applied.update(verbs_of(later.tool))
    return applied


def _check_push_chain(nodes: list[OpNode], **kwargs) -> tuple[bool, str]:
    """RULE-002: 下推生成的目标单必须完成 submit + audit。

    修 R-1：原实现只认 kingdee_push_bill，漏掉 push_stock_transfer、
    push_production_pick、push_production_stock_in 三个 push 变体
    （push_and_audit 内部自带 submit+audit，单独处理）。
    修 R-5：submit/audit 必须作用于 push 产出的目标单，不再"出现过就算数"。
    """
    problems: list[str] = []
    for node in nodes:
        if "push" not in verbs_of(node.tool) or not node.is_success:
            continue
        if node.tool in COMPOSITE_TOOLS:
            continue  # 复合工具内部已含 submit+audit，由 RULE-005 单独看中间态

        targets = set(node.bill_nos) | set(map(str, node.result.get("target_fids") or []))
        if not targets:
            continue  # 拿不到目标单标识，无从追踪

        done = _verbs_applied_after(nodes, node, targets)
        missing = [v for v in ("submit", "audit") if v not in done]
        if missing and _unbindable(nodes, node, targets, missing):
            continue  # 见 RULE-006
        if missing:
            problems.append(
                f"[RULE-002] 下推链不完整: {node.tool} 生成了 {len(targets)} 张目标单 "
                f"{sorted(targets)}，但未检测到针对它们的 {missing}。"
                f"目标单停留在草稿，不产生业务影响却占用了源单的关联数量。"
                f"建议: 对这些单据执行 {missing}，或删除草稿释放关联数量。")

    return (not problems), "\n".join(problems)


def _check_error_recovery(nodes: list[OpNode], **kwargs) -> tuple[bool, str]:
    """RULE-003: 操作失败后必须有真正的修正动作。

    原实现把"修正"定义为「调用了不同工具，或同工具但参数不同」——
    一次无意义的查询即可满足，约束强度接近于零。现在收紧为：
      · 只读动作（查看/校验/查元数据）算**诊断**，不算修正；
      · 原样重试同一个写动作不算修正（对非幂等动词还很危险）；
      · 改了参数的写动作、或明确的补偿动词（delete/unaudit/cancel）才算修正。
    """
    problems: list[str] = []
    DIAGNOSTIC = ("kingdee_view_bill", "kingdee_validate_bill", "kingdee_get_fields",
                  "kingdee_get_bill_template", "kingdee_refresh_metadata")
    for i, node in enumerate(nodes):
        has_error = (node.result.get("error")
                     or (node.result.get("errors") or [])
                     or not node.is_success)
        if not has_error:
            continue

        recovery = None
        for later in nodes[i + 1:]:
            if later.tool in DIAGNOSTIC or not is_write(later.tool):
                continue  # 诊断不是修正
            if later.tool == node.tool and later.params == node.params:
                continue  # 原样重试不是修正
            recovery = later
            break

        if recovery is None:
            problems.append(
                f"[RULE-003] 失败后无修正动作: {node.tool}（第 {i + 1} 步）失败，"
                f"其后没有任何改动参数的写操作或补偿动作。"
                f"错误: {node.result.get('errors', node.result.get('error'))}。"
                f"建议: 依 errors[].matched.suggestion 修正参数后重试，"
                f"或执行补偿（如删除已生成的草稿）；"
                f"若确实决定放弃，应显式记录该中间态而不是静默终止。")

    return (not problems), "\n".join(problems)


def _check_composite_intermediate_state(nodes: list[OpNode], **kwargs) -> tuple[bool, str]:
    """RULE-005: 复合工具中途失败必须有补偿或显式接受（对症审计 A-1）。

    create_and_audit / push_and_audit / create_lx_billing 一次调用内顺序执行
    3~4 个写端点，中途失败时前序副作用已落库且不回滚，代码只返回一段
    recovery_hint 文本。这条规则把"文本建议"变成"可判定的约束"：
    停在中间步骤后，链路里必须出现针对该单据的补偿或续做动作。
    """
    problems: list[str] = []
    for node in nodes:
        if node.tool not in COMPOSITE_TOOLS:
            continue
        halted = node.result.get("halted_at")
        if not halted:
            continue

        fid = node.fid or node.result.get("fid")
        targets = set(node.bill_nos) | ({str(fid)} if fid else set())
        produced = bool(targets)
        if not produced:
            continue  # 第一步就抛异常、未生成任何东西，无需补偿

        done = _verbs_applied_after(nodes, node, targets)
        if not (done & (TERMINAL_VERBS | {"delete", "unaudit", "cancel"})):
            problems.append(
                f"[RULE-005] 复合操作留下无人认领的中间态: {node.tool} 停在 "
                f"'{halted}' 步，已生成 {sorted(targets)}，"
                f"但链路中没有针对它的续做或补偿动作。"
                f"建议: 要么续做完成生命周期，要么补偿清理"
                f"（草稿 → kingdee_delete_bills；已审核 → 先 unaudit 再 delete）。"
                f"这些单据不会自动回滚。")

    return (not problems), "\n".join(problems)


def _check_identity_binding(nodes: list[OpNode], **kwargs) -> tuple[bool, str]:
    """RULE-006: 后续动作无法绑定到前序产物（名词身份双轨）。

    push 返回单据编号 FBillNo，而 submit/audit/delete 只接受内码 FID，
    系统又没有提供 FBillNo → FID 的解析动词（审计 L-3）。
    于是"下推后确实提交审核了"这条完全正常的链路，在自动校验里无法被证实。

    这条规则不判操作链错误——只如实报告"追踪断了"，因为无法证实
    不等于没发生。真正该修的是给系统补一个身份解析动词。
    """
    problems: list[str] = []
    for node in nodes:
        if not node.is_success:
            continue
        expected = list(parse_next_action(node.next_action))
        if "push" in verbs_of(node.tool):
            expected = expected or ["submit", "audit"]
        if not expected:
            continue

        targets = set(node.bill_nos) | set(map(str, node.result.get("target_fids") or []))
        fid = node.fid
        if fid:
            targets.add(str(fid))
        if not targets:
            continue

        done = _verbs_applied_after(nodes, node, targets)
        missing = [v for v in expected if v not in done]
        stray = _unbindable(nodes, node, targets, missing) if missing else []
        if stray:
            problems.append(
                f"[RULE-006] 无法绑定后续动作: {node.tool} 产出 {sorted(targets)}，"
                f"链路中存在 {stray} 动作但作用的标识对不上"
                f"（典型原因：push 返回 FBillNo 而 submit/audit 用 FID）。"
                f"因此无法证实操作链是否完整——这不代表出错，"
                f"但意味着这条链不可审计。"
                f"建议: 为系统补一个 FBillNo → FID 的解析动词，"
                f"或让 push 的返回同时带上两种标识。")

    return (not problems), "\n".join(problems)


def _check_idempotent_read_only(nodes: list[OpNode], **kwargs) -> tuple[bool, str]:
    """RULE-004: 读操作之后应验证结果而非盲目重复

    查询操作（readOnlyHint=true）连续调用且返回相同结果时，视为无效重复。
    """
    if len(nodes) < 2:
        return True, ""

    # 找到连续的非幂等操作
    for i in range(len(nodes) - 1):
        curr = nodes[i]
        nxt = nodes[i + 1]

        # 跳过写操作（由 RULE-001/002/005 覆盖）。用登记表而非硬编码名单，
        # 避免新增写工具后这里静默漏判。
        if is_write(curr.tool):
            continue

        # 两个相同的查询操作且结果相同
        if curr.tool == nxt.tool and curr.params == nxt.params:
            curr_data = curr.result.get("data", curr.result.get("count", 0))
            nxt_data = nxt.result.get("data", nxt.result.get("count", 0))
            if curr_data == nxt_data and curr_data:
                return False, (
                    f"[RULE-004] 无效重复查询: {curr.tool} 连续调用两次，返回相同结果。"
                    f"count={curr_data}。"
                    f"建议: 如果目的是查询最新状态，应在操作之间插入实际修改动作。"
                )
    return True, ""


# ─────────────────────────────────────────────
# 规则注册表
# ─────────────────────────────────────────────

HARNESS_RULES: list[HarnessRule] = [
    HarnessRule(
        id="RULE-001",
        name="生命周期完整性",
        description="写操作必须走完 Save→Submit→Audit 完整链路，next_action != null 时不得终止",
        severity="error",
        check=_check_complete_lifecycle,
    ),
    HarnessRule(
        id="RULE-002",
        name="Push 操作链完整性",
        description="Push 生成的目标单据必须完成 Submit+Audit",
        severity="error",
        check=_check_push_chain,
    ),
    HarnessRule(
        id="RULE-003",
        name="错误恢复检查",
        description="操作失败后必须有对应的修正动作，不能直接终止",
        severity="error",
        check=_check_error_recovery,
    ),
    HarnessRule(
        id="RULE-005",
        name="复合操作中间态补偿",
        description="复合工具中途失败后，已生成的单据必须被续做或补偿，不得无人认领",
        severity="error",
        check=_check_composite_intermediate_state,
    ),
    HarnessRule(
        id="RULE-006",
        name="身份可绑定性",
        description="后续动作必须能绑定到前序产物，否则操作链不可审计（名词身份双轨）",
        severity="warning",
        check=_check_identity_binding,
    ),
    HarnessRule(
        id="RULE-004",
        name="无效重复查询",
        description="连续相同查询且结果相同时视为无效重复",
        severity="warning",
        check=_check_idempotent_read_only,
    ),
]


def validate_operation_chain(nodes: list[OpNode]) -> list[dict]:
    """验证操作链是否符合 Harness 约束

    返回违规列表，每项包含 rule_id, name, message, severity
    """
    violations = []
    for rule in HARNESS_RULES:
        passed, message = rule.check(nodes)
        if not passed:
            violations.append({
                "rule_id": rule.id,
                "name": rule.name,
                "message": message,
                "severity": rule.severity,
            })
    return violations