"""引导式自检：不需要 MCP 协议就能跑的第一步。

这是给"人或另一个 harness 直接执行"用的入口，所以测试重点在**退出码**——
脚本的价值就是能被别的脚本 `&& / ||` 起来当门禁，退出码错了，这个入口
存在的意义就没了。人读的文字报告走 stderr，--json 的结构化结果走 stdout，
两者不能串。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kingdee_ontology.base.transport import FakeTransport  # noqa: E402
from kingdee_ontology.setup_check import _REQUIRED, _run, check_config  # noqa: E402

REQUIRED_ENV = {
    "KINGDEE_SERVER_URL": "http://example.invalid/k3cloud/",
    "KINGDEE_ACCT_ID": "acct",
    "KINGDEE_USERNAME": "tester",
    "KINGDEE_PASSWORD": "pw",
}


@pytest.fixture(autouse=True)
def _isolated_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("KINGDEE_OPERATION_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    import kingdee_ontology.operation_audit as oa
    monkeypatch.setattr(oa, "audit_recorder", oa.AuditRecorder(tmp_path / "audit.jsonl"))
    import kingdee_ontology.base.dispatch as bd
    monkeypatch.setattr(bd, "audit_recorder", oa.audit_recorder)


@pytest.fixture
def configured(monkeypatch):
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def unconfigured(monkeypatch):
    for k in _REQUIRED:
        monkeypatch.delenv(k, raising=False)


def _patch_dispatcher(monkeypatch, script):
    """setup_check 内部惰性 `from kingdee_ontology.base.dispatch import Dispatcher`——
    补丁原类，让它构造出来的实例走假传输。"""
    import kingdee_ontology.base.dispatch as bd
    real_cls = bd.Dispatcher

    def factory(*a, **kw):
        kw["transport"] = FakeTransport(script)
        return real_cls(*a, **kw)
    monkeypatch.setattr(bd, "Dispatcher", factory)


class TestCheckConfig:
    def test_reports_missing_keys(self, unconfigured):
        ok, result = check_config()
        assert ok is False
        assert set(result["missing"]) == set(_REQUIRED)

    def test_all_present_is_ok(self, configured):
        ok, result = check_config()
        assert ok is True
        assert result["missing"] == []


class TestRunExitCodes:
    @pytest.mark.asyncio
    async def test_missing_config_is_exit_1(self, unconfigured, capsys):
        rc = await _run(["--skip-probe"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "缺少" in err

    @pytest.mark.asyncio
    async def test_bad_named_tenant_is_exit_1(self, configured, capsys):
        rc = await _run(["--tenant", "这个租户压根不存在", "--skip-probe"])
        assert rc == 1

    @pytest.mark.asyncio
    async def test_no_tenant_with_skip_probe_is_exit_0(self, configured):
        """默认（无租户覆盖层）+ 跳过联通，是最常见的"只想确认配置能读到"路径。"""
        rc = await _run(["--skip-probe"])
        assert rc == 0

    @pytest.mark.asyncio
    async def test_successful_probe_is_exit_0(self, configured, monkeypatch):
        _patch_dispatcher(monkeypatch, [[] for _ in range(10)])
        rc = await _run([])
        assert rc == 0

    @pytest.mark.asyncio
    async def test_immediate_block_is_exit_2(self, configured, monkeypatch):
        """第一次探测就跑不通（登录失败/网络问题）——这才是"真的没连上"。"""
        class Boom(FakeTransport):
            async def query(self, *a, **kw):
                raise RuntimeError("金蝶登录失败: 密码错误")
        import kingdee_ontology.base.dispatch as bd
        real_cls = bd.Dispatcher
        monkeypatch.setattr(bd, "Dispatcher",
                            lambda *a, **kw: real_cls(*a, **{**kw, "transport": Boom()}))
        rc = await _run([])
        assert rc == 2

    @pytest.mark.asyncio
    async def test_partial_success_then_block_is_still_exit_0(self, configured, monkeypatch):
        """前面已经证明连得上、也有权限——后面某一个候选才失败，不该被算成"没连上"。"""
        class BoomOnSecond(FakeTransport):
            def __init__(self):
                super().__init__()
                self.n = 0

            async def query(self, *a, **kw):
                self.n += 1
                if self.n == 2:
                    raise RuntimeError("ConnectError")
                return []
        import kingdee_ontology.base.dispatch as bd
        real_cls = bd.Dispatcher
        monkeypatch.setattr(bd, "Dispatcher",
                            lambda *a, **kw: real_cls(*a, **{**kw, "transport": BoomOnSecond()}))
        rc = await _run([])
        assert rc == 0

    @pytest.mark.asyncio
    async def test_json_output_goes_to_stdout_report_to_stderr(self, configured,
                                                                monkeypatch, capsys):
        _patch_dispatcher(monkeypatch, [[]])
        rc = await _run(["--nouns", "销售订单", "--json"])
        assert rc == 0
        out, err = capsys.readouterr()
        assert "== ① 配置" in err, "人读的报告应该走 stderr，不该混进 --json 的 stdout"
        payload = json.loads(out.strip().splitlines()[-1])
        assert payload["connection"]["ok"] == 1

    @pytest.mark.asyncio
    async def test_custom_nouns_are_used(self, configured, monkeypatch):
        _patch_dispatcher(monkeypatch, [[]])
        import kingdee_ontology.base.probe as probe_mod
        called = {}
        real = probe_mod.probe_connection

        async def spy(d, nouns=None, limit=10):
            called["nouns"] = nouns
            return await real(d, nouns=nouns, limit=limit)
        monkeypatch.setattr(probe_mod, "probe_connection", spy)
        # setup_check 里是 `from ... import probe_connection` 的惰性导入，
        # 补丁模块属性即可，惰性 import 语句执行时会拿到补丁后的引用。
        await _run(["--nouns", "物料"])
        assert called["nouns"] == ["物料"]
