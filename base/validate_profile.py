"""租户配置校验 —— 用中文告诉业务人员哪里填错了、该怎么改。

    python3 -m base.validate_profile <租户名>

设计原则：报错必须可执行。只说"格式错误"没有意义，要说清楚
「第几个操作的第几步、错在哪个字段、正确写法是什么」。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from base.ontology import OntologyError, load, load_profile  # noqa: E402

STEP_VERBS = {"submit", "audit", "unaudit", "delete", "close", "unclose",
              "void", "cancel", "forbid", "enable", "save"}
STEP_KINDS = {"下推", "确认", "检查"} | STEP_VERBS
# 子任务可选项：多扣扳机组区别于"顺序执行一串动作"的地方
STEP_OPTIONS = {"授权", "补偿", "说明", "操作编码", "过滤", "取数"}


def _check_steps(op_key: str, steps: list, o, errs: list, warns: list) -> None:
    if not steps:
        errs.append(f"操作『{op_key}』没有写 steps —— 至少要有一步。")
        return
    produced: str | None = None
    for i, st in enumerate(steps, 1):
        where = f"操作『{op_key}』第 {i} 步"
        if not isinstance(st, dict) or "做" not in st:
            errs.append(f"{where}：每一步都要有『做』字段。"
                        f"例如 {{做: 下推, 从: A单, 到: B单}}。")
            continue
        kind = st["做"]
        if kind not in STEP_KINDS:
            errs.append(f"{where}：不认识的动作『{kind}』。"
                        f"只能是 下推 / 确认 / {' / '.join(sorted(STEP_VERBS))}。")
            continue

        # 可选项先统一校验
        unknown = set(st) - {"做", "从", "到", "对象", "用", "问", "条件"} - STEP_OPTIONS
        if unknown:
            errs.append(f"{where}：不认识的字段 {sorted(unknown)}。"
                        f"可选项只有 {sorted(STEP_OPTIONS)}。")
        if "补偿" in st:
            if kind in ("确认", "检查"):
                errs.append(f"{where}：『{kind}』不写数据，不需要补偿。")
            elif st["补偿"] not in STEP_VERBS:
                errs.append(f"{where}：补偿动词 {st['补偿']!r} 不认识，"
                            f"只能是 {' / '.join(sorted(STEP_VERBS))}。")
        if "授权" in st and not st["授权"]:
            errs.append(f"{where}：写了『授权』但值是空的。"
                        f"要么去掉，要么写 true（任何人可批）或角色名（如 财务主管）。")

        if kind == "确认":
            if not st.get("问"):
                errs.append(f"{where}：『确认』必须写『问』，即要让人确认什么。")
            continue

        if kind == "检查":
            if not st.get("对象") or not st.get("条件"):
                errs.append(f"{where}：『检查』必须写『对象』和『条件』，"
                            f"如 {{做: 检查, 对象: 即时库存, 条件: \"FBaseQty >= 1\"}}。")
                continue
            try:
                o.check_verb_applies("query", st["对象"])
            except OntologyError as e:
                errs.append(f"{where}：{e}")
            from saga.engine import SagaError, eval_condition
            try:
                eval_condition(st["条件"], [])
            except SagaError as e:
                errs.append(f"{where}：{e}")
            continue

        if kind == "下推":
            src, dst = st.get("从"), st.get("到")
            if not src or not dst:
                errs.append(f"{where}：『下推』必须同时写『从』和『到』。")
                continue
            try:
                o.check_link(src, dst)
            except OntologyError as e:
                errs.append(f"{where}：{e}")
            else:
                produced = dst
            continue

        # 普通动词步
        noun = st.get("对象")
        if not noun:
            errs.append(f"{where}：动词步必须写『对象』(对哪种单据操作)。")
            continue
        try:
            o.check_verb_applies(kind, noun)
        except OntologyError as e:
            errs.append(f"{where}：{e}")
            continue
        use = st.get("用")
        if use is None:
            errs.append(f"{where}：必须写『用』，取值 上一步产物 或 targets。")
        elif use == "上一步产物":
            if produced is None:
                errs.append(f"{where}：写了『用: 上一步产物』，但前面没有任何一步生成单据。"
                            f"若是对调用方传入的单据操作，请改成『用: targets』。")
            elif o.resolve_noun(produced).form_id != o.resolve_noun(noun).form_id:
                errs.append(f"{where}：上一步产物是 {produced}，"
                            f"但这一步的对象是 {noun}，对不上。")
        elif use != "targets":
            errs.append(f"{where}：『用』只能是 上一步产物 或 targets，不能是 {use!r}。")
        produced = None if kind in ("delete", "void") else produced


def validate(tenant: str) -> tuple[list[str], list[str]]:
    errs: list[str] = []
    warns: list[str] = []
    profile = load_profile(tenant)
    if not profile:
        return [f"租户 {tenant!r} 没有配置文件。"], []

    o = load(tenant=tenant)

    # 覆盖层不得删除底座条目
    base_only = load(tenant="")
    for section, getter in (("nouns", lambda x: x.nouns), ("verbs", lambda x: x.verbs)):
        missing = set(getter(base_only)) - set(getter(o))
        if missing:
            errs.append(f"{section} 段删除了底座条目 {sorted(missing)} —— 覆盖层只能新增或改写。")

    for key, spec in (profile.get("nouns") or {}).items():
        if key not in base_only.nouns:
            for req in ("zh", "category", "allowed_verbs"):
                if req not in spec:
                    errs.append(f"新增单据『{key}』缺少必填项『{req}』。"
                                f"category 只能是 bill / master_data / view。")
            bad = set(spec.get("allowed_verbs") or []) - set(o.verbs)
            if bad:
                errs.append(f"单据『{key}』的 allowed_verbs 里有不认识的动词 {sorted(bad)}。")

    for lk in (profile.get("links") or []):
        for side in ("from", "to"):
            try:
                o.resolve_noun(lk.get(side, ""))
            except OntologyError as e:
                errs.append(f"下推关系 {lk.get('from')}→{lk.get('to')} 的『{side}』有问题：{e}")
        if lk.get("verified") not in ("confirmed", "documented", "suspect", None):
            warns.append(f"下推关系 {lk.get('from')}→{lk.get('to')} 的 verified "
                         f"建议填 confirmed（已在本账套实测）/ documented / suspect。")

    for key, spec in (profile.get("operations") or {}).items():
        _check_steps(key, spec.get("steps") or [], o, errs, warns)
        if not spec.get("owner"):
            warns.append(f"操作『{key}』没写 owner（责任部门），出问题时不知道找谁。")
        steps = spec.get("steps") or []
        # 只对**实际退不回来**的写步骤提醒。补偿默认从本体的动词定义继承，
        # 对继承来的步骤报"没声明"是误报——会逼着人在 profile 里重抄一遍，
        # 而重抄正是第二个事实来源的来源。
        from saga.engine import _verb_of_kind
        no_comp = []
        for i, st in enumerate(steps):
            if not isinstance(st, dict) or st.get("做") in ("确认", "检查"):
                continue
            eff = st["补偿"] if "补偿" in st else (
                (o.verbs.get(_verb_of_kind(st.get("做", ""))) or None)
                and o.verbs[_verb_of_kind(st["做"])].compensation)
            if not eff:
                no_comp.append(i + 1)
        if no_comp:
            warns.append(
                f"操作『{key}』第 {no_comp} 步**退不回来**"
                f"（这些动词在本体里没有 compensation）。"
                f"多扣扳机组里只要后面有一步失败，它们就只能人工收拾——"
                f"确认这是可接受的，或把它们挪到组的最后。")
        risky = [s.get("做") for s in steps if isinstance(s, dict)
                 and s.get("做") in ("delete", "void", "close")]
        if risky and not spec.get("confirm") and not any(
                isinstance(s, dict) and s.get("做") == "确认" for s in steps):
            warns.append(f"操作『{key}』含不可逆动作 {risky}，"
                         f"建议加 confirm: true 或一步『确认』。")
    return errs, warns


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("用法：python3 -m base.validate_profile <租户名>")
        return 2
    tenant = argv[1]
    try:
        errs, warns = validate(tenant)
    except OntologyError as e:
        print(f"✗ {e}")
        return 1
    for w in warns:
        print(f"⚠ 建议  {w}")
    for e in errs:
        print(f"✗ 错误  {e}")
    if errs:
        print(f"\n{len(errs)} 处错误，配置不会被加载。改完再跑一次。")
        return 1
    print(f"✓ 租户 {tenant} 配置校验通过"
          + (f"（{len(warns)} 条建议）" if warns else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
