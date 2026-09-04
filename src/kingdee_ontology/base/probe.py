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
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from kingdee_ontology.base.dispatch import Dispatcher
from kingdee_ontology.base.ontology import Ontology, OntologyError
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
    outcome: str            # ok | no_permission | business_error | blocked
    detail: str = ""

    def to_dict(self) -> dict:
        d = {"noun": self.noun, "zh": self.zh, "outcome": self.outcome}
        if self.detail:
            d["detail"] = self.detail
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


async def probe_connection(d: Dispatcher, nouns: Optional[list[str]] = None,
                           limit: int = _DEFAULT_LIMIT) -> dict:
    """真登录一次，逐个只读探测。返回结果不抛异常——探测本身失败也是一种
    "答案"，让调用方决定怎么呈现，而不是把探测流程炸掉。
    """
    candidates = list(nouns) if nouns else default_candidates(d.o, limit=limit)
    results: list[ProbeResult] = []
    stopped: Optional[dict] = None

    for i, ref in enumerate(candidates):
        try:
            n = d.o.resolve_noun(ref)
        except OntologyError:
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

    return {
        "probed": [r.to_dict() for r in results],
        "candidates": candidates,
        "ok": sum(1 for r in results if r.outcome == "ok"),
        "no_permission": sum(1 for r in results if r.outcome == "no_permission"),
        "stopped_early": stopped,
        "note": ("no_permission 是按错误信息里的中文关键词启发式判断的，没有拿真实"
                "账套逐一验证过措辞；拿不准时归到 business_error，不臆断成权限问题。"),
    }
