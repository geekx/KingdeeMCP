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
    what: str = Field(description="静态本体 verbs|nouns|states|links|rules|operations；"
                                  "实时元数据 fields（对账套拉该单据的真实字段清单）；"
                                  "template（已验证的 model 骨架，用于新建单据）")
    key: Optional[str] = Field(default=None, description="具体条目；支持中文名/别名")


@mcp.tool(name="kd_describe", annotations={
    "title": "查本体", "readOnlyHint": True, "idempotentHint": True})
@_guard
async def kd_describe(params: DescribeInput) -> str:
    """查询本体：有哪些单据(nouns)、动词及其原子性契约(verbs)、状态(states)、
    下推关系(links)、规则(rules)、本租户的业务操作入口(operations)。

    先用 what='operations' 看有没有现成的业务操作；没有再用 nouns/verbs 自己组。
    不带 key 返回清单，带 key 返回该条目详情（含可用动词、可下推目标）。

    what='fields' 走实时元数据：返回该单据在**本账套**的真实字段清单与必填项，
    用于二开表单——注册表里的 default_fields 是静态的，可能与账套不符。"""
    if params.what == "template":
        if not params.key:
            return _fmt({"ok": False,
                         "error": "what='template' 需要 key（单据类型），如 key='销售订单'"})
        return _fmt(await _d().template(params.key))
    if params.what == "fields":
        if not params.key:
            return _fmt({"ok": False,
                         "error": "what='fields' 需要 key（单据类型），如 key='采购订单'"})
        return _fmt(await _d().fields(params.key))
    return _fmt(load(tenant=_TENANT).describe(params.what, params.key))


# ── 2. 查询 ──────────────────────────────────────────────────────
class QueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    noun: str = Field(description="单据类型，form_id 或中文名，如 '采购订单'。"
                                  "逗号分隔可一次查多个，如 '销售出库单,采购入库单'")
    filter: str = Field(default="", description="金蝶 FilterString，如 FDate>='2026-01-01'")
    fields: str = Field(default="", description="留空用该单据的默认字段集")
    top: int = Field(default=50, ge=1, le=500)


@mcp.tool(name="kd_query", annotations={
    "title": "查询单据", "readOnlyHint": True, "idempotentHint": True})
@_guard
async def kd_query(params: QueryInput) -> str:
    """按单据类型查询。字段留空时自动使用该单据的默认字段集。

    系统对象（用户/角色/权限/编码规则/系统参数）会自动走各自的专用端点，
    调用方不必知道这个区别。多个名词用逗号分隔即可合并查询。"""
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
    dry_run: bool = Field(default=False,
                          description="仅对 save 有效：只做保存前校验，不写入")


@mcp.tool(name="kd_act", annotations={
    "title": "执行写动词", "readOnlyHint": False, "destructiveHint": True})
@_guard
async def kd_act(params: ActInput) -> str:
    """对单据执行写动词。返回体**必带 contract**（arity/atomicity/idempotent/
    destructive/inverse）与逐目标结果，调用方据此判断失败后能否重试、要不要补偿。

    执行前自动校验：动词是否适用于该单据、当前状态是否满足前置条件。
    不适用时在发请求前就拦下，并给出该单据可用的动词。

    dry_run=True + verb='save' 只做保存前校验不写入，用于先确认字段是否齐全。"""
    return _fmt(await _d().act(params.verb, params.noun, params.targets,
                               model=params.model, current_state=params.current_state,
                               operation=params.operation, dry_run=params.dry_run))


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


# ── 5. 对象层：以对象为中心的操作面 ──────────────────────────────
class ObjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    noun: str = Field(default="", description="对象类型，form_id 或中文名。留空则列出所有类型")
    id: Optional[str] = Field(default=None,
                              description="对象标识（内码或单据编号）。不给则返回类型卡片")
    search: str = Field(default="", description="按关键字搜索对象类型")
    category: str = Field(default="", description="按类别过滤：bill|master_data|view|system")
    navigate_to: Optional[str] = Field(
        default=None, description="要跳转到的下游单据类型（需同时给 id 作为源单编号）")


@mcp.tool(name="kd_object", annotations={
    "title": "打开对象", "readOnlyHint": True, "idempotentHint": True})
@_guard
async def kd_object(params: ObjectInput) -> str:
    """以**对象**为中心操作，而不是以工具为中心。

    打开一个对象，一次拿到：它的属性、它现在处于什么状态、
    **此刻能对它做哪些动作**（不能做的会说明为什么、要先做什么）、
    以及它连到哪些别的对象。

    三种用法：
      kd_object(search="采购")                  搜索对象类型
      kd_object(noun="采购订单")                 类型卡片：这类对象长什么样、能做什么
      kd_object(noun="采购订单", id="CGDD000231") 实例卡片：这一张单此刻能做什么
      kd_object(noun="采购订单", id="CGDD000231", navigate_to="采购入库单")
                                               怎么跳到它的下游单据

    拿到 actions 里 enabled=true 的动词后，用 kd_act 执行。"""
    d = _d()
    if params.navigate_to:
        if not params.id:
            return _fmt({"ok": False, "error": "navigate_to 需要同时给 id（源单编号）"})
        return _fmt(d.navigate(params.noun, params.navigate_to, params.id))
    if not params.noun:
        return _fmt(d.search_types(params.search, params.category))
    if params.search:
        return _fmt(d.search_types(params.search, params.category))
    return _fmt(await d.object_card(params.noun, params.id))


# ── 6. 查看详情 / 报表 ───────────────────────────────────────────
class ReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    noun: str = Field(description="单据类型，form_id 或中文名")
    bill_id: str = Field(description="单据内码 FID")


@mcp.tool(name="kd_read", annotations={
    "title": "查看单据详情", "readOnlyHint": True, "idempotentHint": True})
@_guard
async def kd_read(params: ReadInput) -> str:
    """按 FID 查看单据完整详情（含分录）。列表查询请用 kd_query。"""
    return _fmt(await _d().read(params.noun, params.bill_id))


class ReportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    noun: str = Field(description="报表标识，如 STK_StockSumReport")
    payload: dict = Field(default_factory=dict,
                          description="报表参数（各报表结构不同，需按金蝶报表定义填写）")


@mcp.tool(name="kd_report", annotations={
    "title": "查询报表", "readOnlyHint": True, "idempotentHint": True})
@_guard
async def kd_report(params: ReportInput) -> str:
    """查询金蝶报表（GetSysReportData 端点）。

    报表的参数结构与单据查询完全不同，且每张报表各异，
    所以单列一个工具而不是塞进 kd_query —— 混在一起会让两边的参数都变得含糊。"""
    return _fmt(await _d().report(params.noun, params.payload))


# ── 7. 业务操作入口 ──────────────────────────────────────────────
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


# ── 8. 过程操作审计 ──────────────────────────────────────────────
class AuditInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str = Field(default="dangling",
                       description="dangling(未清算的中间态)|recent|trace|usage(调用统计)")
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
    if params.scope == "usage":
        from collections import Counter
        by_tool = Counter(r["tool"] for r in recs)
        by_outcome = Counter(r["outcome"] for r in recs)
        return _fmt({"total": len(recs), "by_tool": dict(by_tool.most_common(20)),
                     "by_outcome": dict(by_outcome),
                     "note": "unknown 表示结果不可判定（服务端可能已生效），"
                             "与 failed 是两回事。"})
    return _fmt({"records": recs[-params.limit:]})


# ── 9. 校验租户配置 ──────────────────────────────────────────────
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
