---
name: kingdee-ontology
description: 通过 MCP 底座(kd_describe/kd_query/kd_act/kd_push/kd_run/kd_audit)操作金蝶云星空。用于查询单据、执行提交/审核/关闭等写动作、下推生成下游单据、执行本租户定义的业务操作(如"销售开票")、以及排查未清算的中间态。当用户提到金蝶、K3Cloud、云星空、采购订单、销售订单、入库单、下推、审核单据、开票、或询问某张单为什么卡住时使用。
---

# 金蝶云星空操作

底座只有 7 个工具,**实例(有哪些单据、哪些下推关系、本租户怎么定义业务操作)全部按需查**,
不常驻上下文。所以第一步永远是问本体,不要凭记忆猜 form_id。

## 先看有没有现成的业务操作

```
kd_describe(what="operations")
```

租户会把常做的事定义成入口,比如「销售开票」「采购收货入库」。有就直接用:

```
kd_run(operation="销售开票", targets=["XSDD001"])
```

**未确认时 `kd_run` 不做任何写操作**,只返回执行计划和待确认问题。
把计划念给用户听,得到明确同意后再带 `confirmed=True` 重跑。

## 没有现成入口时,自己组

### 1. 先解析名词

```
kd_describe(what="nouns", key="采购订单")
```

返回 form_id、可用动词、可下推目标、默认字段集。**支持中文名和别名**,
不必知道 `PUR_PurchaseOrder` 也能开始。

### 2. 查询

```
kd_query(noun="采购订单", filter="FDocumentStatus='C' AND FDate>='2026-01-01'", top=20)
```

字段留空即用该单据的默认字段集。要更多字段再显式传 `fields`。

### 3. 写动作

```
kd_act(verb="audit", noun="采购订单", targets=["100231","100232"])
```

返回体一定带 `contract`,**先看它再决定失败后怎么办**:

| contract 字段 | 怎么用 |
|---|---|
| `atomicity: per_item` | 部分成功。只重试 `failed` 里的目标,别整批重发 |
| `atomicity: server_defined` | 批量失败时**无法知道哪些已生效**,必须 `kd_query` 逐个查证 |
| `idempotent: false` | 不能盲目重试 |
| `destructive: true` | 无逆动词。执行前必须让用户明确点头 |
| `inverse: "unaudit"` | 做错了可以用这个动词退回 |

动词不适用于该单据时,**在发请求前**就会被拦下,并告诉你该单据可用哪些动词。

### 4. 下推

```
kd_push(source="采购订单", target="采购入库单", source_bill_nos=["CGDD000231"])
```

未登记的下推关系会被前置规则拦下。**`kd_push` 不自动提交审核** ——
目标单是草稿,要生效需显式再调 `kd_act`。这是刻意的:自动串联会在中途失败时
留下无人认领的中间态。

## 排查"这单怎么卡住了"

```
kd_audit(scope="dangling")
```

列出**未清算的中间态**:写操作已生效、但整条链没走到终态、也没有补偿记录的单据。
`left_objects` 里的单据需要人继续处理或清理,系统不会自动回滚。

## 五条硬规矩

1. **不要凭记忆猜 form_id 或下推关系**,一律 `kd_describe`。各家二开不同,记忆一定过期。
2. **destructive 的动作(delete/void/push/close)执行前必须让用户确认**,即使用户催。
3. **`outcome: "unknown"` 不等于失败** —— 服务端可能已生效。重试前先 `kd_query` 查证,
   否则会重复建单。
4. **部分成功时不要整批重发**,只针对 `failed`。已成功的部分不会回滚。
5. **本租户缺某个单据/下推关系时,不要绕过校验**,而是告诉用户去
   `profiles/<租户>/profile.yml` 补一行(业务人员可自行填写,见 `profiles/README.md`)。

## 更多

- [`references/verbs.md`](references/verbs.md) — 14 个动词的完整契约表
- [`references/troubleshooting.md`](references/troubleshooting.md) — 常见报错与对策
- 租户配置怎么填:`profiles/README.md`
