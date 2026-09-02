"""通用动词分发 —— 把 97 个专用工具收敛成 14 个动词 × 48 个名词的组合。

三条硬约束（直接对应审计发现）：
  1. 每个写动作**先过前置规则**（PRE-01..03）再发请求 —— 把服务端报错前移到调用前；
  2. 返回体**永远带 per-target 结果与 atomicity 契约** —— 修 A-2 的批量语义分裂；
  3. 每个写动作**落一条过程操作审计记录** —— 修 P-1，同时成为自优化层的数据源。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "ontology"))

from base.objects import ObjectModel                            # noqa: E402
from base.ontology import Ontology, OntologyError, load        # noqa: E402
from base.transport import KingdeeTransport, Transport         # noqa: E402
from operation_audit import audit_recorder                     # noqa: E402

# ExecuteOperation 的操作编码。随表单而异时由调用方显式传 operation 覆盖。
_OP_NUMBER = {"void": "Cancel", "close": "BillClose", "unclose": "UnBillClose",
              "forbid": "Forbid", "enable": "Enable"}


class _Reuse:
    """复用外层已打开的审计上下文，退出时不结束它。"""
    def __init__(self, op): self._op = op
    def __enter__(self): return self._op
    def __exit__(self, *exc): return False


def _flatten_props(data: Any) -> dict:
    """把 View 返回的嵌套结构压平成属性字典。

    金蝶 View 的返回层级各表单不一，这里只做一层保守展开：
    顶层标量直接取，嵌套对象取其 FName/FNumber 作为 '父.子' 属性。
    取不到就不编——宁可属性少，也不要伪造值。
    """
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, dict):
            for sub in ("FName", "FNumber", "Name", "Number"):
                if sub in v and isinstance(v[sub], (str, int, float)):
                    out[f"{k}.{sub}"] = v[sub]
    return out


def _rows_of(result: Any) -> list:
    """从金蝶返回里取数据行。ExecuteBillQuery 直接返回数组，其它端点包一层。"""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for k in ("Result", "data", "Rows"):
            v = result.get(k)
            if isinstance(v, list):
                return v
        inner = result.get("Result")
        if isinstance(inner, dict):
            for k in ("Result", "Rows", "data"):
                v = inner.get(k)
                if isinstance(v, list):
                    return v
    return []


def _ok(result: Any) -> tuple[bool, list]:
    rs = result.get("Result", result) if isinstance(result, dict) else {}
    st = rs.get("ResponseStatus", {})
    if not isinstance(st, dict):
        return True, []
    if st.get("IsSuccess"):
        return True, []
    return False, [{"message": e.get("Message", ""), "field": e.get("FieldName", "")}
                   for e in st.get("Errors", [])]


class Dispatcher:
    def __init__(self, ontology: Optional[Ontology] = None,
                 transport: Optional[Transport] = None,
                 actor: str = "unknown"):
        self.o = ontology or load()
        self.t = transport or KingdeeTransport()
        self.actor = actor
        self.m = ObjectModel(self.o)
        # 业务操作执行期间共享的审计上下文：让一次操作的所有步骤共用同一 trace_id。
        # 没有它，run_operation 的每一步会各开一条 trace，
        # 悬挂链检测就只能看到"某个 kd_push 失败"，而看不到"『采购收货入库』停在第 2 步"。
        self._op_ctx = None

    # ── 写动词统一入口 ────────────────────────────────────────
    async def act(self, verb: str, noun: str, targets: list[str],
                  model: Optional[dict] = None, current_state: Optional[str] = None,
                  operation: Optional[str] = None, on_behalf_of: Optional[str] = None,
                  no_by_id: Optional[dict[str, str]] = None,
                  dry_run: bool = False) -> dict:
        if dry_run:
            # 目前只有 save 能真正"预演"（金蝶提供保存前校验）。
            # 其它动词没有对应的 dry-run 接口，谎称支持只会误导调用方。
            if verb != "save":
                raise OntologyError(
                    f"dry_run 目前只支持 verb='save'（保存前校验）。"
                    f"{verb} 没有对应的预演接口——要确认状态请先用 kd_read 查当前值。")
            return await self.validate(noun, model or {})

        v, n = self.o.check_verb_applies(verb, noun)
        # 上游（如 push）已知的 FID→单据编号映射。带上它，审计记录里同一张单
        # 才不会因为一步只有 FID、另一步只有单据编号而被算成两个对象。
        self._no_by_id = dict(no_by_id or {})
        if v.kind != "write":
            raise OntologyError(f"{verb} 是只读动词，请用 kd_query / kd_read。")
        warn = self.o.check_state(verb, current_state)

        out: dict[str, Any] = {
            "verb": v.name, "noun": n.form_id,
            # 契约随结果一起返回：调用方不必猜这次批量是不是原子的（修 A-2）
            "contract": {"arity": v.arity, "atomicity": v.atomicity,
                         "idempotent": v.idempotent, "destructive": v.destructive,
                         "inverse": v.inverse},
            "results": [], "succeeded": [], "failed": [],
        }
        if warn:
            out["warning"] = warn

        ctx = (_Reuse(self._op_ctx) if self._op_ctx is not None
               else audit_recorder.operation(f"kd_act:{v.name}", actor=self.actor,
                                             on_behalf_of=on_behalf_of))
        with ctx as op:
            if v.name == "save":
                await self._one(op, v, n, targets[0] if targets else None,
                                {"FID": 0, **(model or {})}, out, current_state)
            elif v.atomicity == "per_item":
                for t in targets:                      # 逐条：可报告 per-id 结果
                    await self._one(op, v, n, t, {"Ids": t}, out, current_state)
            else:                                      # server_defined：一次提交
                await self._one(op, v, n, ",".join(targets),
                                {"Ids": ",".join(targets)}, out, current_state,
                                operation=operation, batch_targets=targets)

        n_fail = len(out["failed"])
        out["success"] = n_fail == 0
        out["outcome"] = ("success" if n_fail == 0
                          else "failed" if not out["succeeded"] else "partial")
        if out["outcome"] == "partial":
            out["tip"] = (f"部分成功：{len(out['succeeded'])} 成 / {n_fail} 败，"
                          f"且**已成功的部分不会回滚**。重试前请只针对 failed 中的目标。")
        elif v.atomicity == "server_defined" and n_fail:
            out["tip"] = ("该动词由服务端决定原子性，返回体不含 per-id 结果——"
                          "无法判定批量中哪些已生效，请用 kd_read 逐个查证后再重试。")
        return out

    async def _one(self, op, v, n, target, payload, out, current_state,
                   operation: Optional[str] = None,
                   batch_targets: Optional[list[str]] = None) -> None:
        endpoint = v.endpoint or "execute"
        if endpoint == "execute":
            payload = {**payload, "_op_number": operation or _OP_NUMBER.get(v.name, v.name)}
        try:
            result = await self.t.call(endpoint, n.form_id, payload)
            ok, errors = _ok(result)
            outcome = "success" if ok else "failed"
        except Exception as exc:                       # 结果不可判定，必须与失败区分
            ok, errors, outcome = False, [{"message": f"{type(exc).__name__}: {exc}"}], "unknown"
            result = None

        rs = (result or {}).get("Result", result or {}) if isinstance(result, dict) else {}
        fid = rs.get("Id") or rs.get("FID") or (target if v.name != "save" else None)
        bill_no = (rs.get("Number") or rs.get("FBillNo")
                   or getattr(self, "_no_by_id", {}).get(str(target)))

        op.step(verb=v.name.capitalize(), noun=n.form_id, endpoint=endpoint,
                object_id=str(fid) if fid else None, object_no=bill_no,
                state_from=current_state,
                state_to=v.to_state if ok and v.to_state else None,
                outcome=outcome,
                error=errors[0] if errors else None,
                batch_targets=batch_targets)

        row = {"target": target, "outcome": outcome, "fid": fid, "bill_no": bill_no}
        if errors:
            row["errors"] = errors
        if batch_targets and outcome != "success":
            row["per_id_unavailable"] = True
        out["results"].append(row)
        (out["succeeded"] if ok else out["failed"]).append(target)

    # ── 链接动词 ──────────────────────────────────────────────
    async def push(self, source: str, target: str, source_bill_nos: list[str],
                   rule_id: str = "", on_behalf_of: Optional[str] = None) -> dict:
        link = self.o.check_link(source, target)                # PRE-02，发请求前拦截
        s, t = self.o.resolve_noun(source), self.o.resolve_noun(target)
        payload: dict[str, Any] = {"TargetFormId": t.form_id, "Numbers": source_bill_nos}
        if rule_id:
            payload["RuleId"] = rule_id

        out: dict[str, Any] = {"verb": "push", "from": s.form_id, "to": t.form_id,
                               "link_verified": link.get("verified"),
                               "contract": {"atomicity": "server_defined",
                                            "idempotent": False, "inverse": None,
                                            "destructive": True}}
        if link.get("verified") == "suspect":
            out["warning"] = ("该下推关系在本仓库中标记为**存疑**，尚未在真实账套验证："
                              + link.get("note", ""))

        ctx = (_Reuse(self._op_ctx) if self._op_ctx is not None
               else audit_recorder.operation("kd_push", actor=self.actor,
                                             on_behalf_of=on_behalf_of))
        with ctx as op:
            try:
                result = await self.t.call("push", s.form_id, payload)
                ok, errors = _ok(result)
                outcome = "success" if ok else "failed"
            except Exception as exc:
                ok, errors, outcome = False, [{"message": f"{type(exc).__name__}: {exc}"}], "unknown"
                result = None
            rs = (result or {}).get("Result", result or {}) if isinstance(result, dict) else {}
            out["target_bill_nos"] = rs.get("Numbers", []) or []
            out["target_fids"] = [str(x) for x in (rs.get("Ids", []) or [])]
            out["success"], out["outcome"] = ok, outcome
            if errors:
                out["errors"] = errors
            for no in (out["target_bill_nos"] or [None]):
                op.step(verb="Push", noun=t.form_id, endpoint="push",
                        object_no=no, outcome=outcome,
                        state_to="Z:暂存" if ok else None,
                        error=errors[0] if errors else None,
                        source_noun=s.form_id, source_bill_nos=source_bill_nos)
        if ok:
            out["next"] = ("目标单为草稿。需要生效时调用 "
                           "kd_act(verb='submit'|'audit', noun=%r, targets=target_fids)。"
                           "本工具**不自动提交审核**——中途失败会留下无人认领的中间态。"
                           % t.form_id)
        return out

    # ── 读动词 ────────────────────────────────────────────────
    async def query(self, noun: str, filter_string: str = "",
                    fields: str = "", top: int = 50) -> dict:
        """查询。支持逗号分隔的多个名词（合并查询，如"销售出库单,采购入库单"）。

        系统对象（用户/角色/权限…）走各自的专用端点，由注册表的
        system_endpoint 决定，调用方不必知道这个区别。
        """
        refs = [x.strip() for x in noun.split(",") if x.strip()] if isinstance(noun, str) else list(noun)
        if len(refs) > 1:
            groups = []
            for ref in refs:
                try:
                    groups.append(await self.query(ref, filter_string, fields, top))
                except OntologyError as e:
                    groups.append({"noun": ref, "error": str(e)})
            return {"nouns": refs, "total": sum(g.get("count", 0) for g in groups),
                    "groups": groups}

        _, n = self.o.check_verb_applies("query", refs[0])
        fk = fields or n.default_fields
        if n.system_endpoint:
            rows = await self.t.system_query(n.system_endpoint, n.form_id, fk, filter_string, top)
        else:
            rows = await self.t.query(n.form_id, fk, filter_string, top)
        rows = rows if isinstance(rows, list) else _rows_of(rows)
        return {"noun": n.form_id, "zh": n.zh, "category": n.category,
                "count": len(rows), "fields": fk, "rows": rows}

    async def read(self, noun: str, bill_id: str) -> dict:
        """查看单据详情（View 端点）。"""
        _, n = self.o.check_verb_applies("read", noun)
        result = await self.t.view(n.form_id, str(bill_id))
        data = result.get("Result", {}).get("Result", result) if isinstance(result, dict) else result
        return {"noun": n.form_id, "zh": n.zh, "bill_id": str(bill_id), "data": data}

    # ── 对象层：以对象为中心的入口（Palantir Ontology 式）────────
    async def object_card(self, noun: str, obj_id: Optional[str] = None) -> dict:
        """打开一个对象：属性 + 状态 + 此刻能做什么 + 连到什么。

        不带 obj_id 时返回**类型卡片**（这类对象长什么样、能做什么），
        与实例卡片同形状 —— 使用者不必学两套结构。
        """
        if obj_id is None:
            card = self.m.card(noun)
            card["hint"] = ("这是类型卡片。带 id 可打开具体对象；"
                            "actions 里的 enabled 此时只反映'该类型是否支持'，"
                            "不反映'这一张单此刻能不能做'。")
            return card

        ot = self.m.object_type(noun)
        raw = await self.read(noun, obj_id)
        props = _flatten_props(raw.get("data"))
        if not props:
            # View 拿不到就退回按标识查一行——有些视图类对象没有 View 接口
            key = ot.title_property if not str(obj_id).isdigit() else ot.id_property
            q = await self.query(noun, f"{key}='{obj_id}'", top=1)
            rows = q.get("rows") or []
            props = rows[0] if rows else {}
        if not props:
            raise OntologyError(
                f"没找到 {ot.form_id} 中标识为 {obj_id!r} 的对象。"
                f"确认传的是内码 {ot.id_property} 还是编号 {ot.title_property}——"
                f"两者不通用，系统也没有提供二者的解析动词（审计 L-3）。")
        return self.m.card(noun, props)

    def identify(self, bill_no: str) -> dict:
        """按编号前缀识别单据类型。启发式，返回候选而非断言。"""
        return self.m.identify(bill_no)

    async def navigate(self, noun: str, to: str, bill_no: str,
                       try_candidates: bool = True, top: int = 20) -> dict:
        """从一个对象跳到它的下游单据。

        下游单据引用源单的字段名随表单与二开而异，静态推断不出唯一答案。
        以前这里只返回候选过滤式让调用方自己试；现在**替它试**：
        逐个候选查一次，命中即返回结果并说明是哪个字段生效。

        命中之后会向知识库提一条「把 link_filter 写进 profile」的建议
        （只提议，不自动改配置）——试出来的答案不该下次再试一遍。
        """
        link = self.o.check_link(noun, to)
        plan = self.m.navigate(noun, to, bill_no)
        if plan.get("confirmed"):
            rows = await self.query(to, plan["filter"], top=top)
            return {**plan, "rows": rows.get("rows", []), "count": rows.get("count", 0)}
        if not try_candidates:
            return plan

        tried: list[dict] = []
        for flt in plan["candidate_filters"]:
            try:
                r = await self.query(to, flt, top=top)
            except Exception as exc:                 # 字段不存在会报错，属正常淘汰
                tried.append({"filter": flt, "error": f"{type(exc).__name__}: {exc}"[:160]})
                continue
            n = r.get("count", 0)
            tried.append({"filter": flt, "count": n})
            if n:
                self._propose_link_filter(noun, to, flt, link)
                return {**plan, "confirmed_by_probe": True, "filter": flt,
                        "rows": r.get("rows", []), "count": n, "tried": tried,
                        "remember": plan["remember"],
                        "note": (f"用 {flt.split('=')[0]} 查到了 {n} 条。"
                                 f"这是**探测出来的**，不是账套确认的字段——"
                                 f"把它写进 profile 之前建议人眼核对一下。")}
        return {**plan, "confirmed_by_probe": False, "rows": [], "count": 0,
                "tried": tried,
                "note": ("所有候选字段都没查到下游单。可能确实还没下推，"
                         "也可能本账套用了别的关联字段——"
                         f"用 kd_describe(what='fields', key='{self.o.resolve_noun(to).form_id}') "
                         "看真实字段清单。")}

    def _propose_link_filter(self, src: str, dst: str, flt: str, link: dict) -> None:
        """把探测出来的关联字段提给知识库。只提议，不自动改配置。"""
        try:
            import sys as _sys
            from pathlib import Path as _P
            _sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
            from wikiskill.knowledge import Entry, Knowledge
            import hashlib
            s = self.o.resolve_noun(src).form_id
            t = self.o.resolve_noun(dst).form_id
            field = flt.split("=")[0]
            k = Knowledge()
            k.merge(Entry(
                id=hashlib.sha1(f"linkfilter|{s}|{t}".encode()).hexdigest()[:12],
                kind="link_filter_learned",
                title=f"{s} → {t} 的关联字段探测为 {field}",
                detail=f"用 {flt} 查到了下游单据。",
                suggestion=(f"人眼核对后，在 profiles/<租户>/profile.yml 的 links 里给 "
                            f"{{from: {s}, to: {t}}} 加 "
                            f"link_filter: \"{field}='{{bill_no}}'\"，此后不必再逐个试。"),
                occurrences=1,
                evidence=[{"from": s, "to": t, "filter": flt}]))
            k.save()
        except Exception:
            pass  # 知识库不可用不该连累导航

    def search_types(self, keyword: str = "", category: str = "") -> dict:
        types = self.m.search_types(keyword, category)
        return {"count": len(types), "types": types}

    async def fields(self, noun: str) -> dict:
        """实时字段清单（对账套拉元数据，不是注册表里的静态默认字段集）。"""
        n = self.o.resolve_noun(noun)
        data = await self.t.fields(n.form_id)
        if data is None:
            raise OntologyError(
                f"拉不到 {n.form_id} 的元数据。可能是 form_id 在本账套不存在，"
                f"或网络/登录态有问题。用 kd_describe(what='nouns') 确认名词是否正确。")
        return {"noun": n.form_id, "zh": n.zh, **data}

    async def template(self, noun: str) -> dict:
        """已验证的 model 骨架：AI 只需替换占位符，跳过字段名摸索。"""
        n = self.o.resolve_noun(noun)
        tpl = await self.t.template(n.form_id)
        if tpl is None:
            raise OntologyError(
                f"{n.form_id} 没有内置模板。可用 kd_describe(what='fields', key=…) "
                f"拉本账套的真实字段清单，或先 kd_query 取一条已有单据参考结构。")
        return {"noun": n.form_id, "zh": n.zh, "template": tpl,
                "tip": "占位符形如 <客户编码>，替换后建议先 kd_act(dry_run=True) 校验。"}

    async def validate(self, noun: str, model: dict) -> dict:
        """保存前校验（不写入）。对应 kd_act(verb='save', dry_run=True)。"""
        _, n = self.o.check_verb_applies("save", noun)
        return {"noun": n.form_id, "zh": n.zh, "dry_run": True,
                **(await self.t.validate(n.form_id, model))}

    async def report(self, noun: str, payload: dict) -> dict:
        """报表查询（GetSysReportData 端点，payload 结构与单据查询不同）。"""
        n = self.o.resolve_noun(noun) if noun in self.o.nouns or noun in self.o._alias_index \
            else None
        form_id = n.form_id if n else noun
        result = await self.t.report(form_id, payload)
        rs = result.get("Result", result) if isinstance(result, dict) else {}
        rows = rs.get("Rows") or rs.get("rows") or []
        return {"noun": form_id, "count": len(rows), "rows": rows, "raw": rs}

    # ── 业务操作入口（面向人的那一层）──────────────────────────
    async def run_operation(self, key: str, targets: list[str],
                            confirmed: bool = False,
                            on_behalf_of: Optional[str] = None) -> dict:
        """执行租户定义的业务操作。

        与 kingdee_create_and_audit 这类"一站式"工具的关键区别（修 A-1）：
          - 每一步都落一条审计记录，共享同一 trace_id；
          - 中途失败**立即停止并明确报告已产生的中间物**，而不是给一段文字建议；
          - 含不可逆动作时必须先拿到人的确认才开跑。
        补偿仍需人工/后续动作完成 —— 但至少中间态是被记录、可清算的。
        """
        op = self.o.operation(key)
        needs_confirm = op.confirm or any(s.get("做") == "确认" for s in op.steps)
        if needs_confirm and not confirmed:
            questions = [s["问"] for s in op.steps if s.get("做") == "确认"]
            return {
                "operation": op.key, "zh": op.zh, "owner": op.owner,
                "status": "awaiting_confirmation",
                "questions": questions or [f"即将执行『{op.zh}』，确认继续？"],
                "plan": [self._describe_step(s) for s in op.steps],
                "targets": targets,
                "tip": "确认后请带 confirmed=True 重新调用。执行前不会有任何写操作。",
            }

        out: dict[str, Any] = {"operation": op.key, "zh": op.zh, "owner": op.owner,
                               "steps": [], "produced": {}, "halted_at": None}
        carry: list[str] = list(targets)
        carry_noun: Optional[str] = None

        with audit_recorder.operation(f"kd_run:{op.key}", actor=self.actor,
                                      on_behalf_of=on_behalf_of) as trace:
            self._op_ctx = trace
            out["trace_id"] = trace.trace_id
            try:
                return await self._run_steps(op, out, targets, carry, on_behalf_of)
            finally:
                self._op_ctx = None

    async def _run_steps(self, op, out: dict, targets: list[str],
                         carry: list[str], on_behalf_of: Optional[str]) -> dict:
        carry_no_by_id: dict[str, str] = {}
        for i, st in enumerate(op.steps, 1):
            kind = st["做"]
            if kind == "确认":
                out["steps"].append({"step": i, "做": "确认", "outcome": "success",
                                     "问": st.get("问", "")})
                continue
            try:
                if kind == "下推":
                    r = await self.push(st["从"], st["到"], carry,
                                        on_behalf_of=on_behalf_of)
                    ok = r.get("success", False)
                    if ok:
                        fids = r.get("target_fids") or []
                        nos = r.get("target_bill_nos") or []
                        carry = fids or nos
                        carry_no_by_id = dict(zip(fids, nos)) if fids and nos else {}
                        produced_noun = self.o.resolve_noun(st["到"]).form_id
                        out["produced"].setdefault(produced_noun, []).extend(
                            r.get("target_bill_nos") or [])
                else:
                    use = st.get("用", "targets")
                    ids = carry if use == "上一步产物" else targets
                    r = await self.act(kind, st["对象"], ids,
                                       on_behalf_of=on_behalf_of,
                                       no_by_id=carry_no_by_id)
                    ok = r.get("success", False)
            except OntologyError as e:
                ok, r = False, {"error": str(e)}
            out["steps"].append({"step": i, **self._describe_step(st),
                                 "outcome": "success" if ok else "failed",
                                 "detail": r})
            if not ok:
                out["halted_at"] = i
                out["success"] = False
                out["left_behind"] = out["produced"]
                out["tip"] = (
                    f"『{op.zh}』在第 {i} 步失败并已停止。"
                    + (f"已经产生但尚未走完流程的单据：{out['produced']} —— "
                       f"这些是**中间态**，需要继续处理或清理，不会自动回滚。"
                       if out["produced"] else "尚未产生任何单据，无需清理。")
                    + " 用 kd_audit(scope='dangling') 可以随时查到未清算的中间态。")
                return out

        out["success"] = True
        out["tip"] = f"『{op.zh}』全部 {len(op.steps)} 步完成。"
        return out

    @staticmethod
    def _describe_step(st: dict) -> dict:
        kind = st.get("做")
        if kind == "下推":
            return {"做": "下推", "说明": f"{st.get('从')} → {st.get('到')}"}
        if kind == "确认":
            return {"做": "确认", "说明": st.get("问", "")}
        return {"做": kind, "说明": f"对 {st.get('对象')} 执行 {kind}"
                                    f"（{st.get('用', 'targets')}）"}
