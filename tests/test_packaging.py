"""打包产物必须真的能装、能跑。

这类缺陷**对现有测试是隐形的**：conftest 往 sys.path 里塞了仓库根、src/、
tools/ontology/，于是 `from operation_audit import ...` 在测试里畅通无阻，
装成 wheel 之后却 ModuleNotFoundError——而 2826 条测试全绿。
（这不是假想：operation_audit 当初就住在 tools/ontology/，是运行期代码
却没进包，正是被这里的第一条测试逼出来的。）

所以分两层守：
  * 静态检查（快，每次都跑）：包里不许出现"只有靠 sys.path 补丁才导得到"的导入；
  * 装包实测（慢，标 slow）：真建 wheel、真装进干净 venv、真跑一遍。
"""
import ast
import os
import subprocess
import sys
import sysconfig
import venv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "kingdee_ontology"

# 声明在 pyproject 里的第三方依赖（含可选）
DECLARED = {"mcp", "httpx", "pydantic", "yaml", "pyodbc", "pytest"}
# 同一个 wheel 里的另一个包
SIBLINGS = {"kingdee_mcp", "kingdee_ontology"}


def _stdlib() -> set[str]:
    names = set(getattr(sys, "stdlib_module_names", ()))
    names |= set(sys.builtin_module_names)
    return names


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:            # 相对导入，包内的事
                continue
            if node.module:
                out.add(node.module.split(".")[0])
    return out


class TestNoUnshippableImports:
    def test_package_only_imports_what_ships(self):
        """包里每一个导入，装完之后都得存在。

        允许的来源只有三类：标准库、pyproject 声明的依赖、同一个 wheel 里的包。
        其它一律是"只有在仓库里才导得到"——那种模块装完就没了。
        """
        allowed = _stdlib() | DECLARED | SIBLINGS
        bad: list[str] = []
        for f in sorted(PKG.rglob("*.py")):
            for mod in _top_level_imports(f) - allowed:
                bad.append(f"{f.relative_to(ROOT)} → import {mod}")
        assert not bad, (
            "这些导入装成 wheel 之后会 ModuleNotFoundError（测试里能过是因为 "
            "conftest 补了 sys.path）：\n  " + "\n  ".join(bad))

    def test_no_syspath_patching_inside_the_package(self):
        """包内不许自己改 sys.path。

        往 sys.path 里塞仓库相对路径，是"只在源码树里能跑"的典型写法：
        装进 site-packages 后那些路径根本不存在，且会静默地什么都不做。
        """
        bad = [str(f.relative_to(ROOT)) for f in PKG.rglob("*.py")
               if "sys.path.insert" in f.read_text(encoding="utf-8")
               or "sys.path.append" in f.read_text(encoding="utf-8")]
        assert not bad, f"包内不该改 sys.path：{bad}"

    def test_registry_ships_with_the_package(self):
        """注册表是数据不是代码，但服务端没它跑不起来。"""
        import tomllib
        cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        inc = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
        assert any(k.endswith("registry.yml") for k in inc), \
            "registry.yml 没进 wheel，装完的包无法加载本体"


class TestJudgementLayerIsSelfContained:
    """判断层要能被单独装、单独跑——这是"独立服务"说法成立的前提。"""

    def test_aip_needs_only_yaml_transitively(self):
        """aip + base.ontology 的传递依赖里不该有 mcp / httpx / pyodbc。

        它们各自是 MCP 协议栈、HTTP 客户端、需要现场编译的 ODBC 驱动。
        判断是纯计算，不该为了它把这三样拖进来。
        """
        heavy = {"mcp", "httpx", "pyodbc"}
        seen: set[Path] = set()
        found: list[str] = []

        def walk(f: Path) -> None:
            if f in seen or not f.is_file():
                return
            seen.add(f)
            for mod in _top_level_imports(f):
                if mod in heavy:
                    found.append(f"{f.relative_to(ROOT)} → {mod}")
                if mod == "kingdee_ontology":
                    pass
            for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                elif isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                for n in names:
                    if not n.startswith("kingdee_ontology."):
                        continue
                    rel = n[len("kingdee_ontology."):].replace(".", "/")
                    for cand in (PKG / f"{rel}.py", PKG / rel / "__init__.py"):
                        if cand.is_file():
                            walk(cand)

        walk(PKG / "aip" / "logic.py")
        walk(PKG / "base" / "ontology.py")
        assert not found, f"判断层被重依赖污染了：{found}"


@pytest.mark.slow
class TestWheelActuallyInstalls:
    """真建、真装、真跑。静态检查再细也代不了这一步。"""

    @staticmethod
    @pytest.fixture(scope="class")
    def installed(tmp_path_factory):
        pytest.importorskip("build", reason="需要 build 才能造 wheel")
        d = tmp_path_factory.mktemp("pkg")
        r = subprocess.run([sys.executable, "-m", "build", "--wheel", "-o", str(d)],
                           cwd=ROOT, capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, f"wheel 都没造出来：{r.stderr[-2000:]}"
        whl = next(d.glob("*.whl"))
        env = d / "venv"
        venv.EnvBuilder(with_pip=True).create(env)
        bindir = "Scripts" if os.name == "nt" else "bin"
        py = env / bindir / ("python.exe" if os.name == "nt" else "python")
        r = subprocess.run([str(py), "-m", "pip", "install", "-q", str(whl)],
                           capture_output=True, text=True, timeout=900)
        assert r.returncode == 0, f"装不上：{r.stderr[-2000:]}"
        return env / bindir, d

    def test_imports_outside_the_repo(self, installed, tmp_path):
        """在与仓库无关的目录里跑——源码树的相对路径在这里全都不成立。"""
        bindir, _ = installed
        py = bindir / ("python.exe" if os.name == "nt" else "python")
        code = ("import kingdee_ontology.base.server, kingdee_ontology.cli, "
                "kingdee_ontology.saga.engine, kingdee_ontology.pipeline.run, "
                "kingdee_ontology.indexlayer.store, kingdee_ontology.harness.rules, "
                "kingdee_ontology.wikiskill.retro, kingdee_mcp.server; print('ok')")
        r = subprocess.run([str(py), "-c", code], cwd=tmp_path,
                           capture_output=True, text=True, timeout=180)
        assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-2000:]

    def test_cli_decides_without_the_repo(self, installed, tmp_path):
        """判断层在干净环境里给出正确答案，且退出码可用于脚本编排。"""
        bindir, _ = installed
        kd = bindir / ("kd-logic.exe" if os.name == "nt" else "kd-logic")
        ok = subprocess.run([str(kd), "can", "audit", "销售订单", "--state", "B:审核中"],
                            cwd=tmp_path, capture_output=True, text=True, timeout=120)
        assert ok.returncode == 0, ok.stdout + ok.stderr
        no = subprocess.run([str(kd), "can", "audit", "物料"],
                            cwd=tmp_path, capture_output=True, text=True, timeout=120)
        assert no.returncode == 1, "明确不行应当退出码 1"
        unk = subprocess.run([str(kd), "can", "audit", "销售订单"],
                             cwd=tmp_path, capture_output=True, text=True, timeout=120)
        assert unk.returncode == 2, "事实不全应当退出码 2，不能和『可以』混为一谈"
