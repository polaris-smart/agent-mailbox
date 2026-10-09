"""cli_query 通用底座：只读、可测、**永不因缺工具/超时/坏输出而炸** ✓。"""

from __future__ import annotations

import os
import stat
import sys

import pytest

from agent_mailbox import workbench_cli_query as cq


def _fake_cli(tmp_path, body: str, name: str = "fake"):
    script = tmp_path / name
    script.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_env_override_wins(tmp_path, monkeypatch):
    script = _fake_cli(tmp_path, "print('{}')")
    monkeypatch.setenv("AGENT_MAIL_GRAFT_BIN", str(script))
    assert cq.resolve_binary("graft") == str(script)


def test_env_override_pointing_nowhere_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MAIL_AOCI_BIN", str(tmp_path / "nope"))
    assert cq.resolve_binary("aoci") is None


def test_unknown_provider_is_a_structured_error(tmp_path):
    out = cq.run("nope", [], cwd=tmp_path)
    assert out["ok"] is False and out["error"]["code"] == "BAD_PROVIDER"


def test_missing_binary_degrades_not_crashes(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MAIL_GRAFT_BIN", str(tmp_path / "absent"))
    out = cq.run("graft", ["ask", "x"], cwd=tmp_path)
    assert out["ok"] is False and out["error"]["code"] == "CLI_MISSING"


def test_successful_query_carries_provenance(tmp_path, monkeypatch):
    script = _fake_cli(tmp_path, "import json;print(json.dumps({'files':['a.py']}))")
    monkeypatch.setenv("AGENT_MAIL_GRAFT_BIN", str(script))
    out = cq.run("graft", ["ask", "who calls X"], cwd=tmp_path)
    assert out["ok"] is True and out["data"] == {"files": ["a.py"]}
    prov = out["provenance"]
    assert prov["provider"] == "graft" and prov["binary"] == str(script)
    assert prov["mode"] == "cli-read-only" and "duration_ms" in prov
    assert "staleness" in prov and prov["staleness"]["index_path"] is None


def test_bad_output_and_empty_output_are_coded(tmp_path, monkeypatch):
    bad = _fake_cli(tmp_path, "print('not json')", name="bad")
    monkeypatch.setenv("AGENT_MAIL_CODE_FAKE", "1")
    monkeypatch.setitem(
        cq.PROVIDERS,
        "fakebad",
        {"env": "FAKEBAD_BIN", "names": ("fakebad",), "version_args": ("--version",)},
    )
    monkeypatch.setenv("FAKEBAD_BIN", str(bad))
    out = cq.run("fakebad", [], cwd=tmp_path)
    assert out["ok"] is False and out["error"]["code"] == "CLI_BAD_OUTPUT"

    empty = _fake_cli(tmp_path, "pass", name="empty")
    monkeypatch.setitem(
        cq.PROVIDERS,
        "fakeempty",
        {"env": "FAKEEMPTY_BIN", "names": ("fakeempty",), "version_args": ("--version",)},
    )
    monkeypatch.setenv("FAKEEMPTY_BIN", str(empty))
    out2 = cq.run("fakeempty", [], cwd=tmp_path)
    assert out2["ok"] is False and out2["error"]["code"] == "CLI_FAILED"


def test_timeout_is_coded(tmp_path, monkeypatch):
    slow = _fake_cli(tmp_path, "import time;time.sleep(3);print('{}')", name="slow")
    monkeypatch.setitem(
        cq.PROVIDERS,
        "fakeslow",
        {"env": "FAKESLOW_BIN", "names": ("fakeslow",), "version_args": ("--version",)},
    )
    monkeypatch.setenv("FAKESLOW_BIN", str(slow))
    out = cq.run("fakeslow", [], cwd=tmp_path, timeout=0.5)
    assert out["ok"] is False and out["error"]["code"] == "CLI_TIMEOUT"


def test_staleness_gate_flags_an_old_index(tmp_path):
    import subprocess
    import time

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=False)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
        check=False,
    )
    index = tmp_path / "baseline.json"
    index.write_text("{}")
    old = time.time() - 3 * 86400
    os.utime(index, (old, old))
    facts = cq.index_staleness(tmp_path, index)
    assert facts["index_mtime"] and facts["head_time"]
    assert facts["stale_days"] >= 2.5, facts


def _repo(tmp_path):
    import subprocess

    d = tmp_path / "proj"
    d.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=d, check=False)
    return d


def test_project_root_accepts_a_git_worktree(tmp_path):
    root = _repo(tmp_path)
    assert cq.resolve_project_root(root) == root.resolve()


@pytest.mark.parametrize("bad", ["/", "~", "/tmp", "/nonexistent-dir-xyz"])
def test_project_root_refuses_dangerous_paths(bad):
    with pytest.raises(cq.ProjectPathError):
        cq.resolve_project_root(bad)


def test_project_root_refuses_non_git_directory(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(cq.ProjectPathError) as caught:
        cq.resolve_project_root(plain)
    assert ".git" in str(caught.value)


def test_project_root_normalizes_dot_dot(tmp_path):
    root = _repo(tmp_path)
    (root / "sub").mkdir(exist_ok=True)
    assert cq.resolve_project_root(root / "sub" / "..") == root.resolve()
