# KingdeeMCP — 操作原子化审计 + Ontology 建模

对 [`WaHaiLong/KingdeeMCP`](https://github.com/WaHaiLong/KingdeeMCP) @ `2c44e6f` 的一次独立审计，
从**操作原子性**切入，用**名词 / 动词 / 状态 / 链接 / 规则**五元本体把 97 个 MCP 工具
重新组织成一个可推理、可校验的领域模型。

## 结论摘要

这套 MCP 把金蝶的动词包装得很完整，但没有把**事务边界**表达出来：
97 个工具没有一个声明自己是「全成功或全失败」还是「逐条、可能部分成功」。
于是 3 个"一站式"复合工具成了**无补偿的 Saga**——中途失败时副作用已落库，
代码只返回一段 `recovery_hint` 文本，把回滚责任交给 LLM 的自觉；
而这 3 个工具恰好又完全落在 harness 约束层的覆盖盲区里。

29 项发现：高 8 / 中 19 / 低 1，另有 1 项需在真实账套实证。

**16 项已修复、2 项已缓解**（见[修复状态](00-atomicity-audit.md#05-修复状态)）——
自动审计从 **15 项发现 / 7 项 error** 降到 **5 项 / 0 项 error**，
约束层覆盖率从 **25% 提到 100%**。

## 文档

| 文件 | 内容 |
|---|---|
| [`00-atomicity-audit.md`](00-atomicity-audit.md) | **审计报告正文**——发现、证据行号、修复方向、处置顺序 |
| [`01-ontology-abstract.md`](01-ontology-abstract.md) | 抽象层：五元的元模型与判定标准 |
| [`02-ontology-instances.md`](02-ontology-instances.md) | 实例层：从代码抽取的 48 名词 / 20 动词 / 10 状态 / 13 链接 |
| [`03-operation-audit-record.md`](03-operation-audit-record.md) | **过程操作审计记录规范** + 接入方式 |
| [`04-audit-trail.md`](04-audit-trail.md) | 审计过程记录：方法、证据链、边界、审计器自身的盲区 |
| [`05-architecture.md`](05-architecture.md) | **三层架构**：MCP 底座 / Skill 实例层 / WikiSkill 自优化层 |
| [`model/`](model/) | 机器可读本体：`nouns` `verbs` `states` `links` `rules` + 实例快照 |
| [`samples/`](samples/) | 审计记录样本 JSONL |

## 落地：三层架构

审计只说问题，[`05-architecture.md`](05-architecture.md) 给出重构方案并已实现：

| 层 | 位置 | 解决什么 |
|---|---|---|
| MCP 底座 | `base/` | 97 工具 → 7 通用工具，**tools/list 从 ~45,873 token 降到 ~1,171（-97%）**；契约随结果返回；前置规则在发请求前拦截 |
| Skill 实例层 | `skill/` `profiles/` | 用法知识渐进披露；**各家二开差异写在租户覆盖层**，业务人员用中文定义业务操作入口 |
| WikiSkill 自优化 | `wikiskill/` | 每日回溯审计记录 → 跨天印证才浮上来 → 人 adopt 后落地 |

```bash
python3 tools/ontology/measure_tool_surface.py --both   # 复现 token 账
python3 -m kingdee_ontology.base.server                                  # 启动底座
python3 -m kingdee_ontology.base.validate_profile example-tenant         # 校验租户配置
python3 -m kingdee_ontology.wikiskill.retro                              # 每日回溯
```

## 工具

```bash
# 从 server.py 抽取本体实例（AST 静态分析，不导入模块、不触发登录）
python3 tools/ontology/extract_ontology.py           # 打印摘要
python3 tools/ontology/extract_ontology.py --write   # 写出 model/instances.snapshot.json

# 操作原子化审计（7 项检查，退出码 1 = 存在 error 级发现，可直接进 CI）
python3 tools/ontology/audit_atomicity.py
python3 tools/ontology/audit_atomicity.py --json

# 过程操作审计记录器 —— 悬挂操作链检测
python3 tools/ontology/operation_audit.py docs/ontology/samples/operation_audit_record.sample.jsonl
python3 -m pytest tests/test_operation_audit.py       # 自测 4 项
```

## 审计范围与边界

- ✅ 静态分析全量代码（`server.py` 7331 行 + `harness/` 639 行）
- ❌ **未连接真实金蝶账套**，未做任何运行期验证

依赖服务端实际行为的结论只有一条（L-2：`PRD_PickMtrl → PRD_Instock` 是否为有效转换关系），
已明确标注「存疑」并写明验证方法。其余发现均可由本仓库代码本身证实。

## 与上游的关系

本目录与 `tools/ontology/` 是新增内容，**未修改上游任何代码**。
上游项目的原始说明见仓库根目录 [`README.md`](../../README.md)。
