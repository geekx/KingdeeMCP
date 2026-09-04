"""本地凭据文件——跨 harness 的配置入口。

驱动这套 MCP 的可能是 Claude Code、也可能是别的 agent harness（Workbuddy、
基于 DeepSeek 的 agent……）。这些 harness 给 MCP 服务端注入环境变量的方式
并不统一：有的有专门的 UI 填 env，有的只会 `python3 -m xxx` 起个子进程，
env 完全继承自它自己的 shell。指望"每个 harness 都替你把 KINGDEE_PASSWORD
设进环境变量"是不现实的。

所以配置也可以放进一个**本地文件**，服务端自己读、自己注入 `os.environ`——
不管是谁在起这个进程，只要 cwd 对、文件在，就能读到。这个文件已经在
`.gitignore` 里（凭据不能进版本库）。

**只补空缺，不覆盖已有环境变量**：谁的 MCP 客户端已经用自己的方式注入了
`KINGDEE_PASSWORD`，这份文件不该把它覆盖掉——文件是兜底，不是真理来源。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# 显式指定优先；其次是当前目录（项目内开发/该项目就是工作目录的场景）；
# 最后是用户主目录下的一份全局配置——harness 的 cwd 未必是这个仓库，
# 但账号密码往往就那一套，放主目录一份，换到哪个项目都能用。
_ENV_VAR = "KINGDEE_ENV_FILE"
_LOCAL_NAME = ".env"
_HOME_NAME = ".kingdee-mcp.env"


def candidate_paths(explicit: Optional[str] = None) -> list[Path]:
    out: list[Path] = []
    e = explicit if explicit is not None else os.environ.get(_ENV_VAR)
    if e:
        out.append(Path(e).expanduser())
    out.append(Path.cwd() / _LOCAL_NAME)
    out.append(Path.home() / _HOME_NAME)
    return out


def parse_env_text(text: str) -> dict[str, str]:
    """`KEY=VALUE`，一行一条。刻意只做这么多：

    - `#` 开头或空行跳过
    - 值两侧的一对单引号/双引号会被剥掉（方便塞含空格或 `#` 的密码）
    - **不做**变量展开、`export` 前缀、多行值——这些是真正的 dotenv 规范才有的
      东西，这里只需要 4-6 个已知的 KINGDEE_* 键，写复杂了反而更容易读错。
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def load_env_file(explicit: Optional[str] = None, override: bool = False) -> Optional[Path]:
    """找第一个存在的候选文件，把它的键值补进 `os.environ`。

    必须在任何 `os.environ.get("KINGDEE_*")` 读取之前调用——这也是为什么它
    不能只在某一个入口调用一次：`kingdee_mcp/server.py` 和
    `kingdee_ontology/base/server.py` 各自在模块顶层就读了好几个 KINGDEE_*
    常量，两边都要在读之前调这个函数。
    """
    for p in candidate_paths(explicit):
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for k, v in parse_env_text(text).items():
            if override or k not in os.environ:
                os.environ[k] = v
        return p
    return None
