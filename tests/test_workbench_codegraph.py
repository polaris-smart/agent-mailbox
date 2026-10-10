"""codegraph 重索引：写操作必须人显式确认 ✓ 未确认不得调 CLI ✓ 与只读分层 ✓。"""

from __future__ import annotations

import subprocess

from agent_mailbox import workbench_codegraph as cg


def _repo(base):
    d = base / "repo"
    d.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=d, check=False)
    return d


def _fake_codegraph(python_cli, marker: str, body: str = "indexed 42 files"):
    return python_cli(
        f"import pathlib\npathlib.Path({marker!r}).touch()\nprint({body!r})\n",
        name="codegraph",
    )


def test_unconfirmed_write_never_calls_the_cli(tmp_path, monkeypatch, python_cli):
    marker = tmp_path / "called.marker"
    monkeypatch.setenv("AGENT_MAIL_CODEGRAPH_BIN", str(_fake_codegraph(python_cli, str(marker))))
    out = cg.reindex(_repo(tmp_path), confirmed=False)
    assert out["ok"] is False and out["error"]["code"] == "CONFIRM_REQUIRED"
    assert not marker.exists(), "未确认的写操作不得真的去调 CLI ✗"


def test_confirmed_write_runs_and_reports(tmp_path, monkeypatch, python_cli):
    marker = tmp_path / "called.marker"
    monkeypatch.setenv("AGENT_MAIL_CODEGRAPH_BIN", str(_fake_codegraph(python_cli, str(marker))))
    out = cg.reindex(_repo(tmp_path), confirmed=True)
    assert out["ok"] is True and marker.exists()
    assert "indexed" in (out["text"] or "")
    assert out["provenance"]["provider"] == "codegraph"


def test_non_repo_path_is_refused_even_when_confirmed(tmp_path, monkeypatch, python_cli):
    marker = tmp_path / "called.marker"
    monkeypatch.setenv("AGENT_MAIL_CODEGRAPH_BIN", str(_fake_codegraph(python_cli, str(marker))))
    plain = tmp_path / "plain"
    plain.mkdir()
    out = cg.reindex(plain, confirmed=True)
    assert out["ok"] is False and out["error"]["code"] == "BAD_PROJECT_PATH"
    assert not marker.exists()


def test_missing_tool_degrades(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MAIL_CODEGRAPH_BIN", str(tmp_path / "absent"))
    out = cg.reindex(_repo(tmp_path), confirmed=True)
    assert out["ok"] is False and out["error"]["code"] == "CLI_MISSING"
    assert cg.codegraph_available() is False


def test_index_is_not_part_of_the_read_only_whitelist():
    """分层断言：写操作**不在**邮箱面只读白名单里 ✓（规范 §七 ✓）。"""
    from agent_mailbox import workbench_mail_sessions as ms

    # 断言**真实集合**（此前断言一个全仓不存在的名字 ⇒ 恒真 ✗ 评审 #7）
    assert set(ms._READ_ONLY_KNOWLEDGE) == {
        "graft_ask",
        "graft_callers",
        "aoci_doctor",
        "aoci_status",
        "aoci_check",
    }, ms._READ_ONLY_KNOWLEDGE
    assert all(not name.startswith(("index", "build", "onboard")) for name in ms.TOOLS)
