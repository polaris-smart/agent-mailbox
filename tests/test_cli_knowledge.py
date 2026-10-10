"""人侧 `agent-mailbox knowledge index`：写操作必须 --yes ✓ 无确认不调 CLI ✓。"""

from __future__ import annotations

import subprocess

from agent_mailbox import cli


def _scene(tmp_path, monkeypatch, python_cli):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=False)
    from agent_mailbox.workbench_store import WorkbenchStore

    store = WorkbenchStore(home)
    project = store.create_project("P", repo)
    marker = tmp_path / "called.marker"
    script = python_cli(
        f"import pathlib\npathlib.Path({str(marker)!r}).touch()\nprint('indexed 7 files')\n",
        name="codegraph",
    )
    monkeypatch.setenv("AGENT_MAIL_CODEGRAPH_BIN", str(script))
    return home, project, marker


def test_without_yes_it_refuses_and_never_calls_the_cli(tmp_path, monkeypatch, capsys, python_cli):
    home, project, marker = _scene(tmp_path, monkeypatch, python_cli)
    code = cli.main(["knowledge", "index", "--project", project["id"], "--home", str(home)])
    assert code == 2 and not marker.exists()
    assert "CONFIRM_REQUIRED" in capsys.readouterr().err


def test_with_yes_it_runs_the_index(tmp_path, monkeypatch, capsys, python_cli):
    home, project, marker = _scene(tmp_path, monkeypatch, python_cli)
    code = cli.main(
        ["knowledge", "index", "--project", project["id"], "--home", str(home), "--yes"]
    )
    assert code == 0 and marker.exists()
    assert "indexed" in capsys.readouterr().out
