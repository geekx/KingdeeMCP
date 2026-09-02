# 三层架构：MCP 底座 / Skill 实例层 / WikiSkill 自优化层

> 本文回应四条设计要求：
> ① MCP 作为底座，抽象出调用能力与状态这些五元的基座；
> ② 五元的实例通过 Skill 操作分离；
> ③ 引入 WikiSkill 理念，回溯每日工作做自优化；
> ④ 接口稳健、独立，避免过多 token 消耗；
> ⑤ 各家表单不同，需要面向人的业务操作入口定义，避免一种米养几种人。

## 0. 一张图

```
┌─────────────────────────────────────────────────────────────┐
│  WikiSkill 自优化层        wikiskill/                        │
│  每日回溯审计记录 → 累积知识 → 跨天印证才浮上来 → 人 adopt    │
└───────────────▲──────────────────────────┬──────────────────┘
                │ operation_audit.jsonl    │ 改注册表/配置/代码
                │                          ▼
┌───────────────┴─────────────────────────────────────────────┐
│  Skill 实例层              skill/ · profiles/                │
│  · kingdee-ontology  怎么用底座（渐进披露，SKILL.md 很小）    │
│  · profiles/<租户>/  这家长什么样（二开表单/操作码/链接）     │
│                      这家怎么干活（业务操作入口，中文）       │
└───────────────┬─────────────────────────────────────────────┘
                │ 注册表驱动，无需改代码
┌───────────────▼─────────────────────────────────────────────┐
│  MCP 底座                  base/                             │
│  7 个通用工具 = 14 动词 × N 名词的组合                        │
│  契约（原子性/幂等/逆动词）· 状态机 · 前置规则 · 审计记录     │
└─────────────────────────────────────────────────────────────┘
```

## 1. 为什么要拆：token 账

实测当前实现的工具面：

| | 工具数 | tools/list 体积 | 估算 token | 占 200k 上下文 |
|---|---|---|---|---|
| 原结构 | 97 | 174 KB | **~45,873** | 23% |
| 底座 | 7 | 3.3 KB | **~1,171** | 0.6% |
| | | | **-97.4%** | |

复现：

```bash
python3 tools/ontology/measure_tool_surface.py            # 原结构
python3 tools/ontology/measure_tool_surface.py --base     # 底座
```

45,873 token 是**每次会话开口之前就要付的固定成本**，其中 60% 是 97 份 `inputSchema`。
更糟的是它随业务增长线性上涨：每接一个新单据类型就多一个工具。

根因是**把实例当成了能力**。`kingdee_query_purchase_orders` 和
`kingdee_query_sale_orders` 不是两种能力，是同一种能力（查询）作用在两个名词上。
97 个工具其实只有 14 个动词。

## 2. 底座：能力是代码，实例是数据

`base/registry.yml` 是唯一事实来源：14 动词 / 48 名词 / 11 状态 / 9 链接 / 3 规则。
**新增一个单据类型只改这份 YAML，不加工具、不涨 token。**

七个工具：

| 工具 | 作用 |
|---|---|
| `kd_describe` | 按需查本体 —— 把实例从常驻 schema 变成拉取 |
| `kd_query` | 查询（字段留空即用该名词默认字段集） |
| `kd_act` | 全部写动词的统一入口 |
| `kd_push` | 下推，校验链接表 |
| `kd_run` | 执行租户定义的业务操作 |
| `kd_audit` | 查过程审计与未清算的中间态 |
| `kd_check_profile` | 校验租户配置，中文报错 |

### 契约随结果返回（修审计 A-2）

原实现里 `kingdee_audit_bills`（逐条、部分成功）和 `kingdee_void_bills`
（一次提交、原子性未知）在 MCP 类型系统里长得一模一样 —— 因为 MCP 只有
`readOnlyHint/destructiveHint/idempotentHint`，**没有 arity 和 atomicity**。

底座把这两项补进契约，并且**每次调用都随结果返回**：

```json
{"contract": {"arity": "batch", "atomicity": "per_item",
              "idempotent": false, "destructive": false, "inverse": "cancel"},
 "outcome": "partial", "succeeded": ["100"], "failed": ["101"],
 "tip": "部分成功：1 成 / 1 败，且**已成功的部分不会回滚**。重试前请只针对 failed 中的目标。"}
```

`destructive` 不再靠人工标注，而是从「有无逆动词」推导 —— 这直接消除了审计
N-1 里「同一底层操作被标成两种破坏性」的可能。

### 前置规则真的在发请求前拦截（修 MISS-01/02/03）

审计指出原 harness 的 4 条规则全是 `chain` 类：CI 里事后读日志，运行期不参与决策。
底座把三条约束前移到发请求之前：

```
kd_act(verb="audit", noun="BD_Material", ...)
→ {"blocked_by": "precondition",
   "error": "动词 'audit'(审核) 不适用于 BD_Material(物料)：基础资料没有审核流…
             可用动词：['enable','forbid','query','read','save']"}
```

一次网络往返都没发生。错误信息自带修正建议，调用方不必再花一轮问。

### 独立可测

`base/` 不导入 `kingdee_mcp.server` 那 7000 多行。传输层是一个两方法的
`Transport` 协议，测试注入 `FakeTransport` 即可跑完整分发逻辑。
默认实现懒加载复用已加固的 `_post_raw`（含 A-3 的会话重放修复）。

## 3. Skill 实例层：渐进披露 + 租户覆盖

### 用法知识放 Skill，不放 schema

`skill/kingdee-ontology/SKILL.md` 讲**怎么用底座**：先查本体、看懂 contract、
危险动作要确认、`unknown` 不等于失败。细节在 `references/` 里按需加载。

### 租户差异放覆盖层（回应⑤）

各家的云星空都做过二开：表单标识、操作编码、下推关系、字段名都不同。
与其为每家改代码，不如让每家描述自己的差异：

```
profiles/<租户>/profile.yml     ← 只写与标准不同的部分
export KINGDEE_TENANT=<租户>    ← 同一份代码服务不同账套
```

合并策略刻意保守：**覆盖层只能新增或改写，不能删除底座条目** ——
删除会让通用流程在某些租户上静默失效，比报错更难排查。
`SAL_SaleOrder` 只想多带一个项目号字段，就只写 `default_fields` 一行，
其余（可用动词、状态、原子性）自动继承。

### 面向人的业务操作入口

最关键的一段是 `operations`。业务人员用自己的话给一件事命名：

```yaml
operations:
  销售开票:
    owner: 财务部
    confirm: true
    steps:
      - {做: 确认, 问: "将对这些销售订单生成开票申请并过账应收，确认继续？"}
      - {做: 下推, 从: SAL_SaleOrder, 到: PAEZ_CustomInvoice}
      - {做: submit, 对象: PAEZ_CustomInvoice, 用: 上一步产物}
      - {做: audit,  对象: PAEZ_CustomInvoice, 用: 上一步产物}
```

之后直接说「帮我做**销售开票**，订单 XSDD001」就能用。
`python3 -m base.validate_profile <租户>` 用中文校验，错在哪一步、该怎么改都说清楚。
填写指南见 [`profiles/README.md`](../../profiles/README.md)，是写给业务人员的，不是写给工程师的。

### 与原「一站式」复合工具的区别（修审计 A-1）

`kingdee_create_and_audit` 中途失败时返回一段 `recovery_hint` 文本，
把补偿责任交给 LLM 的自觉。`kd_run` 不同：

| | 原复合工具 | `kd_run` |
|---|---|---|
| 执行前 | 直接开跑 | 含不可逆动作时**先返回计划等人确认，不做任何写操作** |
| 每步留痕 | 无 | 一条审计记录，共享同一 `trace_id` |
| 中途失败 | 文本建议 | `halted_at` + **`left_behind` 明确列出中间态单据** |
| 事后 | 无从查起 | `kd_audit(scope="dangling")` 随时查未清算的中间态 |

补偿仍需人或后续动作完成 —— 但中间态从"只存在于返回体文本"变成了**可查询、可清算的事实**。

## 4. WikiSkill：让每天的失败变成明天的改进

```
kd_act / kd_push / kd_run  →  operation_audit.jsonl
                                    ↓  python3 -m wikiskill.retro
                          wikiskill/knowledge.json（累积证据、涨置信度）
                                    ↓  达到 medium 才浮上来
              改 base/registry.yml · profiles/<租户>/profile.yml · 代码
                                    ↓
                          下次回溯该现象消失 → 条目自然沉底
```

### 「wiki」的含义是累积，不是生成

每次回溯不是重写报告，而是把新证据**并进已有条目**。条目 id 由现象本身算出，
跨次运行稳定，所以同一个问题不会每天新建一条。

置信度 = 出现次数 × 覆盖天数：

| 置信度 | 门槛 | 含义 |
|---|---|---|
| `noise` | 默认 | 只出现过一两次，先攒证据，不打扰人 |
| `low` | ≥2 次 | 有苗头 |
| `medium` | ≥5 次且跨 ≥2 天 | **浮上来，值得动手** |
| `high` | ≥10 次且跨 ≥3 天 | 长期规律 |

单日刷屏不等同于长期规律，所以刻意要求跨天。实测：同一个「批号不能为空」
第一天 3 次不上榜，第二天再 3 次即升到 `medium` 并给出具体建议。

### 只提议，不自动改

自动改 ERP 的操作定义是危险的。机器只负责积累证据和给建议，落地要人点头：

```bash
python3 -m wikiskill.retro                       # 每日回溯
python3 -m wikiskill.retro --adopt <id>          # 采纳
python3 -m wikiskill.retro --reject <id> --note "业务上就是这样"
```

**被 reject 的条目不会复活。** 计数继续累积，但不再刷屏 ——
否则每天都会重新提一遍同样的建议，人很快就不看了。

### 五条提炼规则

| 规则 | 提炼什么 | 建议指向 |
|---|---|---|
| `failure_pattern` | 反复失败的 (名词, 动词, 错误) | 补 `KNOWN_ERROR_PATTERNS` / 补字段模板 / 补 `requires_state` |
| `dangling` | 反复停在同一步的操作 | 短期人工清理，长期加确认或补偿 |
| `flaky` | `outcome=unknown` | 排查超时；长期给 save/push 加幂等键（A-5） |
| `unlinked_push` | 被 PRE-02 拦下的下推 | 补租户 `links`，或固化正确链路 |
| `slow` | 超过 5s 的操作 | 收窄字段集、减小批量 |

## 4.2 对象层：把本体从「审计产物」变成「操作面」

前面几节把本体建起来是为了审计；但本体真正的用处是**操作**。
参照 Palantir Foundry 的 Ontology：使用者面对的不该是一堆工具，而是**对象**——
打开一个对象，看到它的属性、它现在处于什么状态、它连到哪些别的对象、
以及**此刻能对它做哪些动作**。

`base/objects.py` 由 `registry.yml` 推导出三个概念，不额外维护第二份定义：

| 概念 | 是什么 | 来自 |
|---|---|---|
| `ObjectType` | 对象类型 + 属性 + 状态机 + 可用动作 + 出入链接 | 名词 |
| `ActionType` | 动词 + 参数 schema + 前置条件 + 契约 | 动词 |
| `ObjectCard` | 某实例：属性值 + 当前状态 + 此刻可用/不可用的动作 | 二者合成 |

入口是一个工具 `kd_object`：

```
kd_object(search="采购")                    搜对象类型
kd_object(noun="采购订单")                   类型卡片
kd_object(noun="采购订单", id="CGDD000231")  实例卡片
kd_object(noun="采购订单", id="…", navigate_to="采购入库单")   怎么跳到下游
```

三条设计约束：

**① 类型卡片与实例卡片同形状。** 使用者不该为「看这类东西长什么样」和
「看这一个东西」学两套结构。

**② 动作可用性必须带原因。** `enabled: false` 一定附 `reason`
（"要求 `B:审核中`，当前是 `C:已审核`"）。灰掉却不解释的按钮比没有按钮更让人困惑。
状态取不到时标 `unverified: true`——**不猜**。

**③ 导航替调用方试，但不冒充确定。** 下游单据引用源单的字段名
（`FSrcBillNo` / `FSourceBillNo` / …）随表单和二开而异，静态推断不出唯一答案。
系统逐个候选探测，命中即返回结果并标 `confirmed_by_probe`——
**探测出来的不等于账套确认的**，措辞上要分清。
命中后向知识库提一条「把 `link_filter` 写进 profile」的建议（只提议，不自动改），
于是试出来的答案会沉淀，不必下次再试一遍：这是对象层与 WikiSkill 自优化层的接点。

**④ 「这张单是什么单」返回候选，不返回断言。** 编号前缀由租户的编码规则决定
（对象 `SYS_NumberRule`），所以 `identify` 是启发式：每条前缀都附**出处**
（来自仓库里真实出现过的单号，不是编的），措辞明确说"未经账套核实"。
认不出时说"这不代表单号有问题"——各家编码规则不同。

### 两种形态，同一套本体

| 形态 | 位置 | 给谁用 |
|---|---|---|
| **Skill 形态** | [`skill/kingdee-ontology/`](../../skill/kingdee-ontology/) | Claude：以对象为中心操作，不再先想"该调哪个工具" |
| **界面形态** | [`docs/ontology/ui/`](ui/) | 人：浏览对象、模拟动作可用性、看链路、编排业务操作 |

界面**不连账套**——它操作的是本体定义。三个视图：

- **对象**：状态轨道用金蝶自己的字母码（`Z/A/B/C/D` + `CLOSED/VOID/DELETED`）做进度条，
  点一个状态，右侧动作可用性立刻重算；
- **链路图**：9 条下推关系，存疑的那条（`PRD_PickMtrl → PRD_Instock`）画成虚线琥珀；
- **业务操作编排**：拼出步骤导出 YAML，**直接可过 `validate_profile`**，
  贴进 `profiles/<租户>/profile.yml` 就能用——和面向业务人员的配置面闭环。

## 4.5 只读长尾的收敛

97 个工具里有 72 个是只读的。收敛后 **68 个（94%）可由底座的 9 个工具表达**：

```bash
python3 tools/ontology/measure_convergence.py    # 判定依据是实际端点与 form_id，不是工具名
```

| 承载工具 | 覆盖 | 说明 |
|---|---|---|
| `kd_query` | 57 | 名词登记进注册表即可；支持逗号分隔多名词合并查询 |
| `kd_read` | 3 | View 端点：单据详情、生产订单、工作流状态 |
| `kd_audit(usage)` | 2 | 调用统计 |
| `kd_describe(fields)` | 2 | 实时元数据（对账套拉真实字段清单） |
| `kd_describe(template)` | 1 | 已验证的 model 骨架 |
| `kd_act(dry_run)` | 1 | 保存前校验，不写入 |
| `kd_report` | 1 | 报表端点，参数结构与单据查询完全不同 |

注册表名词从 48 长到 **84**，底座工具从 7 增到 **9**，token 从 1,171 到 1,604 ——
**名词涨了 75%，token 只涨了 433**。这就是「实例是数据、能力是代码」的实际收益。

### 三类需要额外能力的，如何处理

- **系统对象**（用户/角色/权限/编码规则/序列规则/系统参数）走 `UserService` 等
  专用端点而非 `ExecuteBillQuery`。注册表用 `system_endpoint` 标注，
  `kd_query` 自动路由，**调用方不必知道这个区别**。
- **回退表**：同一份业务数据在不同账套/版本下表名不同，legacy 工具的做法是
  "主表查不到就换一张"。这些回退表也登记了，但**底座不做隐式回退** ——
  查不到就报错，由调用方决定换哪张，行为才可预期。
- **模板与校验**直接委托 legacy 的 `BILL_TEMPLATES` 与 `MetadataValidator`，
  不在底座里复制一份：两份实现迟早会不一致。

### 刻意不收的 4 个

SQL Server 目录探查（`discover_tables` / `discover_columns` / `describe_table` /
`discover_metadata_candidates`）数据来自数据库系统表而非金蝶 WebAPI，
需要另一套凭据（`KINGDEE_SQL_*`），且属于可选功能。
折叠进 `kd_describe` 会让同一个工具横跨两个数据源、两套权限模型——
调用方无从判断某次失败是账套问题还是数据库问题。保持独立更清楚。

`tests/test_readonly_convergence.py` 断言覆盖率不许跌破 94%，
且**每个未收敛的工具都必须登记在 `DELIBERATE` 里并写明理由** ——
"还没做"和"不打算做"不能混为一谈。

## 4.8 四层：数据加工 / Funnel 索引 / AIP Logic / Action 闭环

对象层解决了「怎么操作」，但它上下还各缺一截：数据怎么变成可信的对象属性、
对象怎么被快速检索、判断逻辑放在哪、动作做完之后怎么回流。

### 第一层：数据加工（`pipeline/`）

此前解析与标准化散在三处（`dispatch.py` 的 `_rows_of`/`_flatten_props`、
`objects.py` 的属性拆分、`server.py` 的状态名表）。散着写的后果是同一种响应
在不同入口被解析成不同结果，而且没人记得还有第三份。这层收拢成单一实现。

| 子层 | 文件 | 职责 |
|---|---|---|
| **线** | `lineage.py` | 每个列从哪来：WebAPI 字段 / SQL 列 / 推导 / 注册表 / **取不到** |
| **解析** | `parse.py` | 金蝶四种响应形状 → 具名行 |
| **标准** | `standardize.py` | 状态码、标识双轨、字段别名、值类型 |
| **表** | `dataset.py` | 行集 + schema + 血缘 + **出处** |

四个刻意的设计：

- **位置数组位数不符就中断。** `ExecuteBillQuery` 返回的是二维数组，字段名靠
  `FieldKeys` 顺序对位。位数对不上时宁可报错也不猜——错位会让「供应商名称」
  变成「金额」而无人察觉，比缺一列危险得多。
- **标识符永远不转数字。** `FID='0012'` 转成 `12` 会丢前导零，过滤式再也拼不对，
  而这个错误只在真正去查时才暴露。
- **`_id` 与 `_no` 分列不合并。** 内码与编号不通用（写操作要内码、下推要编号，
  见审计 L-3），合并会掩盖它们本来就是两种东西。
- **「字段不存在」与「字段是空的」严格区分。** 前者说明该字段在本账套可能被
  二开删了或改名了，后者只是这批单据没填——两者处置完全不同。

### 第二层：Funnel 索引（`indexlayer/`）

对象台要「按单号找单」「跨类型搜」，每次打账套既慢又打不动
（`ExecuteBillQuery` 不支持跨表检索）。索引把加工好的对象物化到 SQLite，检索走本地。

三条纪律，都为了不让索引变成「看起来是真的假数据」：

1. **每条记录带出处与取数时间**，检索结果必须能回答「这是什么时候的」；
2. **索引不是真相**——写操作后相关对象标 `stale`，检索时如实告知「要准确请回源」；
3. **只接受 `Dataset`，拒绝裸 dict**——绕过标准化的数据进了索引，
   状态码和字段名就又乱了。

### 第四层：Action 闭环（已接通第一环）

`kd_act` 成功后自动把索引里对应对象标脏，返回体带 `index_stale`。
**不自动回源刷新**——那会让每次写都多一轮往返；如实标脏，让检索方自己决定。

配合已有的过程审计与 WikiSkill 每日回溯，闭环是：

```
kd_act → 索引标脏 + 审计留痕 → 每日回溯提炼 → 人 adopt → 改注册表/配置
       → 下次动作可用性与字段解析都更准
```

### 第三层：AIP Logic —— 尚未实现

判断逻辑目前散在各处（`objects.py` 的可用性、`harness/rules.py` 的操作链、
`validate_profile` 的配置校验）。把它们收拢成**声明式的、以本体为参数的逻辑函数**
才算这一层。**现在还没做，不要当它已经存在。**

## 5. 迁移路径

底座与原 97 工具**并存**，不是替换：

1. 原 `src/kingdee_mcp/server.py` 一行未删，现有集成不受影响；
2. 新集成挂 `base/server.py`（`python3 -m base.server`），token 成本降 97%；
3. 只读长尾已收敛 94%，剩余 4 个 SQL 探查工具仍走原路径（刻意保留，见 4.5）；
4. WikiSkill 的回溯同时读两边的日志。

## 5.5 CI：把会悄悄失效的保护做成硬门禁

`.github/workflows/ontology-check.yml`（与既有的 `harness-check.yml` 互补——
后者只跑 `tests/test_server.py` 一个文件）。

这些检查的共同点是**会随时间悄悄失效**，所以必须由机器守：

| 门禁 | 守住什么 | 失效场景 |
|---|---|---|
| 全量测试 | — | — |
| `audit_atomicity.py` | 不允许 error 级发现 | 新写工具重新引入无补偿 Saga |
| `test_harness_coverage.py` | 写工具 100% 登记 | 新增写工具忘了登记 → 不受任何操作链约束 |
| `test_readonly_convergence.py` | 收敛率不跌破 94%，未收敛必须写明理由 | 新查询工具绕过底座，覆盖率悄悄下滑 |
| `validate_profile` | 租户配置可解析 | 改注册表后示例配置失效 |
| `extract_ontology.py` | 本体可抽取 | 代码结构变化后抽取器失灵（审计器盲区，`04-audit-trail.md` 有记录） |
| `measure_tool_surface.py` | token 账可复现 | — |
| `update_readme.py --check` | README 数字与代码一致 | 加了测试/名词后 README 落后 |
| `wikiskill.retro` | 自优化链路可运行 | 审计记录格式变更后回溯断链 |

最后一条值得说明：README 里的数字是**实测生成**的，所以加了测试或改了注册表之后
需要重跑 `update_readme.py` 再提交。这条失败不代表代码有问题，只代表 README 落后于代码。

## 6. 目录

```
base/                    MCP 底座
  registry.yml           唯一事实来源：动词/名词/状态/链接/规则
  ontology.py            本体 + 前置规则 + 租户覆盖层合并
  dispatch.py            通用动词分发 + 业务操作执行
  transport.py           传输抽象（可注入，便于独立测试）
  server.py              7 个 MCP 工具
  validate_profile.py    租户配置校验（中文报错）
profiles/
  README.md              面向业务人员的填写指南
  example-tenant/        示例：二开表单 + 自定义操作码 + 业务操作入口
skill/kingdee-ontology/  Skill 实例层（渐进披露）
wikiskill/
  knowledge.py           知识条目：累积、置信度、状态
  retro.py               每日回溯与自优化
pipeline/                第一层 数据加工：线 lineage / 解析 parse /
                         标准 standardize / 表 dataset / 管道 run
indexlayer/              第二层 Funnel 索引：对象物化与检索（SQLite）
base/objects.py          对象层：ObjectType / ActionType / ObjectCard
docs/ontology/ui/        界面形态（Ontology Explorer）
  _shell.html            模板（手改）
  ontology.json          本体导出（生成）
  explorer.html          成品（生成，勿手改）
tools/ontology/
  export_for_ui.py       本体 → UI 数据
  build_ui.py            数据注入模板 → 成品页面
  operation_audit.py     过程操作审计记录器
  measure_tool_surface.py  token 成本实测
  audit_atomicity.py     原子性审计（CI）
  extract_ontology.py    从代码抽取本体实例
```
