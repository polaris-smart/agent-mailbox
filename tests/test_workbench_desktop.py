"""Desktop-shell gating for the native macOS workbench app."""

from __future__ import annotations

import inspect
import json
import os
import plistlib
import signal
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


@pytest.mark.skipif(sys.platform != "darwin", reason="WKWebView is macOS-only")
@pytest.mark.parametrize("approved", [True, False])
def test_native_shell_returns_javascript_confirm_choice(monkeypatch, approved):
    """An App confirmation must reach WebKit; Cancel must not authorize a DELETE."""
    from agent_mailbox import workbench_desktop as desktop

    assert desktop._DesktopDelegate.instancesRespondToSelector_(
        b"webView:runJavaScriptConfirmPanelWithMessage:initiatedByFrame:completionHandler:"
    )

    class Alert:
        def __init__(self):
            self.buttons = []

        @classmethod
        def alloc(cls):
            cls.latest = cls()
            return cls.latest

        def init(self):
            return self

        def setMessageText_(self, value):
            self.message = value

        def setInformativeText_(self, value):
            self.detail = value

        def addButtonWithTitle_(self, value):
            self.buttons.append(value)

        def beginSheetModalForWindow_completionHandler_(self, window, callback):
            assert window is delegate._window
            assert len(self.buttons) == 2
            callback(desktop.NSAlertFirstButtonReturn if approved else 0)

    monkeypatch.setattr(desktop, "NSAlert", Alert, raising=False)
    delegate = desktop._DesktopDelegate.alloc().initWithURL_("http://127.0.0.1:1")
    delegate._window = object()
    frame = SimpleNamespace(
        request=lambda: SimpleNamespace(URL=lambda: SimpleNamespace(host=lambda: "127.0.0.1"))
    )
    choices = []
    delegate.webView_runJavaScriptConfirmPanelWithMessage_initiatedByFrame_completionHandler_(
        None, "Remove this member?", frame, choices.append
    )
    assert choices == [approved]
    assert Alert.latest.message == "127.0.0.1"
    assert Alert.latest.detail == "Remove this member?"
    assert "setUIDelegate_(self)" in inspect.getsource(desktop._DesktopDelegate._ensure_window)


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
    "workbench.WorkbenchEngine = lambda store: types.SimpleNamespace(start=lambda: None, close=lambda: None, active_worker_count=lambda: 0)\n"
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
def test_native_launch_attaches_to_existing_headless_service(tmp_path, monkeypatch):
    """A background owner has no native window for ``open -b`` to focus."""
    root = tmp_path / "headless-home"
    from agent_mailbox.workbench_lock import WorkbenchLock

    root.joinpath("workbench").mkdir(parents=True)
    lock = WorkbenchLock(root)
    root.joinpath("workbench/instance.json").write_text(
        json.dumps(
            {
                "endpoint": "http://127.0.0.1:1",
                "token": "x",
                "ui_mode": "headless",
            }
        )
    )
    calls = []

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(workbench.urllib.request, "urlopen", lambda *a, **k: _Response())
    monkeypatch.setattr(workbench.subprocess, "run", lambda *a, **k: calls.append("open"))
    monkeypatch.setitem(
        sys.modules,
        "agent_mailbox.workbench_desktop",
        SimpleNamespace(
            run_desktop=lambda url, **kw: calls.append((url, kw)),
        ),
    )
    monkeypatch.setenv("AGENT_MAILBOX_DESKTOP", "1")
    monkeypatch.delenv("AGENT_MAILBOX_NO_DESKTOP", raising=False)
    try:
        workbench.main(["--home", str(root)])
    finally:
        lock.close()
    assert calls == [("http://127.0.0.1:1/#token=x", {})]


def test_owner_instance_records_headless_launch_mode(tmp_path):
    from agent_mailbox.workbench_store import WorkbenchStore

    store = WorkbenchStore(tmp_path)
    engine = SimpleNamespace(close=lambda: None)
    server = workbench.WorkbenchHTTP(store, engine=engine, token="fixture", ui_mode="headless")
    try:
        assert json.loads(server.instance_path.read_text())["ui_mode"] == "headless"
    finally:
        server.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="desktop shell is macOS-only")
def test_native_shell_attaches_to_live_headless_owner_without_stopping_it(tmp_path, monkeypatch):
    """The ordinary App launch can use a resident service's existing URL."""
    root = tmp_path / "attached-home"
    script = (
        "import sys, types\n"
        "from agent_mailbox import workbench\n"
        "workbench.WorkbenchEngine = lambda store: types.SimpleNamespace("
        "start=lambda: None, close=lambda: None, active_worker_count=lambda: 0)\n"
        "workbench.main(['--home', sys.argv[1], '--no-browser'])\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        url = process.stdout.readline().strip()
        assert url.startswith("http://127.0.0.1:") and "/#token=" in url
        assert json.loads((root / "workbench/instance.json").read_text())["ui_mode"] == "headless"
        opened = []
        monkeypatch.setattr(workbench.subprocess, "run", lambda *a, **k: opened.append("open"))
        monkeypatch.setitem(
            sys.modules,
            "agent_mailbox.workbench_desktop",
            SimpleNamespace(
                run_desktop=lambda target, **kw: opened.append((target, kw)),
            ),
        )
        monkeypatch.setenv("AGENT_MAILBOX_DESKTOP", "1")
        monkeypatch.delenv("AGENT_MAILBOX_NO_DESKTOP", raising=False)
        workbench.main(["--home", str(root)])
        assert opened == [(url, {})]
        assert process.poll() is None  # Closing the attached shell does not stop mail delivery.
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=8)
    assert not (root / "workbench/instance.json").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="desktop shell is macOS-only")
def test_relaunch_of_side_by_side_app_focuses_its_own_bundle(tmp_path, monkeypatch):
    root = tmp_path / "focus-home"
    from agent_mailbox.workbench_lock import WorkbenchLock

    root.joinpath("workbench").mkdir(parents=True)
    lock = WorkbenchLock(root)
    probe = tmp_path / "Agent Mailbox Probe.app" / "Contents"
    (probe / "MacOS").mkdir(parents=True)
    with (probe / "Info.plist").open("wb") as file:
        plistlib.dump({"CFBundleIdentifier": "com.polaris-smart.agent-mailbox.probe"}, file)
    root.joinpath("workbench/instance.json").write_text(
        json.dumps({"endpoint": "http://127.0.0.1:1", "token": "x"})
    )
    opened = []

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(probe / "MacOS" / "Agent Mailbox Probe"))
    monkeypatch.setattr(
        workbench.subprocess,
        "run",
        lambda cmd, **_: opened.append(cmd) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(workbench.urllib.request, "urlopen", lambda *a, **k: _Response())
    monkeypatch.setenv("AGENT_MAILBOX_DESKTOP", "1")
    try:
        workbench.main(["--home", str(root)])
    finally:
        lock.close()
    assert opened == [["open", "-b", "com.polaris-smart.agent-mailbox.probe"]]


@pytest.mark.skipif(sys.platform != "darwin", reason="desktop shell is macOS-only")
def test_native_quit_request_stops_run_loop_without_terminating_process(monkeypatch):
    from agent_mailbox import workbench_desktop as desktop

    events = []

    class App:
        def stop_(self, _sender):
            events.append("stop")

        def postEvent_atStart_(self, event, at_start):
            assert event == "wake-event"
            assert at_start is True
            events.append("post")

        def terminate_(self, _sender):
            raise AssertionError("terminate_ bypasses Python owner cleanup")

    app = App()
    monkeypatch.setattr(desktop, "NSApplication", SimpleNamespace(sharedApplication=lambda: app))
    monkeypatch.setattr(
        desktop,
        "NSEvent",
        SimpleNamespace(
            otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_=lambda *_: (
                "wake-event"
            )
        ),
        raising=False,
    )
    monkeypatch.setattr(desktop, "NSEventTypeApplicationDefined", 15, raising=False)
    monkeypatch.setattr(desktop, "NSMakePoint", lambda *_: (0, 0), raising=False)
    monkeypatch.setattr(desktop.AppHelper, "stopEventLoop", lambda: app.terminate_(None))

    desktop._request_app_stop()
    assert events == ["stop", "post"]


@pytest.mark.skipif(sys.platform != "darwin", reason="desktop shell is macOS-only")
def test_native_menu_quit_uses_owner_cleanup_path(monkeypatch):
    from agent_mailbox import workbench_desktop as desktop

    calls = []
    monkeypatch.setattr(desktop, "_request_app_stop", lambda: calls.append("stop"))
    desktop._DesktopDelegate.terminate_.callable(object(), None)
    assert calls == ["stop"]


@pytest.mark.skipif(sys.platform != "darwin", reason="desktop shell is macOS-only")
def test_desktop_sigterm_bridge_wakes_cocoa_run_loop_and_restores_handler(monkeypatch):
    from agent_mailbox import workbench_desktop

    calls = []
    prior_python = lambda *_: None
    installed = {}

    def fake_mach_signal(signum, handler):
        calls.append(("mach", signum, handler))
        installed[signum] = handler

    monkeypatch.setattr(
        workbench_desktop, "MachSignals", SimpleNamespace(signal=fake_mach_signal), raising=False
    )
    monkeypatch.setattr(workbench_desktop.signal, "getsignal", lambda _: prior_python)
    monkeypatch.setattr(
        workbench_desktop.signal, "signal", lambda *args: calls.append(("python", *args))
    )
    monkeypatch.setattr(
        workbench_desktop,
        "NSApplication",
        SimpleNamespace(
            sharedApplication=lambda: SimpleNamespace(
                stop_=lambda _: calls.append(("stop",)),
                postEvent_atStart_=lambda event, at_start: calls.append(("post", event, at_start)),
            )
        ),
    )
    monkeypatch.setattr(
        workbench_desktop,
        "NSEvent",
        SimpleNamespace(
            otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_=lambda *_: (
                "wake-event"
            )
        ),
    )

    restore = workbench_desktop._install_quit_signal()
    installed[signal.SIGTERM](signal.SIGTERM)
    restore()

    assert calls[0][:2] == ("mach", signal.SIGTERM)
    assert ("stop",) in calls
    assert ("post", "wake-event", True) in calls
    assert calls[-2:] == [("mach", signal.SIGTERM, None), ("python", signal.SIGTERM, prior_python)]


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
