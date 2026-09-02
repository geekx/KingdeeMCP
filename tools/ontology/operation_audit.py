#!/usr/bin/env python3
"""过程操作审计记录（Operation Audit Record）参考实现。

解决审计报告 P-1..P-5：现有 usage_log 只记录「调用了哪个工具、耗时多久」，
无法回答审计真正要问的问题——

    谁(actor) 在什么时候(occurred_at) 对哪个对象(noun+object_id)
    执行了什么动作(verb) 使它从什么状态到什么状态(state_from→state_to)
    这一步属于哪一次业务操作(trace_id / step)
    结果是什么(outcome) 失败的话原因是什么(error)
    以及——它是否被补偿过(compensated_by)

设计要点：
  1. 记录的主语是「单据」不是「工具」：一次调用影响 N 张单据就写 N 条记录。
  2. trace_id 贯穿复合动词：一次 create_and_audit 写 3 条同 trace_id 的记录，
     可以拼回完整操作链，也能定位「停在哪一步」。
  3. 追加写 + fsync + 行锁，失败不静默：审计记录丢失必须可感知。
  4. 只记录事实，不记录推测：state_to 在服务端未确认时写 null 而非乐观值。

用法（在 server.py 的写工具里）：

    from tools.ontology.operation_audit import audit_recorder, OperationAuditRecord

    with audit_recorder.operation("create_and_audit", actor=current_user()) as op:
        ...
        op.step(verb="Save", noun="PUR_PurchaseOrder", object_id=fid,
                object_no=bill_no, state_from="不存在", state_to="Z:暂存",
                outcome="success")
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA_VERSION = "1.0"

# 结果取值域：闭集，禁止自由文本
OUTCOMES = ("success", "failed", "partial", "unknown")
# unknown = 请求已发出但响应不可判定（超时/连接中断）——审计上必须与 failed 区分，
# 因为 unknown 意味着服务端可能已经生效，重试前必须先查证。


@dataclass
class OperationAuditRecord:
    """一条过程操作审计记录 = 一个对象上的一次状态迁移尝试。"""

    trace_id: str                       # 一次业务操作（可跨多个 MCP 调用步骤）
    step: int                           # 该 trace 内的步序，从 1 开始
    occurred_at: str                    # RFC3339 UTC
    actor: str                          # 操作人（金蝶登录账号），非 MCP 进程
    on_behalf_of: Optional[str]          # 发起该操作的 Agent/会话标识
    tool: str                           # MCP 工具名
    verb: str                           # 本体动词：Save/Submit/Audit/Push/...
    endpoint: str                       # 实际打到的 WebAPI 端点
    noun: str                           # 本体名词：form_id，如 PUR_PurchaseOrder
    object_id: Optional[str]             # FID
    object_no: Optional[str]             # FBillNo
    state_from: Optional[str]            # 迁移前状态（规范码，见 states.yml）
    state_to: Optional[str]              # 迁移后状态；服务端未确认时为 None
    outcome: str                        # OUTCOMES 之一
    duration_ms: float
    request_digest: str                 # 请求体摘要（脱敏后，用于重放比对）
    error: Optional[dict] = None         # {code, message, field}
    compensated_by: Optional[str] = None  # 补偿它的 trace_id（回滚/反审核/删除）
    schema_version: str = SCHEMA_VERSION
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"outcome 必须是 {OUTCOMES} 之一，得到 {self.outcome!r}")
        if self.step < 1:
            raise ValueError("step 从 1 开始")


class AuditSink:
    """JSONL 落盘。追加写 + fsync；写失败抛出，绝不静默吞掉。"""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, rec: OperationAuditRecord) -> None:
        line = json.dumps(asdict(rec), ensure_ascii=False) + "\n"
        with self._lock:
            # 'a' 模式下单次 write 小于 PIPE_BUF 时原子；再配合进程内锁避免交错
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())


class _Operation:
    """一次业务操作的记录上下文，负责分配 trace_id 与步序。"""

    def __init__(self, recorder: "AuditRecorder", tool: str,
                 actor: str, on_behalf_of: Optional[str]) -> None:
        self.trace_id = uuid.uuid4().hex
        self._recorder = recorder
        self._tool = tool
        self._actor = actor
        self._on_behalf_of = on_behalf_of
        self._step = 0
        self._t0 = time.perf_counter()
        self.records: list[OperationAuditRecord] = []

    def step(self, *, verb: str, noun: str, endpoint: str = "",
             object_id: Optional[str] = None, object_no: Optional[str] = None,
             state_from: Optional[str] = None, state_to: Optional[str] = None,
             outcome: str = "success", request_digest: str = "",
             error: Optional[dict] = None, **extra: Any) -> OperationAuditRecord:
        self._step += 1
        rec = OperationAuditRecord(
            trace_id=self.trace_id,
            step=self._step,
            occurred_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            actor=self._actor,
            on_behalf_of=self._on_behalf_of,
            tool=self._tool,
            verb=verb,
            endpoint=endpoint,
            noun=noun,
            object_id=str(object_id) if object_id is not None else None,
            object_no=object_no,
            state_from=state_from,
            state_to=state_to,
            outcome=outcome,
            duration_ms=round((time.perf_counter() - self._t0) * 1000, 2),
            request_digest=request_digest,
            error=error,
            extra=extra,
        )
        self._recorder.sink.write(rec)
        self.records.append(rec)
        return rec

    @property
    def halted_at(self) -> Optional[int]:
        """返回第一条非 success 记录的步序；全部成功返回 None。"""
        for r in self.records:
            if r.outcome != "success":
                return r.step
        return None


class AuditRecorder:
    def __init__(self, path: Optional[str] = None) -> None:
        self.sink = AuditSink(path or os.environ.get(
            "KINGDEE_OPERATION_AUDIT_LOG", "operation_audit.jsonl"))

    @contextmanager
    def operation(self, tool: str, *, actor: str,
                  on_behalf_of: Optional[str] = None) -> Iterator[_Operation]:
        op = _Operation(self, tool, actor, on_behalf_of)
        try:
            yield op
        except BaseException as exc:
            # 异常逃逸也要留痕：请求可能已经在服务端生效
            op.step(verb="Unknown", noun="-", outcome="unknown",
                    error={"code": type(exc).__name__, "message": str(exc)[:300]})
            raise


audit_recorder = AuditRecorder()


# ── 事后审计查询 ────────────────────────────────────────────────
def load(path: str | os.PathLike[str]) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _identify(records: list[dict]) -> list[str]:
    """把同一对象的多条记录归并成一个标识；同一 FID 出现过单据编号则优先显示编号。"""
    seen: dict[tuple, Optional[str]] = {}
    for r in records:
        oid, ono = r.get("object_id"), r.get("object_no")
        if not oid and not ono:
            continue
        key = (r["noun"], oid or ono)
        seen[key] = seen.get(key) or ono
    # 同一张单可能在一步里只留了 FID、另一步只留了单据编号；
    # 补全编号后按最终标识去重，避免同一对象被数成两个。
    return sorted({f"{noun}:{label or ident}" for (noun, ident), label in seen.items()})


def dangling_traces(records: list[dict]) -> list[dict]:
    """找出停在中间态的操作链：有写动作成功、但整条链未走到终态且无补偿。

    这是复合动词无补偿（AT-01）在运行期的可观测表现。
    """
    TERMINAL = {"C:已审核", "已删除", "已作废", "已关闭"}
    by_trace: dict[str, list[dict]] = {}
    for r in records:
        by_trace.setdefault(r["trace_id"], []).append(r)

    out = []
    for tid, rs in by_trace.items():
        rs.sort(key=lambda r: r["step"])
        # 写步骤：任何非只读动词。失败的写步骤也要计入 left_objects——
        # 它作用的对象（如 push 已生成的目标单草稿）依然遗留在系统里。
        wrote = [r for r in rs
                 if r["verb"] not in ("Query", "Read", "Validate", "Discover", "Unknown")]
        if not any(r["outcome"] in ("success", "partial", "unknown") for r in wrote):
            continue
        last = rs[-1]
        if last["outcome"] == "success" and last.get("state_to") in TERMINAL:
            continue
        if any(r.get("compensated_by") for r in rs):
            continue
        out.append({
            "trace_id": tid,
            "tool": rs[0]["tool"],
            "halted_at_step": next((r["step"] for r in rs if r["outcome"] != "success"),
                                   len(rs)),
            "left_objects": _identify(wrote),
            "last_state": last.get("state_to"),
            "last_outcome": last["outcome"],
        })
    return out


if __name__ == "__main__":
    import sys
    recs = load(sys.argv[1] if len(sys.argv) > 1 else "operation_audit.jsonl")
    print(f"读入 {len(recs)} 条审计记录")
    for d in dangling_traces(recs):
        print(f"  [悬挂操作链] {d['trace_id'][:8]} {d['tool']} "
              f"停在第 {d['halted_at_step']} 步，遗留 {d['left_objects']}，"
              f"末状态={d['last_state']} ({d['last_outcome']})")
