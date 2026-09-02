"""对象层 —— 把本体从「审计产物」变成「操作面」。

设计参照 Palantir Foundry 的 Ontology：使用者面对的不是一堆工具，而是**对象**。
打开一个对象，看到它的属性、它现在处在什么状态、它连到哪些别的对象、
以及**此刻能对它做哪些动作**（不能做的要说明为什么不能）。

三个核心概念，全部由 base/registry.yml 推导，不额外维护第二份定义：

    ObjectType   名词 + 属性 + 状态机 + 可用动作 + 出入链接
    ActionType   动词 + 参数 schema + 前置条件 + 契约（原子性/可逆性）
    ObjectCard   某个具体实例：属性值 + 当前状态 + 此刻可用/不可用的动作 + 可导航的链接

与 dispatch.py 的分工：dispatch 负责「执行」，objects 负责「呈现能做什么」。
UI 和 Skill 都消费这一层，所以它必须是纯数据、可序列化、不含表现逻辑。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from aip import Decision
from aip.logic import NEEDS_OPERATION_CODE, can as aip_can
from base.ontology import Noun, Ontology, OntologyError, Verb

# 动词 → 参数 schema。UI 据此生成表单，Skill 据此知道要问用户什么。
# type 取值：ids（单据标识列表）/ model（单据字段对象）/ enum / string / bool / noun_ref
_ACTION_PARAMS: dict[str, list[dict]] = {
    "save": [
        {"name": "model", "type": "model", "required": True,
         "label": "单据字段", "hint": "可先用 kd_describe(what='template') 取骨架"},
    ],
    "push": [
        {"name": "target", "type": "noun_ref", "required": True, "label": "目标单据",
         "hint": "只能选已登记的下推目标"},
        {"name": "source_bill_nos", "type": "ids", "required": True, "label": "源单编号",
         "hint": "注意是单据编号 FBillNo，不是内码 FID"},
        {"name": "rule_id", "type": "string", "required": False, "label": "转换规则 ID"},
    ],
}
_DEFAULT_PARAMS = [
    {"name": "targets", "type": "ids", "required": True, "label": "单据内码 FID"},
]
# 这几个动词随表单而异，二开单常需显式指定操作编码。
# 单一出处在判断层（aip.logic），这里只是取别名——两处各留一份迟早会分叉。
_NEEDS_OPERATION = NEEDS_OPERATION_CODE


@dataclass
class ActionType:
    """一个可对某类对象施加的动作。"""
    verb: str
    zh: str
    object_type: str
    params: list[dict]
    requires_state: tuple[str, ...]
    to_state: Optional[str]
    atomicity: str
    idempotent: bool
    inverse: Optional[str]
    destructive: bool
    # 判断要靠本体，不能只靠这张卡片自己知道的几个字段。原来的
    # availability() 自带一份状态比对逻辑，与 check_state 各写一遍；
    # 卡片还漏报了两件执行时才知道的事（不可逆、需操作编码）。
    ontology: Any = field(default=None, repr=False, compare=False)

    @property
    def needs_confirmation(self) -> bool:
        """无逆动词的写动作必须先让人点头。"""
        return self.destructive

    def decide(self, current_state: Optional[str] = None) -> Decision:
        """判断层的完整结论。UI 之外的调用方应当读这个，而不是 availability()。"""
        return aip_can(self.ontology, self.verb, self.object_type, state=current_state)

    def availability(self, current_state: Optional[str]) -> dict:
        """此刻能不能做。不能做时必须说清**为什么**和**怎么办**——
        一个灰掉却不解释的按钮比没有按钮更让人困惑。

        这里是判断层结论的**界面投影**，两者刻意不同义：
          enabled  按钮点不点得动。状态未知时仍可点（点了会先去查），
                   所以它比 Decision.allowed 宽松。
          unverified  这个"能点"是没核实过的。
        程序判断请用 decide()——`allowed` 在事实不全时为 False，
        不会把"不知道"读成"可以"。
        """
        d = self.decide(current_state)
        out: dict = {"enabled": not d.blocks}
        if d.blocks:
            out["reason"] = "；".join(r.text for r in d.blocks)
        if d.undetermined:
            out["unverified"] = True
            out["note"] = "；".join(r.text for r in d.undetermined)
        return out

    def to_dict(self) -> dict:
        return {"verb": self.verb, "zh": self.zh, "object_type": self.object_type,
                "params": self.params, "requires_state": list(self.requires_state),
                "to_state": self.to_state, "atomicity": self.atomicity,
                "idempotent": self.idempotent, "inverse": self.inverse,
                "destructive": self.destructive,
                "needs_confirmation": self.needs_confirmation}


@dataclass
class LinkRef:
    """对象之间的一条可导航关系。"""
    name: str
    direction: str            # outgoing（我能推出它）| incoming（它由我推出）
    kind: str                 # push_down
    target_type: str
    target_zh: str
    verified: str
    note: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "direction": self.direction, "kind": self.kind,
                "target_type": self.target_type, "target_zh": self.target_zh,
                "verified": self.verified, "note": self.note}


@dataclass
class ObjectType:
    form_id: str
    zh: str
    category: str
    alias: tuple[str, ...]
    properties: list[dict]
    title_property: str
    id_property: str
    states: list[str]
    actions: list[ActionType]
    links: list[LinkRef]
    system_endpoint: str = ""

    def to_dict(self, with_actions: bool = True) -> dict:
        d = {"form_id": self.form_id, "zh": self.zh, "category": self.category,
             "alias": list(self.alias), "properties": self.properties,
             "title_property": self.title_property, "id_property": self.id_property,
             "states": self.states, "links": [l.to_dict() for l in self.links],
             "system_endpoint": self.system_endpoint or None}
        if with_actions:
            d["actions"] = [a.to_dict() for a in self.actions]
        return d


# 单据的标识属性：内码用于写操作，编号用于下推与人读
_ID_CANDIDATES = ("FID", "FMaterialId", "FSupplierId", "FUserID", "FRoleID", "FDetailId")
_TITLE_CANDIDATES = ("FBillNo", "FNumber", "FName", "FConfigKey", "FMoBillNo")


def _split_fields(default_fields: str) -> list[dict]:
    out: list[dict] = []
    for raw in (default_fields or "").split(","):
        f = raw.strip()
        if not f:
            continue
        out.append({"name": f, "is_lookup": "." in f,
                    "base": f.split(".")[0]})
    return out


def _pick(props: list[dict], candidates: tuple[str, ...], fallback: str) -> str:
    names = [p["name"] for p in props]
    bases = [p["base"] for p in props]
    for c in candidates:
        if c in names:
            return c
        if c in bases:
            return names[bases.index(c)]
    return names[0] if names else fallback


class ObjectModel:
    """由 Ontology 推导出的对象模型。纯读，无副作用。"""

    def __init__(self, onto: Ontology):
        self.o = onto

    # ── 类型层 ────────────────────────────────────────────────
    def object_type(self, ref: str) -> ObjectType:
        n = self.o.resolve_noun(ref)
        props = _split_fields(n.default_fields)
        return ObjectType(
            form_id=n.form_id, zh=n.zh, category=n.category, alias=n.alias,
            properties=props,
            id_property=_pick(props, _ID_CANDIDATES, "FID"),
            title_property=_pick(props, _TITLE_CANDIDATES, "FID"),
            states=self._states_for(n),
            actions=self._actions_for(n),
            links=self._links_for(n),
            system_endpoint=n.system_endpoint,
        )

    def _states_for(self, n: Noun) -> list[str]:
        if n.category == "bill":
            return [s for s in self.o.states
                    if s not in ("ENABLED", "FORBIDDEN", "NONEXISTENT")]
        if n.category == "master_data":
            return ["ENABLED", "FORBIDDEN"]
        return []

    def _actions_for(self, n: Noun) -> list[ActionType]:
        out: list[ActionType] = []
        for verb in sorted(n.allowed_verbs):
            v = self.o.verb(verb)
            if v.kind != "write":
                continue
            params = list(_ACTION_PARAMS.get(verb, _DEFAULT_PARAMS))
            if verb in _NEEDS_OPERATION:
                params = params + [{
                    "name": "operation", "type": "string", "required": False,
                    "label": "操作编码", "hint": "二开单据常需显式指定，如 YLBillClose"}]
            out.append(ActionType(
                verb=v.name, zh=v.zh, object_type=n.form_id, params=params,
                requires_state=v.requires_state, to_state=v.to_state,
                atomicity=v.atomicity, idempotent=v.idempotent,
                inverse=v.inverse, destructive=v.destructive,
                ontology=self.o))
        return out

    def _links_for(self, n: Noun) -> list[LinkRef]:
        out: list[LinkRef] = []
        for l in self.o.links:
            if l.get("from") == n.form_id:
                t = self.o.nouns.get(l["to"])
                out.append(LinkRef(name=l.get("zh") or f"{l['from']}→{l['to']}",
                                   direction="outgoing", kind="push_down",
                                   target_type=l["to"], target_zh=t.zh if t else l["to"],
                                   verified=l.get("verified", "documented"),
                                   note=l.get("note", "")))
            elif l.get("to") == n.form_id:
                s = self.o.nouns.get(l["from"])
                out.append(LinkRef(name=l.get("zh") or f"{l['from']}→{l['to']}",
                                   direction="incoming", kind="push_down",
                                   target_type=l["from"], target_zh=s.zh if s else l["from"],
                                   verified=l.get("verified", "documented"),
                                   note=l.get("note", "")))
        return out

    # ── 实例层 ────────────────────────────────────────────────
    def state_of(self, ot: ObjectType, props: dict) -> Optional[str]:
        """从属性值反推规范状态码。取不到就返回 None，不猜。"""
        for code, meta in self.o.states.items():
            fld, val = meta.get("field"), meta.get("value")
            if not fld or val is None:
                continue
            actual = props.get(fld) or props.get(fld.upper()) or props.get(fld.lower())
            if actual is not None and str(actual) == str(val):
                return code
        return None

    def card(self, ref: str, props: Optional[dict] = None) -> dict:
        """对象卡片：属性 + 状态 + 此刻可用/不可用的动作 + 可导航链接。

        props 为空时返回「类型卡片」——同样的形状，只是没有实例数据。
        UI 和 Skill 用同一个形状，省得两边各写一套。
        """
        ot = self.object_type(ref)
        props = props or {}
        state = self.state_of(ot, props) if props else None
        actions = []
        for a in ot.actions:
            av = a.availability(state)
            actions.append({**a.to_dict(), **av})
        return {
            "object_type": ot.form_id, "zh": ot.zh, "category": ot.category,
            "is_instance": bool(props),
            "id": props.get(ot.id_property) if props else None,
            "title": props.get(ot.title_property) if props else None,
            "state": state,
            "state_zh": (self.o.states.get(state) or {}).get("zh") if state else None,
            "properties": [
                {**p, "value": props.get(p["name"])} for p in ot.properties
            ] if props else ot.properties,
            "actions": actions,
            "links": [l.to_dict() for l in ot.links],
            "operations": self._operations_for(ot.form_id),
        }

    def _operations_for(self, form_id: str) -> list[dict]:
        """本租户定义的业务操作里，哪些会碰到这类对象。

        只给原子动作、不给现成流程，会让人以为得自己一步步拼——
        而这类事租户往往已经编排好了。
        """
        out = []
        for op in self.o.operations.values():
            touched = set()
            for st in op.steps:
                for k in ("从", "到", "对象"):
                    v = st.get(k)
                    if v:
                        try:
                            touched.add(self.o.resolve_noun(v).form_id)
                        except OntologyError:
                            pass
            if form_id in touched:
                out.append({"key": op.key, "zh": op.zh, "owner": op.owner,
                            "desc": op.desc, "steps": len(op.steps),
                            "starts_here": bool(op.steps) and
                            self._first_noun(op) == form_id})
        return out

    def _first_noun(self, op) -> Optional[str]:
        for st in op.steps:
            v = st.get("从") or st.get("对象")
            if v:
                try:
                    return self.o.resolve_noun(v).form_id
                except OntologyError:
                    return None
        return None

    def navigate(self, ref: str, link_target: str, bill_no: str) -> dict:
        """给出「从这个对象跳到它的下游单据」该怎么查。

        **不直接执行**：下游单据引用源单的字段名（FSrcBillNo / FSourceBillNo /
        _LK 关联表）随表单和二开而异，静态推断不出唯一答案。
        这里给出候选过滤式，由调用方用 kd_query 执行；
        租户确认了正确字段后可在 profile 的 links 段写死 link_filter。
        """
        link = self.o.check_link(ref, link_target) \
            if self.o.resolve_noun(ref).form_id == self.o.resolve_noun(ref).form_id else None
        src = self.o.resolve_noun(ref)
        tgt = self.o.resolve_noun(link_target)
        explicit = (link or {}).get("link_filter")
        if explicit:
            return {"from": src.form_id, "to": tgt.form_id, "confirmed": True,
                    "filter": explicit.replace("{bill_no}", bill_no),
                    "next": f"kd_query(noun='{tgt.form_id}', filter=…)"}
        candidates = [f"{f}='{bill_no}'" for f in
                      ("FSrcBillNo", "FSourceBillNo", "FSrcBillNumber", "FMoBillNo")]
        return {
            "from": src.form_id, "to": tgt.form_id, "confirmed": False,
            "candidate_filters": candidates,
            "why": ("下游单据引用源单的字段名随表单与二开而异，静态推断不出唯一答案。"
                    "请逐个试，或用 kd_describe(what='fields', key='%s') 看本账套的真实字段。"
                    % tgt.form_id),
            "remember": ("确认后请在 profiles/<租户>/profile.yml 的对应 links 条目上加 "
                         "link_filter: \"FSrcBillNo='{bill_no}'\"，此后就不必再试。"),
        }

    def identify(self, bill_no: str) -> dict:
        """「这张单是什么单？」——按编号前缀猜类型。

        前缀由租户的编码规则决定（名词 SYS_NumberRule），所以这是**启发式**：
        命中只说明"很可能是"，没命中也不说明"不是"。
        因此返回的是**候选列表 + 置信度 + 依据**，不是一个断言。
        """
        no = (bill_no or "").strip().upper()
        if not no:
            raise OntologyError("要识别的单据编号不能为空")
        hits = []
        for prefix, meta in self.o.bill_prefixes.items():
            if not no.startswith(prefix.upper()):
                continue
            fid = meta.get("form_id")
            n = self.o.nouns.get(fid)
            hits.append({"form_id": fid, "zh": n.zh if n else fid,
                         "prefix": prefix, "confidence": "likely",
                         "evidence": meta.get("evidence", "")})
        # 长前缀优先（CGRKD 比 CGRK 更具体），并按类型去重——
        # 同一个类型登记了多个前缀时，重复列出只是噪声。
        hits.sort(key=lambda h: -len(h["prefix"]))
        seen: set[str] = set()
        hits = [h for h in hits if not (h["form_id"] in seen or seen.add(h["form_id"]))]
        if hits:
            return {"bill_no": bill_no, "candidates": hits,
                    "note": ("按编号前缀推断，**未经账套核实**。"
                             "确认用 kd_object(noun=候选, id=编号)——查得到就是它。"
                             if len(hits) > 1 else
                             "按编号前缀推断，**未经账套核实**。查一下即可确认。")}
        return {
            "bill_no": bill_no, "candidates": [],
            "note": (f"前缀不在已登记的 {len(self.o.bill_prefixes)} 条里，认不出类型。"
                     "这不代表单号有问题——各家的编码规则不同。"
                     "知道是什么单就直接传 noun；想让系统记住，"
                     "在 profiles/<租户>/profile.yml 的 bill_prefixes 段加一条。"),
        }

    # ── 检索 ──────────────────────────────────────────────────
    def search_types(self, keyword: str = "", category: str = "") -> list[dict]:
        out = []
        for n in self.o.nouns.values():
            if category and n.category != category:
                continue
            hay = f"{n.form_id} {n.zh} {' '.join(n.alias)}"
            if keyword and keyword.lower() not in hay.lower():
                continue
            out.append({"form_id": n.form_id, "zh": n.zh, "category": n.category,
                        "alias": list(n.alias),
                        "action_count": sum(1 for v in n.allowed_verbs
                                            if self.o.verbs[v].kind == "write"),
                        "link_count": sum(1 for l in self.o.links
                                          if n.form_id in (l.get("from"), l.get("to")))})
        return sorted(out, key=lambda x: (x["category"], x["form_id"]))
