"""Desktop-shell gating for the native macOS workbench app."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent_mailbox import workbench
from agent_mailbox.workbench import _desktop_wanted


@pytest.fixture
def args():
    return SimpleNamespace(no_browser=False)


def _darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("AGENT_MAILBOX_DESKTOP", raising=False)
    monkeypatch.delenv("AGENT_MAILBOX_NO_DESKTOP", raising=False)


def test_non_mac_and_unfrozen_cli_keep_browser_behaviour(args, monkeypatch):
    _darwin(monkeypatch)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setitem(sys.modules, "agent_mailbox.workbench_desktop", SimpleNamespace())
    assert _desktop_wanted(args) is True
    monkeypatch.setattr(sys, "platform", "linux")
    assert _desktop_wanted(args) is False
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert _desktop_wanted(args) is False
    args.no_browser = True
    monkeypatch.setenv("AGENT_MAILBOX_DESKTOP", "1")
    assert _desktop_wanted(args) is False


def test_env_overrides_gate(args, monkeypatch):
    _darwin(monkeypatch)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setenv("AGENT_MAILBOX_NO_DESKTOP", "1")
    assert _desktop_wanted(args) is False
    monkeypatch.delenv("AGENT_MAILBOX_NO_DESKTOP", raising=False)
    monkeypatch.setenv("AGENT_MAILBOX_DESKTOP", "1")
    assert _desktop_wanted(args) is True


def test_frozen_mac_without_pyobjc_falls_back(args, monkeypatch):
    _darwin(monkeypatch)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    real_import_module = workbench.importlib.import_module

    def blocked(name, package=None):
        if "workbench_desktop" in str(name):
            raise ImportError("no pyobjc")
        return real_import_module(name, package)

    monkeypatch.setattr(workbench.importlib, "import_module", blocked)
    assert _desktop_wanted(args) is False


@pytest.mark.parametrize(
    "stale", ['{"endpoint":"http://127.0.0.1:1","token":"stale"}', "broken-json"]
)
def test_stale_instance_file_is_replaced_after_acquiring_owner_lock(tmp_path, stale):
    """An instance file is a locator, not proof that its previous owner is alive."""
    from agent_mailbox.workbench_store import WorkbenchStore

    store = WorkbenchStore(tmp_path)
    path = tmp_path / "workbench/instance.json"
    path.write_text(stale)
    engine = SimpleNamespace(close=lambda: None)
    server = workbench.WorkbenchHTTP(store, engine=engine, token="fixture")
    try:
        saved = json.loads(path.read_text())
        assert saved == {"endpoint": server.endpoint, "token": "fixture"}
        assert server.server_port != 1
    finally:
        server.close()
    assert not path.exists()


SCRIPT = (
    "import sys, types, urllib.request\n"
    "def fake_desktop(url, *, service_stopped):\n"
    "    root = url.split('#', 1)[0] + '/'\n"
    "    with urllib.request.urlopen(root, timeout=5) as response:\n"
    "        assert response.status == 200\n"
    "    print('DESKTOP_OK', flush=True)\n"
    "fake = types.ModuleType('agent_mailbox.workbench_desktop')\n"
    "fake.run_desktop = fake_desktop\n"
    "sys.modules['agent_mailbox.workbench_desktop'] = fake\n"
    "from agent_mailbox import workbench\n"
    "workbench.WorkbenchEngine = lambda store: types.SimpleNamespace(start=lambda: None, close=lambda: None)\n"
    "workbench.main(['--home', sys.argv[1]])\n"
)


@pytest.mark.skipif(sys.platform != "darwin", reason="desktop shell is macOS-only")
def test_desktop_main_serves_while_ui_blocks_and_exits_cleanly(tmp_path):
    root = tmp_path / "desktop-home"
    env = {**os.environ, "AGENT_MAILBOX_DESKTOP": "1"}
    env.pop("AGENT_MAILBOX_NO_DESKTOP", None)
    process = subprocess.Popen(
        [sys.executable, "-c", SCRIPT, str(root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        url = process.stdout.readline().strip()
        assert url.startswith("http://127.0.0.1:") and "/#token=" in url
        assert process.stdout.readline().strip() == "DESKTOP_OK"
        process.terminate()
        assert process.wait(timeout=8) == 0
        assert not (root / "workbench/instance.json").exists()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=8)


@pytest.mark.skipif(sys.platform != "darwin", reason="desktop shell is macOS-only")
def test_relaunch_focuses_existing_desktop_window(tmp_path, monkeypatch):
    root = tmp_path / "focus-home"
    from agent_mailbox.workbench_lock import WorkbenchLock

    root.joinpath("workbench").mkdir(parents=True)
    lock = WorkbenchLock(root)  # simulate the already-running native app
    opened: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        opened.append(cmd)
        return SimpleNamespace(returncode=0)

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    root.joinpath("workbench/instance.json").write_text(
        json.dumps({"endpoint": "http://127.0.0.1:1", "token": "x"})
    )
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(workbench.subprocess, "run", fake_run)
    monkeypatch.setattr(workbench.urllib.request, "urlopen", lambda *a, **k: _Response())
    monkeypatch.delenv("AGENT_MAILBOX_NO_DESKTOP", raising=False)
    monkeypatch.setenv("AGENT_MAILBOX_DESKTOP", "1")
    try:
        workbench.main(["--home", str(root)])
    finally:
        lock.close()
    assert opened == [["open", "-b", workbench.DESKTOP_BUNDLE_ID]]


@pytest.mark.skipif(sys.platform != "darwin", reason="desktop shell is macOS-only")
def test_quit_endpoint_releases_desktop_home_for_restart(tmp_path):
    script = SCRIPT.replace(
        "    print('DESKTOP_OK', flush=True)\n",
        "    token = url.split('#token=', 1)[1]\n"
        "    request = urllib.request.Request(root + 'api/workbench/application/quit', "
        "data=b'{}', headers={'Authorization': 'Bearer ' + token, "
        "'Content-Type': 'application/json'})\n"
        "    with urllib.request.urlopen(request, timeout=5) as response:\n"
        "        assert response.status == 200\n"
        "    assert service_stopped.wait(5), 'quit did not stop HTTP loop'\n",
    )
    env = {**os.environ, "AGENT_MAILBOX_DESKTOP": "1"}
    env.pop("AGENT_MAILBOX_NO_DESKTOP", None)
    root = tmp_path / "restart-home"
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-c", script, str(root)],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert not (root / "workbench/instance.json").exists()
