"""工具 → 本体动词 的登记表（修审计 R-1 / AT-05）。

原 harness 规则用**硬编码的工具名字符串**匹配：

    if node.tool in ("kingdee_save_bill", "kingdee_push_bill", "kingdee_submit_bills"):

24 个写动词里只认 3 个，覆盖率 25%；而漏掉的恰好包括全部 3 个复合工具
和 5 个 push 变体中的 4 个 —— 无补偿 Saga 正好落在约束层的盲区里。
更糟的是这种写法**静默失效**：新增一个写工具，规则不会报错，只是不再覆盖它。

改法：规则改按**动词**匹配，工具到动词的映射集中登记在这里，
并由 tests/test_harness_coverage.py 断言「server.py 里每个 readOnlyHint=False
的工具都必须登记」。漏登记 = CI 失败，而不是静默不覆盖。
"""
from __future__ import annotations

# 写工具 → (主动词, 该工具内部顺序执行的动词序列)
# 单动词工具的序列就是它自己；复合工具列出全部步骤，用于操作链完整性判定。
WRITE_TOOL_VERBS: dict[str, tuple[str, ...]] = {
    # ── 生命周期 ────────────────────────────────────────────
    "kingdee_save_bill":                ("save",),
    "kingdee_save_asset":               ("save",),
    "kingdee_save_cost_adjustment":     ("save",),
    "kingdee_save_production_order":    ("save",),
    "kingdee_submit_bills":             ("submit",),
    "kingdee_submit_production_orders": ("submit",),
    "kingdee_audit_bills":              ("audit",),
    "kingdee_audit_production_orders":  ("audit",),
    "kingdee_unaudit_bills":            ("unaudit",),
    "kingdee_delete_bills":             ("delete",),
    # ── 标准动作（ExecuteOperation / CancelAssign）──────────
    "kingdee_cancel_bills":             ("cancel",),
    "kingdee_void_bills":               ("void",),
    "kingdee_close_bill":               ("close",),
    "kingdee_unclose_bill":             ("unclose",),
    "kingdee_forbid_bills":             ("forbid",),
    "kingdee_enable_bills":             ("enable",),
    # ── 下推 ────────────────────────────────────────────────
    "kingdee_push_bill":                ("push",),
    "kingdee_push_stock_transfer":      ("push",),
    "kingdee_push_production_pick":     ("push",),
    "kingdee_push_production_stock_in": ("push",),
    # ── 复合（顺序执行多个写动词，无补偿；见审计 A-1）───────
    "kingdee_create_and_audit":         ("save", "submit", "audit"),
    "kingdee_push_and_audit":           ("push", "submit", "audit"),
    "kingdee_create_lx_billing":        ("push", "save", "submit", "audit"),
    # ── 审批（原 workflow_approve 已按审计 R-7 拆分为两个工具）──
    "kingdee_workflow_approve":         ("audit",),
    "kingdee_workflow_reject":          ("unaudit",),
    # ── 写缓存但不碰金蝶（审计 N-2 后 readOnlyHint 改为 False）──
    "kingdee_refresh_metadata":         ("refresh",),
}

# 一次调用内顺序执行多个写动词 = 无补偿 Saga
COMPOSITE_TOOLS: frozenset[str] = frozenset({
    "kingdee_create_and_audit", "kingdee_push_and_audit", "kingdee_create_lx_billing",
})

# 能把单据推进到终态的动词。到达其一即视为操作链完成。
TERMINAL_VERBS: frozenset[str] = frozenset({"audit", "delete", "void", "close"})

# 各动词之后应当发生的动词（None = 已是终态）
NEXT_VERB: dict[str, str | None] = {
    "save": "submit", "submit": "audit", "push": "submit",
    "audit": None, "unaudit": None, "delete": None, "refresh": None,
    "cancel": None, "void": None, "close": None, "unclose": None,
    "forbid": None, "enable": None,
}

# next_action 字段的合法取值域（修审计 R-2）。
# 规则用它反推后继动词；工具返回的 next_action 必须落在这里，
# 否则规则永远匹配不上、恒定误报。由 tests 断言 server.py 不越界。
NEXT_ACTION_VOCAB: frozenset[str] = frozenset({
    "submit", "audit", "submit+audit", "unaudit", "delete",
})


def verbs_of(tool: str) -> tuple[str, ...]:
    """工具执行的动词序列。未登记的写工具返回空元组。"""
    return WRITE_TOOL_VERBS.get(tool, ())


def primary_verb(tool: str) -> str | None:
    vs = verbs_of(tool)
    return vs[0] if vs else None


def is_write(tool: str) -> bool:
    return tool in WRITE_TOOL_VERBS


def parse_next_action(value: str | None) -> tuple[str, ...]:
    """把 next_action 解析成动词序列。

    合法：'submit' / 'audit' / 'submit+audit'
    非法（如塞了工具名 'kingdee_submit_bills + kingdee_audit_bills'）返回空元组，
    调用方据此报"词表越界"而不是去找一个不存在的工具。
    """
    if not value:
        return ()
    parts = tuple(p.strip() for p in value.split("+") if p.strip())
    if not parts or any(p not in NEXT_VERB for p in parts):
        return ()
    return parts
