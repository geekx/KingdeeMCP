"""AIP Logic —— 判断层。

在这一层之前，「这一步能不能做」有**四种答法**，分别长在四个地方：

  base/ontology.py     check_verb_applies / check_link / check_state → 抛异常
  base/objects.py      ActionType.availability                       → {"enabled": ...}
  base/validate_profile.py  validate                                 → errs / warns 两个列表
  saga/engine.py       守卫求值                                       → 又一套

四份实现回答同一个问题，于是它们可以彼此矛盾而没人发现——`availability()`
只看状态，压根不检查动词是否适用于这个名词，所以对象卡上会出现一个
「可用」的动作，真去执行时被 PRE-01 拦下。

这一层把判断收拢成**声明式的、以本体为参数的逻辑函数**：

  * **纯函数。** 输入 (本体, 事实)，输出 Decision，不发请求、不读文件、不看时钟。
    因此可以离线跑、可以在 CI 里跑、可以做成毫秒级的独立服务。
  * **声明依赖。** 每个函数写明自己需要哪些事实（`needs`）。缺了就报
    undetermined 并说明缺什么，而不是当作通过。
  * **可反射。** 注册表把规则号映射到实现，`registry.yml` 的 `enforced_by`
    指到这里，CI 校验指针不落空。

判断层不执行任何动作，也不知道金蝶存在。它只回答「按本体，这样做成不成立」。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from aip.decide import BLOCK, INFO, WARN, Decision, Reason, merge

# 这几个动词随表单而异：金蝶把它们做成"操作"而非独立接口，二开单常需显式
# 指定操作编码。原来这张表埋在 objects.py 里，只在渲染卡片时用得到，
# 真正执行的路径看不见它。
NEEDS_OPERATION_CODE = frozenset({"void", "close", "unclose", "forbid", "enable", "cancel"})


@dataclass
class Facts:
    """判断所依据的事实。全部可选——缺什么由逻辑函数自己报 undetermined。

    刻意不放 dispatcher、不放 http 客户端：事实是**已经查到的东西**，
    判断层不负责去查。谁需要补事实，由 `missing()` 告诉他该查什么。
    """
    ontology: Any
    verb: Optional[str] = None
    noun: Optional[str] = None
    state: Optional[str] = None
    target: Optional[str] = None          # 下推目标名词
    params: dict = field(default_factory=dict)
    step: Optional[dict] = None           # Saga 步骤（判断补偿是否齐备时用）

    def get(self, name: str) -> Any:
        return getattr(self, name, None)


@dataclass(frozen=True)
class LogicFn:
    """一条已登记的逻辑函数。"""
    id: str
    zh: str
    rule: str                    # 它执行的是注册表里的哪条规则
    needs: tuple[str, ...]       # 需要哪些事实
    fn: Callable[[Facts], Decision]
    doc: str = ""

    def applicable(self, f: Facts) -> bool:
        """事实齐了才评。缺事实由 evaluate 统一报 undetermined，
        而不是让每个函数自己写一遍 if x is None。"""
        return all(f.get(n) is not None for n in self.needs)

    def missing(self, f: Facts) -> tuple[str, ...]:
        return tuple(n for n in self.needs if f.get(n) is None)

    def to_dict(self) -> dict:
        return {"id": self.id, "zh": self.zh, "rule": self.rule,
                "needs": list(self.needs), "doc": self.doc.strip()}


REGISTRY: dict[str, LogicFn] = {}

_NEED_ZH = {"verb": "动词", "noun": "名词（单据类型）", "state": "对象当前状态",
            "target": "下推目标单类型", "step": "Saga 步骤定义"}


def logic(id: str, zh: str, rule: str, needs: tuple[str, ...] = ()):
    def deco(fn: Callable[[Facts], Decision]) -> Callable[[Facts], Decision]:
        if id in REGISTRY:
            raise ValueError(f"逻辑函数 {id} 重复登记")
        REGISTRY[id] = LogicFn(id=id, zh=zh, rule=rule, needs=needs,
                               fn=fn, doc=fn.__doc__ or "")
        return fn
    return deco


# ─────────────────────────────────────────────────────────────
# 逻辑函数
# ─────────────────────────────────────────────────────────────

@logic("AIP-01", "动词是否适用于该名词", rule="PRE-01", needs=("verb", "noun"))
def verb_applies(f: Facts) -> Decision:
    """视图没有生命周期、基础资料没有审核流——动词不是对谁都能用。"""
    o = f.ontology
    v, n = o.verb(f.verb), o.resolve_noun(f.noun)
    if v.name in n.allowed_verbs:
        return Decision()
    why = {
        "view": "这是查询视图，没有自己的生命周期，只能 query",
        "system": "这是系统对象（用户/角色/权限等），由金蝶系统管理，只能 query",
        "master_data": "基础资料没有审核流，只有 save/forbid/enable",
    }.get(n.category, "该动词不适用于此名词")
    return Decision((Reason(
        rule="PRE-01", severity=BLOCK,
        message=f"动词 {v.name!r}({v.zh}) 不适用于 {n.form_id}({n.zh})：{why}。",
        fix=f"可用动词：{sorted(n.allowed_verbs)}"),))


@logic("AIP-02", "下推关系是否已登记", rule="PRE-02", needs=("noun", "target"))
def link_registered(f: Facts) -> Decision:
    """没登记的下推不是"可能可以"，是**不知道**——金蝶会照转换规则报一串
    看不懂的错。宁可在发请求前拦下并说清该补哪一行。"""
    o = f.ontology
    s, t = o.resolve_noun(f.noun), o.resolve_noun(f.target)
    if o._link_index.get((s.form_id, t.form_id)) is not None:
        return Decision()
    outs = [l["to"] for l in o.links if l["from"] == s.form_id]
    return Decision((Reason(
        rule="PRE-02", severity=BLOCK,
        message=f"未登记的下推关系 {s.form_id} → {t.form_id}。"
                + (f"{s.form_id} 已登记的目标单：{outs}" if outs else
                   f"{s.form_id} 没有任何已登记的下推目标。"),
        fix="若确认该转换规则存在，请补进 base/registry.yml:links 而不是绕过校验。"),))


@logic("AIP-03", "当前状态是否满足动词要求", rule="PRE-03", needs=("verb",))
def state_satisfied(f: Facts) -> Decision:
    """状态取不到时报 undetermined，**不当作通过**。

    刻意不在这里自动补一次查询：那会把每个写操作的往返翻倍。缺状态是
    调用方的事实缺口，由它决定是先查还是接受风险。
    """
    v = f.ontology.verb(f.verb)
    if not v.requires_state:
        return Decision()
    if f.state is None:
        return Decision(undetermined=(Reason(
            rule="PRE-03", severity=BLOCK,
            message=f"不知道对象当前状态，判不了。{v.name}({v.zh}) 要求处于 "
                    f"{list(v.requires_state)} 之一。",
            fix="先 kd_read 取当前状态，或显式接受未核实的风险。"),))
    if f.state in v.requires_state:
        return Decision()
    reach = _verb_reaching(f.ontology, v.requires_state[0])
    return Decision((Reason(
        rule="PRE-03", severity=BLOCK,
        message=f"{v.name}({v.zh}) 要求对象处于 {list(v.requires_state)} 之一，"
                f"当前为 {f.state!r}。",
        fix=(f"可先执行 {reach} 到达所需状态。" if reach else "")),))


@logic("AIP-04", "是否退不回来（需人工确认）", rule="AIP-04", needs=("verb",))
def irreversible(f: Facts) -> Decision:
    """没有逆动词的写动作做完就退不回来了，执行前必须有人点头。

    注意 inverse 与 compensation 是两回事：push 没有逆动词（不存在 unpush），
    但它的产物可以 delete 掉。这里只看**这个对象本身**能不能退回去。
    """
    v = f.ontology.verb(f.verb)
    if not v.destructive:
        return Decision()
    comp = getattr(v, "compensation", None)
    fix = ("这一步没有逆动词，做完退不回来，执行前请人工确认。"
           if not comp else
           f"这一步没有逆动词；若失败，产物只能靠 {comp} 清理。执行前请人工确认。")
    return Decision((Reason(rule="AIP-04", severity=WARN,
                            message=f"{v.name}({v.zh}) 不可逆。", fix=fix),))


@logic("AIP-05", "是否需要显式操作编码", rule="AIP-05", needs=("verb", "noun"))
def needs_operation_code(f: Facts) -> Decision:
    """作废/关闭/禁用这些走金蝶"操作"接口，二开单的操作编码常被改过。

    没给编码不阻断——多数标准单用默认值能过。但要说出来：这是二开环境里
    最常见的一类"参数都对却报错"，事后翻日志远不如事前一句提醒。
    """
    if f.verb not in NEEDS_OPERATION_CODE:
        return Decision()
    n = f.ontology.resolve_noun(f.noun)
    if (f.params or {}).get("operation"):
        return Decision()
    custom = (n.operations or {}).get(f.verb) if hasattr(n, "operations") else None
    if custom:
        return Decision((Reason(
            rule="AIP-05", severity=INFO,
            message=f"{f.verb} 将使用本租户为 {n.form_id} 配置的操作编码 {custom!r}。"),))
    return Decision((Reason(
        rule="AIP-05", severity=INFO,
        message=f"{f.verb}({f.ontology.verb(f.verb).zh}) 走金蝶「操作」接口，"
                f"{n.form_id} 未配置操作编码，将用默认值。",
        fix="二开单若报『操作不存在』，在租户 profile 的 operations 里补上编码。"),))


@logic("AIP-06", "Saga 写步骤是否备有补偿", rule="SAGA-03", needs=("step",))
def step_compensable(f: Facts) -> Decision:
    """写了一半失败，已生效的步骤得能退回去。

    补偿**不靠推断**：猜错的补偿动作比不补偿更糟——它会去动一张不该动的单。
    没有声明也没有本体可继承时，如实说"这一步退不回来"。
    """
    st = f.step or {}
    kind = st.get("做")
    if kind in (None, "确认", "检查"):
        return Decision()
    declared = st.get("补偿")
    if declared:
        return Decision()
    verb = "push" if kind == "下推" else kind
    v = f.ontology.verbs.get(verb)
    inherited = getattr(v, "compensation", None) if v else None
    if inherited:
        return Decision((Reason(
            rule="SAGA-03", severity=INFO,
            message=f"『{kind}』未声明补偿，按本体继承 {inherited}。"),))
    return Decision((Reason(
        rule="SAGA-03", severity=WARN,
        message=f"『{kind}』没有补偿动作，这一步一旦生效就退不回来。",
        fix="若该步骤确实不可逆，把它排在整个操作的最后，让前面的步骤仍可回滚。"),))


def _verb_reaching(o, state: str) -> Optional[str]:
    for v in o.verbs.values():
        if v.to_state == state:
            return v.name
    return None


# ─────────────────────────────────────────────────────────────
# 求值
# ─────────────────────────────────────────────────────────────

def evaluate(facts: Facts, only: Optional[list[str]] = None) -> Decision:
    """把所有适用的逻辑函数跑一遍，一次给全部理由。

    这里刻意**不短路**。短路能省几微秒，代价是调用方修完第一个问题、
    再调一次、撞上第二个——对 agent 来说那是一整轮重新思考。
    """
    fns = [REGISTRY[i] for i in (only or sorted(REGISTRY))]
    parts: list[Decision] = []
    for lf in fns:
        miss = lf.missing(facts)
        if miss:
            # 只有当这条函数确实与本次判断有关时，缺事实才算悬而未决。
            # 否则问一个「审核能不能做」的人会被告知缺"下推目标"。
            if _relevant(lf, facts):
                parts.append(Decision(undetermined=(Reason(
                    rule=lf.rule, severity=BLOCK,
                    message=f"{lf.zh}：缺少 {[_NEED_ZH.get(m, m) for m in miss]}，判不了。",
                    fix=f"补齐后重新判断（逻辑函数 {lf.id}）。"),)))
            continue
        parts.append(lf.fn(facts))
    return merge(parts)


def _relevant(lf: LogicFn, f: Facts) -> bool:
    """这条函数管不管本次判断。

    判断依据是**问题的形状**，不是有没有凑巧给了参数：问"下推"才轮到
    AIP-02，问 Saga 步骤才轮到 AIP-06。
    """
    if "target" in lf.needs:
        return f.target is not None
    if "step" in lf.needs:
        return f.step is not None
    if "noun" in lf.needs and f.noun is None:
        return False
    return f.verb is not None


def can(ontology, verb: str, noun: str, state: Optional[str] = None,
        target: Optional[str] = None, params: Optional[dict] = None) -> Decision:
    """最常用的一问：现在能不能对这个对象做这个动作。"""
    return evaluate(Facts(ontology=ontology, verb=verb, noun=noun, state=state,
                          target=target, params=params or {}))


def describe() -> list[dict]:
    """自我说明：有哪些逻辑函数、各自执行哪条规则、需要哪些事实。"""
    return [REGISTRY[i].to_dict() for i in sorted(REGISTRY)]
