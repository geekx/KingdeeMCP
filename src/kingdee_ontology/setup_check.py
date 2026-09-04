"""引导式环境自检——不需要先接上 MCP 协议就能跑的第一步。

驱动这套 MCP 的可能是 Claude Code，也可能是别的 agent harness（Workbuddy、
基于 DeepSeek 的 agent……），它们发现和调用 MCP 工具的方式、注入配置的方式
都不尽相同。但**所有 harness 都能起一个子进程、读它的输出**——所以第一步
干脆不依赖 MCP 协议本身：

    $ python3 -m kingdee_ontology.setup_check

三件事，依次做，前一件不过就不做后一件：

    1. 配置从哪读到的——四个必填变量都有值了吗
    2. 租户配置（如果指定了 --tenant）静态校验过不过
    3. 真登录一次、只读探测几类常见单据，报告这个账号大概能碰到哪些模块

第 3 步会真的向目标金蝶账套发请求，脚本会在做之前明确打印出来——不做静默的
网络访问。不想联网测试就加 --skip-probe，只看前两步。

退出码：0 全部正常 / 1 配置或 profile 有问题 / 2 连不上或登录失败。
适合接在 CI 或安装脚本里当门禁：`python3 -m kingdee_ontology.setup_check || exit`。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Optional

from kingdee_ontology.envfile import load_env_file

_ENV_PATH = load_env_file()   # 必须在下面任何 os.environ.get("KINGDEE_*") 之前

_REQUIRED = ["KINGDEE_SERVER_URL", "KINGDEE_ACCT_ID", "KINGDEE_USERNAME", "KINGDEE_PASSWORD"]


def _p(line: str = "") -> None:
    print(line, file=sys.stderr)


def check_config() -> tuple[bool, dict]:
    have = {k: bool(os.environ.get(k)) for k in _REQUIRED}
    missing = [k for k, v in have.items() if not v]
    return not missing, {"env_file": str(_ENV_PATH) if _ENV_PATH else None,
                         "have": have, "missing": missing}


def report_config(result: dict) -> None:
    _p("== ① 配置 ==")
    if result["env_file"]:
        _p(f"从这个文件读到的：{result['env_file']}")
    else:
        _p("没找到本地凭据文件（.env / ~/.kingdee-mcp.env），"
           "配置只能来自真实环境变量。")
    if result["missing"]:
        _p(f"缺少：{', '.join(result['missing'])}")
        _p("把 .env.example 复制成 .env，填好这几项再跑一遍——"
           "这份文件已经在 .gitignore 里，不会被提交。")
    else:
        _p("四项必填变量都有值了。")


def report_profile(tenant: str, errs: list[str], warns: list[str]) -> None:
    _p(f"\n== ② 租户配置（{tenant or '（默认，无覆盖层）'}）==")
    if errs:
        _p("有错误，需要先修：")
        for e in errs:
            _p(f"  ✗ {e}")
    else:
        _p("校验通过。")
    for w in warns:
        _p(f"  ⚠ {w}")


def report_connection(result: dict) -> None:
    _p("\n== ③ 联通与权限探测 ==")
    for r in result["probed"]:
        mark = {"ok": "✓", "no_permission": "✗ 无权限", "business_error": "? ",
                "blocked": "‼"}.get(r["outcome"], r["outcome"])
        line = f"  {mark} {r['zh']}（{r['noun']}）"
        if r.get("unregistered"):
            line += "　⟨本体未登记，见下方⟩" if r["outcome"] == "ok" else "　⟨本体未登记⟩"
        if r.get("detail"):
            line += f" —— {r['detail']}"
        _p(line)
    _p(f"\n可查：{result['ok']} / 候选共 {len(result['candidates'])}")
    if result.get("unregistered_found"):
        _p(f"\n有 {result['unregistered_found']} 个是账号能用、但本体没登记的表单——"
           f"已提给 WikiSkill（wikiskill/knowledge.json）当建议，不会自动改配置。"
           f"人眼核对后想接进来，去补 profiles/<租户>/profile.yml 的 nouns 段。")
    if result["stopped_early"]:
        s = result["stopped_early"]
        _p(f"探测在「{s['at']}」中断：{s['reason']}")
        if s["untested"]:
            _p(f"还有 {len(s['untested'])} 个没测到：{', '.join(s['untested'])}")
    _p(f"\n（{result['note']}）")


async def _run(argv: Optional[list[str]]) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m kingdee_ontology.setup_check",
        description="引导式环境自检：配置从哪读、连不连得上、这个账号能碰到什么。")
    ap.add_argument("--tenant", default=os.environ.get("KINGDEE_TENANT", ""),
                    help="租户名，读 profiles/<租户>/profile.yml；留空只用底座默认注册表")
    ap.add_argument("--nouns", default="",
                    help="逗号分隔，自己指定要探测哪几类单据；留空用系统默认候选。"
                        "写一个本体不认识的 form_id 也可以——如果账号真的能查，"
                        "会提一条建议到 wikiskill/knowledge.json")
    ap.add_argument("--limit", type=int, default=10, help="默认候选最多挑几个")
    ap.add_argument("--skip-probe", action="store_true", help="只查配置与 profile，不联网")
    ap.add_argument("--json", action="store_true", help="额外把结构化结果打到 stdout")
    a = ap.parse_args(argv)

    ok, cfg = check_config()
    report_config(cfg)
    if not ok:
        return 1

    from kingdee_ontology.base.ontology import OntologyError
    from kingdee_ontology.base.validate_profile import validate
    try:
        errs, warns = validate(a.tenant)
    except OntologyError as e:
        # 指定了一个不存在的租户名——load_profile 直接抛，不是走 errs 列表。
        # 这里接住，给一份和"配置里有错误"同样干净的报告，而不是让整个自检
        # 脚本崩成一截 traceback。
        report_profile(a.tenant, [str(e)], [])
        return 1
    report_profile(a.tenant, errs, warns)
    if errs:
        return 1

    if a.skip_probe:
        _p("\n（已跳过联通测试：--skip-probe）")
        if a.json:
            print(json.dumps({"config": cfg, "profile_errors": errs,
                              "profile_warnings": warns}, ensure_ascii=False))
        return 0

    _p("\n即将用这个账号真实登录并做只读查询探测——请确认这是你打算测试的账套。")
    from kingdee_ontology.base.dispatch import Dispatcher
    from kingdee_ontology.base.ontology import load
    from kingdee_ontology.base.probe import probe_connection

    nouns = [x.strip() for x in a.nouns.split(",") if x.strip()] or None
    conn: dict
    try:
        d = Dispatcher(ontology=load(tenant=a.tenant),
                      actor=os.environ.get("KINGDEE_USERNAME", "setup_check"))
        conn = await probe_connection(d, nouns=nouns, limit=a.limit)
    except Exception as e:
        conn = {"login": "failed", "error": f"{type(e).__name__}: {e}"}
        _p(f"\n== ③ 联通与权限探测 ==\n登录失败：{conn['error']}")
        if a.json:
            print(json.dumps({"config": cfg, "profile_errors": errs,
                              "profile_warnings": warns, "connection": conn},
                             ensure_ascii=False))
        return 2

    report_connection(conn)
    if a.json:
        print(json.dumps({"config": cfg, "profile_errors": errs,
                          "profile_warnings": warns, "connection": conn},
                         ensure_ascii=False))
    # 一次成功都没有、且是因为探测本身跑不通才停的（不是全部候选都试完、
    # 只是恰好都无权限）——这才叫真的没连上，退出码要能被脚本用来判断。
    if conn["ok"] == 0 and conn["stopped_early"]:
        return 2
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    return asyncio.run(_run(argv if argv is not None else sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
