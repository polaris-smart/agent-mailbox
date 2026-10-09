"""人侧 `agent-mailbox knowledge index`：写操作必须 --yes ✓ 无确认不调 CLI ✓。"""

from __future__ import annotations

import stat
import subprocess
import sys

from agent_mailbox import cli


def _scene(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=False)
    from agent_mailbox.workbench_store import WorkbenchStore

    store = WorkbenchStore(home)
    project = store.create_project("P", repo)
    marker = tmp_path / "called.marker"
    script = tmp_path / "codegraph"
    script.write_text(
        f"#!{sys.executable}\nimport pathlib\npathlib.Path({str(marker)!r}).touch()\nprint('indexed 7 files')\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("AGENT_MAIL_CODEGRAPH_BIN", str(script))
    return home, project, marker


def test_without_yes_it_refuses_and_never_calls_the_cli(tmp_path, monkeypatch, capsys):
    home, project, marker = _scene(tmp_path, monkeypatch)
    code = cli.main(["knowledge", "index", "--project", project["id"], "--home", str(home)])
    assert code == 2 and not marker.exists()
    assert "CONFIRM_REQUIRED" in capsys.readouterr().err


def test_with_yes_it_runs_the_index(tmp_path, monkeypatch, capsys):
    home, project, marker = _scene(tmp_path, monkeypatch)
    code = cli.main(
        ["knowledge", "index", "--project", project["id"], "--home", str(home), "--yes"]
    )
    assert code == 0 and marker.exists()
    assert "indexed" in capsys.readouterr().out
