# Ontology Explorer（界面形态）

**[打开界面 →](https://claude.ai/code/artifact/91595855-e6de-4182-8369-ddaa7c09fd50)**

以对象为中心浏览本体——这是 MCP 的**界面形态**，与 `skill/kingdee-ontology/`
（Skill 形态）成对：同一套本体，两种操作面。

## 它操作什么

**本体定义，不是账套。** 页面不连金蝶，所以：

- 能做的：看有哪些对象类型、它们的属性与状态机、能施加哪些动作、
  某个状态下哪些动作可用（不可用的为什么）、对象之间怎么连、
  以及把一串动作编排成业务操作并导出配置；
- 不能做的：查真实单据、执行真实动作。

动作面板给出的是**可复制的 `kd_act(…)` 调用**，由你在 MCP 里执行。

## 三个视图

**对象** — 主视图。左栏浏览 84 个对象类型，中栏是对象卡片
（属性 · 标识 · 链接），右栏是动作检查器。

中间那条**状态轨道**用的是金蝶自己的字母码（`Z 暂存 / A 创建 / B 审核中 /
C 已审核 / D 重新审核`，加上 `CLOSED / VOID / DELETED`）。点任一状态，
右侧动作可用性立刻重算：已审核的单不能再审核，但能反审核、下推、作废、整单关闭。
**不可用的动作会说清「要求什么状态、当前是什么」**——灰掉却不解释比没有按钮更让人困惑。

**链路图** — 9 条已登记的下推关系。实线=已实证/文档记载，
虚线琥珀=**存疑**（`PRD_PickMtrl → PRD_Instock`，见审计 L-2，需真实账套验证）。

**业务操作编排** — 用你自己的说法给一件事命名，拼出步骤，导出 YAML 贴进
`profiles/<租户>/profile.yml` 的 `operations` 段。之后直接说「帮我做销售开票」就能用。
导出的 YAML 直接可过 `python3 -m base.validate_profile`，不需要再改格式。

## 重新生成

页面里的数据由本体导出，改了 `base/registry.yml` 之后重跑：

```bash
python3 tools/ontology/export_for_ui.py            # → ui/ontology.json
python3 tools/ontology/build_ui.py                 # → ui/explorer.html
```

`_shell.html` 是模板（含 `__ONTOLOGY_JSON__` 占位），`explorer.html` 是注入数据后的成品。
成品不要手改——下次重新生成会覆盖。

## 一处刻意的重复

动作可用性的判定（当前状态 ∈ `requires_state`）在 Python
（`base/objects.py:ActionType.availability`）和页面 JS 里各有一份。

规则只有一行，且两边都从 `verbs[verb].requires_state` 取——**事实来源仍是单一的**，
重复的只是那一行判断。把整张「状态 × 动作」可用性矩阵导出来存反而更糟：
实测导出体积从 156 KB 涨到 581 KB，而矩阵本身仍然是同一条规则算出来的。
