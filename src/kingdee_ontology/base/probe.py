"""连通性 + 只读权限探测——引导流程的「测试联通」那一步。

`kd_check_profile` 原本只做静态 YAML 校验，从不发请求。装好之后第一次真正
知道账号密码对不对、这个账号能碰到哪些模块，往往是**第一次业务查询失败的
时候**——离"刚填完配置"已经隔了好几步，排查起来要往回倒好几层。

这里把"真登录一次、挑几类常见单据各查一下"做成一个显式、可控的步骤：

  * **只读**。全程只调 `Dispatcher.query()`，不会碰 `kd_act`——不管探测到
    什么，都不该在探测过程中改动账套里的任何数据。
  * **一出问题就停**，不把全部候选都超时一遍。能查到 Kingdee 明确拒绝
    （权限不足/表单不存在这类，走的是 `PipelineError`）不算"出问题"——那是
    探测本来就想知道的答案；真正的"出问题"是探测本身跑不通（登录失败、
    连不上服务器），此时继续测下一个只是在重复同一个失败。
  * **权限判定是启发式的**。Kingdee 的业务错误是一段自由文本，这里按几个
    常见的中文关键词猜"是不是权限问题"，没有拿真实账套验证过全部措辞
    ——不确定时归到更保守的 `business_error`，不臆断成权限问题。
  * **候选优先复用 legacy 工具目录**，不是另起一份"常见表单"清单。
    `kingdee_mcp` 那 97 个专用工具背后有一份人工维护的 `FORM_CATALOG`，
    久经使用、行为摸得清楚，见 `default_candidates()`。

## 嗅探到本体不认识的表单，怎么处理

`default_candidates()` 选出来的永远是本体里已经注册过的名词——探测不到
"未知"的东西。但调用方可以用 `nouns=` **明确点名**一个本体不认识的字符串
（比如二开新建的单据 `form_id`）。这种情况下不是简单跳过：会绕开本体解析，
直接拿这个字符串当 form_id 发一次原始查询，看金蝶认不认——

  * 金蝶报"表单不存在"这类业务错误 → 按原样归到 `business_error`/
    `no_permission`，只是多标一个 `unregistered: true`；
  * 金蝶真的认、查得通 → 说明这是个账号能用、但本体没登记的表单，往
    WikiSkill 提一条建议（`unregistered_form_reachable`），供人日后决定
    要不要补进 `profiles/<租户>/profile.yml` 的 `nouns` 段——**只提议，
    不自动改配置**，和 `navigate()` 探测出下推关联字段时的做法一样。
    本体认得它之前，`kd_object` 这类工具依然用不了它，只有 `probe` 能查。

## 一个结构性的盲区：报表类二开对象

`query()` 走的是 `ExecuteBillQuery`——只认普通单据。二开报表（金蝶后台叫
"报表"，走 `GetSysReportData`）不是单据，`ExecuteBillQuery` 查不到，
不管有没有权限都会报错，探测器分不清"没权限"和"这压根不是张单据"。

`GetSysReportData` 需要真实的 `FieldKeys`/`Model` 键名才能查——这些键名
因表而异、猜不出来（有真实案例试了十几种组合全部失败，见
`docs/ontology/06-report-probing.md`），所以探测器**不会去猜参数**，
只做一件更保守的事：`query()` 判定为业务错误时，追加试一次
`report(ref, {})`（空参数）。金蝶在这种情况下典型会报一个 .NET 风格的
参数缺失异常（`ArgumentNullException`，形如"值不能为 null。参数名: key"）
——这个异常形状本身就是信号：请求已经进了 `GetSysReportData` 的处理逻辑，
说明 `ref` **很可能**是个真实存在的报表 form_id，只是这里测不出它的具体
权限。这种情况归为 `possible_report`，不算"发现"（不确定读得通），
但会说清"这大概率是报表，别再当成普通单据去猜参数"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from kingdee_ontology.base.dispatch import Dispatcher
from kingdee_ontology.base.ontology import Ontology, OntologyError
from kingdee_ontology.pipeline.parse import is_business_error
from kingdee_ontology.pipeline.run import PipelineError

# 覆盖不了所有账套的措辞，只是常见的几种——见模块 docstring 的"启发式"说明。
_PERMISSION_KEYWORDS = (
    "无权限", "没有权限", "权限不足", "无该功能", "无此功能", "未获授权",
    "未授权", "无权操作", "禁止访问", "没有操作权限", "没有访问权限",
    "forbidden", "access denied", "accessdenied", "permission denied",
)

_DEFAULT_LIMIT = 10
_CATEGORY_RANK = {"bill": 0, "master_data": 1, "view": 2, "system": 3}


@dataclass
class ProbeResult:
    noun: str
    zh: str
    outcome: str            # ok | no_permission | business_error | possible_report | blocked
    detail: str = ""
    unregistered: bool = False   # 本体不认识这个 form_id，是调用方点名探测出来的

    def to_dict(self) -> dict:
        d = {"noun": self.noun, "zh": self.zh, "outcome": self.outcome}
        if self.detail:
            d["detail"] = self.detail
        if self.unregistered:
            d["unregistered"] = True
        return d


def _legacy_catalog_forms() -> frozenset[str]:
    """`kingdee_mcp` 那 97 个专用工具背后的表单目录——人工维护、久经使用的
    一份"常见表单"清单，不是从注册表派生的。

    拿来给候选排序当参考：这些是已经在旧工具上跑了很久、行为摸得比较清楚的
    表单。默认探测候选优先从这里选，比单纯按出边数量排序更不容易碰上冷门
    或刚接入本体、行为还没摸透的表单——避免探测本身先撞上奇怪问题，
    干扰了「这个账号到底有没有权限」这个真正想知道的答案。

    惰性导入：`kingdee_mcp.server` 是个 7000+ 行的大模块，选几个候选名词不该
    把它整个拉起来（同样的顾虑见 `base/transport.py` 的说明）；导入失败也不
    影响候选选择，只是少了这一个排序信号，退化成纯按类别/出边排。
    """
    try:
        from kingdee_mcp.server import FORM_CATALOG
        return frozenset(FORM_CATALOG)
    except Exception:
        return frozenset()


def default_candidates(ontology: Ontology, limit: int = _DEFAULT_LIMIT) -> list[str]:
    """挑一批"值得先探一下"的名词。

    不穷举——85 个名词全探一遍等于把每次自检都变成一次小压测，多数账套用不到
    那么多模块，而且会把探测时间拖得很长。优先级：

      1. 本租户已经配置好的业务操作入口——这是用户真的会用到的东西；
      2. legacy 工具目录里出现过的表单——久经使用、行为摸得清楚；
      3. 按类别（bill 优先于 master_data、view、system）、按下推目标数降序
         补齐——出边越多的单据，越可能是某条业务流程的起点。
    """
    picked: list[str] = []
    seen: set[str] = set()

    def add(form_id: Optional[str]) -> None:
        if form_id and form_id not in seen and form_id in ontology.nouns:
            seen.add(form_id)
            picked.append(form_id)

    for op in ontology.operations.values():
        if len(picked) >= limit:
            break
        for st in op.steps:
            ref = st.get("从") or st.get("对象")
            if not ref:
                continue
            try:
                add(ontology.resolve_noun(ref).form_id)
            except OntologyError:
                pass
            break   # 只要这个操作的起点，不是它途经的每一站

    if len(picked) < limit:
        legacy = _legacy_catalog_forms()
        out_links: dict[str, int] = {}
        for lk in ontology.links:
            out_links[lk["from"]] = out_links.get(lk["from"], 0) + 1
        rest = sorted(
            (n for n in ontology.nouns.values()
             if n.form_id not in seen and "query" in n.allowed_verbs),
            key=lambda n: (0 if n.form_id in legacy else 1,
                          _CATEGORY_RANK.get(n.category, 9),
                          -out_links.get(n.form_id, 0), n.form_id))
        for n in rest:
            add(n.form_id)
            if len(picked) >= limit:
                break

    return picked[:limit]


def _classify_business_error(msg: str) -> str:
    low = msg.lower()
    if any(kw.lower() in low for kw in _PERMISSION_KEYWORDS):
        return "no_permission"
    return "business_error"


# 一次真实排障里观察到的现象：GetSysReportData 参数（FieldKeys/Model）不对
# 时，金蝶给的是这种 .NET ArgumentNullException 风格的通用错误——与权限
# 无关，纯粹是参数缺失。见 docs/ontology/06-report-probing.md。
# 只在这两个词同时出现时才判定，避免"key"这种常见词单独出现时误报。
_REPORT_PARAM_ERROR_MARKERS = ("参数名", "key")


def _looks_like_report_param_error(msg: str) -> bool:
    low = msg.lower()
    return all(kw.lower() in low for kw in _REPORT_PARAM_ERROR_MARKERS)


def _propose_to_wikiskill(kind: str, ref: str, title: str, detail: str,
                          suggestion: str) -> None:
    """通用的"只提议，不自动改配置"落盘。

    与 `dispatch.Dispatcher._propose_link_filter` 是同一套做法：写失败不该
    连累探测本身，全程吞掉异常。
    """
    try:
        import hashlib

        from kingdee_ontology.wikiskill.knowledge import Entry, Knowledge
        k = Knowledge()
        k.merge(Entry(
            id=hashlib.sha1(f"{kind}|{ref}".encode()).hexdigest()[:12],
            kind=kind, title=title, detail=detail, suggestion=suggestion,
            occurrences=1, evidence=[{"form_id": ref}]))
        k.save()
    except Exception:
        pass  # 知识库不可用不该连累探测


def _propose_unregistered_form(ref: str) -> None:
    """把探测到的、账号能查但本体没登记的表单提给知识库。"""
    _propose_to_wikiskill(
        "unregistered_form_reachable", ref,
        title=f"探测到未登记的表单 {ref}，账号能查",
        detail=(f"直接用 {ref!r} 当 form_id 发只读查询，金蝶没有报"
                f"'表单不存在'这类业务错误。"),
        suggestion=(f"人眼核对 {ref} 确实是想用的表单后，在 "
                    f"profiles/<租户>/profile.yml 的 nouns 里补一条定义"
                    f"（zh/category/allowed_verbs），见 profiles/README.md；"
                    f"本体认得它之前，kd_object 等工具用不了它。"))


def _propose_possible_report(ref: str) -> None:
    """`query()` 判定为业务错误，但用空参数试 `report()` 时出现了
    `GetSysReportData` 典型的参数缺失异常——大概率是张报表，不是权限问题。
    只提议，不自动改配置，也不假装验证过完整权限（没有真实 FieldKeys 测不出）。
    见 `docs/ontology/06-report-probing.md`。
    """
    _propose_to_wikiskill(
        "possible_report_unconfirmed", ref,
        title=f"{ref} 可能是报表（GetSysReportData），ExecuteBillQuery 查不到它",
        detail=(f"query() 对 {ref!r} 报业务错误，但用空参数试 report() 时出现了"
                f"参数缺失异常（形如'参数名: key'）——这个异常形状说明请求已经"
                f"进了 GetSysReportData 的处理逻辑，不是'表单不存在'。"),
        suggestion=("这不算确认可用——没有真实 FieldKeys/Model 验证不了完整权限。"
                    "浏览器 F12 抓一次这张报表的 GetSysReportData 请求，"
                    "拿到真实参数后手动验证，见 docs/ontology/06-report-probing.md。"))


async def _probe_unregistered(d: Dispatcher, ref: str) -> ProbeResult:
    """本体解析不出这个名词，但调用方明确点名要探测——绕开本体，直接拿这个
    字符串当 form_id 发一次原始查询，看金蝶认不认。见模块 docstring
    「嗅探到本体不认识的表单，怎么处理」与「一个结构性的盲区：报表类二开对象」。
    """
    raw = await d.t.query(ref, "", "", 1)
    err = is_business_error(raw)
    if err is None:
        # 没有业务错误——金蝶接受了这个 form_id，是账号能查、本体没登记的表单。
        _propose_unregistered_form(ref)
        return ProbeResult(ref, ref, "ok", unregistered=True)

    # query() 说不行，但 ExecuteBillQuery 从设计上就查不到报表——追加用空
    # 参数试一次 report()，看是不是问错了端点。report() 本身出岔子（网络/
    # 序列化之类，不是我们要找的那个特定异常形状）不该盖掉已经拿到的
    # query() 结论，全程吞掉异常。
    try:
        report_raw = await d.t.report(ref, {})
        report_err = is_business_error(report_raw)
        if report_err and _looks_like_report_param_error(report_err):
            _propose_possible_report(ref)
            return ProbeResult(ref, ref, "possible_report", err[:300], unregistered=True)
    except Exception:
        pass

    return ProbeResult(ref, ref, _classify_business_error(err), err[:300],
                       unregistered=True)


async def probe_connection(d: Dispatcher, nouns: Optional[list[str]] = None,
                           limit: int = _DEFAULT_LIMIT) -> dict:
    """真登录一次，逐个只读探测。返回结果不抛异常——探测本身失败也是一种
    "答案"，让调用方决定怎么呈现，而不是把探测流程炸掉。
    """
    candidates = list(nouns) if nouns else default_candidates(d.o, limit=limit)
    # 只有调用方明确点名的候选，才值得为"本体不认识"做兜底探测——自动挑出的
    # 候选保证已经在本体里（default_candidates 只从 ontology.nouns 里选），
    # 不会走到这条分支，不用担心默认探测平白多出一堆探测请求。
    explicit = nouns is not None
    results: list[ProbeResult] = []
    stopped: Optional[dict] = None

    for i, ref in enumerate(candidates):
        try:
            n = d.o.resolve_noun(ref)
        except OntologyError:
            if not explicit:
                continue
            try:
                results.append(await _probe_unregistered(d, ref))
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"[:300]
                results.append(ProbeResult(ref, ref, "blocked", msg, unregistered=True))
                stopped = {"at": ref, "reason": msg, "untested": candidates[i + 1:]}
                break
            continue
        try:
            await d.query(n.form_id, top=1)
        except PipelineError as e:
            msg = str(e)
            results.append(ProbeResult(n.form_id, n.zh, _classify_business_error(msg),
                                       msg[:300]))
            continue
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"[:300]
            results.append(ProbeResult(n.form_id, n.zh, "blocked", msg))
            # 按位置切剩下的，不按名字集合做差——候选里的原始写法（可能是中文名/
            # 别名）和已探测结果里存的规范 form_id 不是同一份字符串，用集合减法
            # 会把"刚测完但失败的这条"也算成"没测过"。
            stopped = {"at": n.form_id, "reason": msg, "untested": candidates[i + 1:]}
            break
        else:
            results.append(ProbeResult(n.form_id, n.zh, "ok"))

    n_report = sum(1 for r in results if r.outcome == "possible_report")
    return {
        "probed": [r.to_dict() for r in results],
        "candidates": candidates,
        "ok": sum(1 for r in results if r.outcome == "ok"),
        "no_permission": sum(1 for r in results if r.outcome == "no_permission"),
        "unregistered_found": sum(1 for r in results
                                  if r.unregistered and r.outcome == "ok"),
        "possible_reports": n_report,
        "stopped_early": stopped,
        "note": ("no_permission 是按错误信息里的中文关键词启发式判断的，没有拿真实"
                "账套逐一验证过措辞；拿不准时归到 business_error，不臆断成权限问题。"
                " unregistered_found 里的表单已提给 WikiSkill，只是建议，"
                "不会自动改配置。"
                + (" possible_reports 里的名词像是报表（GetSysReportData），"
                   "ExecuteBillQuery 天生查不到——不是权限问题，也不算确认可用，"
                   "见 docs/ontology/06-report-probing.md。" if n_report else "")),
    }
