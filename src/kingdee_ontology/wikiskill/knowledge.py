"""知识条目 —— WikiSkill 的存储层。

「wiki」的关键不是"生成一份报告"，而是**同一条知识被反复印证后不断增强**：
每次回溯不是重写，而是把新证据并进已有条目，更新出现次数、覆盖天数与置信度。
一条只出现过一次的观察是噪声；跨 3 天出现 10 次的才值得改代码。

条目状态由人决定（open → adopted / rejected / wontfix），机器只负责积累证据。
被 rejected 的条目不会复活成新条目——否则每天都会重新提一遍同样的建议。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = "1.0"
DEFAULT_STORE = Path("wikiskill/knowledge.json")

# 置信度阶梯：出现次数 × 覆盖天数。刻意保守——单日刷屏不该等同于长期规律。
_CONFIDENCE = [
    (10, 3, "high"),      # ≥10 次且跨 ≥3 天
    (5, 2, "medium"),
    (2, 1, "low"),
]


@dataclass
class Entry:
    id: str                       # 稳定标识，同一现象每次回溯必须算出同一个 id
    kind: str                     # failure_pattern | dangling | unlinked_push | flaky | slow
    title: str
    detail: str
    suggestion: str               # 建议的动作，必须具体到"改哪个文件的哪一段"
    occurrences: int = 0
    days: list[str] = field(default_factory=list)     # 观察到的日期（去重）
    first_seen: str = ""
    last_seen: str = ""
    evidence: list[dict] = field(default_factory=list)  # 最近若干条原始证据
    status: str = "open"          # open | adopted | rejected | wontfix
    note: str = ""                # 人写的处置说明
    schema_version: str = SCHEMA_VERSION

    @property
    def confidence(self) -> str:
        for min_n, min_d, level in _CONFIDENCE:
            if self.occurrences >= min_n and len(self.days) >= min_d:
                return level
        return "noise"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["confidence"] = self.confidence
        return d


class Knowledge:
    def __init__(self, path: Path | str = DEFAULT_STORE):
        self.path = Path(path)
        self.entries: dict[str, Entry] = {}
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for e in raw.get("entries", []):
                e.pop("confidence", None)
                self.entries[e["id"]] = Entry(**e)

    def merge(self, obs: Entry, day: Optional[str] = None) -> tuple[Entry, str]:
        """并入一条观察。返回 (条目, 动作)，动作 ∈ new / reinforced / skipped。"""
        day = day or date.today().isoformat()
        cur = self.entries.get(obs.id)
        if cur is None:
            obs.first_seen = obs.last_seen = day
            obs.days = [day]
            obs.evidence = obs.evidence[:5]
            self.entries[obs.id] = obs
            return obs, "new"
        if cur.status in ("rejected", "wontfix"):
            # 人已经明确不处理，只累计计数，不再刷屏
            cur.occurrences += obs.occurrences
            cur.last_seen = day
            if day not in cur.days:
                cur.days.append(day)
            return cur, "skipped"
        cur.occurrences += obs.occurrences
        cur.last_seen = day
        if day not in cur.days:
            cur.days.append(day)
        # 证据只留最近 5 条，避免知识库无限膨胀
        cur.evidence = (obs.evidence + cur.evidence)[:5]
        cur.detail = obs.detail          # 细节用最新的（含最新数字）
        return cur, "reinforced"

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": date.today().isoformat(),
            "entries": [e.to_dict() for e in sorted(
                self.entries.values(),
                key=lambda x: (-x.occurrences, x.id))],
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    def actionable(self, min_confidence: str = "medium") -> list[Entry]:
        order = {"noise": 0, "low": 1, "medium": 2, "high": 3}
        floor = order[min_confidence]
        return [e for e in self.entries.values()
                if e.status == "open" and order[e.confidence] >= floor]

    def set_status(self, entry_id: str, status: str, note: str = "") -> Entry:
        if entry_id not in self.entries:
            raise KeyError(f"没有这条知识：{entry_id}")
        if status not in ("open", "adopted", "rejected", "wontfix"):
            raise ValueError(f"未知状态 {status!r}")
        e = self.entries[entry_id]
        e.status, e.note = status, note
        return e
