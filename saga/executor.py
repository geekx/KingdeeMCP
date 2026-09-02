"""把 Saga 步骤接到真实执行上。

引擎本身不认识网络，执行由这里注入——所以全部分支（守卫不满足、
授权被拒、补偿失败）都能离线测到，测的还是生产那条路径。
"""
from __future__ import annotations

from typing import Any, Optional

from saga.engine import SagaError, eval_condition
from saga.model import StepKind, StepResult


def make_executor(dispatcher, on_behalf_of: Optional[str] = None):
    """返回一个 executor(run, spec, carry) -> StepResult。"""

    async def execute(run, spec: dict, carry: list[str]) -> StepResult:
        raw = spec.get("raw", {})
        kind = spec["kind"]
        idx = spec["index"]
        compensating = spec.get("__compensating__", False)

        # ── 确认：纯人工闸，到这里说明已经批过了 ──
        if kind == StepKind.CONFIRM.value:
            return StepResult(index=idx, kind=kind, outcome="success",
                              detail={"问": raw.get("问", "")})

        # ── 检查：守卫。条件不满足就让这一步失败，触发补偿 ──
        if kind == StepKind.CHECK.value:
            noun, cond = raw.get("对象"), raw.get("条件", "")
            try:
                q = await dispatcher.query(noun, raw.get("过滤", ""), top=raw.get("取数", 50))
            except Exception as exc:
                return StepResult(index=idx, kind=kind, outcome="failed", noun=str(noun),
                                  detail={"error": f"守卫查询失败：{exc}"[:300]})
            ok, why = eval_condition(cond, q.get("rows") or [])
            return StepResult(
                index=idx, kind=kind, outcome="success" if ok else "failed",
                noun=q.get("noun", ""), detail={"条件": cond, "结论": why,
                                                "检查了": q.get("count", 0)})

        # ── 下推 ──
        if kind == StepKind.PUSH.value and not compensating:
            r = await dispatcher.push(raw["从"], raw["到"], carry,
                                      on_behalf_of=on_behalf_of)
            ok = bool(r.get("success"))
            produced = r.get("target_fids") or r.get("target_bill_nos") or []
            return StepResult(index=idx, kind=kind,
                              outcome="success" if ok else "failed",
                              noun=r.get("to", ""), produced=list(produced),
                              detail={k: r.get(k) for k in
                                      ("errors", "target_bill_nos", "link_verified",
                                       "warning") if r.get(k)})

        # ── 动词（正向或补偿）──
        noun = raw.get("到") if (compensating and raw.get("做") == StepKind.PUSH.value) \
            else raw.get("对象") or raw.get("到")
        if not noun:
            raise SagaError(f"第 {idx + 1} 步没有指明对象，无法执行 {kind}")
        use = raw.get("用", "targets")
        ids = list(carry) if (compensating or use == "上一步产物") else list(run.targets)
        r = await dispatcher.act(kind, noun, ids, on_behalf_of=on_behalf_of,
                                 operation=raw.get("操作编码"))
        ok = bool(r.get("success"))
        return StepResult(
            index=idx, kind=kind, outcome="success" if ok else r.get("outcome", "failed"),
            noun=r.get("noun", ""), produced=list(r.get("succeeded") or []),
            detail={k: r.get(k) for k in ("contract", "failed", "results", "tip")
                    if r.get(k)})

    return execute
