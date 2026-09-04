"""本地凭据文件——跨 harness 的配置入口。

这层的价值就在"不管谁起的进程都读得到"，所以测试重点不是"能不能解析
KEY=VALUE"（那是最简单的部分），而是：

  * 已有的真实环境变量不能被文件悄悄覆盖——文件是兜底，不是真理来源；
  * 候选路径的优先级要对：显式指定 > 当前目录 > 主目录；
  * 找不到任何候选文件时不能报错、也不能是静默的例外——就是没有配置可加载。
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kingdee_ontology.envfile import (  # noqa: E402
    candidate_paths, load_env_file, parse_env_text,
)


class TestParsing:
    def test_basic_key_value(self):
        assert parse_env_text("A=1\nB=2\n") == {"A": "1", "B": "2"}

    def test_blank_and_comment_lines_skipped(self):
        text = "# 这是注释\n\nA=1\n   \n# B=2 也不该生效\nC=3\n"
        assert parse_env_text(text) == {"A": "1", "C": "3"}

    def test_quotes_are_stripped(self):
        text = 'A="has space"\nB=\'single\'\nC=no_quotes\n'
        assert parse_env_text(text) == {"A": "has space", "B": "single", "C": "no_quotes"}

    def test_mismatched_quotes_kept_as_is(self):
        """一头引号一头没有：这不是"引用值"，是字面量的一部分，不该被剥掉半个。"""
        assert parse_env_text('A="oops\n')["A"] == '"oops'

    def test_value_can_contain_equals_sign(self):
        """密码本身带 = 号是完全合理的，用 partition 而不是 split 就是为了这个。"""
        assert parse_env_text("KINGDEE_PASSWORD=ab=cd==\n") == {"KINGDEE_PASSWORD": "ab=cd=="}

    def test_no_equals_sign_is_ignored(self):
        assert parse_env_text("这不是配置行\nA=1\n") == {"A": "1"}

    def test_whitespace_around_key_and_value_trimmed(self):
        assert parse_env_text("  A  =  1  \n") == {"A": "1"}


class TestCandidatePaths:
    def test_order_is_explicit_then_cwd_then_home(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KINGDEE_ENV_FILE", raising=False)
        monkeypatch.chdir(tmp_path)
        cands = candidate_paths(explicit="/x/explicit.env")
        assert cands[0] == Path("/x/explicit.env")
        assert cands[1] == tmp_path / ".env"
        assert cands[2] == Path.home() / ".kingdee-mcp.env"

    def test_env_var_used_when_no_explicit_arg(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KINGDEE_ENV_FILE", str(tmp_path / "custom.env"))
        assert candidate_paths()[0] == tmp_path / "custom.env"

    def test_no_explicit_source_skips_that_candidate(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KINGDEE_ENV_FILE", raising=False)
        monkeypatch.chdir(tmp_path)
        cands = candidate_paths()
        assert cands == [tmp_path / ".env", Path.home() / ".kingdee-mcp.env"]


class TestLoadEnvFile:
    def test_loads_first_existing_candidate(self, tmp_path, monkeypatch):
        f = tmp_path / "creds.env"
        f.write_text("KINGDEE_ACCT_ID=acct-123\n", encoding="utf-8")
        monkeypatch.delenv("KINGDEE_ACCT_ID", raising=False)
        loaded = load_env_file(explicit=str(f))
        assert loaded == f
        assert os.environ["KINGDEE_ACCT_ID"] == "acct-123"

    def test_real_env_var_wins_over_file(self, tmp_path, monkeypatch):
        """文件是兜底。已经通过 MCP 客户端自己的方式配置好的调用方，
        不该被这个文件覆盖——那会让人以为改了 .env 就生效，实际上没有。"""
        f = tmp_path / "creds.env"
        f.write_text("KINGDEE_PASSWORD=from-file\n", encoding="utf-8")
        monkeypatch.setenv("KINGDEE_PASSWORD", "from-real-env")
        load_env_file(explicit=str(f))
        assert os.environ["KINGDEE_PASSWORD"] == "from-real-env"

    def test_override_flag_lets_file_win(self, tmp_path, monkeypatch):
        f = tmp_path / "creds.env"
        f.write_text("KINGDEE_PASSWORD=from-file\n", encoding="utf-8")
        monkeypatch.setenv("KINGDEE_PASSWORD", "from-real-env")
        load_env_file(explicit=str(f), override=True)
        assert os.environ["KINGDEE_PASSWORD"] == "from-file"

    def test_missing_file_returns_none_without_raising(self, tmp_path):
        assert load_env_file(explicit=str(tmp_path / "does-not-exist.env")) is None

    def test_falls_through_to_next_candidate(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KINGDEE_ENV_FILE", raising=False)
        monkeypatch.chdir(tmp_path)
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        (home / ".kingdee-mcp.env").write_text("KINGDEE_USERNAME=from-home\n", encoding="utf-8")
        monkeypatch.delenv("KINGDEE_USERNAME", raising=False)
        # cwd 里没有 .env，应该落到主目录那份
        loaded = load_env_file()
        assert loaded == home / ".kingdee-mcp.env"
        assert os.environ["KINGDEE_USERNAME"] == "from-home"

    def test_empty_file_returns_its_path_not_none(self, tmp_path):
        """文件存在但没内容,仍算"找到了"——调用方能用返回值区分
        "没配置文件"和"配置文件是空的",两者该有不同的排错提示。"""
        f = tmp_path / "empty.env"
        f.write_text("", encoding="utf-8")
        assert load_env_file(explicit=str(f)) == f
