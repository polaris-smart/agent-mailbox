"""aoci 只读诊断包装：可离线测 ✓ 写操作被拒 ✓ 文本输出不假装结构化 ✓。"""

from __future__ import annotations

from agent_mailbox import workbench_aoci as a


def _fake_aoci(python_cli, body: str = "AOCI Doctor —— 环境与接入诊断\n[✓] 仓库根定位\n"):
    return python_cli(f"print({body!r})", name="aoci")


def test_doctor_returns_text_and_marks_unstructured(tmp_path, monkeypatch, python_cli):
    script = _fake_aoci(python_cli)
    monkeypatch.setenv("AGENT_MAIL_AOCI_BIN", str(script))
    out = a.aoci_doctor(_repo(tmp_path))
    assert out["ok"] is True
    assert out["structured"] is False, "aoci 实测是文本输出 ⇒ 不得假装结构化 ✗"
    assert out["summary"].startswith("AOCI Doctor")
    assert out["provenance"]["provider"] == "aoci" and "staleness" in out["provenance"]


def test_status_and_check_share_the_same_shape(tmp_path, monkeypatch, python_cli):
    script = _fake_aoci(python_cli, "条目 0 · 基线 .aoci/baseline.json\n")
    monkeypatch.setenv("AGENT_MAIL_AOCI_BIN", str(script))
    for fn in (a.aoci_status, a.aoci_check):
        out = fn(_repo(tmp_path))
        assert out["ok"] is True and out["structured"] is False and out["summary"]


def test_write_subcommands_are_refused(tmp_path):
    for write_cmd in ("index", "onboard", "baseline", "cognition"):
        out = a._guard(write_cmd, tmp_path, None)
        assert out["ok"] is False and out["error"]["code"] == "WRITE_NOT_INTEGRATED", write_cmd
        assert "手工" in out["error"]["message"]


def test_missing_aoci_degrades(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MAIL_AOCI_BIN", str(tmp_path / "absent"))
    out = a.aoci_doctor(_repo(tmp_path))
    assert out["ok"] is False and out["error"]["code"] == "CLI_MISSING"
    assert a.aoci_available() is False


def test_no_write_subcommand_is_reachable_through_public_api():
    """公开 API 只有 doctor/status/check 三个 ✓（写操作不可达 ✓）。"""
    public = [name for name in dir(a) if name.startswith("aoci_") and name != "aoci_available"]
    assert sorted(public) == ["aoci_check", "aoci_doctor", "aoci_status"], public


def test_non_repo_path_is_refused_with_structured_error(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    out = a.aoci_doctor(plain)
    assert out["ok"] is False and out["error"]["code"] == "BAD_PROJECT_PATH"
    assert ".git" in out["error"]["message"]


def test_repo_path_passes_the_guard(tmp_path, monkeypatch, python_cli):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=False)
    monkeypatch.setenv("AGENT_MAIL_AOCI_BIN", str(_fake_aoci(python_cli)))
    out = a.aoci_status(repo)
    assert out["ok"] is True and out["structured"] is False


def _repo(base):
    """测试用**真 git 工作树**（路径约束已生效 ✓ 非仓库路径会被拒 ✗）。"""
    import subprocess

    d = base / "repo"
    d.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=d, check=False)
    return d
