"""
failure_log.py — 失败日志记录器

由 server.py 在操作失败时调用，记录到 examples/failure_log.jsonl。
格式：
{"timestamp": "...", "op": "...", "error_type": "...", "message": "...", "context": {...}, "resolved": false}
"""

import json
import os
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Optional

# 失败日志文件路径。
# 默认写在仓库内的 examples/ 下，但那份文件是**被 git 追踪的示例数据**——
# 测试或本地跑一次就会往里追加，污染工作区并可能被误提交。
# 因此允许用环境变量改写；测试与 CI 应指向临时目录。
FAILURE_LOG_PATH = os.environ.get("KINGDEE_FAILURE_LOG") or os.path.join(
    os.path.dirname(__file__), "..", "examples", "failure_log.jsonl"
)


@dataclass
class FailureEntry:
    timestamp: str
    op: str
    error_type: str
    message: str
    context: dict
    resolved: bool = False
    resolution: Optional[str] = None
    pattern_id: Optional[str] = None


class FailureLogger:
    """失败日志记录器（延迟导入避免循环依赖）"""

    def __init__(self, log_path: str = FAILURE_LOG_PATH):
        self.log_path = log_path

    def log(self, op: str, result: dict, error_info: dict = None) -> None:
        """记录操作失败到日志文件

        Args:
            op: 操作类型（save/submit/audit/push 等）
            result: _err() 返回的完整结果（包含 errors 列表）
            error_info: 可选的额外上下文信息
        """
        errors = result.get("errors", []) or []
        if not errors and not result.get("error"):
            return

        for err in (errors if errors else [{"type": "unknown", "message": str(result)}]):
            entry = FailureEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                op=op,
                error_type=err.get("type", "unknown"),
                message=err.get("message", err.get("raw", "")),
                context={
                    "op": op,
                    "form_id": result.get("form_id", ""),
                    "fid": result.get("fid", result.get("bill_id", "")),
                    "matched_reason": err.get("matched", {}).get("reason", ""),
                    "extra": error_info or {},
                },
                resolved=False,
            )
            self._append(entry)

    def _append(self, entry: FailureEntry) -> None:
        """追加到日志文件"""
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")