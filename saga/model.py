"""Saga 定义与运行状态。

「多扣扳机」不是把几个动作排成一列按顺序打，那只是把风险串起来。
参照 12306 那条链——查询 → 付款状态 → 扣库存 → 出单——真正需要的是四件事：

  守卫  每一步开跑前先验条件（有没有货、单是不是已审核）。
        没有守卫的顺序执行只是"盲发"，失败时才发现前提早就不成立。
  授权  某些步骤要人点头。而且是**逐步授权**，不是开头点一次就全权委托：
        「生成开票申请」和「过账应收」的风险完全不同，凭什么一次确认覆盖两者。
  补偿  任一步失败，已生效的前序步骤按**逆序**补偿掉。
        补偿动作必须**显式声明**，不能靠"逆动词"推——
        push 的逆不是"unpush"（不存在），而是删除下游单据。
  留痕  谁在什么时候授权了哪一步、补偿做没做成，全部落审计。

运行状态必须**持久化**：人工授权发生在带外（可能隔几分钟、换个人、换个会话），
不落盘就意味着一断就丢，已生效的写操作变成无人认领的中间态。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class RunState(str, Enum):
    PENDING = "pending"                    # 已创建，未开跑
    AWAITING_AUTH = "awaiting_auth"        # 停在某一步，等人授权
    RUNNING = "running"
    DONE = "done"
    HALTED = "halted"                      # 失败且**未**补偿（补偿未声明或被拒）
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"            # 失败但已按逆序补偿干净
    COMPENSATION_FAILED = "compensation_failed"   # 最坏情况：必须人来收拾


# 终态。COMPENSATION_FAILED 也算"跑完了"，但它是最需要人看的一种。
TERMINAL = {RunState.DONE, RunState.HALTED, RunState.COMPENSATED,
            RunState.COMPENSATION_FAILED}


class StepKind(str, Enum):
    PUSH = "下推"
    CONFIRM = "确认"
    CHECK = "检查"          # 守卫：查一下，条件不满足就不往下走
    VERB = "verb"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class StepSpec:
    """一个子任务的声明。"""
    index: int
    kind: str                              # 下推 / 确认 / 检查 / 具体动词
    raw: dict                              # 原始 YAML 步骤，保留以便回显
    # 三个可选项，正是「多扣扳机」区别于「顺序执行」的地方
    authorize: Optional[str] = None        # None=不需要；"true"=任何人；其它=需要该角色
    compensate: Optional[str] = None       # 显式补偿动词；None=此步不可补偿
    note: str = ""

    @property
    def needs_auth(self) -> bool:
        return self.authorize is not None

    @property
    def is_write(self) -> bool:
        return self.kind not in (StepKind.CONFIRM.value, StepKind.CHECK.value)

    def describe(self) -> str:
        r = self.raw
        if self.kind == StepKind.PUSH.value:
            return f"下推：{r.get('从')} → {r.get('到')}"
        if self.kind == StepKind.CONFIRM.value:
            return f"确认：{r.get('问', '')}"
        if self.kind == StepKind.CHECK.value:
            return f"检查：{r.get('对象')} 满足 {r.get('条件')}"
        return f"{self.kind}：对 {r.get('对象')}（{r.get('用', 'targets')}）"

    def to_dict(self) -> dict:
        # raw 必须序列化进去：运行状态要落盘、跨会话续跑，执行器只能从
        # 存下来的 dict 里拿参数。漏掉它的话所有步骤都会拿到空参数——
        # 而且症状很误导（守卫报 "NoneType is not iterable"）。
        return {"index": self.index, "kind": self.kind, "describe": self.describe(),
                "raw": dict(self.raw),
                "needs_auth": self.needs_auth, "authorize": self.authorize,
                "compensate": self.compensate, "note": self.note,
                "is_write": self.is_write}


@dataclass
class StepResult:
    index: int
    kind: str
    outcome: str                           # success / failed / skipped / unknown
    detail: dict = field(default_factory=dict)
    produced: list[str] = field(default_factory=list)   # 本步生成的对象标识
    noun: str = ""
    at: str = field(default_factory=_now)
    authorized_by: Optional[str] = None
    authorized_at: Optional[str] = None
    compensated: Optional[str] = None      # success / failed / not_declared / not_needed
    compensation_detail: dict = field(default_factory=dict)


@dataclass
class SagaRun:
    run_id: str
    operation: str
    zh: str
    targets: list[str]
    steps: list[dict]                      # StepSpec.to_dict()
    state: str = RunState.PENDING.value
    cursor: int = 0                        # 下一个要执行的步骤序号（0 基）
    results: list[dict] = field(default_factory=list)
    trace_id: str = ""
    actor: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    error: str = ""

    @staticmethod
    def new(operation: str, zh: str, targets: list[str],
            steps: list[StepSpec], actor: str = "") -> "SagaRun":
        return SagaRun(run_id=uuid.uuid4().hex[:12], operation=operation, zh=zh,
                       targets=list(targets), steps=[s.to_dict() for s in steps],
                       actor=actor)

    @property
    def is_terminal(self) -> bool:
        return RunState(self.state) in TERMINAL

    def produced_objects(self) -> list[dict]:
        """已经生成、还没被补偿掉的对象。

        按 (对象类型, 标识) 去重：同一张单会被多个步骤"产出"——
        push 生成它，随后的 submit/audit 也会把它算进 succeeded。
        重复列出会让人以为遗留了好几张。保留最早的那一步，
        因为补偿要从那里开始退。
        """
        seen: set[tuple] = set()
        out = []
        for r in self.results:
            if r.get("outcome") != "success" or r.get("compensated") == "success":
                continue
            for oid in r.get("produced") or []:
                key = (r.get("noun", ""), str(oid))
                if key in seen:
                    continue
                seen.add(key)
                out.append({"noun": r.get("noun", ""), "id": oid, "step": r["index"]})
        return out

    def touch(self, state: Optional[str] = None) -> None:
        if state:
            self.state = state
        self.updated_at = _now()

    def to_dict(self) -> dict:
        return asdict(self)


class RunStore:
    """Saga 运行状态的持久化。

    人工授权发生在带外，一次运行可能横跨很长时间与多个会话。
    不落盘 = 一断就丢，已生效的写操作变成无人认领的中间态。
    """

    def __init__(self, path: str | Path = "saga/runs.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, run: SagaRun) -> None:
        runs = {r["run_id"]: r for r in self.load_all()}
        run.touch()
        runs[run.run_id] = run.to_dict()
        self.path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in runs.values()) + "\n",
            encoding="utf-8")

    def load_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(l) for l in self.path.read_text(encoding="utf-8").splitlines()
                if l.strip()]

    def get(self, run_id: str) -> Optional[SagaRun]:
        for r in self.load_all():
            if r["run_id"] == run_id:
                return SagaRun(**r)
        return None

    def pending_auth(self) -> list[dict]:
        """等着人授权的运行——这类最容易被忘掉，要能一眼列出来。"""
        return [r for r in self.load_all()
                if r["state"] == RunState.AWAITING_AUTH.value]

    def unresolved(self) -> list[dict]:
        """没走到干净终态的运行：等授权的、停住的、补偿失败的。"""
        bad = {RunState.AWAITING_AUTH.value, RunState.HALTED.value,
               RunState.COMPENSATING.value, RunState.COMPENSATION_FAILED.value,
               RunState.RUNNING.value}
        return [r for r in self.load_all() if r["state"] in bad]
