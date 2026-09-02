"""判断的结果类型。

为什么不用 bool、也不用抛异常：

**bool 说不出「为什么不行」。** 一个灰掉却不解释的按钮比没有按钮更让人困惑，
对 agent 更是——它只会换个参数再试一次。

**异常只能报第一个问题。** `check_verb_applies` 抛出后，调用方改完再调，
撞上下一个问题，再改再调。三个前置条件就是三个来回。判断层一次把
**所有**理由给全，让调用方一轮修完。这条是这一层最实在的收益：
少一次来回，就少一次让模型重新思考的机会。

**「不知道」不是「可以」。** 旧的 `availability()` 在状态未知时返回
`{"enabled": True, "unverified": True}`——只读 enabled 的调用方看到 True
就往下走了。这里把 undetermined 拆成独立字段，且 `allowed` 在未定时为 False：
要放行必须显式接受不确定性，而不是没注意到它。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

BLOCK = "block"     # 不能做
WARN = "warn"       # 能做，但有代价（不可逆、要人点头）
INFO = "info"       # 只是说明
SEVERITIES = (BLOCK, WARN, INFO)


@dataclass(frozen=True)
class Reason:
    """一条理由。

    `fix` 不是可选的礼貌用语：说了「不行」却不说「那该怎么办」，
    调用方只能猜，而猜出来的下一步通常还是错的。
    """
    rule: str                   # 规则号，如 PRE-01 / AIP-04
    severity: str               # block | warn | info
    message: str                # 为什么
    fix: str = ""               # 怎么办

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"未知严重度 {self.severity!r}，只能是 {SEVERITIES}")

    def to_dict(self) -> dict:
        d = {"rule": self.rule, "severity": self.severity, "message": self.message}
        if self.fix:
            d["fix"] = self.fix
        return d

    @property
    def text(self) -> str:
        return f"{self.message}{(' ' + self.fix) if self.fix else ''}"


@dataclass(frozen=True)
class Decision:
    """一次判断的完整结果。

    三态而非两态：
      allowed=True                 可以做
      allowed=False, undetermined  缺输入，判不了——**不等于可以**
      allowed=False                明确不行，reasons 里有 block
    """
    reasons: tuple[Reason, ...] = ()
    undetermined: tuple[Reason, ...] = ()

    @property
    def blocks(self) -> tuple[Reason, ...]:
        return tuple(r for r in self.reasons if r.severity == BLOCK)

    @property
    def warnings(self) -> tuple[Reason, ...]:
        return tuple(r for r in self.reasons if r.severity == WARN)

    @property
    def allowed(self) -> bool:
        """没有 block、也没有悬而未决的输入，才算可以做。"""
        return not self.blocks and not self.undetermined

    @property
    def needs_confirmation(self) -> bool:
        """有 warn 级理由 = 能做，但要先让人点头。"""
        return bool(self.warnings)

    def why(self) -> str:
        """给人看的一句话。空字符串表示无话可说（畅通无阻）。"""
        return "；".join(r.text for r in self.blocks + self.undetermined + self.warnings)

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "undetermined": bool(self.undetermined),
            "needs_confirmation": self.needs_confirmation,
            "reasons": [r.to_dict() for r in self.reasons],
            "missing": [r.to_dict() for r in self.undetermined],
            "why": self.why(),
        }

    def __add__(self, other: "Decision") -> "Decision":
        return Decision(self.reasons + other.reasons,
                        self.undetermined + other.undetermined)


def merge(parts: Iterable[Decision]) -> Decision:
    out = Decision()
    for p in parts:
        out = out + p
    return out


ALLOW = Decision()
