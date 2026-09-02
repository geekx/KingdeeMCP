"""AIP Logic —— 判断层：声明式、以本体为参数、纯函数的逻辑。

只回答「按本体，这样做成不成立」，不执行任何动作、不发任何请求。
"""
from aip.decide import ALLOW, BLOCK, INFO, WARN, Decision, Reason, merge
from aip.logic import (
    NEEDS_OPERATION_CODE, REGISTRY, Facts, LogicFn, can, describe, evaluate, logic,
)

__all__ = ["ALLOW", "BLOCK", "INFO", "WARN", "Decision", "Reason", "merge",
           "NEEDS_OPERATION_CODE", "REGISTRY", "Facts", "LogicFn",
           "can", "describe", "evaluate", "logic"]
