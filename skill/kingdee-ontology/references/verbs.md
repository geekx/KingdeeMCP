# 动词契约速查

以 `base/registry.yml` 为准;本表是给人看的摘要,冲突时以注册表为准。

| 动词 | 中文 | 批量原子性 | 幂等 | 逆动词 | 前置状态 | 到达状态 |
|---|---|---|---|---|---|---|
| `query` | 查询 | atomic | ✅ | — | — | — |
| `read` | 查看详情 | atomic | ✅ | — | — | — |
| `save` | 新建/修改 | atomic(单对象) | ❌ | delete | — | Z:暂存 |
| `submit` | 提交 | **per_item** | ❌ | cancel | Z:暂存 / A:创建 | B:审核中 |
| `audit` | 审核 | **per_item** | ❌ | unaudit | B:审核中 | C:已审核 |
| `unaudit` | 反审核 | **per_item** | ❌ | audit | C:已审核 | A:创建 |
| `delete` | 删除 | **per_item** | ❌ | **无** | Z:暂存 / A:创建 | DELETED |
| `cancel` | 撤销 | server_defined | ❌ | submit | — | — |
| `void` | 作废 | server_defined | ❌ | **无** | C:已审核 | VOID |
| `close` | 整单关闭 | server_defined | ❌ | unclose | C:已审核 | CLOSED |
| `unclose` | 反关闭 | server_defined | ❌ | close | CLOSED | C:已审核 |
| `forbid` | 禁用 | server_defined | ❌ | enable | ENABLED | FORBIDDEN |
| `enable` | 启用 | server_defined | ❌ | forbid | FORBIDDEN | ENABLED |
| `push` | 下推 | server_defined | ❌ | **无** | C:已审核 | (生成草稿) |

## 三种原子性的实际后果

**`atomic`** — 全成功或全失败。失败后系统状态未变,可以直接改了重试。

**`per_item`** — 逐条执行。10 个目标可能 7 成 3 败,**已成功的 7 个不会回滚**。
返回体给出 `succeeded` / `failed`,重试**只针对 failed**。

**`server_defined`** — 一次请求带多个 ID,由服务端决定。失败时返回体**没有 per-id 结果**,
你不知道 10 个里哪几个生效了。必须 `kd_query` 逐个查证后才能安全重试。
这是这套 API 的固有限制,不是实现偷懒——所以宁可小批量多次调用。

## 状态词表

| 规范码 | 中文 | 金蝶字段 | 终态 |
|---|---|---|---|
| `NONEXISTENT` | 不存在 | — | ❌ |
| `Z:暂存` | 暂存 | FDocumentStatus='Z' | ❌ |
| `A:创建` | 创建 | ='A' | ❌ |
| `B:审核中` | 审核中 | ='B' | ❌ |
| `C:已审核` | 已审核 | ='C' | ✅ |
| `D:重新审核` | 重新审核 | ='D' | ❌ |
| `CLOSED` / `VOID` / `DELETED` | 已关闭/已作废/已删除 | FCloseStatus / FCancelStatus / — | 后两者 ✅ |
| `ENABLED` / `FORBIDDEN` | 已启用/已禁用 | FForbidStatus | ❌ |

⚠️ **`D:重新审核` 属于「待处理」,不是「已驳回」。**
工作流驳回要查审批实例,单据状态表达不了。旧实现把 rejected 定义成 D,
与 pending 重叠,任何基于它的统计都不可信。
