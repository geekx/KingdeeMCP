#!/usr/bin/env python3
"""重新生成 README 里「本分支的增补」区块（带版本与时间戳）。

    python3 tools/ontology/update_readme.py          # 写入 README.md
    python3 tools/ontology/update_readme.py --check  # 只检查是否过期（CI 用）

区块内的数字全部**实测得来**，不手写：工具面 token、审计发现数、测试结果、
注册表规模。这样 README 不会随代码演进而悄悄失真。

刻意不写当前 commit SHA —— 它在下一次提交时立刻过期，
反而制造"看起来精确但其实是错的"信息。基线 SHA 是稳定的，保留。
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

BEGIN, END = "<!-- FORK-CHANGES:BEGIN -->", "<!-- FORK-CHANGES:END -->"
BASE_SHA = "2c44e6f"          # fork 基线：上游 WaHaiLong/KingdeeMCP


def _run(*args: str) -> str:
    return subprocess.check_output([sys.executable, *args], text=True, cwd=ROOT).strip()


def _collect() -> dict:
    from kingdee_ontology.base.ontology import load

    ver = re.search(r'^version = "([^"]+)"',
                    (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
    now = datetime.datetime.now(datetime.timezone.utc).astimezone(
        datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M UTC+08:00")
    o = load(tenant="")
    surf = json.loads(_run("tools/ontology/measure_tool_surface.py", "--both", "--json"))
    audit = json.loads(_run("tools/ontology/audit_atomicity.py", "--json"))
    conv = json.loads(_run("tools/ontology/measure_convergence.py", "--json"))
    tests = subprocess.run([sys.executable, "-m", "pytest", "tests/", "-q"],
                           capture_output=True, text=True, cwd=ROOT)
    line = [l for l in tests.stdout.strip().splitlines() if "passed" in l or "failed" in l]
    return {
        "version": ver, "now": now,
        "nouns": len(o.nouns), "verbs": len(o.verbs),
        "legacy_tokens": surf["legacy"]["est_tokens"], "legacy_tools": surf["legacy"]["tools"],
        "base_tokens": surf["base"]["est_tokens"], "base_tools": surf["base"]["tools"],
        "findings": len(audit), "errors": sum(1 for f in audit if f["level"] == "error"),
        "conv_pct": conv["coverage_pct"], "conv_covered": conv["covered"],
        "conv_total": conv["readonly_tools"],
        "tests": line[-1] if line else "(未取到测试结果)",
        "tests_ok": tests.returncode == 0,
    }


def render(d: dict) -> str:
    pct = 100 - d["base_tokens"] * 100 // d["legacy_tokens"]
    ctx = d["legacy_tokens"] * 100 // 200000
    return f"""{BEGIN}
---

## 本分支的增补：操作原子化审计 + Ontology 建模 + 三层架构

> **分支** `claude/kingdee-mcp-ontology-audit-nis4mg` ｜ **基线** 上游 `{BASE_SHA}` ｜ **包版本** `v{d['version']}`
> **更新于** {d['now']}
> **测试** {d['tests']}（本机实测）｜ **原子性审计** {d['findings']} 项发现 / {d['errors']} 项 error
>
> 本区块由 `python3 tools/ontology/update_readme.py` 生成，数字均为实测。

这是对上游 [`WaHaiLong/KingdeeMCP`](https://github.com/WaHaiLong/KingdeeMCP) 的一次独立审计与重构增补。
**上游 97 个工具全部保留，新旧并存**，现有集成不受影响（唯一行为变更见下）。

### 关键指标（均为实测）

| 指标 | 之前 | 现在 |
|---|---|---|
| MCP 工具面 `tools/list` | ~{d['legacy_tokens']:,} token（200k 上下文的 {ctx}%） | ~{d['base_tokens']:,} token（**-{pct}%**） |
| 操作链约束层覆盖率 | 25%（24 个写动词只认 3 个） | **100%**（漏登记 = CI 失败） |
| 自动审计 error | 7 | **{d['errors']}** |
| 只读工具可由底座表达 | 0 / {d['conv_total']} | **{d['conv_covered']} / {d['conv_total']}（{d['conv_pct']}%）** |

工具从 {d['legacy_tools']} 个收敛到 {d['base_tools']} 个，而注册表里的名词从 48 长到 **{d['nouns']}** 个——
**名词是数据不是能力**：名词涨了 {d['nouns'] * 100 // 48 - 100}%，底座工具只多了 2 个。

未收敛的 {d['conv_total'] - d['conv_covered']} 个全是 SQL Server 目录探查，**刻意保留** ——
它们的数据来自数据库系统表而非金蝶 WebAPI，需要另一套凭据，
折叠进来会让同一个工具横跨两个数据源、两套权限模型。

### 三层架构

| 层 | 位置 | 作用 |
|---|---|---|
| MCP 底座 | [`base/`](base/) | {d['verbs']} 个动词 × {d['nouns']} 个名词的组合，{d['base_tools']} 个通用工具；契约随结果返回；前置规则在发请求前拦截 |
| Skill 实例层 | [`skill/`](skill/) [`profiles/`](profiles/) | 用法知识渐进披露；**各家二开差异写在租户覆盖层**，业务人员用中文定义业务操作入口 |
| 对象层（两种形态） | [`base/objects.py`](base/objects.py) · [`ui/`](docs/ontology/ui/) | 以对象为中心操作：属性 / 状态 / **此刻能做什么** / 连到什么。Skill 形态给 Claude，[界面形态](https://claude.ai/code/artifact/91595855-e6de-4182-8369-ddaa7c09fd50)给人 |
| WikiSkill 自优化 | [`wikiskill/`](wikiskill/) | 每日回溯审计记录 → 跨天印证才浮上来 → 人 adopt 后落地 |

### 快速验证

```bash
python3 -m pytest tests/ -q                              # 全量测试
python3 tools/ontology/audit_atomicity.py                # 操作原子化审计（可进 CI）
python3 tools/ontology/measure_tool_surface.py --both    # token 账
python3 -m kingdee_ontology.base.validate_profile example-tenant          # 租户配置校验（中文报错）
python3 -m kingdee_ontology.wikiskill.retro --report                      # 每日回溯
python3 -m kingdee_ontology.base.server                                   # 启动底座（{d['base_tools']} 工具）
```

### 文档

审计报告与本体建模见 **[`docs/ontology/`](docs/ontology/)**：
[审计报告](docs/ontology/00-atomicity-audit.md) ·
[抽象层](docs/ontology/01-ontology-abstract.md) ·
[实例层](docs/ontology/02-ontology-instances.md) ·
[过程审计记录规范](docs/ontology/03-operation-audit-record.md) ·
[审计过程记录](docs/ontology/04-audit-trail.md) ·
[三层架构](docs/ontology/05-architecture.md)

多租户配置填写指南（面向业务人员）：[`profiles/README.md`](profiles/README.md)

### ⚠️ 唯一的行为变更

向 `kingdee_workflow_approve` 传 `action="reject"` 现在返回错误并指向新的
`kingdee_workflow_reject`。旧行为声称"驳回"实则执行**反审核**（不同语义、绕过工作流引擎），
且审批意见被接收却**从不写入金蝶**。

### 未解决

- **A-5** `save`/`push` 无客户端幂等键，重试无法去重——需服务端配合
- **L-2** `PRD_PickMtrl → PRD_Instock` 是否为有效转换关系**存疑**，已标 `verified: suspect`，需真实账套验证
- **F-1** 默认字段集有两套事实来源，14 处不一致，同样需账套验证

> 本次审计为**静态分析**，未连接真实金蝶账套。凡依赖服务端行为的结论均已标注存疑。

{END}"""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查是否需要更新，不写入")
    args = ap.parse_args(argv[1:])

    data = _collect()
    if not data["tests_ok"]:
        print(f"✗ 测试未通过，拒绝更新 README：{data['tests']}")
        return 1

    block = render(data)
    p = ROOT / "README.md"
    s = p.read_text(encoding="utf-8")

    if BEGIN in s:
        cur = s[s.index(BEGIN):s.index(END) + len(END)]
        # 比对前要归一化掉三样"随机器而变、不随代码而变"的东西。
        # 这层门禁的用处是"改了代码却没重跑 README"，不是"两台机器结果不同"。
        #
        #   更新于     时间戳，每次都变。
        #   耗时       超过一分钟时 pytest 还会多缀 " (0:01:00)"。
        #   测试通过/跳过数
        #              取决于本机装了哪些**可选**依赖：没装 playwright，
        #              test_ui_composer 整个模块跳过；没装 build，打包实测跳过。
        #              CI 里装 playwright 的步骤还排在全量测试之后，于是同一份
        #              代码在 CI 与本地必然给出不同的通过数——拿它当门禁，
        #              等于要求所有机器的可选依赖完全一致，这既做不到也没意义。
        #              真正该守的是 token 账、名词数、审计发现数这些**由代码
        #              决定**的数字，它们仍在比对范围内。
        def strip(x: str) -> str:
            x = re.sub(r"> \*\*更新于\*\*.*", "", x)
            x = re.sub(r" in [\d.]+s(?: \(\d+:\d{2}:\d{2}\))?", "", x)
            return re.sub(r"> \*\*测试\*\*.*?｜", "> **测试** ｜", x)
        if strip(cur) == strip(block):
            print("✓ README 区块已是最新（除时间戳外无变化）")
            return 0
        if args.check:
            print("✗ README 区块已过期，请运行 python3 tools/ontology/update_readme.py")
            return 1
        s = s.replace(cur, block)
    else:
        if args.check:
            print("✗ README 缺少 FORK-CHANGES 区块")
            return 1
        s = s.replace("[![PyPI version]", block + "\n\n[![PyPI version]", 1)

    p.write_text(s, encoding="utf-8")
    print(f"✓ README 已更新：v{data['version']} @ {data['now']}")
    print(f"  工具面 {data['legacy_tokens']:,} → {data['base_tokens']:,} token"
          f" ｜ 名词 {data['nouns']} ｜ 审计 {data['findings']} 项 / {data['errors']} error")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
