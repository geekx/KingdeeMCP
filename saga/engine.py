"""Saga 引擎 —— 多扣扳机组的执行、暂停、补偿。

执行模型是一个可中断的前向推进 + 失败时的逆序补偿：

    advance()  从 cursor 往下跑，遇到需要授权的步骤就停住存盘并返回；
    authorize() 记下是谁授权的，然后继续 advance()；
    失败时自动进入 compensate()，把已成功的写步骤**按逆序**补偿。

三个刻意的取舍：

  ① 补偿必须显式声明。不给 `补偿:` 的写步骤，失败时只报告"遗留了什么"，
     **不猜**该怎么收拾。靠"逆动词"推是危险的：push 的逆不是 unpush
     （不存在），而是删除下游单据；save 的逆在已审核后也不是 delete。
  ② 补偿失败要吼出来。COMPENSATION_FAILED 是比 HALTED 更坏的终态——
     系统试图收拾却没收拾干净，必须有人去看。静默吞掉是最糟的选择。
  ③ 授权是**逐步**的。开头点一次不等于全权委托：
     「生成开票申请」和「过账应收」的风险完全不同。
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

from saga.model import (RunState, RunStore, SagaRun, StepKind, StepResult,
                        StepSpec, _now)

# 检查步骤的条件表达式：字段 运算符 值。刻意只支持这几种——
# 支持任意表达式就等于把一个求值器塞进配置文件，业务人员写错了很难查。
_COND = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*(>=|<=|!=|=|>|<)\s*(.+?)\s*$")


class SagaError(RuntimeError):
    pass


def parse_steps(op, ontology=None) -> list[StepSpec]:
    """把 profile 里的步骤声明解析成 StepSpec。

    补偿**默认取本体里动词的 compensation 字段**，profile 不写就自动继承。
    这样"哪些动作能退、怎么退"只有一个事实来源（base/registry.yml:verbs），
    不会出现注册表说一套、每个租户的 profile 各写一套的局面。
    profile 显式写 `补偿:` 才覆盖；写 `补偿: null` 表示"这步就是退不回来"。
    """
    out: list[StepSpec] = []
    for i, raw in enumerate(op.steps):
        kind = raw.get("做", "")
        auth = raw.get("授权")
        if auth is True:
            auth = "true"
        elif auth is not None:
            auth = str(auth)

        if "补偿" in raw:
            comp = raw["补偿"]                       # 显式覆盖（含显式 null）
        elif ontology is not None:
            v = ontology.verbs.get(_verb_of_kind(kind))
            comp = v.compensation if v else None    # 继承本体
        else:
            comp = None
        out.append(StepSpec(index=i, kind=kind, raw=dict(raw), authorize=auth,
                            compensate=comp, note=raw.get("说明", "")))
    return out


def _verb_of_kind(kind: str) -> str:
    """步骤类型 → 本体动词。『下推』就是 push。"""
    return {StepKind.PUSH.value: "push"}.get(kind, kind)


def eval_condition(cond: str, rows: list[dict]) -> tuple[bool, str]:
    """守卫条件求值。返回 (是否满足, 说明)。

    语义是「至少有一行满足」——检查库存够不够、单据在不在，
    问的都是"存在这样一行吗"。全表都要满足的场景另说，目前用不上。
    """
    m = _COND.match(cond or "")
    if not m:
        raise SagaError(
            f"看不懂的检查条件 {cond!r}。只支持 `字段 运算符 值`，"
            f"如 FBaseQty >= 1、FDocumentStatus = 'C'。"
            f"刻意不支持任意表达式——把求值器塞进配置文件，写错了很难查。")
    field, opr, rhs = m.group(1), m.group(2), m.group(3).strip().strip("'\"")
    try:
        want: Any = float(rhs)
        numeric = True
    except ValueError:
        want, numeric = rhs, False

    hit = 0
    for r in rows:
        v = r.get(field)
        if v is None:
            continue
        try:
            lv: Any = float(v) if numeric else str(v)
        except (TypeError, ValueError):
            continue
        ok = {"=": lv == want, "!=": lv != want, ">": lv > want,
              "<": lv < want, ">=": lv >= want, "<=": lv <= want}[opr]
        if ok:
            hit += 1
    if hit:
        return True, f"{hit}/{len(rows)} 行满足 {field} {opr} {rhs}"
    return False, (f"{len(rows)} 行里没有一行满足 {field} {opr} {rhs}"
                   if rows else f"查不到任何行，无法确认 {field} {opr} {rhs}")


class SagaEngine:
    """引擎不认识网络。执行动作交给注入的 executor，便于离线测全部分支。"""

    def __init__(self, ontology, store: Optional[RunStore] = None,
                 actor: str = "unknown"):
        self.o = ontology
        self.store = store or RunStore()
        self.actor = actor

    # ── 生命周期 ──────────────────────────────────────────────
    def plan(self, key: str, targets: list[str]) -> SagaRun:
        op = self.o.operation(key)
        steps = parse_steps(op, self.o)
        self._validate(op, steps)
        run = SagaRun.new(op.key, op.zh, targets, steps, actor=self.actor)
        self.store.save(run)
        return run

    def _validate(self, op, steps: list[StepSpec]) -> None:
        writes = [s for s in steps if s.is_write]
        if not writes:
            return
        # 不可补偿的写步骤不是错误，但要在计划阶段就让人看见
        for s in writes:
            if not s.compensate:
                continue
            v = self.o.verbs.get(s.compensate)
            if v is None:
                raise SagaError(
                    f"第 {s.index + 1} 步声明的补偿动词 {s.compensate!r} 不存在。"
                    f"可用：{sorted(k for k, x in self.o.verbs.items() if x.kind == 'write')}")
            # profile 覆盖与本体矛盾时要说清——两个事实来源打架，
            # 静默采信任何一方都会让人以为另一方也生效了
            step_verb = self.o.verbs.get(_verb_of_kind(s.kind))
            if step_verb and step_verb.compensation and \
                    s.compensate != step_verb.compensation and \
                    "补偿" in s.raw:
                raise SagaError(
                    f"第 {s.index + 1} 步的补偿声明与本体矛盾："
                    f"profile 写 {s.compensate!r}，但 base/registry.yml 里 "
                    f"{s.kind} 的 compensation 是 {step_verb.compensation!r}。"
                    f"要么改 profile 与之一致，要么先改注册表——"
                    f"两处不一致时无法判断哪个是真的。")

    async def advance(self, run: SagaRun, executor: Callable,
                      carry: Optional[list[str]] = None) -> SagaRun:
        """从 cursor 往下跑，直到需要授权、失败、或跑完。"""
        if run.is_terminal:
            return run
        carry = carry if carry is not None else self._carry_from(run)
        run.touch(RunState.RUNNING.value)

        while run.cursor < len(run.steps):
            spec = run.steps[run.cursor]

            if spec["needs_auth"] and not self._authorized(run, run.cursor):
                run.touch(RunState.AWAITING_AUTH.value)
                self.store.save(run)
                return run

            res = await executor(run, spec, carry)
            run.results.append(res.__dict__ if hasattr(res, "__dict__") else res)
            r = res if isinstance(res, StepResult) else StepResult(**res)

            if r.outcome == "success":
                if r.produced:
                    carry = list(r.produced)
                run.cursor += 1
                self.store.save(run)
                continue

            # 失败：进入逆序补偿
            run.error = f"第 {run.cursor + 1} 步失败：{r.detail.get('error') or r.outcome}"
            await self.compensate(run, executor)
            return run

        run.touch(RunState.DONE.value)
        self.store.save(run)
        return run

    def authorize(self, run: SagaRun, by: str, step: Optional[int] = None,
                  approve: bool = True, reason: str = "") -> SagaRun:
        """给某一步授权（或拒绝）。默认是当前停住的那一步。"""
        idx = run.cursor if step is None else step
        if idx >= len(run.steps):
            raise SagaError(f"没有第 {idx + 1} 步")
        spec = run.steps[idx]
        if not spec["needs_auth"]:
            raise SagaError(f"第 {idx + 1} 步不需要授权（{spec['describe']}）")
        if not by:
            raise SagaError("授权必须记名——谁批的要能查到")

        if not approve:
            run.results.append(StepResult(
                index=idx, kind=spec["kind"], outcome="skipped",
                detail={"rejected_by": by, "reason": reason},
                authorized_by=by, authorized_at=_now()).__dict__)
            run.error = f"第 {idx + 1} 步被 {by} 拒绝：{reason or '未说明理由'}"
            run.touch(RunState.HALTED.value)
            self.store.save(run)
            return run

        run.results.append(StepResult(
            index=idx, kind=spec["kind"], outcome="authorized",
            detail={"role_required": spec["authorize"]},
            authorized_by=by, authorized_at=_now()).__dict__)
        run.touch(RunState.RUNNING.value)
        self.store.save(run)
        return run

    async def compensate(self, run: SagaRun, executor: Callable) -> SagaRun:
        """把已成功的写步骤按**逆序**补偿。"""
        run.touch(RunState.COMPENSATING.value)
        self.store.save(run)

        done = [r for r in run.results
                if r.get("outcome") == "success" and r.get("produced") is not None]
        failed_any, skipped_any = False, False

        for r in reversed(done):
            spec = run.steps[r["index"]]
            if not spec["is_write"]:
                continue
            if not spec["compensate"]:
                r["compensated"] = "not_declared"
                skipped_any = True
                continue
            if not r.get("produced"):
                r["compensated"] = "not_needed"
                continue
            try:
                res = await executor(run, {**spec, "kind": spec["compensate"],
                                           "__compensating__": True}, r["produced"])
                ok = (res.outcome if isinstance(res, StepResult) else res["outcome"]) == "success"
                r["compensated"] = "success" if ok else "failed"
                r["compensation_detail"] = (res.detail if isinstance(res, StepResult)
                                            else res.get("detail", {}))
                failed_any = failed_any or not ok
            except Exception as exc:
                r["compensated"] = "failed"
                r["compensation_detail"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
                failed_any = True

        if failed_any:
            run.touch(RunState.COMPENSATION_FAILED.value)
        elif skipped_any:
            run.touch(RunState.HALTED.value)      # 有东西没补偿掉，不能说"已补偿"
        else:
            run.touch(RunState.COMPENSATED.value)
        self.store.save(run)
        return run

    # ── 辅助 ──────────────────────────────────────────────────
    def _authorized(self, run: SagaRun, idx: int) -> bool:
        return any(r.get("index") == idx and r.get("outcome") == "authorized"
                   for r in run.results)

    def _carry_from(self, run: SagaRun) -> list[str]:
        for r in reversed(run.results):
            if r.get("outcome") == "success" and r.get("produced"):
                return list(r["produced"])
        return list(run.targets)

    # ── 对外呈现 ──────────────────────────────────────────────
    def report(self, run: SagaRun) -> dict:
        left = run.produced_objects()
        out = {
            "run_id": run.run_id, "operation": run.operation, "zh": run.zh,
            "state": run.state, "cursor": run.cursor, "total_steps": len(run.steps),
            "targets": run.targets, "trace_id": run.trace_id,
            "steps": [{**s, "result": next(
                (r for r in run.results
                 if r["index"] == s["index"] and r["outcome"] != "authorized"), None)}
                for s in run.steps],
            "left_behind": left,
        }
        st = RunState(run.state)
        if st is RunState.AWAITING_AUTH:
            spec = run.steps[run.cursor]
            out["awaiting"] = {
                "step": run.cursor + 1, "describe": spec["describe"],
                "role_required": None if spec["authorize"] == "true" else spec["authorize"],
                "is_write": spec["is_write"],
                "compensable": bool(spec["compensate"]),
            }
            out["tip"] = (
                f"停在第 {run.cursor + 1} 步等授权："
                f"{spec['describe']}。"
                + (f"需要 {spec['authorize']} 授权。" if spec["authorize"] != "true" else "")
                + ("这一步失败可以补偿。" if spec["compensate"] else
                   "⚠️ 这一步**没有声明补偿**，做了就退不回来。")
                + " 批准用 kd_saga(action='authorize', run_id=…, by='姓名')。")
        elif st is RunState.COMPENSATION_FAILED:
            out["tip"] = ("⚠️ 补偿失败——系统试图收拾但没收拾干净，"
                          "left_behind 里的对象**必须人工处理**。")
        elif st is RunState.HALTED:
            undeclared = [s["index"] + 1 for s in run.steps
                          if s["is_write"] and not s["compensate"]]
            out["tip"] = (run.error or "已停止。") + (
                f" 第 {undeclared} 步没有声明补偿，遗留物需人工处理。"
                if undeclared and left else "")
        elif st is RunState.COMPENSATED:
            out["tip"] = "失败后已按逆序补偿干净，没有遗留。"
        elif st is RunState.DONE:
            out["tip"] = f"『{run.zh}』全部 {len(run.steps)} 步完成。"
        return out
