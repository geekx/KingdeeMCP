"""按需独立运行的判断服务。

**为什么值得单独做一个入口**：一个 agent 要知道「这张单现在能不能审核」，
今天的做法是把本体读进上下文，然后由模型推。那件事有三重成本——token、
延迟，以及它会推错。判断是**确定性的**，不该由概率模型来做。

    $ kd-logic can audit 销售订单 --state Z:暂存
    {"allowed": false, "why": "audit(审核) 要求对象处于 ['B:审核中'] 之一…"}

用时几十毫秒，零 token，答案每次都一样。

**装它不必把整个 MCP 服务端拖下来**：判断层只依赖 PyYAML，
`uvx --from kingdee-mcp kd-logic ...` 秒级就绪，不会去编译 pyodbc。

退出码是给脚本用的：0 = 可以做，1 = 不可以，2 = 事实不全判不了，
3 = 用法错误。所以 `kd-logic can ... && 真去执行` 这种写法是成立的。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

EXIT_OK, EXIT_DENIED, EXIT_UNDETERMINED, EXIT_USAGE = 0, 1, 2, 3


def _out(obj, pretty: bool) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2 if pretty else None))


def _load(tenant: Optional[str], registry: Optional[str]):
    from kingdee_ontology.base.ontology import load
    return load(path=registry, tenant=tenant)


def cmd_can(a) -> int:
    from kingdee_ontology.aip.logic import can
    o = _load(a.tenant, a.registry)
    d = can(o, a.verb, a.noun, state=a.state, target=a.target,
            params={"operation": a.operation} if a.operation else {})
    _out(d.to_dict(), a.pretty)
    if d.blocks:
        return EXIT_DENIED
    return EXIT_UNDETERMINED if d.undetermined else EXIT_OK


def cmd_explain(a) -> int:
    """一个对象**现在**能做什么、不能做什么，各自为什么。

    比 `can` 少一轮猜：不必先想到该问哪个动词。
    """
    from kingdee_ontology.base.objects import ObjectModel
    o = _load(a.tenant, a.registry)
    m = ObjectModel(o)
    card = m.card(a.noun)
    rows = []
    for act in card["actions"]:
        d = next(x for x in m.object_type(a.noun).actions
                 if x.verb == act["verb"]).decide(a.state)
        rows.append({"verb": act["verb"], "zh": act["zh"],
                     "allowed": d.allowed, "undetermined": bool(d.undetermined),
                     "why": d.why()})
    _out({"object_type": card["object_type"], "zh": card["zh"],
          "state": a.state, "actions": rows}, a.pretty)
    return EXIT_OK


def cmd_describe(a) -> int:
    o = _load(a.tenant, a.registry)
    _out(o.describe(a.what, a.key), a.pretty)
    return EXIT_OK


def cmd_validate(a) -> int:
    from kingdee_ontology.base.validate_profile import validate
    errs, warns = validate(a.tenant or "")
    _out({"ok": not errs, "errors": errs, "warnings": warns}, a.pretty)
    return EXIT_OK if not errs else EXIT_DENIED


def cmd_serve(a) -> int:
    """把判断层挂成一个本地 HTTP 端点。

    只在**反复**判断时才值得起进程——单次判断用 `can` 更省事。
    刻意只监听回环地址：判断层不做鉴权，不该被暴露到网络上。
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    from kingdee_ontology.aip.logic import can
    o = _load(a.tenant, a.registry)

    class H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:                      # noqa: N802
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/health":
                    body = {"ok": True, "nouns": len(o.nouns), "verbs": len(o.verbs)}
                elif u.path == "/can":
                    body = can(o, q["verb"], q["noun"], state=q.get("state"),
                               target=q.get("target")).to_dict()
                else:
                    body, code = {"error": "只有 /can 与 /health"}, 404
                    return self._send(body, code)
            except KeyError as e:
                return self._send({"error": f"缺少参数 {e}"}, 400)
            except Exception as e:                     # 本体错误也要如实回，不要 500 白页
                return self._send({"error": str(e)}, 400)
            self._send(body, 200)

        def _send(self, body: dict, code: int) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args) -> None:          # 别把访问日志刷进 stderr
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print(f"判断层已就绪 http://127.0.0.1:{a.port}/can?verb=audit&noun=销售订单&state=B:审核中",
          file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return EXIT_OK


# 全局开关在子命令前后都要认。argparse 默认只认前面，而"把 --pretty 写在
# 末尾"恰恰是最自然的写法——为这个报一次错，调用方（尤其是 agent）就得
# 重来一轮。用 SUPPRESS 让未给出的选项不写进 namespace，两处便不会互相覆盖。
_COMMON = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
_COMMON.add_argument("--tenant", help="租户名，读 profiles/<租户>/profile.yml")
_COMMON.add_argument("--registry", help="自定义 registry.yml 路径")
_COMMON.add_argument("--pretty", action="store_true", help="缩进输出")
_DEFAULTS = {"tenant": None, "registry": None, "pretty": False}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kd-logic", parents=[_COMMON],
        description="金蝶本体判断层：确定性地回答『这一步能不能做』，不需要模型。",
        epilog="退出码：0 可以 / 1 不可以 / 2 事实不全判不了 / 3 用法错误")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("can", parents=[_COMMON], help="能不能对这个对象做这个动作")
    c.add_argument("verb"); c.add_argument("noun")
    c.add_argument("--state", help="对象当前状态；不给则判为『事实不全』")
    c.add_argument("--target", help="下推目标单类型")
    c.add_argument("--operation", help="金蝶操作编码")
    c.set_defaults(fn=cmd_can)

    e = sub.add_parser("explain", parents=[_COMMON], help="这个对象现在能做什么、不能做什么")
    e.add_argument("noun"); e.add_argument("--state")
    e.set_defaults(fn=cmd_explain)

    d = sub.add_parser("describe", parents=[_COMMON], help="查本体")
    d.add_argument("what", choices=["verbs", "nouns", "states", "links", "rules",
                                    "operations", "prefixes", "logic"])
    d.add_argument("key", nargs="?")
    d.set_defaults(fn=cmd_describe)

    v = sub.add_parser("validate", parents=[_COMMON], help="校验租户配置")
    v.set_defaults(fn=cmd_validate)

    s = sub.add_parser("serve", parents=[_COMMON], help="挂成本地 HTTP 端点（只监听回环）")
    s.add_argument("--port", type=int, default=8787)
    s.set_defaults(fn=cmd_serve)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    a = build_parser().parse_args(argv)
    for k, v in _DEFAULTS.items():
        if not hasattr(a, k):
            setattr(a, k, v)
    try:
        return a.fn(a)
    except Exception as e:
        _out({"error": str(e), "type": type(e).__name__}, getattr(a, "pretty", False))
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
