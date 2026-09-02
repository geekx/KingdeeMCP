"""
pytest 配置：集成 + 端到端测试需要真实金蝶服务器
没有配置环境变量时静默跳过
"""

import os
import sys
from pathlib import Path

import pytest

# 让测试无需手动设置 PYTHONPATH 即可导入 src/ 下的包与仓库根的 base/ wikiskill/
_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT, _ROOT / "src", _ROOT / "tools" / "ontology"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# tests/test_crud_all_objects.py 默认指向一台开发机的 Windows 路径；
# 仓库内自带同名文件，未显式指定时用它，避免在别的机器上收集期就报错。
os.environ.setdefault("KINGDEE_API_INVENTORY_JSON", str(
    _ROOT / "expert" / "kingdee-mcp-dev" / "skills" / "kingdee-mcp-dev"
    / "references" / "api-inventory-raw.json"))

HAS_KINGDEE_CONFIG = bool(
    os.getenv("KINGDEE_SERVER_URL") and
    os.getenv("KINGDEE_ACCT_ID") and
    os.getenv("KINGDEE_USERNAME") and
    os.getenv("KINGDEE_APP_ID") and
    os.getenv("KINGDEE_APP_SEC")
)


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: 真实金蝶环境的端到端测试（需 KINGDEE_* 环境变量）")


def pytest_collection_modifyitems(config, items):
    """test_integration.py 与 tests/e2e/ 在无真实配置时跳过"""
    skip_reason = "需要真实金蝶服务器环境变量（KINGDEE_SERVER_URL 等），当前未配置"
    needs_real = ("test_integration", "tests/e2e", "tests\\e2e")
    for item in items:
        if any(key in item.nodeid for key in needs_real):
            if not HAS_KINGDEE_CONFIG:
                item.add_marker(pytest.mark.skip(reason=skip_reason))