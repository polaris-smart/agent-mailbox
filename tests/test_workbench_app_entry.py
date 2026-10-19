"""The frozen App's CLI entry must preserve command failure status."""

import pytest

from agent_mailbox.workbench_app import main
from agent_mailbox.workbench_store import WorkbenchStore


def test_packaged_cli_reports_paused_wake_toggle_failure(tmp_path):
    home = tmp_path / "home"
    store = WorkbenchStore(home)
    store.pause_updates(True)

    with pytest.raises(SystemExit) as result:
        main(["wake", "--disable", "--home", str(home)])

    assert result.value.code == 2
    assert not (home / "wake/wake-state.json").exists()


def test_packaged_cli_reports_paused_wake_round_as_deferred(tmp_path, capsys):
    home = tmp_path / "home"
    store = WorkbenchStore(home)
    store.pause_updates(True)

    with pytest.raises(SystemExit) as result:
        main(["wake", "--once", "--home", str(home)])

    assert result.value.code == 2
    assert "更新准备期间" in capsys.readouterr().out
    assert not (home / "wake/wake-state.json").exists()
