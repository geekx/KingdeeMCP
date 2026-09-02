# 实例层：从代码抽取的本体实例

本文所有数字与清单由 [`tools/ontology/extract_ontology.py`](../../tools/ontology/extract_ontology.py)
从 `src/kingdee_mcp/server.py` 静态抽取（不导入模块、不触发登录），
机器可读快照见 [`model/instances.snapshot.json`](model/instances.snapshot.json)。
重跑：`python3 tools/ontology/extract_ontology.py --write`

```
名词 Noun (FORM_CATALOG)   : 48
动词 Verb (MCP tools)      : 97   （读 72 / 写 25）→ 归并为 20 类动词
状态 State (DOC_LIFECYCLE) : 6    仅覆盖 save/submit/audit/unaudit/delete/push
链接 Link (硬编码下推)     : 3    另有 2 个自由参数入口、4 条只写在 docstring 里
规则 Rule (错误模式)       : 18   + harness 操作链规则 4 条
```

## 1. 名词实例（48）

| 类别 | 生命周期 | 实例 |
|---|---|---|
| 基础资料 | 启用/禁用 | `BD_Material` `BD_Customer` `BD_Supplier` `BD_Department` `BD_Empinfo` `BD_Stock` `BD_Unit` `BD_Currency` |
| 采购 | 完整单据生命周期 | `PUR_PurchaseOrder` `PUR_ReceiveBill` `PUR_MRB` `PUR_Requisition` `PUR_MRAPP` `PUR_Contract` `PUR_PriceCategory` `PUR_PAT` |
| 寻源 | 同上 | `SVM_InquiryBill` `SVM_QuoteBill` `SVM_ComparePrice` |
| 销售 | 同上 | `SAL_SaleOrder` `SAL_OUTSTOCK` `SAL_RETURNSTOCK` `SAL_Quotation` `SAL_DELIVERYNOTICE` `SAL_RetuenNotice` |
| 库存 | 同上 | `STK_InStock` `STK_MisDelivery` `STK_Miscellaneous` `STK_TransferDirect` `STK_StockCountInput` `STK_TransferApply` `STK_TRANSFERIN` `STK_TRANSFEROUT` `STK_AssembledApp` `STK_OutStockApply` `STK_StatusConvert` |
| 质检 | 同上 | `QIS_InspectBill` |
| 财务 | 同上 | `AP_Payable` `AR_Receivable` `TRNV_Receipt` `TRNV_PaymentSlip` |
| 费用 | 同上 | `ER_ExpenseRequest` `ER_ExpenseReimburse` |
| 生产 | 同上 | `PRD_MO` `PRD_PickMtrl` `PRD_Instock` |
| **查询视图**（本体上非名词） | 无 | `STK_Inventory` `SAL_AvailableQuery` |

## 2. 动词实例（97 工具 → 20 类）

| 动词 | 实例数 | 代表工具 | 读/写 |
|---|---|---|---|
| Query | 60 | `kingdee_query_bills`, `kingdee_query_purchase_orders` …(+58) | 读 |
| Read | 4 | `kingdee_view_bill`, `kingdee_get_bill_template` …(+2) | 读 |
| Discover | 5 | `kingdee_discover_tables`, `kingdee_discover_columns` …(+3) | 读 |
| Validate | 1 | `kingdee_validate_bill` | 读 |
| Introspect | 2 | `kingdee_usage_report`, `kingdee_usage_stats` | 读 |
| Refresh | 1 | `kingdee_refresh_metadata` | 读 |
| Save | 4 | `kingdee_save_bill`, `kingdee_save_asset` …(+2) | 写 |
| Submit | 2 | `kingdee_submit_bills`, `kingdee_submit_production_orders` | 写 |
| Audit | 2 | `kingdee_audit_bills`, `kingdee_audit_production_orders` | 写 |
| Unaudit | 1 | `kingdee_unaudit_bills` | 写 |
| Delete | 1 | `kingdee_delete_bills` | 写 |
| Cancel | 1 | `kingdee_cancel_bills` | 写 |
| Void | 1 | `kingdee_void_bills` | 写 |
| Close | 1 | `kingdee_close_bill` | 写 |
| Unclose | 1 | `kingdee_unclose_bill` | 写 |
| Forbid | 1 | `kingdee_forbid_bills` | 写 |
| Enable | 1 | `kingdee_enable_bills` | 写 |
| Push | 5 | `kingdee_push_bill`, `kingdee_push_and_audit` …(+3) | 写 |
| Approve | 1 | `kingdee_workflow_approve` | 写 |
| Composite | 2 | `kingdee_create_and_audit`, `kingdee_create_lx_billing` | 写 |
### 底层端点分布（原子性判定的真实依据）

工具名会骗人，端点不会。同一个端点被多个工具复用，是 AT-03 注解冲突的来源：

| 端点 | 被几个写工具使用 | 批量语义 |
|---|---|---|
| `save` | 6 | 单对象 |
| `submit` | 5 | 逐条循环（per_item） |
| `audit` | 6 | 逐条循环（per_item） |
| `unaudit` | 2 | 逐条循环（per_item） |
| `delete` | 1 | 逐条循环（per_item） |
| `push` | 6 | 服务端决定（server_defined） |
| `execute` | 5 | `Ids` 逗号拼接，服务端决定 |
| `cancel_assign` | 1 | `Ids` 逗号拼接，服务端决定 |

> 端点数之和（32）大于写工具数（25），差额来自 3 个复合工具各占 3~4 个端点
> ——这个差额就是 AT-01 的量化表现。

### 复合动词实例（AT-01 的三个当事人）

| 工具 | 端点序列 | 步数 | 补偿 |
|---|---|---|---|
| `kingdee_create_and_audit` | save → submit → audit | 3 | ✗ 仅 `recovery_hint` 文本 |
| `kingdee_push_and_audit` | push → submit → audit | 3 | ✗ 同上 |
| `kingdee_create_lx_billing` | push → save → submit → audit | 4 | ✗ 同上 |

`kingdee_workflow_approve` 也横跨 `audit`/`unaudit` 两个端点，但落在**互斥分支**
（`action` 参数二选一），不是顺序 Saga，无中间态风险；
它的问题是动词过载与注解冲突（N-1、R-7）。

## 3. 状态实例

代码中实际出现的状态取值，及其在两套词表中的名字：

| 规范码（本次定义） | `DOC_LIFECYCLE` 中文名 | 金蝶字段值 | 由谁改变 | 终态 |
|---|---|---|---|---|
| `NONEXISTENT` | （被"草稿"覆盖） | — | Save | ✗ |
| `Z:暂存` | **缺失** | `FDocumentStatus='Z'` | Save | ✗ |
| `A:创建` | 草稿 | `='A'` | Save / Unaudit | ✗ |
| `B:审核中` | 待审核 | `='B'` | Submit | ✗ |
| `C:已审核` | 已审核 | `='C'` | Audit | ✓ |
| `D:重新审核` | **缺失** | `='D'` | 工作流退回 | ✗ |
| `CLOSED` | **缺失** | `FCloseStatus` | Close / Unclose | ✗ |
| `VOID` | **缺失** | `FCancelStatus` | Void | ✓ |
| `DELETED` | 已删除 | — | Delete | ✓ |
| `ENABLED` / `FORBIDDEN` | **缺失** | `FForbidStatus` | Enable / Forbid | ✗ |

10 个真实可达的状态里，`DOC_LIFECYCLE` 只登记了 4 个。
详细定义与冲突说明见 [`model/states.yml`](model/states.yml)。

## 4. 链接实例

| 来源 | 关系 | 承载形式 | 可校验 |
|---|---|---|---|
| `kingdee_push_stock_transfer:6176` | `STK_TransferApply → STK_TransferDirect` | 函数体硬编码 | ✗ |
| `kingdee_push_production_pick:6990` | `PRD_MO → PRD_PickMtrl` | 函数体硬编码 | ✗ |
| `kingdee_push_production_stock_in:7025` | `PRD_PickMtrl → PRD_Instock` ⚠️存疑 | 函数体硬编码 | ✗ |
| `kingdee_push_bill` | `<param> → <param>` | 完全开放 | ✗ |
| `kingdee_push_and_audit` | `<param> → <param>` | 完全开放 | ✗ |
| `kingdee_create_lx_billing` | `SAL_SaleOrder→TRNV_Receipt` / `PUR_PurchaseOrder→TRNV_PaymentSlip` | 由 `kind` 派发 | ✗ |
| `server.py:3573-3576` docstring | 销售订单→出库单 / 采购订单→入库单 / 采购订单→收料通知单 / 销售订单→退货单 | **仅文字** | ✗ |

全部 13 条链接实例，**没有一条**被任何数据结构承载到可校验的程度。
详见 [`model/links.yml`](model/links.yml)。

## 5. 规则实例

| 层 | 条数 | 求值时点 | 实际效力 |
|---|---|---|---|
| 金蝶服务端（`SRV-01..03`） | 3（已识别） | 前置/不变量 | ✅ 真拦截，但客户端不预校验，只能等报错 |
| 客户端错误模式（`KNOWN_ERROR_PATTERNS`） | 18 | 后置 | ⚠️ 是"解释与建议"，不是约束 |
| harness 操作链（`RULE-001..004`） | 4 | 链结束后（CI） | ⚠️ 覆盖 25% 写动词，且含 4 个实现缺陷 |
| **缺失**（`MISS-01..05`） | 5 | — | ✗ |

规则逐条的缺陷分析见 [`model/rules.yml`](model/rules.yml) 与
审计报告[第 6 节](00-atomicity-audit.md#6-规则rule)。
