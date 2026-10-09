"""graft 只读包装：可离线测 ✓ 缺工具降级 ✓ 不炸 ✗。"""

from __future__ import annotations

import stat
import sys

import pytest

from agent_mailbox import workbench_graft as g


def _fake_graft(tmp_path, payload: dict):
    script = tmp_path / "graft"
    script.write_text(
        f"#!{sys.executable}\nimport json,sys\nprint(json.dumps({payload!r}))\n", encoding="utf-8"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_ask_normalizes_and_carries_provenance(tmp_path, monkeypatch):
    script = _fake_graft(tmp_path, {"references": [{"file": "a.py", "line": 3}]})
    monkeypatch.setenv("AGENT_MAIL_GRAFT_BIN", str(script))
    out = g.graft_ask(_repo(tmp_path), "谁调用 _dispatch_guard")
    assert out["ok"] is True
    assert out["references"] == [{"file": "a.py", "line": 3}]
    assert out["args"][:2] == ["ask", "谁调用 _dispatch_guard"]
    assert out["provenance"]["provider"] == "graft" and out["provenance"]["mode"] == "cli-read-only"
    assert "staleness" in out["provenance"]


def test_callers_passes_symbol_through(tmp_path, monkeypatch):
    script = _fake_graft(tmp_path, {"files": ["x.py"]})
    monkeypatch.setenv("AGENT_MAIL_GRAFT_BIN", str(script))
    out = g.graft_callers(_repo(tmp_path), "create_task")
    assert (
        out["ok"] is True
        and out["references"] == ["x.py"]
        and out["args"] == ["callers", "create_task"]
    )


def test_missing_graft_degrades_with_install_hint(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MAIL_GRAFT_BIN", str(tmp_path / "absent"))
    out = g.graft_ask(_repo(tmp_path), "任意问题")
    assert out["ok"] is False and out["error"]["code"] == "CLI_MISSING"
    assert "AGENT_MAIL_GRAFT_BIN" in out["error"]["message"]
    assert g.graft_available() is False


def test_empty_arguments_are_rejected_not_sent(tmp_path):
    assert g.graft_ask(_repo(tmp_path), "   ")["error"]["code"] == "BAD_ARGUMENT"
    assert g.graft_callers(_repo(tmp_path), "")["error"]["code"] == "BAD_ARGUMENT"


def test_write_operations_are_not_exposed():
    """只读边界：包装层**不得**暴露 build/写操作（规范 §二 ✓）。"""
    import inspect

    source = inspect.getsource(g)
    assert '"build"' not in source and "'build'" not in source


def test_non_repo_path_is_refused_before_calling_the_cli(tmp_path, monkeypatch):
    """路径约束：非仓库路径**不调 CLI**，直接拒绝。"""
    from agent_mailbox import workbench_cli_query as cq

    called = tmp_path / "called.marker"
    script = tmp_path / "graft"
    script.write_text(
        f"#!{sys.executable}\nimport pathlib\npathlib.Path({str(called)!r}).touch()\nprint('{{}}')\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("AGENT_MAIL_GRAFT_BIN", str(script))
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(cq.ProjectPathError):
        g.graft_ask(plain, "任意问题")
    assert not called.exists()


def test_repo_path_passes_the_guard(tmp_path, monkeypatch):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=False)
    script = _fake_graft(tmp_path, {"references": ["ok.py"]})
    monkeypatch.setenv("AGENT_MAIL_GRAFT_BIN", str(script))
    out = g.graft_callers(repo, "symbol")
    assert out["ok"] is True and out["references"] == ["ok.py"]


def _repo(base):
    """测试用**真 git 工作树**（路径约束已生效 ✓ 非仓库路径会被拒 ✗）。"""
    import subprocess

    d = base / "repo"
    d.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=d, check=False)
    return d


def test_callers_also_enforces_the_path_guard(tmp_path, monkeypatch):
    """评审点名过：callers 曾漏掉路径守卫 ✗ ⇒ 单独钉一条 ✓。"""
    from agent_mailbox import workbench_cli_query as cq

    monkeypatch.setenv("AGENT_MAIL_GRAFT_BIN", str(tmp_path / "graft"))
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(cq.ProjectPathError):
        g.graft_callers(plain, "symbol")
