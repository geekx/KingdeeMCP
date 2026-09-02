# 过程操作审计记录规范（Operation Audit Record）

> 对应审计发现 **P-1 ~ P-5**。参考实现：[`tools/ontology/operation_audit.py`](../../tools/ontology/operation_audit.py)
> · 自测：`python3 tools/ontology/test_operation_audit.py` · 样本：[`samples/operation_audit_record.sample.jsonl`](samples/operation_audit_record.sample.jsonl)

## 1. 为什么现有日志不够

`server.py:log_tool_usage()` 记录的是**调用行为**：谁调了哪个工具、多久、成没成。
审计需要的是**业务事实**：哪张单、从什么状态到什么状态、属于哪次操作、有没有被补偿。

| 审计问题 | 现有 `usage_log` | 本规范 |
|---|---|---|
| 谁做的？ | ✗ | `actor` + `on_behalf_of` |
| 动了哪张单？ | ✗ 只记参数键名，不记值 | `noun` + `object_id` + `object_no` |
| 状态怎么变的？ | ✗ | `state_from` → `state_to` |
| 属于哪次业务操作？ | ✗ | `trace_id` + `step` |
| 失败原因？ | ✗ `error_type` 恒为空（P-3） | `outcome` + `error` |
| 补偿过吗？ | ✗ 无此概念 | `compensated_by` |

## 2. 四条设计约束

**① 记录的主语是对象，不是工具。**
一次调用影响 N 张单据就写 N 条记录。工具是手段，单据才是被审计的实体。
这条直接决定了批量操作（A-2）的可审计性：逐条循环的 `audit_bills` 写 N 条，
其中失败的那几条一目了然。

**② `trace_id` 必须贯穿复合动词的每一步。**
一次 `create_and_audit` 写 3 条同 `trace_id`、`step` 递增的记录。
这样才能回答"这次业务操作停在哪一步、之前几步的副作用是什么"——
这是 A-1 中间态从"只存在于返回体文本"变成"可查询事实"的关键。

**③ `outcome` 是闭集，且 `unknown` 与 `failed` 严格区分。**

| 取值 | 含义 | 重试前必须做什么 |
|---|---|---|
| `success` | 服务端确认成功 | — |
| `failed` | 服务端明确拒绝，副作用未产生 | 可直接修正后重试 |
| `partial` | 批量操作部分成功 | 先取 `succeeded_ids` 差集 |
| `unknown` | **请求已发出但响应不可判定**（超时、连接中断） | **必须先查证服务端状态** |

把超时记成 `failed` 是审计上最危险的错误：它会诱导调用方直接重试，
而服务端可能已经建了单。当前 `_post_raw` 在超时时抛异常，
上层统一按失败处理，`unknown` 这个类别根本不存在。

**④ 审计写入失败必须抛出，不能静默。**
`server.py:76` 的 `except Exception: pass` 对普通日志是合理取舍，
对审计记录不是——审计的价值在完备性，"丢了也不知道"等于没有审计。
本实现采用 追加写 + `flush` + `fsync` + 进程内锁，写失败向上抛。

## 3. 记录 Schema（v1.0）

```jsonc
{
  "schema_version": "1.0",
  "trace_id":    "83922de2…",        // 一次业务操作，贯穿复合动词各步
  "step":        1,                   // 该 trace 内步序，从 1 开始
  "occurred_at": "2026-09-02T…Z",     // RFC3339 UTC
  "actor":       "Kingdee\\demo",     // 金蝶登录账号——不是 MCP 进程
  "on_behalf_of":"agent:claude/session-01",  // 发起该操作的 Agent/会话
  "tool":        "kingdee_create_and_audit", // MCP 工具名
  "verb":        "Save",              // 本体动词
  "endpoint":    "save",              // 实际 WebAPI 端点
  "noun":        "PUR_PurchaseOrder", // 本体名词 = form_id
  "object_id":   "100231",            // FID
  "object_no":   "CGDD000231",        // FBillNo
  "state_from":  null,                // 迁移前（规范码，见 states.yml）
  "state_to":    "Z:暂存",            // 迁移后；服务端未确认时为 null
  "outcome":     "success",           // 闭集
  "duration_ms": 412.7,
  "request_digest": "sha256:9f2c…",   // 脱敏后请求摘要，用于重放比对
  "error":       null,                // {code, message, field}
  "compensated_by": null,             // 补偿它的 trace_id
  "extra":       {"supplier": "S001", "entry_rows": 2}
}
```

`state_to` 在服务端未确认时**必须写 `null`，不能写乐观值**。
当前 `_result_status` 按 `DOC_LIFECYCLE` 直接断言迁移结果、从不回读（`MISS-04`），
如果照搬到审计记录里，审计日志会变成"我们以为发生了什么"而不是"实际发生了什么"。

## 4. 接入方式

```python
from tools.ontology.operation_audit import audit_recorder

with audit_recorder.operation("kingdee_create_and_audit",
                              actor=USERNAME,
                              on_behalf_of=mcp_session_id()) as op:
    save_result = await _post_raw("save", form_id, model)
    st = _result_status(save_result, "save")
    op.step(verb="Save", noun=form_id, endpoint="save",
            object_id=st.get("fid"), object_no=st.get("bill_no"),
            state_from=None,
            state_to="Z:暂存" if st.get("success") else None,
            outcome="success" if st.get("success") else "failed",
            error=(st.get("errors") or [None])[0])
    if not st.get("success"):
        return _fmt(out)          # op.halted_at == 1
    ...
```

上下文管理器会捕获逃逸异常并补一条 `outcome="unknown"` 的记录——
因为异常逃逸时请求可能已经在服务端生效。

## 5. 事后审计：把中间态变成可查询事实

```bash
$ python3 tools/ontology/operation_audit.py operation_audit.jsonl
读入 6 条审计记录
  [悬挂操作链] 954e6707 kingdee_push_and_audit 停在第 2 步，
               遗留 ['PUR_PurchaseOrder:CGDD000231', 'STK_InStock:RKD000318']，末状态=None (failed)
  [悬挂操作链] ad3be388 kingdee_save_bill 停在第 1 步，遗留 []，末状态=None (unknown)
```

`dangling_traces()` 的判定：一条 trace 里有写动作已生效（`success`/`partial`/`unknown`），
但整条链没走到终态（`C:已审核` / `已删除` / `已作废` / `已关闭`），且无补偿记录。
失败步骤作用的对象**也计入 `left_objects`**——
`push` 生成的目标单草稿在后续 `submit` 失败时依然遗留在系统里。

这条查询就是 `rules.yml:MISS-05`「补偿完备性」规则的运行期实现，
也是把 A-1 从"设计缺陷"变成"每日可清算的待办"的手段。

## 6. 与现有日志的关系

两者并存，职责不同：

| | `usage_log.jsonl` | `operation_audit.jsonl` |
|---|---|---|
| 主语 | 工具调用 | 单据上的状态迁移 |
| 用途 | 性能分析、工具改进 | 合规审计、中间态清算 |
| 丢失容忍 | 可以（`except: pass`） | 不可以（写失败抛出） |
| 覆盖范围 | 全部 97 个工具 | 25 个写工具 |

建议：`usage_log` 保持现状，但顺手修掉 P-3——
把 `error_type=... if 'e' in dir() else ""`（`server.py:1729/1792/1911` 三处）
改为在 `except` 块内捕获异常类型名到局部变量，否则 `_ERROR_STATS`
和 `kingdee_usage_stats` 的错误统计永远是空的。
