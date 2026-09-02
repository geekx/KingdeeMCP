# KingdeeMCP 操作原子化审计报告

- **审计对象**：`WaHaiLong/KingdeeMCP` @ `2c44e6f`（97 个 MCP 工具，`server.py` 7331 行 + `harness/` 639 行）
- **审计方式**：静态分析（AST 抽取 + 人工阅读全部写路径）。**未接入真实金蝶账套**，凡需运行期实证的结论均标注「存疑」。
- **可复现**：`python3 tools/ontology/audit_atomicity.py`（退出码 1 = 存在 error 级发现）

## 0. 一句话结论

这套 MCP 把金蝶的**动词**包装得很完整，但没有把**事务边界**表达出来：
97 个工具里没有任何一个声明自己是「全成功或全失败」还是「逐条、可能部分成功」。
于是 3 个"一站式"复合工具成了**无补偿的 Saga**——中途失败时副作用已落库，代码只返回一段
`recovery_hint` 文本，把回滚责任交给 LLM 的自觉。

## 0.5 修复状态

审计后已在本仓库落地的修复（`python3 tools/ontology/audit_atomicity.py` 从
**15 项发现 / 7 项 error** 降到 **5 项 / 0 项 error**）：

| 编号 | 状态 | 做法 |
|---|---|---|
| A-3 | ✅ 已修 | 会话失效判定收紧为正则；**只有 HTTP 401 才允许重放写请求**，200+失效改抛 `SessionAmbiguousError` |
| A-4 | ✅ 已修 | `_login()` 真正持锁 + 双重检查 |
| A-6 | ✅ 已修 | 抽出 `_prepare_save_model()`，`save_bill` 与复合工具共用字段自愈与顺序防御 |
| N-1 | ✅ 已修 | 拆分 `workflow_approve` / `workflow_reject`，各自如实标注破坏性 |
| N-2 | ✅ 已修 | `refresh_metadata` 改为 `readOnly=False, idempotent=True` |
| N-3 | ✅ 已修 | 三个查询工具 `idempotentHint` 改回 `True` |
| S-3 | ✅ 已修 | `DOC_LIFECYCLE` 补齐 6 个 execute 系动词的状态定义 |
| R-1 | ✅ 已修 | 规则改按**动词**匹配，工具集中登记在 `harness/tools.py`，**覆盖率 25% → 100%**，漏登记 = CI 失败 |
| R-2 | ✅ 已修 | 三处把工具名塞进 `next_action` 的改为动词；`NEXT_ACTION_VOCAB` 由测试守住 |
| R-3 | ✅ 已修 | `return` → `continue`，并改为收集全部违规而非首个即返回 |
| R-4 | ✅ 已修 | 契约统一为 `(passed, message)`，docstring 与实现对齐 |
| R-5 | ✅ 已修 | 后续动词必须**绑定到具体单据**；绑不上时降级为新增的 RULE-006 警告 |
| R-6 | ✅ 已修 | `opinion` 不再假装被记录，返回 `opinion_persisted: false` + 明确警告 |
| R-7 | ✅ 已修 | `reject` 拆为独立工具，返回体声明 `bypassed_workflow: true` |
| P-3 | ✅ 已修 | `error_type` 在 `except` 块内捕获，三处 |
| P-5 | ✅ 已修 | 复合工具的各步骤共享同一 `trace_id` |
| A-2 | ⚠️ 缓解 | 底层 API 决定了两种原子性并存；现在**契约随结果返回**（`contract.atomicity` + `atomicity_note`），调用方可据此决定重试策略 |
| A-1 | ⚠️ 缓解 | 无事务的底层 API 做不到原子；现在中途失败返回结构化 `pending_compensation`（遗留对象 + 建议动作）并落过程审计，可被 `kd_audit(scope='dangling')` 与每日回溯查出。**补偿仍需人执行，不会自动回滚**（自动删单风险更高） |
| A-5 | ❌ 未修 | 幂等键需要服务端配合 |
| S-1/S-2 | ◐ 部分 | 底座 `base/registry.yml` 已给出唯一权威状态词表并修正 `D` 的双重定义；legacy 的中文名词表保留以免破坏既有返回体 |
| L-1/L-3 | ◐ 部分 | 底座有集中链接表并在发请求前校验；legacy 的 5 个 push 工具尚未接入 |
| L-2 | ❌ 存疑 | 需在真实账套实证 |

新增的约束：RULE-005（复合操作中间态补偿）、RULE-006（身份可绑定性）。

## 1. 发现汇总

| 级别 | 数量 | 编号 |
|---|---|---|
| 高 | 8 | A-1 A-2 A-3 S-1 S-2 R-1 R-2 P-1 |
| 中 | 19 | A-4 A-5 A-6 N-1 N-2 N-4 S-3 S-4 L-1 L-3 R-3 R-4 R-5 R-6 R-7 P-2 P-3 P-4 P-5 |
| 低 | 1 | N-3 |
| 存疑（需账套实证） | 1 | L-2 |
| **合计** | **29** | |

其中 `audit_atomicity.py` 可自动检出 15 项（error 7 / warning 7 / info 1）；其余需阅读代码判定。

---

## 2. 原子性（Atomicity）

### A-1【高】复合工具是无补偿 Saga

`server.py:3667` `kingdee_create_and_audit`、`:3800` `kingdee_push_and_audit`、`:4015` `kingdee_create_lx_billing`
分别在**一次 MCP 调用内顺序打 3、3、4 个写端点**：

| 工具 | 端点序列 | 中途失败后系统里留下什么 |
|---|---|---|
| `create_and_audit` | save → submit → audit | 已落库的草稿单（`Z:暂存`），或已提交待审的单据 |
| `push_and_audit` | push → submit → audit | 已生成的下游目标单草稿 + 已消耗的源单关联数量 |
| `create_lx_billing` | push → save → submit → audit | 同上，且草稿已被写入业务字段 |

失败路径不做任何回滚，只在返回体里放一段自然语言：

```python
out["recovery_hint"] = (
    f"草稿已生成 (fid={fid})。Submit 失败：检查 errors[].matched.suggestion。"
    f"修正后调用 kingdee_submit_bills(...) 重试。")            # server.py:3746
```

**为什么这是原子性问题而不是"设计取舍"**：补偿动作既没有被执行，也没有被记录，
更没有任何机制保证它会被执行。调用方是 LLM——它可能重试、可能改用别的工具、
也可能直接向用户报告"创建失败"，而账套里已经躺着一张孤儿草稿。
这三个工具恰好又是 harness 规则完全没覆盖的（见 R-1），所以连事后 CI 检查都发现不了。

> 修复方向：要么在工具内实现真正的补偿（失败即 `delete_bills` 清理草稿、
> 或 `unaudit`+`delete` 清理下游单），要么把 `halted_at` 升级为一条**结构化的待补偿事项**
> 写进审计日志，由 `dangling_traces()`（本次新增）在 CI/巡检中强制清算。

### A-2【高】批量语义分裂：同为 `bill_ids`，两种原子性级别

| 动词组 | 实现 | 原子性 | 能否知道哪些 id 生效 |
|---|---|---|---|
| `submit` `audit` `unaudit` `delete` | `for bill_id in params.bill_ids:` 逐条 POST（`server.py:3067`） | per_item，部分成功 | 能，返回 `succeeded_ids` / `failed_details` |
| `cancel` `void` `close` `unclose` `forbid` `enable` | `business["Ids"] = ",".join(params.bill_ids)` 单次 POST（`server.py:3333`） | server_defined | **不能**，返回体无 per-id 结果 |

两组工具的入参形状完全一样（`bill_ids: List[str]`），语义却不同。
调用方从签名和注解上无从区分，第二组失败时也无法判断"10 张单里哪几张已作废"。
第一组还有一个隐含问题：`success = len(failed) == 0`，但已成功的那部分**不会回滚**——
返回 `success=false` 的同时，系统状态已经被部分改变。

### A-3【高】写请求在会话判定误命中时会被无条件重放

`server.py:1886`：

```python
if resp.status_code == 401 or (
        resp.status_code == 200 and
        ("会话" in resp.text or "session" in resp.text.lower())):
    await _login()
    resp = await client.post(...)          # 原样重发，含 save / push
```

判定条件是**对 HTTP 200 正文做子串匹配**。任何成功响应只要正文里出现 `session`
（大小写不敏感）或"会话"二字，就会触发重新登录并**重发整个写请求**。
`kingdee_query_audit_log` / `kingdee_query_operation_logs` 这类工具的返回内容天然包含会话字样；
而 Save/Push 本身没有幂等键（A-5），一次误命中就是一张重复单据。

同一段逻辑在只读路径 `_post()`（`server.py:1701`、`:1763`）里重复出现。

### A-4【中】声明的并发登录锁从未生效

```python
_session_lock: asyncio.Lock = None          # server.py:1928
def _get_session_lock() -> asyncio.Lock: ...  # :1930
```

全文件对 `_get_session_lock` / `_session_lock` 的引用只有定义处这 3 行——**没有任何调用点**。
`_post`、`_post_raw`、`_query_metadata` 都直接 `await _login()` 并写全局 `_session_id`。
多协程并发时会互相覆盖 SessionId，正在途中的请求可能带着已失效的 Cookie。
注释写着"防止多协程同时触发 `_login()`"，实现缺失。

### A-5【中】无幂等键

Save / Push 都不接受客户端请求 ID，服务端也就无从去重。
配合 A-3 的自动重放和 LLM 的自主重试，重复建单没有任何防线。
金蝶的"关联数量 >= 订单数量"（`SRV-02`）只在下游单据**审核后**才计入关联数量，
草稿阶段可以穿透，因此不能当作 Push 的幂等兜底。

### A-6【中】复合工具绕过了原子工具的字段自愈

`kingdee_save_bill` 在保存前调用元数据校验器自动纠正字段名（`server.py:2826`）：

```python
validator = await _get_metadata_validator(params.form_id)
if validator:
    model, auto_fixes = validator.validate_and_fix(model)
```

`kingdee_create_and_audit` 的 Save 步骤（`server.py:3684`）**没有这段**，
也没有做 `save_bill` 里那段"财务信息必须排在分录之前"的防御性字段排序（`server.py:2835`）。
同一个「保存」动词，走两条路径行为不同：一站式工具在本环境下反而更容易触发
"含税单价不能小于等于0"。

---

## 3. 契约与注解（Annotation）

### N-1【中】同一底层操作，破坏性声明相反

| 工具 | 底层端点 | `destructiveHint` |
|---|---|---|
| `kingdee_unaudit_bills`（`:3126`） | `unaudit` | **True** |
| `kingdee_workflow_approve(action="reject")`（`:4845`） | `unaudit` | **False** |

同理 `execute` 端点上：`void`=True，而 `close`/`unclose`/`forbid`/`enable` 全为 False。
下游 Agent 的"危险操作需确认"策略会因入口不同而失效。

### N-2 / N-3 / N-4【中/低】只读声明与事实不符

- `kingdee_refresh_metadata`（`:3032`）：`readOnly=True` 且 `idempotent=False`——两者不能同时成立；它确实会写元数据缓存。
- `kingdee_validate_bill`（`:2926`）：`readOnly=True`，但会拉取元数据并写进程内全局 `_METADATA_CACHE`。对金蝶只读，对 MCP 进程不是。
- `kingdee_query_mrp_result` / `query_production_plan` / `query_production_report`：`readOnly=True` 但 `idempotent=False`，与其余 69 个查询工具口径不一致。

---

## 4. 状态（State）

### S-1【高】系统里有两套互不映射的状态词表

| 来源 | 词表 |
|---|---|
| `server.py:367` `DOC_LIFECYCLE` | 中文名：草稿 / 待审核 / 已审核 / 已删除 / 源单 / 目标单草稿 |
| `server.py:2333, 4739-4743` SQL 过滤 | 金蝶字母码：`A` `B` `C` `D` `Z` |

代码中**没有任何一处把二者对齐**。于是"提交后是什么状态"有两个互不相认的答案：
`DOC_LIFECYCLE` 说是"待审核"，SQL 侧 `B` 叫"审核中"，而 `Z:暂存` 在 `DOC_LIFECYCLE` 里根本不存在（S-4）。
本次审计给出的唯一权威词表见 [`model/states.yml`](model/states.yml)。

### S-2【高】`D` 同时被定义为 pending 和 rejected

```python
if params.status == "pending":
    status_filter = "FDocumentStatus IN ('A', 'B', 'D')"   # server.py:4739
elif params.status == "rejected":
    status_filter = "FDocumentStatus = 'D'"                # server.py:4743
```

两个本应互斥的语义类共用同一个码，且一个包含另一个。
`kingdee_query_pending_approvals(status="pending")` 的结果集包含全部 rejected 单据，
反之 `status="rejected"` 又漏掉真正被工作流驳回、但单据状态不是 D 的单子。
任何基于这个参数的统计都不可信。

### S-3【中】6 个写动词没有状态定义

`DOC_LIFECYCLE` 只覆盖 `save/submit/audit/unaudit/delete/push`。
走 `execute` / `cancel_assign` 端点的 `cancel` `void` `close` `unclose` `forbid` `enable`
在 `_result_status()` 里取到空 lifecycle（`server.py:455`），于是 `next_action` 一律为 `None`——
按该函数的约定，`next_action=None` 表示**流程已完成**。
作废一张单和审核完一张单，对调用方返回同样的"完成"信号。

---

## 5. 链接（Link）

### L-1【中】下推关系没有集中登记表

3 条硬编码在函数体内、2 个工具以自由参数开放、4 条只写在 docstring 里，
分散于 5 个函数。系统无法回答"这条下推是否合法"，只能发请求等服务端报错。
完整清单见 [`model/links.yml`](model/links.yml)。

### L-2【存疑，需账套实证】`PRD_PickMtrl → PRD_Instock` 可能不是有效转换关系

`kingdee_push_production_stock_in`（`server.py:7025`）硬编码从**生产领料单**下推**生产入库单**。
金蝶标准转换关系中生产入库单由**生产订单**（`PRD_MO`）下推，领料单→入库单不是标准链路。
若目标账套未配置该规则，此工具恒定失败，且报错会指向"转换规则不匹配"，掩盖工具定义本身的问题。

**本次为静态审计，无法验证。** 请在真实环境用
`kingdee_push_bill(form_id="PRD_MO", target_form_id="PRD_Instock")` 对照确认后再定论。

### L-3【中】名词表与工具之间没有绑定

`FORM_CATALOG` 的 48 个名词只服务于 SQL 表名映射和一个默认清单；
约 60 个查询工具把 `form_id` 硬编码在函数体内。
无法回答"名词 X 上能执行哪些动词"，也就无法拦截"对基础资料调用 Audit"这类非法组合。

---

## 6. 规则（Rule）

### R-1【高】约束层覆盖率 25%

`harness/rules.py` 的规则以**工具名字符串硬编码**匹配：

```python
if node.tool in ("kingdee_save_bill", "kingdee_push_bill", "kingdee_submit_bills"):  # :74
if node.tool == "kingdee_push_bill":                                                 # :113
```

24 个写动词中 **18 个不被任何规则点名**，包括全部 3 个复合工具、5 个 push 变体中的 4 个、
4 个 save 变体中的 3 个、以及全部 6 个 execute 系动作。
换言之，A-1 描述的无补偿 Saga 恰好处在约束层的盲区里。

### R-2【高】`next_action` 词表越界，规则对两个工具恒定误报

RULE-001 这样反推后继工具名（`harness/rules.py:87`）：

```python
f"kingdee_{node.next_action.replace('+', '_').split('_')[0]}_bills"
```

- `next_action="submit"` → `kingdee_submit_bills` ✅
- `next_action="submit+audit"` → `kingdee_submit_bills` ✅
- `next_action="kingdee_submit_bills + kingdee_audit_bills"`（`server.py:7005`、`:7040`）→ **`kingdee_kingdee_bills`** ❌
- `next_action="kingdee_audit_production_orders"`（`server.py:6953`）→ **`kingdee_kingdee_bills`** ❌

这三处直接把**工具名**塞进了本该放**动词**的 `next_action`，反推出一个不存在的工具，
规则永远找不到后继操作，恒定判定"操作链不完整"。

### R-3【中】规则用 `return` 代替 `continue`，后续节点全部漏检

```python
fid = node.fid or (node.bill_ids[0] if node.bill_ids else None)
if not fid:
    return True, ""          # harness/rules.py:80  ← 应为 continue
```

操作链中第一个拿不到 fid 的节点会让整条规则提前退出。

### R-4【中】规则返回值语义与文档相反

`HarnessRule.check` 的 docstring 写"返回 `(violated: bool, message)`"（`rules.py:23`），
而 4 个实现全部返回 `True` 表示**通过**；调用处写作：

```python
violated, message = rule.check(nodes)
if not violated:                        # rules.py:250
    violations.append(...)
```

变量名与语义颠倒。照文档新增一条规则，行为会全盘反向且不报错。

### R-5【中】RULE-002 的 submit/audit 判定不绑定单据

只要链路后面出现过任意一次 `submit` 和任意一次 `audit` 就判通过（`rules.py:120-129`），
不校验它们作用的是不是 push 生成的目标单。对无关单据的操作即可骗过规则。

### R-6【中】审批意见静默丢失

`WorkflowActionInput.opinion`（`server.py:4712`）被接收，但 `kingdee_workflow_approve`
**从未把它发给金蝶**——只在响应里回显：

```python
if params.opinion:
    status_data["opinion"] = params.opinion      # server.py:4869
```

调用方（和用户）会认为审批意见已入库，实际没有。

### R-7【中】`reject` ≠ 驳回

`action="reject"` 实际执行 `unaudit`（反审核）。反审核是单据状态操作，
工作流驳回是审批实例操作，二者作用对象不同；且此路径**绕过工作流引擎**，
审批流实例不会被更新（见 `links.yml:workflow_instance`）。

---

## 7. 过程操作审计记录（Audit Record）

> 本节回应「增加对过程操作审计的记录」。规范见 [`03-operation-audit-record.md`](03-operation-audit-record.md)，
> 参考实现见 [`tools/ontology/operation_audit.py`](../../tools/ontology/operation_audit.py)。

### P-1【高】现有日志记录的是"调用了什么工具"，不是"发生了什么业务事实"

`log_tool_usage()`（`server.py:60-84`）落盘字段：
`timestamp / tool / params_keys / duration_ms / success / error_type / result_preview`。

审计要回答的问题它一个都答不了：

| 审计问题 | 现有日志 |
|---|---|
| 谁做的？ | ✗ 无 actor |
| 动了哪张单？ | ✗ 只记参数**键名**，不记 `form_id`、不记 FID/单据号（P-2） |
| 从什么状态到什么状态？ | ✗ 无状态字段 |
| 属于哪一次业务操作？ | ✗ 无 trace_id，复合工具的 3 步与其内部 3 次 `api:*` 记录无法关联（P-5） |
| 失败原因是什么？ | ✗ `error_type` 恒为空串（P-3） |
| 这次操作被补偿过吗？ | ✗ 无此概念 |

### P-3【中】`error_type` 永远是空字符串

```python
error_type=type(e).__name__ if not success and 'e' in dir() else ""   # server.py:1729, :1792, :1911（三处相同）
```

`except Exception as e: ... raise` 在退出 except 子句时会隐式 `del e`，
`finally` 里 `'e' in dir()` 恒为 `False`。已用最小复现确认。
`_ERROR_STATS` 因此永远不会累积任何条目，`kingdee_usage_stats` 的错误统计恒为空。

### P-4【中】审计写入失败被静默吞掉

```python
try:
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
except Exception:
    pass          # server.py:76  日志记录失败不影响主流程
```

对普通日志这是合理取舍，对**审计记录**不是：审计的价值恰恰在于完备性，
"丢了也不知道"等于没有审计。且无 `flush`/`fsync`、无锁，并发写 JSONL 可能交错成坏行。

### P-5【中】两套记录无法拼回一次业务操作

复合工具"失败日志只在 composite 层记一次"（`server.py:3671` 注释），
但其内部 3 次 `_post_raw` 仍各自写一条 `api:save` / `api:submit` / `api:audit`。
两套记录之间没有共同标识，事后无法把它们归并成一次操作，
也就无法回答"这次 create_and_audit 到底停在哪一步、留下了什么"。

### 本次交付的补救

`tools/ontology/operation_audit.py` 提供了一个可直接接入的记录器：

- 记录主语是**单据**而非工具：一次调用影响 N 张单就写 N 条；
- `trace_id` + `step` 贯穿复合动词，可拼回完整操作链；
- `outcome` 是闭集 `{success, failed, partial, unknown}`，其中 **`unknown` 与 `failed` 严格区分**——
  超时/连接中断意味着服务端可能已生效，重试前必须先查证；
- 追加写 + `fsync` + 进程内锁，**写失败抛出而不静默**；
- 上下文管理器捕获逃逸异常并留痕；
- `dangling_traces()` 把 A-1 的中间态变成**可查询的运行期事实**：

```
$ python3 tools/ontology/operation_audit.py docs/ontology/samples/operation_audit_record.sample.jsonl
读入 6 条审计记录
  [悬挂操作链] 954e6707 kingdee_push_and_audit 停在第 2 步，
               遗留 ['PUR_PurchaseOrder:CGDD000231', 'STK_InStock:RKD000318']，末状态=None (failed)
  [悬挂操作链] ad3be388 kingdee_save_bill 停在第 1 步，遗留 []，末状态=None (unknown)
```

自测：`python3 tools/ontology/test_operation_audit.py`（4 项，全部通过）。

---

## 8. 建议的处置顺序

| 顺序 | 动作 | 对应发现 | 理由 |
|---|---|---|---|
| 1 | 修 `_post_raw` 的会话重放判定，改为只认 401 + 金蝶明确的会话失效错误码 | A-3 | 单点、低风险、直接消除重复建单 |
| 2 | 接入 `operation_audit.py`，先让中间态可见 | P-1..P-5, A-1 | 不改业务行为，先获得观测能力 |
| 3 | 给每个写动词补 `arity` / `atomicity` 声明，并统一批量语义 | A-2 | 让契约可被工具消费方读到 |
| 4 | 统一状态词表到 `model/states.yml`，修 `D` 的双重定义 | S-1, S-2, S-3 | 影响所有查询与状态断言的正确性 |
| 5 | 修 harness 的 4 个规则缺陷，改为按**端点**而非工具名匹配 | R-1..R-5 | 覆盖率从 25% 提到 100% 且不再随新增工具失效 |
| 6 | 给复合工具实现真补偿，或降级为"必须显式确认中间态" | A-1 | 改动最大，放在有观测和约束之后 |
| 7 | 补 `opinion` 透传、拆分 `reject` 与 `unaudit` | R-6, R-7 | 语义正确性 |
| 8 | 在目标账套验证 `PRD_PickMtrl → PRD_Instock` | L-2 | 需要环境，独立推进 |
