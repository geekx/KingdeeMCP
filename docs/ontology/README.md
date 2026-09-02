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
其中 15 项可由 `tools/ontology/audit_atomicity.py` 自动检出并直接接入 CI。

## 文档

| 文件 | 内容 |
|---|---|
| [`00-atomicity-audit.md`](00-atomicity-audit.md) | **审计报告正文**——发现、证据行号、修复方向、处置顺序 |
| [`01-ontology-abstract.md`](01-ontology-abstract.md) | 抽象层：五元的元模型与判定标准 |
| [`02-ontology-instances.md`](02-ontology-instances.md) | 实例层：从代码抽取的 48 名词 / 20 动词 / 10 状态 / 13 链接 |
| [`03-operation-audit-record.md`](03-operation-audit-record.md) | **过程操作审计记录规范** + 接入方式 |
| [`04-audit-trail.md`](04-audit-trail.md) | 审计过程记录：方法、证据链、边界、审计器自身的盲区 |
| [`model/`](model/) | 机器可读本体：`nouns` `verbs` `states` `links` `rules` + 实例快照 |
| [`samples/`](samples/) | 审计记录样本 JSONL |

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
python3 tools/ontology/test_operation_audit.py       # 自测 4 项
```

## 审计范围与边界

- ✅ 静态分析全量代码（`server.py` 7331 行 + `harness/` 639 行）
- ❌ **未连接真实金蝶账套**，未做任何运行期验证

依赖服务端实际行为的结论只有一条（L-2：`PRD_PickMtrl → PRD_Instock` 是否为有效转换关系），
已明确标注「存疑」并写明验证方法。其余发现均可由本仓库代码本身证实。

## 与上游的关系

本目录与 `tools/ontology/` 是新增内容，**未修改上游任何代码**。
上游项目的原始说明见仓库根目录 [`README.md`](../../README.md)。
