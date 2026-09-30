"""The product entry cannot fall back to v0.7 and setup keeps a single owner."""

import importlib.util
import json

import pytest

from agent_mailbox import __version__, cli
from agent_mailbox.workbench_lock import WorkbenchLock


@pytest.mark.parametrize("module", ["server", "store", "wake", "watch", "installer", "web"])
def test_legacy_runtime_is_not_in_independent_distribution(module):
    assert importlib.util.find_spec("agent_mailbox." + module) is None


def test_default_entry_only_invokes_workbench(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr("agent_mailbox.workbench.main", lambda args: calls.append(args))
    cli.main(["--home", str(tmp_path)])
    assert calls == [["--home", str(tmp_path)]]
    cli.main(["--version"])
    assert capsys.readouterr().out.strip() == __version__
    cli.main(["workbench", "--home", str(tmp_path)])
    assert calls[-1] == ["--home", str(tmp_path)]


def test_prepare_is_explicit_private_home_setup_with_readiness(monkeypatch, tmp_path, capsys):
    paths = []
    monkeypatch.setattr(
        "agent_mailbox.runtime_bridge.install_runtime.install_runtime", paths.append
    )
    monkeypatch.setattr(
        "agent_mailbox.workbench_runtime.runtime_status", lambda home: {"installed": True}
    )
    cli.main(["prepare", "--home", str(tmp_path / "home")])
    assert paths == [tmp_path / "home/workbench/runtime/deps"]
    assert json.loads(capsys.readouterr().out)["installed"]


def test_prepare_respects_owner_and_returns_actionable_failure(monkeypatch, tmp_path, capsys):
    (tmp_path / "workbench").mkdir()
    lock = WorkbenchLock(tmp_path)
    monkeypatch.setattr(
        "agent_mailbox.runtime_bridge.install_runtime.install_runtime",
        lambda _: pytest.fail("Setup ran during live owner"),
    )
    try:
        with pytest.raises(SystemExit) as failed:
            cli.main(["prepare", "--home", str(tmp_path)])
        assert failed.value.code == 1
        assert json.loads(capsys.readouterr().err)["error"]["code"] == "ALREADY_RUNNING"
    finally:
        lock.close()
