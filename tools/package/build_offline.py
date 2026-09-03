"""造离线包。

金蝶云星空常常部署在内网／隔离网段——装个东西要先申请开外网，或者压根开不了。
所以"能不能离线装"对这个项目不是锦上添花。

造两种产物，对应两种不同的离线处境：

  kd-logic.pyz      单文件，不用装，有个 Python 解释器就能跑。判断层用它。
                    纯 Python，跨平台，随便拷。
  wheelhouse/       一堆 wheel + 安装脚本，`pip install --no-index` 完整安装。
                    **认平台**：pydantic-core、PyYAML 带二进制轮子，
                    Linux 上造的装不到 Windows 上去。所以可以指定目标平台。

两者都带 SHA256 清单——离线传输往往靠 U 盘和邮件附件，传坏了要能发现。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import zipapp
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "src" / "kingdee_ontology"
OUT = ROOT / "dist" / "offline"

# 判断层跑起来只需要 PyYAML。mcp / httpx / pydantic 只有 MCP 服务端要，
# pyodbc 只有 SQL 探查要——单文件包里一个都不带。
PYZ_VENDOR = ["yaml"]


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_pkg(src: Path, dst: Path) -> None:
    """拷包，丢掉 __pycache__ 和二进制扩展。

    二进制扩展必须丢：zipimport 加载不了 .so/.pyd，留着只会让 import 在
    运行时炸；而 PyYAML 本来就有纯 Python 实现，去掉 _yaml 会自动回退。
    """
    shutil.copytree(
        src, dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo",
                                      "*.so", "*.pyd", "*.dylib"))


def _vendor_dir(name: str) -> Path:
    mod = __import__(name)
    p = Path(mod.__file__).resolve().parent
    if not p.is_dir():
        raise SystemExit(f"找不到 {name} 的包目录：{p}")
    return p


def build_pyz(out: Path, quiet: bool = False) -> Path:
    staging = out.parent / "_pyz_staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    _copy_pkg(PKG, staging / "kingdee_ontology")
    for name in PYZ_VENDOR:
        _copy_pkg(_vendor_dir(name), staging / name)

    # 确认没混进二进制扩展——混进去了 zipimport 会在运行时才炸，
    # 而那时人已经在没有网的机器前面了。
    stray = [str(p.relative_to(staging)) for p in staging.rglob("*")
             if p.suffix in {".so", ".pyd", ".dylib"}]
    if stray:
        raise SystemExit(f"单文件包里混进了二进制扩展，zipimport 加载不了：{stray}")

    # 自己写 __main__.py，不用 zipapp 的 main= —— 它生成的是
    #     kingdee_ontology.cli.main()
    # 返回值被丢掉，退出码永远是 0。而这个 CLI 的退出码就是它的用处
    # （`kd-logic can … && 真去执行`），丢了等于把"不可以"当成"可以"。
    (staging / "__main__.py").write_text(
        "import sys\n"
        "from kingdee_ontology.cli import main\n"
        "sys.exit(main())\n", encoding="utf-8")

    target = out / "kd-logic.pyz"
    out.mkdir(parents=True, exist_ok=True)
    zipapp.create_archive(staging, target,
                          interpreter="/usr/bin/env python3",
                          compressed=True)
    target.chmod(0o755)
    shutil.rmtree(staging)
    if not quiet:
        print(f"✓ {target.relative_to(ROOT)}  {target.stat().st_size // 1024} KB")
    return target


def build_wheelhouse(out: Path, extras: str, platform: str | None,
                     py: str | None, quiet: bool = False) -> Path:
    wh = out / "wheelhouse"
    if wh.exists():
        shutil.rmtree(wh)
    wh.mkdir(parents=True)

    subprocess.run([sys.executable, "-m", "build", "--wheel", "-o", str(wh)],
                   cwd=ROOT, check=True,
                   stdout=subprocess.DEVNULL if quiet else None)
    ours = next(wh.glob("kingdee_mcp-*.whl"))

    cmd = [sys.executable, "-m", "pip", "download", "-d", str(wh),
           f"{ours}[{extras}]" if extras else str(ours)]
    if platform or py:
        # 交叉下载必须只要 binary：pip 无法为别的平台构建 sdist。
        cmd += ["--only-binary=:all:"]
        if platform:
            cmd += ["--platform", platform]
        if py:
            cmd += ["--python-version", py]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(
            "下载依赖失败（离线包必须在**有网**的机器上造）：\n"
            + r.stdout[-1500:] + r.stderr[-1500:])

    (wh / "install.sh").write_text(_INSTALL_SH.format(extras=extras or ""),
                                   encoding="utf-8")
    (wh / "install.ps1").write_text(_INSTALL_PS1.format(extras=extras or ""),
                                    encoding="utf-8")
    (wh / "install.sh").chmod(0o755)
    if not quiet:
        n = len(list(wh.glob("*.whl")))
        print(f"✓ {wh.relative_to(ROOT)}  {n} 个 wheel")
    return wh


_INSTALL_SH = """#!/bin/sh
# 离线安装。不联网：--no-index 禁掉 PyPI，只用这个目录里的 wheel。
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
PY=${{PYTHON:-python3}}
"$PY" -m pip install --no-index --find-links "$DIR" "kingdee-mcp[{extras}]"
echo "装好了。试一下： kd-logic can audit 销售订单 --state B:审核中"
"""

_INSTALL_PS1 = """# 离线安装（Windows）。--no-index 禁掉 PyPI，只用本目录的 wheel。
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$py  = if ($env:PYTHON) {{ $env:PYTHON }} else {{ "python" }}
& $py -m pip install --no-index --find-links $dir "kingdee-mcp[{extras}]"
Write-Host "装好了。试一下： kd-logic can audit 销售订单 --state B:审核中"
"""


def write_manifest(out: Path, quiet: bool = False) -> Path:
    files = sorted(p for p in out.rglob("*")
                   if p.is_file() and p.name != "MANIFEST.json")
    man = {
        "package": "kingdee-mcp",
        "version": _version(),
        "built_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "built_on": {"python": sys.version.split()[0], "platform": sys.platform},
        "note": "离线传输常靠 U 盘和邮件附件——装之前先核对 sha256，传坏了要能发现。",
        "files": [{"path": str(p.relative_to(out)), "bytes": p.stat().st_size,
                   "sha256": _sha256(p)} for p in files],
    }
    mp = out / "MANIFEST.json"
    mp.write_text(json.dumps(man, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not quiet:
        print(f"✓ {mp.relative_to(ROOT)}  {len(files)} 个文件")
    return mp


def _version() -> str:
    import tomllib
    return tomllib.loads((ROOT / "pyproject.toml").read_text(
        encoding="utf-8"))["project"]["version"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="build_offline",
        description="造离线安装包：单文件 kd-logic.pyz + 可 --no-index 安装的 wheelhouse")
    ap.add_argument("--only", choices=["pyz", "wheelhouse"],
                    help="只造其中一种；默认两种都造")
    ap.add_argument("--extras", default="",
                    help="wheelhouse 里一并带上的可选依赖，如 sql")
    ap.add_argument("--platform", help="目标平台标签，如 win_amd64、manylinux2014_x86_64")
    ap.add_argument("--python-version", dest="py", help="目标 Python 版本，如 3.11")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args(argv)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if a.only != "wheelhouse":
        build_pyz(out, a.quiet)
    if a.only != "pyz":
        build_wheelhouse(out, a.extras, a.platform, a.py, a.quiet)
    write_manifest(out, a.quiet)
    if not a.quiet:
        print(f"\n离线包在 {out}")
        print("  单文件： python3 kd-logic.pyz can audit 销售订单 --state B:审核中")
        print("  完整装： sh wheelhouse/install.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
