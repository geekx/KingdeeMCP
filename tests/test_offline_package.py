"""离线包必须真的能在没网、没装任何依赖的机器上跑。

金蝶云星空常部署在内网／隔离网段，装东西要先申请开外网，或者压根开不了。
所以这组测试的关键不是"造得出来"，而是**造出来的东西在什么都没有的地方能用**：

  * 单文件包里不许有二进制扩展——zipimport 加载不了 .so/.pyd，
    留着只会在没网的机器前面才炸；
  * 退出码必须真的传出来——zipapp 默认生成的 __main__.py 是
    `cli.main()`，返回值被丢掉，退出码永远 0，于是"不可以"被当成"可以"
    （这不是假想，第一版就是这么错的）；
  * 得用一个**没装 PyYAML 的解释器**去跑，否则证明不了它自带了依赖。
"""
import json
import os
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "package"))

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def pyz(tmp_path_factory):
    import build_offline
    out = tmp_path_factory.mktemp("offline")
    return build_offline.build_pyz(out, quiet=True)


@pytest.fixture(scope="module")
def bare_python(tmp_path_factory):
    """一个什么都没装的解释器——连 PyYAML 都没有。

    用当前解释器测等于没测：系统里本来就有 yaml，单文件包哪怕漏带了
    也照样跑得通，而到了客户那台机器上就是 ModuleNotFoundError。
    """
    d = tmp_path_factory.mktemp("bare")
    venv.EnvBuilder(with_pip=False).create(d / "v")
    py = d / "v" / ("Scripts" if os.name == "nt" else "bin") / \
        ("python.exe" if os.name == "nt" else "python")
    probe = subprocess.run([str(py), "-c", "import yaml"],
                           capture_output=True, text=True)
    assert probe.returncode != 0, "这个环境里居然有 yaml，测不出自带依赖有没有生效"
    return py


class TestSingleFileBundle:
    def test_no_binary_extensions(self, pyz):
        """zipimport 加载不了 .so/.pyd。混进去了运行时才炸。"""
        names = zipfile.ZipFile(pyz).namelist()
        bad = [n for n in names if n.endswith((".so", ".pyd", ".dylib"))]
        assert not bad, f"单文件包里有二进制扩展：{bad}"

    def test_carries_its_own_yaml_and_registry(self, pyz):
        names = zipfile.ZipFile(pyz).namelist()
        assert any(n.startswith("yaml/") for n in names), "没带 PyYAML，离线机器上跑不起来"
        assert any(n.endswith("base/registry.yml") for n in names), "没带本体注册表"

    def test_runs_with_zero_installed_packages(self, pyz, bare_python, tmp_path):
        r = subprocess.run([str(bare_python), str(pyz), "can", "audit", "销售订单",
                            "--state", "B:审核中"],
                           cwd=tmp_path, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr
        assert json.loads(r.stdout)["allowed"] is True

    @pytest.mark.parametrize("args,want", [
        (["can", "audit", "销售订单", "--state", "B:审核中"], 0),   # 可以
        (["can", "audit", "销售订单", "--state", "Z:暂存"], 1),     # 不可以
        (["can", "audit", "物料"], 1),                              # 动词就不适用
        (["can", "audit", "销售订单"], 2),                          # 事实不全，判不了
        (["can", "瞎写的动词", "销售订单"], 3),                      # 用法错误
    ])
    def test_exit_codes_survive_the_zipapp(self, pyz, bare_python, tmp_path,
                                           args, want):
        """退出码是这个 CLI 的用处所在（`kd-logic can … && 真去执行`）。

        zipapp 的 main= 生成的入口会丢掉返回值，退出码恒为 0——
        那等于把"不可以"和"事实不全"都当成"可以"，是最坏的一种坏法。
        """
        r = subprocess.run([str(bare_python), str(pyz), *args],
                           cwd=tmp_path, capture_output=True, text=True, timeout=120)
        assert r.returncode == want, (
            f"{args} 期望退出码 {want}，实际 {r.returncode}\n{r.stdout}{r.stderr}")

    def test_works_from_an_unrelated_directory(self, pyz, bare_python, tmp_path):
        """离线包会被拷到任何地方——不能依赖仓库或当前目录的结构。"""
        d = tmp_path / "somewhere" / "else"
        d.mkdir(parents=True)
        r = subprocess.run([str(bare_python), str(pyz), "describe", "logic"],
                           cwd=d, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        assert len(json.loads(r.stdout)["functions"]) >= 6


class TestManifest:
    def test_checksums_match(self, tmp_path_factory):
        """离线传输靠 U 盘和邮件附件，传坏了要能发现。"""
        import build_offline
        import hashlib
        out = tmp_path_factory.mktemp("man")
        build_offline.build_pyz(out, quiet=True)
        man = json.loads(build_offline.write_manifest(out, quiet=True)
                         .read_text(encoding="utf-8"))
        assert man["files"], "清单是空的"
        for f in man["files"]:
            h = hashlib.sha256((out / f["path"]).read_bytes()).hexdigest()
            assert h == f["sha256"], f"{f['path']} 校验和对不上"
