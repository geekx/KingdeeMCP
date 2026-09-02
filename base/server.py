"""MCP 底座 —— 7 个通用工具取代 97 个专用工具。

token 账（实测，见 docs/ontology/05-architecture.md）：
    旧：97 个工具的 tools/list ≈ 45,900 token，每次会话开口前的固定成本
    新：7 个工具 ≈ 1,500 token，实例经 kd_describe 按需拉取

能这样收敛的前提是把「能力」和「实例」分开：
    能力（14 个动词 + 原子性契约 + 状态机）→ 底座，稳定，人人相同
    实例（48+ 名词 / 链接 / 二开操作码 / 业务操作入口）→ 注册表 + 租户覆盖层，各家不同
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server.fastmcp import FastMCP  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

from base.dispatch import Dispatcher  # noqa: E402
from base.ontology import OntologyError, load  # noqa: E402

mcp = FastMCP("kingdee-base")

_TENANT = os.environ.get("KINGDEE_TENANT", "")
_ACTOR = os.environ.get("KINGDEE_USERNAME", "unknown")


def _d() -> Dispatcher:
    return Dispatcher(ontology=load(tenant=_TENANT), actor=_ACTOR)


def _fmt(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _guard(fn):
    async def wrapper(*a, **kw):
        try:
            return await fn(*a, **kw)
        except OntologyError as e:            # 前置条件不满足：错误自带修正建议
            return _fmt({"ok": False, "blocked_by": "precondition", "error": str(e)})
        except Exception as e:
            return _fmt({"ok": False, "error": f"{type(e).__name__}: {e}"})
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# ── 1. 自省：把实例从常驻 schema 变成按需拉取 ────────────────────
class DescribeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    what: str = Field(description="verbs|nouns|states|links|rules|operations")
    key: Optional[str] = Field(default=None, description="具体条目；支持中文名/别名")


@mcp.tool(name="kd_describe", annotations={
    "title": "查本体", "readOnlyHint": True, "idempotentHint": True})
@_guard
async def kd_describe(params: DescribeInput) -> str:
    """查询本体：有哪些单据(nouns)、动词及其原子性契约(verbs)、状态(states)、
    下推关系(links)、规则(rules)、本租户的业务操作入口(operations)。

    先用 what='operations' 看有没有现成的业务操作；没有再用 nouns/verbs 自己组。
    不带 key 返回清单，带 key 返回该条目详情（含可用动词、可下推目标）。"""
    return _fmt(load(tenant=_TENANT).describe(params.what, params.key))


# ── 2. 查询 ──────────────────────────────────────────────────────
class QueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    noun: str = Field(description="单据类型，form_id 或中文名，如 '采购订单'")
    filter: str = Field(default="", description="金蝶 FilterString，如 FDate>='2026-01-01'")
    fields: str = Field(default="", description="留空用该单据的默认字段集")
    top: int = Field(default=50, ge=1, le=500)


@mcp.tool(name="kd_query", annotations={
    "title": "查询单据", "readOnlyHint": True, "idempotentHint": True})
@_guard
async def kd_query(params: QueryInput) -> str:
    """按单据类型查询。字段留空时自动使用该单据的默认字段集。"""
    return _fmt(await _d().query(params.noun, params.filter, params.fields, params.top))


# ── 3. 写动词统一入口 ────────────────────────────────────────────
class ActInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verb: str = Field(description="submit|audit|unaudit|delete|close|unclose|void|cancel|forbid|enable|save")
    noun: str = Field(description="单据类型，form_id 或中文名")
    targets: list[str] = Field(default_factory=list, description="FID 列表；save 时可留空")
    model: Optional[dict] = Field(default=None, description="save 时的单据字段")
    current_state: Optional[str] = Field(default=None, description="已知的当前状态，用于前置校验")
    operation: Optional[str] = Field(default=None, description="二开单的自定义操作编码，覆盖默认值")


@mcp.tool(name="kd_act", annotations={
    "title": "执行写动词", "readOnlyHint": False, "destructiveHint": True})
@_guard
async def kd_act(params: ActInput) -> str:
    """对单据执行写动词。返回体**必带 contract**（arity/atomicity/idempotent/
    destructive/inverse）与逐目标结果，调用方据此判断失败后能否重试、要不要补偿。

    执行前自动校验：动词是否适用于该单据、当前状态是否满足前置条件。
    不适用时在发请求前就拦下，并给出该单据可用的动词。"""
    return _fmt(await _d().act(params.verb, params.noun, params.targets,
                               model=params.model, current_state=params.current_state,
                               operation=params.operation))


# ── 4. 下推 ──────────────────────────────────────────────────────
class PushInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(description="源单类型")
    target: str = Field(description="目标单类型")
    source_bill_nos: list[str] = Field(description="源单编号 FBillNo 列表")
    rule_id: str = Field(default="", description="转换规则 ID，通常留空")


@mcp.tool(name="kd_push", annotations={
    "title": "下推生成下游单", "readOnlyHint": False, "destructiveHint": True})
@_guard
async def kd_push(params: PushInput) -> str:
    """由源单下推生成目标单。未登记的下推关系会在发请求前被拦下。

    **不自动提交审核** —— 目标单保持草稿，由调用方显式 kd_act。
    自动串联会在中途失败时留下无人认领的中间态。"""
    return _fmt(await _d().push(params.source, params.target,
                                params.source_bill_nos, params.rule_id))


# ── 5. 业务操作入口 ──────────────────────────────────────────────
class RunOpInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str = Field(description="业务操作名，见 kd_describe(what='operations')")
    targets: list[str] = Field(description="起始单据（编号或 FID）")
    confirmed: bool = Field(default=False, description="是否已获得人的确认")


@mcp.tool(name="kd_run", annotations={
    "title": "执行业务操作", "readOnlyHint": False, "destructiveHint": True})
@_guard
async def kd_run(params: RunOpInput) -> str:
    """执行本租户定义的业务操作（如『销售开票』『采购收货入库』）。

    未确认时**不做任何写操作**，只返回执行计划和待确认问题。
    中途失败立即停止，并明确列出已产生的中间态单据（left_behind）。"""
    return _fmt(await _d().run_operation(params.operation, params.targets,
                                         confirmed=params.confirmed))


# ── 6. 过程操作审计 ──────────────────────────────────────────────
class AuditInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str = Field(default="dangling", description="dangling(未清算的中间态)|recent|trace")
    trace_id: Optional[str] = Field(default=None)
    limit: int = Field(default=20, ge=1, le=200)


@mcp.tool(name="kd_audit", annotations={
    "title": "查过程操作审计", "readOnlyHint": True, "idempotentHint": True})
@_guard
async def kd_audit(params: AuditInput) -> str:
    """查过程操作审计记录。scope='dangling' 列出**未清算的中间态**：
    有写操作已生效、但整条操作链没走到终态且无补偿的单据。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "ontology"))
    from operation_audit import dangling_traces, load as load_audit
    recs = load_audit(os.environ.get("KINGDEE_OPERATION_AUDIT_LOG", "operation_audit.jsonl"))
    if params.scope == "dangling":
        d = dangling_traces(recs)
        return _fmt({"total_records": len(recs), "dangling": len(d),
                     "traces": d[:params.limit],
                     "tip": "left_objects 里的单据处于中间态，需要继续处理或清理。"})
    if params.scope == "trace":
        return _fmt({"records": [r for r in recs if r["trace_id"] == params.trace_id]})
    return _fmt({"records": recs[-params.limit:]})


# ── 7. 校验租户配置 ──────────────────────────────────────────────
@mcp.tool(name="kd_check_profile", annotations={
    "title": "校验租户配置", "readOnlyHint": True, "idempotentHint": True})
@_guard
async def kd_check_profile(tenant: str = "") -> str:
    """校验租户配置（二开表单/操作码/下推关系/业务操作入口）是否填写正确。
    返回中文的错误与建议，供业务人员自行修正 profiles/<租户>/profile.yml。"""
    from base.validate_profile import validate
    errs, warns = validate(tenant or _TENANT)
    return _fmt({"tenant": tenant or _TENANT, "ok": not errs,
                 "errors": errs, "warnings": warns})


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
