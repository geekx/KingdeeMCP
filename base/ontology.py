"""本体注册表 —— MCP 底座的唯一事实来源。

设计意图（回应「MCP 作为底座，抽象出调用能力、状态这些五元的基座」）：

    旧结构：97 个工具 = 97 份 inputSchema 常驻上下文（实测 ~45.9k token）。
            每新增一个单据类型就多一个工具，token 成本线性增长。
    新结构：动词（14）是**能力**，名词（48）/状态（11）/链接（9）/规则（3）是**数据**。
            底座只暴露 6 个通用工具，实例经由 kd_describe 按需拉取。

本模块不依赖 kingdee_mcp.server，可独立导入与测试（「接口要稳健和独立」）。
"""
from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

REGISTRY_PATH = Path(__file__).with_name("registry.yml")


class OntologyError(ValueError):
    """前置条件不满足。错误信息必须自带修正建议，避免调用方再花一轮问。"""


@dataclass(frozen=True)
class Verb:
    name: str
    zh: str
    kind: str                     # read | write
    arity: str                    # single | batch
    atomicity: str                # atomic | per_item | server_defined
    idempotent: bool
    inverse: Optional[str]
    endpoint: Optional[str] = None
    requires_state: tuple[str, ...] = ()
    to_state: Optional[str] = None
    # 补偿 ≠ 逆动词：inverse 把**同一个对象**退回上一状态；
    # compensation 把**这一步的产物**清理掉。push 没有 inverse（不存在 unpush），
    # 但补偿是 delete——删掉新生成的下游单。对象都不是同一个。
    compensation: Optional[str] = None
    compensation_target: Optional[str] = None   # self | produced | None(退不回来)

    @property
    def destructive(self) -> bool:
        """无逆动词的写动词即破坏性——不再依赖人工标注，从契约推导（修 N-1）。"""
        return self.kind == "write" and self.inverse is None


@dataclass(frozen=True)
class Operation:
    """业务操作入口 —— 面向人的那一层。

    业务人员用自己的话给一件事命名（"给客户开票"），并声明它由哪几步组成。
    工程上它只是 verb/noun/link 的一个命名组合，但对使用者而言它才是"操作"。
    """
    key: str
    zh: str
    steps: tuple[dict, ...]
    desc: str = ""
    confirm: bool = False
    owner: str = ""


@dataclass(frozen=True)
class Noun:
    form_id: str
    zh: str
    category: str                 # bill | master_data | view | system
    allowed_verbs: frozenset[str]
    alias: tuple[str, ...] = ()
    default_fields: str = ""
    # 系统对象（用户/角色/权限/编码规则…）走各自的专用端点，
    # 而不是通用的 ExecuteBillQuery。有值即表示"别走通用单据查询"。
    system_endpoint: str = ""


class Ontology:
    def __init__(self, raw: dict, profile: Optional[dict] = None):
        """raw = 通用底座；profile = 租户覆盖层（二开表单、自定义操作码、业务入口）。

        合并策略刻意保守：覆盖层只能**新增**或**改写已有键**，不能删除底座条目——
        删除会让通用剧本在某些租户上静默失效，比报错更难排查。
        """
        raw = _merge(raw, profile) if profile else raw
        self.profile_name = (profile or {}).get("tenant", "")
        self.version = raw.get("version", 0)
        self.verbs: dict[str, Verb] = {
            k: Verb(
                name=k, zh=v.get("zh", k), kind=v.get("kind", "write"),
                arity=v.get("arity", "batch"), atomicity=v.get("atomicity", "server_defined"),
                idempotent=bool(v.get("idempotent", False)), inverse=v.get("inverse"),
                endpoint=v.get("endpoint"),
                requires_state=tuple(v.get("requires_state") or ()),
                to_state=v.get("to_state"),
                compensation=v.get("compensation"),
                compensation_target=v.get("compensation_target"),
            ) for k, v in (raw.get("verbs") or {}).items()
        }
        self.nouns: dict[str, Noun] = {
            k: Noun(
                form_id=k, zh=v.get("zh", k), category=v.get("category", "bill"),
                allowed_verbs=frozenset(v.get("allowed_verbs") or ()),
                alias=tuple(v.get("alias") or ()),
                default_fields=v.get("default_fields", ""),
                system_endpoint=v.get("system_endpoint", ""),
            ) for k, v in (raw.get("nouns") or {}).items()
        }
        self.states: dict[str, dict] = raw.get("states") or {}
        self.state_groups: dict[str, Any] = raw.get("state_groups") or {}
        self.links: list[dict] = raw.get("links") or []
        self.bill_prefixes: dict[str, dict] = raw.get("bill_prefixes") or {}
        self.rules: list[dict] = raw.get("rules") or []
        self._link_index = {(l["from"], l["to"]): l for l in self.links}
        self.operations: dict[str, Operation] = {
            k: Operation(
                key=k, zh=v.get("zh", k), desc=v.get("desc", ""),
                steps=tuple(v.get("steps") or ()),
                confirm=bool(v.get("confirm", False)), owner=v.get("owner", ""),
            ) for k, v in (raw.get("operations") or {}).items()
        }
        self._alias_index: dict[str, str] = {}
        for n in self.nouns.values():
            self._alias_index[n.zh] = n.form_id
            for a in n.alias:
                self._alias_index.setdefault(a, n.form_id)

    # ── 解析 ──────────────────────────────────────────────────
    def resolve_noun(self, ref: str) -> Noun:
        """接受 form_id、中文名或别名。解析失败时给出候选，而不是干巴巴报错。"""
        if ref in self.nouns:
            return self.nouns[ref]
        if ref in self._alias_index:
            return self.nouns[self._alias_index[ref]]
        hits = [n.form_id for n in self.nouns.values()
                if ref in n.form_id or ref in n.zh or any(ref in a for a in n.alias)]
        raise OntologyError(
            f"未知名词 {ref!r}。" +
            (f"可能是：{hits[:8]}" if hits else
             "用 kd_describe(what='nouns') 查看全部 48 个名词，或用中文名/别名指称。"))

    def verb(self, name: str) -> Verb:
        if name not in self.verbs:
            raise OntologyError(
                f"未知动词 {name!r}。可用动词：{sorted(self.verbs)}。"
                f"用 kd_describe(what='verbs') 查看每个动词的原子性契约。")
        return self.verbs[name]

    # ── 前置条件（PRE-01..03）─────────────────────────────────
    # 判断本身住在 aip/（判断层）：同一个问题只有一份实现，这里只做两件事——
    # 把结论翻译成本层的调用约定（抛异常 / 返回警告串），并保持既有签名不变。
    def decide(self, verb: str, noun_ref: str, state: Optional[str] = None,
               target: Optional[str] = None, params: Optional[dict] = None):
        """完整结论：一次给出全部理由，不在第一个问题上短路。"""
        from aip.logic import can
        return can(self, verb, noun_ref, state=state, target=target, params=params)

    def check_verb_applies(self, verb: str, noun_ref: str) -> tuple[Verb, Noun]:
        """PRE-01：动词必须在名词的 allowed_verbs 内。"""
        from aip.logic import Facts, evaluate
        v, n = self.verb(verb), self.resolve_noun(noun_ref)
        d = evaluate(Facts(ontology=self, verb=v.name, noun=n.form_id), only=["AIP-01"])
        if d.blocks:
            raise OntologyError(d.why())
        return v, n

    def check_link(self, source_ref: str, target_ref: str) -> dict:
        """PRE-02：下推的 (from,to) 必须已登记。"""
        from aip.logic import Facts, evaluate
        s, t = self.resolve_noun(source_ref), self.resolve_noun(target_ref)
        d = evaluate(Facts(ontology=self, noun=s.form_id, target=t.form_id),
                     only=["AIP-02"])
        if d.blocks:
            raise OntologyError(d.why())
        return self._link_index[(s.form_id, t.form_id)]

    def check_state(self, verb: str, current_state: Optional[str]) -> Optional[str]:
        """PRE-03：requires_state 校验。未知当前状态时降级为警告，不阻断。

        刻意不在这里自动补一次查询——那会把每个写操作的往返翻倍。
        判断层把「不知道」记为 undetermined；这个入口按既有约定把它降级成
        一句警告返回，而不是拦下。想要严格语义的调用方请用 decide()。
        """
        from aip.logic import Facts, evaluate
        d = evaluate(Facts(ontology=self, verb=self.verb(verb).name,
                           state=current_state), only=["AIP-03"])
        if d.blocks:
            raise OntologyError(d.why())
        if d.undetermined:
            return d.undetermined[0].text
        return None

    def _verb_reaching(self, state: str) -> Optional[str]:
        for v in self.verbs.values():
            if v.to_state == state:
                return v.name
        return None

    # ── 描述（供 kd_describe 按需返回，替代常驻 schema）────────
    def describe(self, what: str, key: Optional[str] = None) -> dict:
        if what == "verbs":
            if key:
                v = self.verb(key)
                return {"verb": v.name, "zh": v.zh, "kind": v.kind, "arity": v.arity,
                        "atomicity": v.atomicity, "idempotent": v.idempotent,
                        "inverse": v.inverse, "destructive": v.destructive,
                        "compensation": v.compensation,
                        "compensation_target": v.compensation_target,
                        "requires_state": list(v.requires_state), "to_state": v.to_state}
            return {"verbs": [{"verb": v.name, "zh": v.zh, "kind": v.kind,
                               "atomicity": v.atomicity, "destructive": v.destructive}
                              for v in self.verbs.values()]}
        if what == "nouns":
            if key:
                n = self.resolve_noun(key)
                return {"form_id": n.form_id, "zh": n.zh, "category": n.category,
                        "alias": list(n.alias), "allowed_verbs": sorted(n.allowed_verbs),
                        "default_fields": n.default_fields,
                        "system_endpoint": n.system_endpoint or None,
                        "push_targets": [l["to"] for l in self.links if l["from"] == n.form_id]}
            return {"count": len(self.nouns),
                    "nouns": [{"form_id": n.form_id, "zh": n.zh, "category": n.category}
                              for n in self.nouns.values()]}
        if what == "states":
            return {"states": self.states, "groups": self.state_groups}
        if what == "links":
            if key:
                n = self.resolve_noun(key)
                return {"from": n.form_id,
                        "links": [l for l in self.links if l["from"] == n.form_id]}
            return {"links": self.links}
        if what == "rules":
            return {"rules": self.rules}
        if what == "logic":
            # 判断层的自我说明：有哪些逻辑函数、各自执行哪条规则、需要哪些事实。
            # key 形如 "audit@SAL_SaleOrder" 或 "audit@SAL_SaleOrder@C" 时直接判一次，
            # 省得调用方为了知道"能不能做"先拉一遍全量本体自己推。
            from aip.logic import describe as _logic_describe
            if not key:
                return {"note": "判断层：纯函数，不发请求。key='动词@名词[@当前状态]' 可直接判一次。",
                        "functions": _logic_describe()}
            parts = [p.strip() for p in key.split("@")]
            if len(parts) < 2:
                raise OntologyError(
                    f"what='logic' 的 key 要写成 '动词@名词[@当前状态]'，"
                    f"如 'audit@销售订单@B'。收到的是 {key!r}。")
            verb, noun, state = parts[0], parts[1], (parts[2] if len(parts) > 2 else None)
            return self.decide(verb, noun, state=state).to_dict()
        if what == "prefixes":
            return {"note": "编号前缀由租户的编码规则决定，此表为启发式，命中只说明"
                            "『很可能是』；各家可在 profile 的 bill_prefixes 段覆盖。",
                    "prefixes": self.bill_prefixes}
        if what == "operations":
            if key:
                op = self.operation(key)
                return {"key": op.key, "zh": op.zh, "desc": op.desc,
                        "owner": op.owner, "confirm": op.confirm,
                        "steps": list(op.steps)}
            return {"tenant": self.profile_name or "(未加载租户配置)",
                    "count": len(self.operations),
                    "operations": [{"key": o.key, "zh": o.zh, "desc": o.desc}
                                   for o in self.operations.values()]}
        raise OntologyError(
            f"未知的描述对象 {what!r}。"
            f"可用：verbs / nouns / states / links / rules / operations / prefixes")

    def operation(self, key: str) -> Operation:
        if key in self.operations:
            return self.operations[key]
        for o in self.operations.values():
            if o.zh == key:
                return o
        hits = [f"{o.key}({o.zh})" for o in self.operations.values()
                if key in o.key or key in o.zh or key in o.desc]
        raise OntologyError(
            f"未定义的业务操作 {key!r}。" +
            (f"可能是：{hits[:6]}" if hits else
             "用 kd_describe(what='operations') 查看本租户已定义的操作入口；"
             "要新增请编辑 profiles/<租户>/profile.yml 的 operations 段，"
             "格式见 profiles/README.md（面向业务人员，无需改代码）。"))


def _merge(base: dict, profile: dict) -> dict:
    """把租户覆盖层合并到底座上。

    - dict 段（verbs/nouns/states/operations）：逐键深合并，覆盖层同名键改写底座；
    - list 段（links/rules）：追加，并按业务主键去重（覆盖层同键条目胜出）。
    覆盖层不允许删除底座条目——见 Ontology.__init__ 的说明。
    """
    out = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
           for k, v in base.items()}
    for section in ("verbs", "nouns", "states", "state_groups", "operations",
                    "bill_prefixes"):
        over = profile.get(section)
        if not over:
            continue
        merged = dict(out.get(section) or {})
        for k, v in over.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k] = {**merged[k], **v}      # 部分覆盖：只改声明了的字段
            else:
                merged[k] = v
        out[section] = merged
    for section, keyf in (("links", lambda x: (x.get("from"), x.get("to"))),
                          ("rules", lambda x: x.get("id"))):
        over = profile.get(section)
        if not over:
            continue
        idx = {keyf(x): x for x in (out.get(section) or [])}
        for x in over:
            idx[keyf(x)] = x
        out[section] = list(idx.values())
    return out


def load_profile(tenant: Optional[str]) -> Optional[dict]:
    """按租户名加载 profiles/<tenant>/profile.yml；未指定或不存在返回 None。"""
    if not tenant:
        return None
    p = Path(__file__).resolve().parents[1] / "profiles" / tenant / "profile.yml"
    if not p.exists():
        raise OntologyError(
            f"找不到租户配置 {p}。"
            f"新建租户请复制 profiles/example-tenant/ 并改名，"
            f"填写说明见 profiles/README.md。")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    data.setdefault("tenant", tenant)
    return data


@functools.lru_cache(maxsize=8)
def load(path: Optional[str] = None, tenant: Optional[str] = None) -> Ontology:
    """加载本体。tenant 非空时叠加该租户的覆盖层。

    tenant 默认取环境变量 KINGDEE_TENANT —— 部署方只需设一个环境变量，
    同一份代码即可服务不同账套（「避免一种米养几种人」）。
    """
    import os
    p = Path(path) if path else REGISTRY_PATH
    tenant = tenant if tenant is not None else os.environ.get("KINGDEE_TENANT", "")
    return Ontology(yaml.safe_load(p.read_text(encoding="utf-8")),
                    load_profile(tenant))
