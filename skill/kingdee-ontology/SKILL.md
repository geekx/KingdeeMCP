---
name: kingdee-ontology
description: 以对象为中心操作金蝶云星空。打开一个对象就能看到它的属性、当前状态、此刻能对它做哪些动作(不能做的会说明原因)、以及它连到哪些别的对象。用于查询单据、执行提交/审核/关闭/下推等动作、执行本租户定义的业务操作(如"销售开票")、排查卡住的单据。当用户提到金蝶、K3Cloud、云星空、采购订单、销售订单、入库单、下推、审核单据、开票、或问某张单为什么卡住、能做什么时使用。
---

# 金蝶对象操作

**以对象为中心,不是以工具为中心。** 不要先想"该调哪个工具",
先打开对象,系统会告诉你此刻能做什么。

底座只有 10 个工具,对象类型(84 个)和它们的动作、链接全部按需查,不常驻上下文。
所以第一步永远是打开对象,不要凭记忆猜 form_id——各家二开不同,记忆一定过期。

## 主循环:打开 → 看能做什么 → 做

```
kd_object(noun="采购订单", id="CGDD000231")
```

一次返回四样东西:

| 字段 | 是什么 |
|---|---|
| `properties` | 属性及其当前值 |
| `state` / `state_zh` | 当前状态(从属性反推的规范码,如 `C:已审核`) |
| `actions` | **此刻**能做哪些动作,不能做的带 `reason` |
| `links` | 它连到哪些对象,方向是 outgoing(能推出)还是 incoming(由谁推来) |

然后照着 `actions` 里 `enabled=true` 的动词调 `kd_act`。

### 关键:不要自己判断能不能做

`actions[].enabled` 已经把状态前置条件算过了。看到 `enabled=false` 就读 `reason`,
它会说清"要求什么状态、当前是什么"。**不要绕过它硬发请求**——
那只会换来服务端一个更难懂的报错。

`unverified: true` 表示状态没取到,可用性未经核实。此时可以试,但要预期可能被拒。

## 不知道对象叫什么时

```
kd_object(search="采购")              按关键字搜类型
kd_object(category="bill")            按类别列:bill/master_data/view/system
kd_object(noun="采购订单")             类型卡片:这类对象长什么样、能做什么
```

类型卡片和实例卡片**同形状**,只是没有实例数据——不必学两套结构。

## 动作:先看契约再决定怎么重试

`kd_act` 的返回体一定带 `contract`。**失败后先读它再决定动作**:

| `atomicity` | 失败后怎么办 |
|---|---|
| `per_item` | 部分成功。只重试 `failed` 里的目标,别整批重发——已成功的不会回滚 |
| `server_defined` | **无法知道哪些已生效**。必须 `kd_query` 逐个查证后再重试 |
| `atomic` | 系统状态未变,改了直接重试 |

还有两个字段决定要不要先问人:

- `destructive: true`(无逆动词)——**执行前必须让用户明确点头**,即使用户在催
- `inverse: "unaudit"`——做错了可以用这个动词退回

## 导航到下游单据

```
kd_object(noun="采购订单", id="CGDD000231", navigate_to="采购入库单")
```

返回的是**该怎么查**,不是直接查出来。因为下游单据引用源单的字段名
(`FSrcBillNo` / `FSourceBillNo` / …)随表单和二开而异,静态推断不出唯一答案。
拿 `candidate_filters` 逐个试,试通之后**告诉用户把它写进 profile 固化下来**,
此后就不必再试。

## 新建单据

```
kd_describe(what="template", key="销售订单")     取已验证的骨架
kd_act(verb="save", noun="销售订单", model={...}, dry_run=True)   先校验不写入
kd_act(verb="save", noun="销售订单", model={...})                 确认无误再存
```

`dry_run` 只对 `save` 有效——其它动词没有预演接口。

## 多步业务操作

租户可能已经把常做的事定义成入口:

```
kd_describe(what="operations")
kd_run(operation="销售开票", targets=["XSDD001"])
```

**未确认时 `kd_run` 不做任何写操作**,只返回执行计划。把计划念给用户听,
得到同意后再带 `confirmed=True` 重跑。

中途失败会返回 `left_behind`——已经产生但流程没走完的单据。这些**不会自动回滚**,
要告诉用户并帮他决定:续做完成,还是清理掉。

## 排查"这单怎么卡住了"

```
kd_audit(scope="dangling")
```

列出未清算的中间态:写操作已生效、整条链没走完、也没有补偿记录的单据。

## 五条硬规矩

1. **不要凭记忆猜 form_id 或下推关系**,一律先 `kd_object`。
2. **`destructive` 的动作执行前必须让用户确认**,即使用户催。
3. **`outcome: "unknown"` 不等于失败**——服务端可能已生效。重试前先查证,否则会重复建单。
4. **部分成功时不要整批重发**,只针对 `failed`。
5. **缺某个对象类型或下推关系时不要绕过校验**,告诉用户去
   `profiles/<租户>/profile.yml` 补一行(业务人员可自行填写,见 `profiles/README.md`)。

## 更多

- [`references/verbs.md`](references/verbs.md) — 14 个动词的完整契约表与状态词表
- [`references/troubleshooting.md`](references/troubleshooting.md) — 常见报错与对策
- 可视化浏览对象、模拟动作可用性、可视化编排业务操作:见仓库 README 里的 Ontology Explorer
