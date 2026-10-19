"""Owner onboarding contracts: isolated files and fake process boundary, no real host."""

import json
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS Codex App receiver")

from agent_mailbox import workbench_codex_cli_service as cli_service
from agent_mailbox import workbench_codex_connection as connection
from agent_mailbox.workbench_host_bindings import set_binding
from agent_mailbox.workbench_host_transport import endpoint_for
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


@pytest.fixture
def setup(tmp_path, monkeypatch, request):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Synthetic reception", tmp_path)
    employee = store.create_employee(
        "Synthetic Codex", "codex", project["id"], connection_type=getattr(request, "param", "cli")
    )
    binding = set_binding(
        store, employee["id"], project["id"], host_kind="codex", host_session_id="original"
    )
    app = tmp_path / "Test Host.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": "TestHost"}))
    cli = app / "Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
    cli.parent.mkdir(parents=True)
    cli.write_text("synthetic CLI; never executed")
    source = tmp_path / "assets"
    actual = Path(__file__).resolve().parents[1] / "integrations/codex-mailbox-host"
    source.mkdir()
    for name in connection.FILES:
        shutil.copyfile(actual / name, source / name)
    (source / "node_modules/ws").mkdir(parents=True)
    (source / "node_modules/ws/package.json").write_text('{"version":"8.22.0"}')
    (source / "node_modules/ws/index.js").write_text("// isolated dependency fixture")
    node = tmp_path / "node's binary"
    node.write_text("synthetic node; never executed")
    root, ipc = store.root / "workbench/receivers", tmp_path / "ipc"
    monkeypatch.setattr(connection, "_paths", lambda _: (root, ipc))
    monkeypatch.setattr(connection, "_payload", lambda: (source, node))
    monkeypatch.setattr(
        connection,
        "discover_applications",
        lambda: [{"entrypoint": str(app), "bundle_id": "com.openai.codex"}],
    )
    state = {
        "running": False,
        "online": None,
        "ready": True,
        "fail_open": False,
        "version": connection.CLI_VERSION,
    }
    calls = []

    def run(args):
        calls.append(args)
        if args[0] == "/bin/ps":
            return SimpleNamespace(
                stdout=str(app / "Contents/MacOS/TestHost") if state["running"] else ""
            )
        if args[0] == "/usr/bin/codesign":
            assert args[-1] == str(app) and "=" + connection.SIGNATURE in args
        elif args[0] == str(cli):
            assert args[1:] == ["--version"]
            return SimpleNamespace(stdout=state["version"])
        elif args[0] == "/usr/bin/open":
            assert args[1:4] == ["-a", str(app), "--env"] and len(args) == 5
            assert args[4].startswith("CODEX_CLI_PATH=")
            if state["fail_open"]:
                raise subprocess.CalledProcessError(1, args, output="PRIVATE OUTPUT")
        else:
            pytest.fail(f"Unexpected process: {args[0]}")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(connection, "_run", run)
    monkeypatch.setattr(connection, "_codex_launcher_endpoint", lambda *_: state["online"])
    monkeypatch.setattr(
        connection,
        "_codex_health",
        lambda endpoint, thread_id: (
            state["ready"]
            and thread_id
            == next(
                (
                    b["host_session_id"]
                    for b in connection.load_bindings(store.root)
                    if b["employee_id"] == employee["id"] and b["project_id"] == project["id"]
                ),
                None,
            )
        ),
    )

    def act(action="status"):
        return connection.connection(store, employee["id"], project["id"], action)

    return SimpleNamespace(
        store=store,
        project=project,
        employee=employee,
        binding=binding,
        root=root,
        ipc=ipc,
        source=source,
        state=state,
        calls=calls,
        act=act,
    )


@pytest.fixture
def short_ipc():
    with tempfile.TemporaryDirectory(prefix="am-cli-test-", dir="/private/tmp") as base:
        yield Path(base) / "ipc"


def test_status_does_not_install_or_claim_connected(setup):
    s = setup
    result = s.act()
    assert result["state"] == "install_required" and not result["receiver_online"]
    assert not s.root.exists() and not s.ipc.exists()
    assert all(call[0] == "/bin/ps" for call in s.calls)


def test_cli_employee_cannot_be_newly_bound_or_launched_as_desktop(setup, monkeypatch):
    s = setup
    s.act("install")
    s.ipc.mkdir(exist_ok=True)
    monkeypatch.setattr(connection, "_codex_launcher_endpoint", lambda *_: "/tmp/receiver.sock")
    monkeypatch.setattr(connection, "_sessions", lambda *_: {"sessions": []})
    before = connection.load_bindings(s.store.root)
    route = s.store.root / "wake/host-endpoints.json"
    assert not route.exists()
    with pytest.raises(WorkbenchError, match="desktop_employee_required"):
        connection.connection(
            s.store,
            s.employee["id"],
            s.project["id"],
            "bind",
            thread_id="desktop-thread",
            expected_binding_id=s.binding["id"],
            confirmed=True,
        )
    for action in ("connect", "launch", "shortcut", "shortcut_status"):
        with pytest.raises(WorkbenchError, match="desktop_employee_required"):
            connection.connection(
                s.store, s.employee["id"], s.project["id"], action, confirmed=True
            )
    assert connection.load_bindings(s.store.root) == before
    assert not route.exists()


def test_legacy_cli_employee_can_reuse_only_its_existing_desktop_route(setup, monkeypatch):
    s = setup
    s.act("install")
    route = s.store.root / "wake/host-endpoints.json"
    route.write_text(
        json.dumps(
            {
                "version": 1,
                "endpoints": [
                    {
                        "binding_id": s.binding["id"],
                        "employee_id": s.employee["id"],
                        "host_kind": "codex",
                        "launcher_directory": str(s.ipc),
                    }
                ],
            }
        )
    )
    route.chmod(0o600)
    from agent_mailbox import workbench_codex_shortcut

    monkeypatch.setattr(
        workbench_codex_shortcut,
        "shortcut",
        lambda *args, **kwargs: {"state": "shortcut_missing", "available": False},
    )
    assert (
        connection.connection(s.store, s.employee["id"], s.project["id"], "shortcut_status")[
            "state"
        ]
        == "shortcut_missing"
    )
    assert (
        connection.connection(
            s.store, s.employee["id"], s.project["id"], "shortcut", confirmed=True
        )["state"]
        == "shortcut_missing"
    )
    for action in ("connect", "launch"):
        assert (
            connection.connection(s.store, s.employee["id"], s.project["id"], action)["binding_id"]
            == s.binding["id"]
        )
    assert connection.load_bindings(s.store.root) == [s.binding]
    other = s.store.create_project("Another project", s.project["path"])
    s.store.add_project_member(other["id"], s.employee["id"])
    set_binding(
        s.store, s.employee["id"], other["id"], host_kind="codex", host_session_id="another-thread"
    )
    with pytest.raises(WorkbenchError, match="desktop_employee_required"):
        connection.connection(s.store, s.employee["id"], other["id"], "shortcut_status")
    route.write_text(
        json.dumps(
            {
                "version": 1,
                "endpoints": [
                    {
                        "binding_id": s.binding["id"],
                        "employee_id": s.employee["id"],
                        "host_kind": "codex",
                        "launcher_directory": str(s.ipc / "other"),
                    }
                ],
            }
        )
    )
    with pytest.raises(WorkbenchError, match="separate_employee_required_for_desktop"):
        connection.connection(s.store, s.employee["id"], s.project["id"], "shortcut_status")


def test_cli_thread_preflight_accepts_only_exact_user_exec_workspace(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    sessions = codex_home / "sessions/2026/10/18"
    sessions.mkdir(parents=True)
    workspace = tmp_path / "project"
    workspace.mkdir()
    thread_id = "12345678-1234-1234-1234-123456789abc"
    record = sessions / f"rollout-2026-10-18T12-00-00-{thread_id}.jsonl"
    metadata = {
        "type": "session_meta",
        "payload": {
            "id": thread_id,
            "cwd": str(workspace),
            "originator": "codex_exec",
            "source": "exec",
            "thread_source": "user",
        },
    }
    record.write_text(json.dumps(metadata) + "\nPRIVATE BODY NOT READ\n")
    monkeypatch.setattr(connection, "_codex_home", lambda: codex_home)
    assert connection._verify_cli_thread(thread_id, workspace) == {
        "thread_id": thread_id,
        "workspace_verified": True,
    }
    for changes in (
        {"originator": "Codex Desktop"},
        {"cwd": str(tmp_path)},
        {"id": "another"},
        {"thread_source": "subagent"},
    ):
        record.write_text(
            json.dumps({**metadata, "payload": {**metadata["payload"], **changes}}) + "\n"
        )
        with pytest.raises(ValueError, match="cli_thread_not_verified"):
            connection._verify_cli_thread(thread_id, workspace)
    record.write_text(json.dumps(metadata) + "\n")
    with pytest.raises(ValueError, match="cli_thread_not_verified"):
        connection._verify_cli_thread("../other", workspace)


def test_cli_preflight_owner_action_is_read_only_and_rejects_unverified_thread(setup, monkeypatch):
    s = setup

    def verify(thread_id, path):
        if thread_id != "cli-thread":
            raise ValueError("cli_thread_not_verified")
        return {"thread_id": thread_id, "workspace_verified": path == s.project["path"]}

    monkeypatch.setattr(connection, "_verify_cli_thread", verify)
    result = connection.connection(
        s.store, s.employee["id"], s.project["id"], "cli_preflight", thread_id="cli-thread"
    )
    assert result == {"thread_id": "cli-thread", "workspace_verified": True}
    assert not s.root.exists() and not s.ipc.exists()
    assert not (s.store.root / "wake/host-endpoints.json").exists()
    with pytest.raises(WorkbenchError, match="cli_thread_not_verified"):
        connection.connection(
            s.store, s.employee["id"], s.project["id"], "cli_preflight", thread_id="../other"
        )


def test_cli_bind_requires_verified_thread_and_binding_cas(setup, monkeypatch):
    s = setup
    route = s.store.root / "wake/host-endpoints.json"
    original = {
        "version": 1,
        "endpoints": [
            {
                "binding_id": s.binding["id"],
                "employee_id": s.employee["id"],
                "host_kind": "codex",
                "launcher_directory": str(s.ipc),
            },
            {
                "binding_id": "another",
                "employee_id": "another",
                "host_kind": "hermes",
                "socket_path": "/tmp/another.sock",
            },
        ],
    }
    route.write_text(json.dumps(original))
    route.chmod(0o600)

    def verify(thread_id, path):
        if thread_id != "cli-thread" or path != s.project["path"]:
            raise ValueError("cli_thread_not_verified")
        return {"thread_id": thread_id, "workspace_verified": True}

    monkeypatch.setattr(connection, "_verify_cli_thread", verify)
    with pytest.raises(WorkbenchError, match="cli_thread_not_verified"):
        connection.connection(
            s.store,
            s.employee["id"],
            s.project["id"],
            "cli_bind",
            thread_id="other",
            expected_binding_id=s.binding["id"],
            confirmed=True,
        )
    assert connection.load_bindings(s.store.root)[0]["host_session_id"] == "original"
    with pytest.raises(WorkbenchError):
        connection.connection(
            s.store,
            s.employee["id"],
            s.project["id"],
            "cli_bind",
            thread_id="cli-thread",
            expected_binding_id="stale",
            confirmed=True,
        )
    assert connection.load_bindings(s.store.root)[0]["host_session_id"] == "original"
    assert json.loads(route.read_text()) == original
    with pytest.raises(WorkbenchError, match="separate_employee_required_for_cli"):
        connection.connection(
            s.store,
            s.employee["id"],
            s.project["id"],
            "cli_bind",
            thread_id="cli-thread",
            expected_binding_id=s.binding["id"],
            confirmed=True,
        )
    assert connection.load_bindings(s.store.root)[0]["host_session_id"] == "original"
    assert json.loads(route.read_text()) == original
    route.write_text(json.dumps({"version": 1, "endpoints": original["endpoints"][1:]}))
    result = connection.connection(
        s.store,
        s.employee["id"],
        s.project["id"],
        "cli_bind",
        thread_id="cli-thread",
        expected_binding_id=s.binding["id"],
        confirmed=True,
    )
    assert result["state"] == "cli_install_required"
    assert result["binding_id"] != s.binding["id"]
    assert connection.load_bindings(s.store.root)[0]["host_session_id"] == "cli-thread"
    assert json.loads(route.read_text())["endpoints"] == original["endpoints"][1:]


def test_codex_desktop_and_cli_require_distinct_employees_across_projects(setup, monkeypatch):
    s = setup
    other = s.store.create_project("Other project", s.project["path"])
    s.store.add_project_member(other["id"], s.employee["id"])
    route = s.store.root / "wake/host-endpoints.json"
    route.parent.mkdir(parents=True, exist_ok=True)
    desktop = {
        "binding_id": s.binding["id"],
        "employee_id": s.employee["id"],
        "host_kind": "codex",
        "launcher_directory": str(s.ipc),
    }
    route.write_text(json.dumps({"version": 1, "endpoints": [desktop]}))
    route.chmod(0o600)
    monkeypatch.setattr(
        connection,
        "_verify_cli_thread",
        lambda thread_id, path: {"thread_id": thread_id, "workspace_verified": True},
    )

    with pytest.raises(WorkbenchError, match="separate_employee_required_for_cli"):
        connection.connection(
            s.store,
            s.employee["id"],
            other["id"],
            "cli_bind",
            thread_id="cli-thread",
            expected_binding_id=None,
            confirmed=True,
        )
    assert len(connection.load_bindings(s.store.root)) == 1
    assert json.loads(route.read_text())["endpoints"] == [desktop]

    # Reverse direction: a CLI employee cannot add a Desktop route through
    # another project while the existing CLI receiver owns its identity.
    route.write_text(
        json.dumps(
            {
                "version": 1,
                "endpoints": [
                    {
                        "binding_id": s.binding["id"],
                        "employee_id": s.employee["id"],
                        "host_kind": "codex",
                        "mode": "on_demand_cli",
                        "socket_path": "/tmp/am-codex-cli-test.sock",
                    },
                ],
            }
        )
    )
    s.act("install")
    s.ipc.mkdir(exist_ok=True)
    monkeypatch.setattr(connection, "_codex_launcher_endpoint", lambda *_: "/tmp/receiver.sock")
    monkeypatch.setattr(
        connection,
        "_sessions",
        lambda endpoint, thread_id=None: {
            "sessions": [{"id": thread_id}],
        },
    )
    with pytest.raises(WorkbenchError, match="separate_employee_required_for_desktop"):
        connection.connection(
            s.store,
            s.employee["id"],
            other["id"],
            "bind",
            thread_id="desktop-thread",
            expected_binding_id=None,
            confirmed=True,
        )
    for action in ("connect", "launch", "shortcut", "shortcut_status"):
        with pytest.raises(WorkbenchError, match="separate_employee_required_for_desktop"):
            connection.connection(s.store, s.employee["id"], other["id"], action, confirmed=True)
    assert len(connection.load_bindings(s.store.root)) == 1


@pytest.mark.parametrize("setup", ["app"], indirect=True)
def test_pending_desktop_binding_blocks_cli_in_another_project(setup, monkeypatch):
    s = setup
    other = s.store.create_project("Second project", s.project["path"])
    s.store.add_project_member(other["id"], s.employee["id"])
    s.act("install")
    s.ipc.mkdir(exist_ok=True)
    monkeypatch.setattr(connection, "_codex_launcher_endpoint", lambda *_: "/tmp/receiver.sock")
    monkeypatch.setattr(connection, "_sessions", lambda *_: {"sessions": []})
    pending = connection.connection(
        s.store,
        s.employee["id"],
        s.project["id"],
        "bind",
        thread_id="desktop-thread",
        expected_binding_id=s.binding["id"],
        confirmed=True,
    )
    assert pending["state"] == "connect_required"
    monkeypatch.setattr(
        connection,
        "_verify_cli_thread",
        lambda thread_id, path: {
            "thread_id": thread_id,
            "workspace_verified": True,
        },
    )
    before = connection.load_bindings(s.store.root)
    with pytest.raises(WorkbenchError, match="cli_employee_required"):
        connection.connection(
            s.store,
            s.employee["id"],
            other["id"],
            "cli_bind",
            thread_id="cli-thread",
            expected_binding_id=None,
            confirmed=True,
        )
    assert connection.load_bindings(s.store.root) == before


def test_pending_cli_binding_blocks_desktop_in_another_project(setup, monkeypatch):
    s = setup
    other = s.store.create_project("Second project", s.project["path"])
    s.store.add_project_member(other["id"], s.employee["id"])
    monkeypatch.setattr(
        connection,
        "_verify_cli_thread",
        lambda thread_id, path: {
            "thread_id": thread_id,
            "workspace_verified": True,
        },
    )
    pending = connection.connection(
        s.store,
        s.employee["id"],
        s.project["id"],
        "cli_bind",
        thread_id="cli-thread",
        expected_binding_id=s.binding["id"],
        confirmed=True,
    )
    assert pending["state"] == "cli_install_required"
    assert s.act("status")["state"] == "cli_install_required"
    s.act("install")
    s.ipc.mkdir(exist_ok=True)
    monkeypatch.setattr(connection, "_codex_launcher_endpoint", lambda *_: "/tmp/receiver.sock")
    monkeypatch.setattr(connection, "_sessions", lambda *_: {"sessions": []})
    before = connection.load_bindings(s.store.root)
    with pytest.raises(WorkbenchError, match="separate_employee_required_for_desktop"):
        connection.connection(
            s.store,
            s.employee["id"],
            other["id"],
            "bind",
            thread_id="desktop-thread",
            expected_binding_id=None,
            confirmed=True,
        )
    assert connection.load_bindings(s.store.root) == before


def test_legacy_unclassified_codex_binding_blocks_cross_project_surface(setup, monkeypatch):
    s = setup
    other = s.store.create_project("Second project", s.project["path"])
    s.store.add_project_member(other["id"], s.employee["id"])
    assert "receiver_surface" not in s.binding
    assert connection.endpoints(s.store.root) == []
    monkeypatch.setattr(
        connection,
        "_verify_cli_thread",
        lambda thread_id, path: {
            "thread_id": thread_id,
            "workspace_verified": True,
        },
    )
    before = connection.load_bindings(s.store.root)
    with pytest.raises(WorkbenchError, match="separate_employee_required_for_cli"):
        connection.connection(
            s.store,
            s.employee["id"],
            other["id"],
            "cli_bind",
            thread_id="cli-thread",
            expected_binding_id=None,
            confirmed=True,
        )
    assert connection.load_bindings(s.store.root) == before

    s.act("install")
    s.ipc.mkdir(exist_ok=True)
    monkeypatch.setattr(connection, "_codex_launcher_endpoint", lambda *_: "/tmp/receiver.sock")
    monkeypatch.setattr(connection, "_sessions", lambda *_: {"sessions": []})
    with pytest.raises(WorkbenchError, match="separate_employee_required_for_desktop"):
        connection.connection(
            s.store,
            s.employee["id"],
            other["id"],
            "bind",
            thread_id="desktop-thread",
            expected_binding_id=None,
            confirmed=True,
        )
    assert connection.load_bindings(s.store.root) == before


def test_legacy_app_employee_with_two_projects_can_check_its_one_desktop_shortcut(
    setup, monkeypatch
):
    s = setup
    other = s.store.create_project("Second project", s.project["path"])
    app_employee = s.store.create_employee(
        "Synthetic Codex App", "codex", s.project["id"], connection_type="app"
    )
    s.store.add_project_member(other["id"], app_employee["id"])
    set_binding(
        s.store,
        app_employee["id"],
        s.project["id"],
        host_kind="codex",
        host_session_id="legacy-app-first",
    )
    set_binding(
        s.store,
        app_employee["id"],
        other["id"],
        host_kind="codex",
        host_session_id="legacy-app-second",
    )
    assert all(
        "receiver_surface" not in row
        for row in connection.load_bindings(s.store.root)
        if row["employee_id"] == app_employee["id"]
    )
    from agent_mailbox import workbench_codex_shortcut

    monkeypatch.setattr(
        workbench_codex_shortcut,
        "shortcut",
        lambda *args, **kwargs: {"state": "shortcut_missing", "available": False},
    )
    for project_id in (s.project["id"], other["id"]):
        assert (
            connection.connection(s.store, app_employee["id"], project_id, "shortcut_status")[
                "state"
            ]
            == "shortcut_missing"
        )
    legacy_second = next(
        row
        for row in connection.load_bindings(s.store.root)
        if row["employee_id"] == app_employee["id"] and row["project_id"] == other["id"]
    )
    route = s.store.root / "wake/host-endpoints.json"
    route.write_text(
        json.dumps(
            {
                "version": 1,
                "endpoints": [
                    {
                        "binding_id": legacy_second["id"],
                        "employee_id": app_employee["id"],
                        "host_kind": "codex",
                        "mode": "on_demand_cli",
                        "socket_path": "/private/tmp/synthetic-codex-cli.sock",
                    }
                ],
            }
        )
    )
    route.chmod(0o600)
    with pytest.raises(WorkbenchError, match="separate_employee_required_for_desktop"):
        connection.connection(s.store, app_employee["id"], s.project["id"], "shortcut_status")


def test_cli_rebind_prunes_only_stale_routes_for_same_employee(setup, monkeypatch):
    s = setup
    other_project = s.store.create_project("Second project", s.project["path"])
    s.store.add_project_member(other_project["id"], s.employee["id"])
    with connection.state_lock(s.store.root):
        other_binding = connection._set_binding_locked(
            s.store,
            s.employee["id"],
            other_project["id"],
            host_kind="codex",
            host_session_id="other-thread",
            receiver_surface="cli",
        )
    route = s.store.root / "wake/host-endpoints.json"
    old = {
        "binding_id": s.binding["id"],
        "employee_id": s.employee["id"],
        "host_kind": "codex",
        "socket_path": "/private/tmp/old.sock",
    }
    another_project = {
        "binding_id": other_binding["id"],
        "employee_id": s.employee["id"],
        "host_kind": "codex",
        "socket_path": "/private/tmp/other.sock",
    }
    stale = {
        "binding_id": "binding_stale",
        "employee_id": s.employee["id"],
        "host_kind": "codex",
        "socket_path": "/private/tmp/stale.sock",
    }
    foreign = {
        "binding_id": "binding_foreign",
        "employee_id": "another-employee",
        "host_kind": "hermes",
        "socket_path": "/private/tmp/foreign.sock",
    }
    route.write_text(
        json.dumps({"version": 1, "endpoints": [old, another_project, stale, foreign]})
    )
    route.chmod(0o600)
    monkeypatch.setattr(
        connection,
        "_verify_cli_thread",
        lambda thread_id, path: {"thread_id": thread_id, "workspace_verified": True},
    )

    with pytest.raises(WorkbenchError, match="receiver_binding_changed_refresh_required"):
        connection.connection(
            s.store,
            s.employee["id"],
            s.project["id"],
            "cli_bind",
            thread_id="new-thread",
            expected_binding_id="wrong",
            confirmed=True,
        )
    assert connection.endpoints(s.store.root) == [old, another_project, stale, foreign]

    updated = connection.connection(
        s.store,
        s.employee["id"],
        s.project["id"],
        "cli_bind",
        thread_id="new-thread",
        expected_binding_id=s.binding["id"],
        confirmed=True,
    )
    assert updated["binding_id"] != s.binding["id"]
    assert connection.endpoints(s.store.root) == [another_project, foreign]


def test_cli_enable_stages_private_config_and_exact_endpoint_then_activates(
    setup, monkeypatch, short_ipc
):
    s = setup
    s.act("install")
    monkeypatch.setattr(connection, "_paths", lambda _: (s.root, short_ipc))
    monkeypatch.setattr(
        connection,
        "_verify_cli_thread",
        lambda thread_id, path: {"thread_id": thread_id, "workspace_verified": True},
    )
    binding = connection.connection(
        s.store,
        s.employee["id"],
        s.project["id"],
        "cli_bind",
        thread_id="cli-thread",
        expected_binding_id=s.binding["id"],
        confirmed=True,
    )
    activated = []
    result = connection.connection(
        s.store,
        s.employee["id"],
        s.project["id"],
        "cli_enable",
        thread_id="cli-thread",
        expected_binding_id=binding["binding_id"],
        confirmed=True,
        activate=lambda path: activated.append(path),
    )
    assert result["state"] == "cli_launch_requested" and not result["receiver_online"]
    assert len(activated) == 1
    config = json.loads(activated[0].read_text())
    assert config["binding_id"] == binding["binding_id"]
    assert config["thread_id"] == "cli-thread" and config["employee_id"] == s.employee["id"]
    assert activated[0].stat().st_mode & 0o077 == 0
    rows = json.loads((s.store.root / "wake/host-endpoints.json").read_text())["endpoints"]
    assert rows == [
        {
            "binding_id": binding["binding_id"],
            "employee_id": s.employee["id"],
            "host_kind": "codex",
            "mode": "on_demand_cli",
            "socket_path": config["socket_path"],
        }
    ]
    status = s.act("status")
    assert status["state"] == "cli_receiver_unavailable" and not status["receiver_online"]
    assert status["ordinary_icon_supported"] is True
    assert status["binding_id"] == binding["binding_id"]
    assert status["bound_thread_id"] == "cli-thread"
    monkeypatch.setattr(cli_service, "_paths", lambda _: (s.root, short_ipc))
    monkeypatch.setattr(cli_service, "_payload", connection._payload)
    monkeypatch.setattr(cli_service, "_verify_cli_thread", connection._verify_cli_thread)
    command, cwd = cli_service.CodexCliManager(s.store)._command(activated[0])
    assert command[-1] == str(activated[0]) and cwd == config["cwd"]
    changed = {**config, "cwd": str(s.store.root)}
    activated[0].write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="cli_workspace_changed"):
        cli_service.CodexCliManager(s.store)._command(activated[0])


def test_cli_enable_activation_failure_rolls_back_route_and_config(setup, monkeypatch, short_ipc):
    s = setup
    s.act("install")
    monkeypatch.setattr(connection, "_paths", lambda _: (s.root, short_ipc))
    monkeypatch.setattr(
        connection,
        "_verify_cli_thread",
        lambda thread_id, path: {"thread_id": thread_id, "workspace_verified": True},
    )
    binding = connection.connection(
        s.store,
        s.employee["id"],
        s.project["id"],
        "cli_bind",
        thread_id="cli-thread",
        expected_binding_id=s.binding["id"],
        confirmed=True,
    )

    def fail(_path):
        raise ValueError("synthetic_activation_failure")

    with pytest.raises(WorkbenchError, match="synthetic_activation_failure"):
        connection.connection(
            s.store,
            s.employee["id"],
            s.project["id"],
            "cli_enable",
            thread_id="cli-thread",
            expected_binding_id=binding["binding_id"],
            confirmed=True,
            activate=fail,
        )
    assert not (s.store.root / "wake/host-endpoints.json").exists()
    assert list((s.root / "cli").glob("*.json")) == []


def test_one_cli_employee_shares_execution_lock_across_project_routes(
    setup, monkeypatch, short_ipc
):
    s = setup
    s.act("install")
    monkeypatch.setattr(connection, "_paths", lambda _: (s.root, short_ipc))
    monkeypatch.setattr(cli_service, "_paths", lambda _: (s.root, short_ipc))
    monkeypatch.setattr(
        connection,
        "_verify_cli_thread",
        lambda thread_id, path: {"thread_id": thread_id, "workspace_verified": True},
    )
    monkeypatch.setattr(cli_service, "_verify_cli_thread", connection._verify_cli_thread)
    first = connection.connection(
        s.store,
        s.employee["id"],
        s.project["id"],
        "cli_bind",
        thread_id="one-agent-thread",
        expected_binding_id=s.binding["id"],
        confirmed=True,
    )
    connection.connection(
        s.store,
        s.employee["id"],
        s.project["id"],
        "cli_enable",
        thread_id="one-agent-thread",
        expected_binding_id=first["binding_id"],
        confirmed=True,
        activate=lambda _: None,
    )
    other = s.store.create_project("Second authorized project", s.project["path"])
    s.store.add_project_member(other["id"], s.employee["id"])
    second = connection.connection(
        s.store,
        s.employee["id"],
        other["id"],
        "cli_bind",
        thread_id="one-agent-thread",
        expected_binding_id=None,
        confirmed=True,
    )
    connection.connection(
        s.store,
        s.employee["id"],
        other["id"],
        "cli_enable",
        thread_id="one-agent-thread",
        expected_binding_id=second["binding_id"],
        confirmed=True,
        activate=lambda _: None,
    )
    manager = cli_service.CodexCliManager(s.store)
    first_config = s.root / "cli" / (first["binding_id"] + ".json")
    second_config = s.root / "cli" / (second["binding_id"] + ".json")
    first_command, _ = manager._command(first_config)
    second_command, _ = manager._command(second_config)
    assert first_command[-2] == second_command[-2]
    assert Path(first_command[-2]).parent.name == s.employee["id"]
    assert first_command[-3] != second_command[-3], "project routes keep separate sockets"


def test_cli_restore_does_not_count_superseded_binding_configs(setup, monkeypatch, short_ipc):
    s = setup
    s.act("install")
    monkeypatch.setattr(connection, "_paths", lambda _: (s.root, short_ipc))
    monkeypatch.setattr(cli_service, "_paths", lambda _: (s.root, short_ipc))
    monkeypatch.setattr(
        connection,
        "_verify_cli_thread",
        lambda thread_id, path: {"thread_id": thread_id, "workspace_verified": True},
    )
    bound = connection.connection(
        s.store,
        s.employee["id"],
        s.project["id"],
        "cli_bind",
        thread_id="cli-thread",
        expected_binding_id=s.binding["id"],
        confirmed=True,
    )
    connection.connection(
        s.store,
        s.employee["id"],
        s.project["id"],
        "cli_enable",
        thread_id="cli-thread",
        expected_binding_id=bound["binding_id"],
        confirmed=True,
        activate=lambda _: None,
    )
    directory = s.root / "cli"
    for index in range(33):
        (directory / f"binding_old{index:02d}.json").write_text("{}")
    activated = []
    manager = cli_service.CodexCliManager(s.store)
    monkeypatch.setattr(manager, "activate", lambda path: activated.append(path))
    manager.restore()
    assert activated == [directory / (bound["binding_id"] + ".json")]


def test_cli_rebind_retires_only_superseded_worker(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(
        connection,
        "_verify_cli_thread",
        lambda thread_id, path: {"thread_id": thread_id, "workspace_verified": True},
    )
    other = s.store.create_project("Other project", s.project["path"])
    s.store.add_project_member(other["id"], s.employee["id"])
    with connection.state_lock(s.store.root):
        other_binding = connection._set_binding_locked(
            s.store,
            s.employee["id"],
            other["id"],
            host_kind="codex",
            host_session_id="other-thread",
            receiver_surface="cli",
        )
    manager = cli_service.CodexCliManager(s.store)
    old_config = s.root / "cli" / (s.binding["id"] + ".json")
    other_config = s.root / "cli" / (other_binding["id"] + ".json")

    class Process:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    old_process, other_process = Process(), Process()
    manager.processes = {old_config: old_process, other_config: other_process}
    with pytest.raises(WorkbenchError):
        connection.connection(
            s.store,
            s.employee["id"],
            s.project["id"],
            "cli_bind",
            thread_id="new-thread",
            expected_binding_id="stale",
            confirmed=True,
            retire=manager.retire_stale,
        )
    assert not old_process.terminated and not other_process.terminated
    result = connection.connection(
        s.store,
        s.employee["id"],
        s.project["id"],
        "cli_bind",
        thread_id="new-thread",
        expected_binding_id=s.binding["id"],
        confirmed=True,
        retire=manager.retire_stale,
    )
    assert result["binding_id"] != s.binding["id"]
    assert old_process.terminated
    assert not other_process.terminated
    assert old_config in manager.retired
    assert other_config not in manager.retired


def test_cli_retired_worker_does_not_restart(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(
        connection,
        "_verify_cli_thread",
        lambda thread_id, path: {"thread_id": thread_id, "workspace_verified": True},
    )
    config = s.root / "cli" / (s.binding["id"] + ".json")
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("{}")
    stopped = threading.Event()
    calls = []

    class Process:
        def poll(self):
            return 0 if stopped.is_set() else None

        def wait(self, timeout=None):
            assert stopped.wait(timeout)
            return 0

        def terminate(self):
            stopped.set()

        def kill(self):
            stopped.set()

    manager = cli_service.CodexCliManager(s.store)
    monkeypatch.setattr(manager, "_spawn", lambda path: calls.append(path) or Process())
    try:
        manager.activate(config)
        connection.connection(
            s.store,
            s.employee["id"],
            s.project["id"],
            "cli_bind",
            thread_id="replacement",
            expected_binding_id=s.binding["id"],
            confirmed=True,
            retire=manager.retire_stale,
        )
        deadline = time.monotonic() + 2
        while config in manager.processes and time.monotonic() < deadline:
            time.sleep(0.01)
        assert config not in manager.processes
        assert calls == [config], "superseded receiver must not respawn"
    finally:
        manager.close()


def test_cli_retry_budget_resets_after_stable_runtime(setup, monkeypatch):
    s = setup
    config = s.root / "cli" / (s.binding["id"] + ".json")
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("{}")
    processes = []

    class Process:
        def __init__(self):
            self.stopped = threading.Event()

        def poll(self):
            return 1 if self.stopped.is_set() else None

        def wait(self, timeout=None):
            assert self.stopped.wait(timeout)
            return 1

        def terminate(self):
            self.stopped.set()

        def kill(self):
            self.stopped.set()

    manager = cli_service.CodexCliManager(s.store)
    clock = [0.0]
    manager.clock = lambda: clock[0]
    monkeypatch.setattr(
        manager, "_spawn", lambda path: processes.append(Process()) or processes[-1]
    )
    normal_wait = manager.closing.wait
    monkeypatch.setattr(manager.closing, "wait", lambda timeout=None: False)
    try:
        manager.activate(config)
        for index in range(3):
            processes[index].stopped.set()
            deadline = time.monotonic() + 2
            while len(processes) < index + 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(processes) == index + 2
        clock[0] = cli_service.STABLE_RUNTIME_SECONDS + 1.0
        processes[3].stopped.set()
        deadline = time.monotonic() + 2
        while len(processes) < 5 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(processes) == 5, "a stable receiver should regain its retry budget"
        for index in (4, 5):
            processes[index].stopped.set()
            deadline = time.monotonic() + 2
            while len(processes) < index + 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(processes) == index + 2
        processes[6].stopped.set()
        deadline = time.monotonic() + 2
        while config in manager.processes and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(processes) == 7
        assert manager.errors[config.name] == "receiver_restart_limit"
    finally:
        monkeypatch.setattr(manager.closing, "wait", normal_wait)
        manager.close()


def test_owner_http_cli_rebind_retires_old_worker(setup, monkeypatch):
    from agent_mailbox.workbench import WorkbenchHTTP

    s = setup
    monkeypatch.setattr(
        connection,
        "_verify_cli_thread",
        lambda thread_id, path: {"thread_id": thread_id, "workspace_verified": True},
    )
    server = WorkbenchHTTP(s.store)
    retired = []
    monkeypatch.setattr(server.codex_cli, "retire_stale", lambda: retired.append(True))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            server.endpoint + "/api/workbench/mailbox/receivers/codex",
            method="POST",
            data=json.dumps(
                {
                    "employee_id": s.employee["id"],
                    "project_id": s.project["id"],
                    "action": "cli_bind",
                    "thread_id": "new-thread",
                    "expected_binding_id": s.binding["id"],
                    "confirmed": True,
                }
            ).encode(),
            headers={"Authorization": "Bearer " + server.token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
            assert json.load(response)["state"] == "cli_install_required"
        assert retired == [True]
    finally:
        server.shutdown()
        thread.join(5)
        server.close()


def test_cli_manager_restores_lightweight_receiver_and_stops_on_workbench_close(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(cli_service, "_paths", lambda _: (s.root, s.ipc))
    directory = s.root / "cli"
    directory.mkdir(parents=True, mode=0o700)
    (directory / (s.binding["id"] + ".json")).write_text("{}")
    manager = cli_service.CodexCliManager(s.store)
    stopped = threading.Event()

    class Process:
        def poll(self):
            return 0 if stopped.is_set() else None

        def wait(self, timeout=None):
            assert stopped.wait(timeout)
            return 0

        def terminate(self):
            stopped.set()

        def kill(self):
            stopped.set()

    monkeypatch.setattr(manager, "_spawn", lambda _: Process())
    manager.restore()
    assert len(manager.processes) == 1
    manager.close()
    assert stopped.is_set()


def test_ordinary_workbench_start_restores_approved_cli_receiver(setup, monkeypatch, short_ipc):
    from agent_mailbox.workbench import WorkbenchHTTP

    s = setup
    s.act("install")
    monkeypatch.setattr(connection, "_paths", lambda _: (s.root, short_ipc))
    monkeypatch.setattr(cli_service, "_paths", lambda _: (s.root, short_ipc))
    monkeypatch.setattr(
        connection,
        "_verify_cli_thread",
        lambda thread_id, path: {"thread_id": thread_id, "workspace_verified": True},
    )
    binding = connection.connection(
        s.store,
        s.employee["id"],
        s.project["id"],
        "cli_bind",
        thread_id="cli-thread",
        expected_binding_id=s.binding["id"],
        confirmed=True,
    )
    connection.connection(
        s.store,
        s.employee["id"],
        s.project["id"],
        "cli_enable",
        thread_id="cli-thread",
        expected_binding_id=binding["binding_id"],
        confirmed=True,
        activate=lambda _: None,
    )
    stopped = threading.Event()

    class Process:
        def poll(self):
            return 0 if stopped.is_set() else None

        def wait(self, timeout=None):
            assert stopped.wait(timeout)
            return 0

        def terminate(self):
            stopped.set()

        def kill(self):
            stopped.set()

    monkeypatch.setattr(cli_service.CodexCliManager, "_spawn", lambda self, _: Process())
    server = WorkbenchHTTP(s.store)
    try:
        assert len(server.codex_cli.processes) == 1
    finally:
        server.close()
    assert stopped.is_set()


def test_real_lightweight_receiver_recovers_after_ordinary_restart_without_model(
    setup, monkeypatch, short_ipc
):
    s = setup
    s.act("install")
    monkeypatch.setattr(connection, "_paths", lambda _: (s.root, short_ipc))
    monkeypatch.setattr(cli_service, "_paths", lambda _: (s.root, short_ipc))
    monkeypatch.setattr(
        connection,
        "_verify_cli_thread",
        lambda thread_id, path: {"thread_id": thread_id, "workspace_verified": True},
    )
    monkeypatch.setattr(cli_service, "_verify_cli_thread", connection._verify_cli_thread)
    cli = connection._app()[1]
    cli.chmod(0o700)  # Synthetic executable is never called: no mail is sent.
    binding = connection.connection(
        s.store,
        s.employee["id"],
        s.project["id"],
        "cli_bind",
        thread_id="cli-thread",
        expected_binding_id=s.binding["id"],
        confirmed=True,
    )
    connection.connection(
        s.store,
        s.employee["id"],
        s.project["id"],
        "cli_enable",
        thread_id="cli-thread",
        expected_binding_id=binding["binding_id"],
        confirmed=True,
        activate=lambda _: None,
    )
    source = Path(__file__).resolve().parents[1] / "integrations/codex-mailbox-host"
    monkeypatch.setattr(cli_service, "_installed", lambda _: source)
    monkeypatch.setattr(cli_service, "_payload", lambda: (source, Path(shutil.which("node"))))
    original_popen = subprocess.Popen
    startup_log = s.store.root / "receiver-startup.log"

    def diagnostic_popen(command, **kwargs):
        with startup_log.open("ab") as log:
            kwargs["stderr"] = log
            return original_popen(command, **kwargs)

    monkeypatch.setattr(cli_service.subprocess, "Popen", diagnostic_popen)
    current = connection.load_bindings(s.store.root)[0]
    for _ in range(2):
        manager = cli_service.CodexCliManager(s.store)
        try:
            manager.restore()
            assert len(manager.processes) == 1, manager.errors
            deadline = time.monotonic() + 8
            while (
                not connection.receiver_online(s.store.root, current)
                and time.monotonic() < deadline
            ):
                time.sleep(0.03)
            assert connection.receiver_online(s.store.root, current), {
                "errors": manager.errors,
                "workers": [(worker.pid, worker.poll()) for worker in manager.processes.values()],
                "socket_exists": Path(endpoint_for(s.store.root, current)).exists(),
                "startup_log": startup_log.read_text(errors="replace")[-500:],
            }
        finally:
            manager.close()


def test_install_idempotent_and_upgrade_keeps_old_files(setup):
    s = setup
    assert s.act("install")["state"] == "ready_to_start"
    first = connection._installed(s.root)
    content = (first / "launcher.mjs").read_bytes()
    assert (first / "cli-on-demand.mjs").read_bytes() == (
        s.source / "cli-on-demand.mjs"
    ).read_bytes()
    descriptor = (s.root / "codex.json").read_bytes()
    s.act("install")
    assert (s.root / "codex.json").read_bytes() == descriptor
    (s.source / "launcher.mjs").write_bytes(content + b"\n// new isolated candidate\n")
    s.act("install")
    assert connection._installed(s.root) != first
    assert (first / "launcher.mjs").read_bytes() == content
    assert not (s.store.root / "wake/host-endpoints.json").exists()
    assert all(call[0] != "/usr/bin/open" for call in s.calls)


@pytest.mark.parametrize("setup", ["app"], indirect=True)
def test_running_host_not_restarted_or_routed(setup):
    s = setup
    s.state["running"] = True
    assert s.act("install")["state"] == "restart_required"
    s.calls.clear()
    assert s.act("connect")["state"] == "restart_required"
    assert [call[0] for call in s.calls] == ["/bin/ps"]
    assert not (s.store.root / "wake/host-endpoints.json").exists()


@pytest.mark.parametrize("setup", ["app"], indirect=True)
def test_stopped_host_launch_is_not_success_receipt(setup):
    s = setup
    s.act("install")
    result = s.act("connect")
    assert result["state"] == "launch_requested" and not result["receiver_online"]
    route = json.loads((s.store.root / "wake/host-endpoints.json").read_text())["endpoints"][0]
    assert route == {
        "binding_id": s.binding["id"],
        "employee_id": s.employee["id"],
        "host_kind": "codex",
        "launcher_directory": str(s.ipc),
    }
    wrapper = (connection._installed(s.root) / "codex-mailbox").read_text()
    assert '"$@"' in wrapper and "actual_version=" in wrapper
    # Shell syntax is checked without executing the wrapper or synthetic binaries.
    subprocess.run(
        ["/bin/sh", "-n", str(connection._installed(s.root) / "codex-mailbox")], check=True
    )


@pytest.mark.parametrize("prior", [False, True])
@pytest.mark.parametrize("setup", ["app"], indirect=True)
def test_launch_failure_rolls_back_only_its_route(setup, prior):
    s = setup
    s.act("install")
    route = s.store.root / "wake/host-endpoints.json"
    original = b'{"version":1,"endpoints":[{"binding_id":"other","employee_id":"other","host_kind":"hermes","socket_path":"/tmp/synthetic"}]}'
    if prior:
        route.write_bytes(original)
        route.chmod(0o600)
    s.state["fail_open"] = True
    with pytest.raises(WorkbenchError, match="receiver_operation_failed") as e:
        s.act("connect")
    assert "PRIVATE OUTPUT" not in str(e.value)
    assert (route.read_bytes() == original) if prior else not route.exists()


@pytest.mark.parametrize("setup", ["app"], indirect=True)
def test_online_connect_preserves_other_routes_and_does_not_open_app(setup):
    s = setup
    s.act("install")
    path = s.store.root / "wake/host-endpoints.json"
    other = {
        "binding_id": "other",
        "employee_id": "other",
        "host_kind": "deepseek",
        "socket_path": "/tmp/synthetic",
    }
    path.write_text(json.dumps({"version": 1, "endpoints": [other]}))
    path.chmod(0o600)
    s.state.update(running=True, online="synthetic-unique-endpoint")
    s.store.mail_wake_event.clear()  # Ignore setup's employee/project commits.
    assert not s.store.mail_wake_event.is_set()
    assert s.act("connect")["state"] == "receiver_online"
    assert s.store.mail_wake_event.is_set(), "Recovered route must prompt pending-mail delivery"
    assert json.loads(path.read_text())["endpoints"][0] == other
    assert not any(c[0] == "/usr/bin/open" for c in s.calls)


@pytest.mark.parametrize("setup", ["app"], indirect=True)
def test_connected_socket_is_not_original_session_readiness(setup):
    s = setup
    s.act("install")
    s.state.update(running=True, online="synthetic-unique-endpoint", ready=False)
    s.store.mail_wake_event.clear()  # Only the connect action is under test.
    result = s.act("connect")
    assert result["state"] == "receiver_not_ready"
    assert not s.store.mail_wake_event.is_set(), "Unverified session must not trigger delivery"
    assert result["transport_online"] is True and result["receiver_online"] is False
    assert s.act()["state"] == "receiver_not_ready"
    s.state["ready"] = True
    result = s.act()
    assert result["state"] == "receiver_online"
    assert result["transport_online"] is True and result["receiver_online"] is True


def test_connected_socket_with_other_employee_route_is_not_ready(setup):
    s = setup
    s.act("install")
    s.state.update(running=True, online="synthetic-unique-endpoint")
    route = s.store.root / "wake/host-endpoints.json"
    route.write_text(
        json.dumps(
            {
                "version": 1,
                "endpoints": [
                    {
                        "binding_id": s.binding["id"],
                        "employee_id": "another-employee",
                        "host_kind": "codex",
                        "launcher_directory": str(s.ipc),
                    }
                ],
            }
        )
    )
    route.chmod(0o600)
    result = s.act()
    assert result["state"] == "connect_required"
    assert result["transport_online"] is True and result["receiver_online"] is False


@pytest.mark.parametrize(
    "fault",
    [
        "version",
        "assets_changed",
        "public_file",
        "manifest_link",
        "public_root",
        "dependency_version",
        "dependency_link",
    ],
)
def test_unsafe_or_unverified_installation_fails_closed(setup, fault):
    s = setup
    s.act("install")
    if fault == "version":
        s.state["version"] = "codex-cli unknown"
    elif fault == "assets_changed":
        (connection._installed(s.root) / "native.mjs").write_text("changed")
    elif fault == "public_file":
        (connection._installed(s.root) / "native.mjs").chmod(0o644)
    elif fault == "manifest_link":
        p = s.root / "codex.json"
        p.rename(s.root / "old.json")
        p.symlink_to(s.root / "old.json")
    elif fault == "public_root":
        s.root.chmod(0o755)
    elif fault == "dependency_version":
        (s.source / "node_modules/ws/package.json").write_text('{"version":"other"}')
    else:
        p = s.source / "node_modules/ws/link"
        p.symlink_to(s.source / "package.json")
    with pytest.raises(WorkbenchError):
        s.act("install")
    assert not any(c[0] == "/usr/bin/open" for c in s.calls)


@pytest.mark.parametrize("version", sorted(connection.TESTED_CLI_VERSIONS))
def test_install_pins_the_exact_tested_cli_version(setup, version):
    s = setup
    s.state["version"] = version
    s.act("install")
    wrapper = (connection._installed(s.root) / "codex-mailbox").read_text()
    assert f'if [ "$actual_version" != {shlex.quote(version)} ]; then' in wrapper
    subprocess.run(
        ["/bin/sh", "-n", str(connection._installed(s.root) / "codex-mailbox")], check=True
    )


def test_future_unvalidated_cli_version_cannot_install_receiver(setup):
    s = setup
    s.state["version"] = "codex-cli 0.164.0-alpha.unverified"
    with pytest.raises(WorkbenchError, match="codex_cli_version_unverified"):
        s.act("install")
    assert not s.root.exists()
    assert not any(call[0] == "/usr/bin/open" for call in s.calls)


@pytest.mark.parametrize("setup", ["app"], indirect=True)
def test_no_binding_cannot_be_replaced_with_new_chat(setup):
    s = setup
    set_binding(s.store, s.employee["id"], s.project["id"])
    assert s.act("install")["state"] == "binding_required"
    with pytest.raises(WorkbenchError, match="original_session_binding_required"):
        s.act("connect")
    assert not any(c[0] == "/usr/bin/open" for c in s.calls)


def test_maintenance_and_nonmember_cannot_install(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(s.store, "update_maintenance", lambda: {"paused": True})
    with pytest.raises(WorkbenchError, match="Resume"):
        s.act("install")
    assert not s.root.exists()
    monkeypatch.setattr(s.store, "update_maintenance", lambda: {"paused": False})
    s.store.remove_project_member(s.project["id"], s.employee["id"])
    with pytest.raises(WorkbenchError, match="active local Codex"):
        s.act("install")
    assert not s.root.exists()


def test_unsupported_platform_does_not_install(setup, monkeypatch):
    monkeypatch.setattr(connection.sys, "platform", "win32")
    assert setup.act()["state"] == "unsupported"
    with pytest.raises(WorkbenchError, match="macOS"):
        setup.act("install")
    assert not setup.root.exists()


@pytest.mark.parametrize("setup", ["app"], indirect=True)
def test_http_receiver_requires_owner_and_member_scope(setup):
    from agent_mailbox.workbench import WorkbenchHTTP
    from agent_mailbox.workbench_mail_sessions import create_session

    s = setup
    session = create_session(s.store, s.employee["id"], s.project["id"], "isolated HTTP")
    server = WorkbenchHTTP(s.store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(token, method="POST", **overrides):
        data = {
            "employee_id": s.employee["id"],
            "project_id": s.project["id"],
            "action": "install",
            **overrides,
        }
        path = "/api/workbench/mailbox/receivers/codex"
        if method == "GET":
            from urllib.parse import urlencode

            path += "?" + urlencode(data)
        req = urllib.request.Request(
            server.endpoint + path,
            method=method,
            data=json.dumps(data).encode() if method == "POST" else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    try:
        for token in ("", "wrong", session["token"]):
            for method in ("GET", "POST"):
                for action in ("install", "launch", "sessions", "bind", "configuration"):
                    code, body = request(
                        token, method, action=action, thread_id="injected", confirmed=True
                    )
                    assert code == 401 and body["error"]["code"] == "UNAUTHORIZED"
        assert s.calls == [] and not s.root.exists()
        code, body = request(server.token, "GET")
        assert code == 200 and body["state"] == "install_required"
        assert not s.root.exists(), "GET must ignore the action query parameter"
        other = s.store.create_project("Other synthetic project", s.store.root.parent)
        code, body = request(server.token, project_id=other["id"])
        assert code >= 400 and body["error"]["code"] == "RECEIVER_MEMBER_REQUIRED"
        assert not s.root.exists()
        code, body = request(server.token)
        assert code == 200 and body["installed"] and body["state"] == "ready_to_start"
        assert not any(c[0] == "/usr/bin/open" for c in s.calls)
        assert s.store.snapshot()["tasks"] == []
    finally:
        server.shutdown()
        thread.join(5)
        server.close()
    assert not thread.is_alive()


@pytest.mark.parametrize("setup", ["app"], indirect=True)
def test_unbound_launch_neither_routes_nor_creates_binding(setup):
    s = setup
    set_binding(s.store, s.employee["id"], s.project["id"])
    s.act("install")
    assert s.act("launch")["state"] == "launch_requested"
    assert connection.load_bindings(s.store.root) == []
    assert not (s.store.root / "wake/host-endpoints.json").exists()
    assert s.store.snapshot()["tasks"] == []


@pytest.mark.parametrize("setup", ["app"], indirect=True)
def test_rebinding_codex_prunes_old_endpoint_record(setup):
    s = setup
    s.act("install")
    s.act("connect")
    other_project = s.store.create_project("Other synthetic project", s.project["path"])
    s.store.add_project_member(other_project["id"], s.employee["id"])
    other = set_binding(
        s.store,
        s.employee["id"],
        other_project["id"],
        host_kind="codex",
        host_session_id="other-thread",
    )
    endpoint_path = s.store.root / "wake/host-endpoints.json"
    routes = json.loads(endpoint_path.read_text())
    routes["endpoints"].append(
        {
            "binding_id": other["id"],
            "employee_id": s.employee["id"],
            "host_kind": "codex",
            "launcher_directory": str(s.ipc),
        }
    )
    endpoint_path.write_text(json.dumps(routes))
    old_id = s.binding["id"]
    newer = set_binding(
        s.store, s.employee["id"], s.project["id"], host_kind="codex", host_session_id="new-thread"
    )
    assert newer["id"] != old_id
    s.act("connect")
    rows = connection.endpoints(s.store.root)
    assert [row["binding_id"] for row in rows] == [other["id"], newer["id"]]


@pytest.mark.parametrize("setup", ["app"], indirect=True)
def test_unbinding_codex_removes_only_its_endpoint_record(setup):
    s = setup
    s.act("install")
    s.act("connect")
    path = s.store.root / "wake/host-endpoints.json"
    other = {
        "binding_id": "other",
        "employee_id": "other",
        "host_kind": "deepseek",
        "socket_path": "/tmp/synthetic",
    }
    rows = json.loads(path.read_text())
    rows["endpoints"].append(other)
    path.write_text(json.dumps(rows))
    set_binding(s.store, s.employee["id"], s.project["id"])
    assert connection.endpoints(s.store.root) == [other]


@pytest.mark.parametrize("setup", ["app"], indirect=True)
def test_unbinding_failure_restores_endpoint_record(setup, monkeypatch):
    s = setup
    s.act("install")
    s.act("connect")
    path = s.store.root / "wake/host-endpoints.json"
    before = path.read_bytes()

    def reject(*_args, **_kwargs):
        raise ValueError("synthetic_binding_failure")

    monkeypatch.setattr("agent_mailbox.workbench_host_bindings._set_binding_locked", reject)
    with pytest.raises(ValueError, match="synthetic_binding_failure"):
        set_binding(s.store, s.employee["id"], s.project["id"])
    assert path.read_bytes() == before


@pytest.mark.parametrize("setup", ["app"], indirect=True)
def test_first_binding_requires_confirmation_loaded_thread_and_cas(setup, monkeypatch):
    s = setup
    s.act("install")
    s.state.update(running=True, online="synthetic")
    calls = []

    def sessions(endpoint, thread_id=None):
        assert endpoint == "synthetic"
        calls.append(thread_id)
        if thread_id == "gone":
            raise ValueError("receiver_session_not_loaded")
        return {
            "sessions": [{"id": thread_id or "new-thread", "name": "Synthetic", "cwd": "/test"}],
            "truncated": False,
        }

    monkeypatch.setattr(connection, "_sessions", sessions)

    def act(action, **params):
        return connection.connection(s.store, s.employee["id"], s.project["id"], action, **params)

    original = (s.store.root / "wake/host-bindings.json").read_bytes()
    listed = act("sessions")
    assert listed["binding_id"] == s.binding["id"]
    assert (s.store.root / "wake/host-bindings.json").read_bytes() == original
    for args in (
        {},
        {"confirmed": "true"},
        {"confirmed": True},
        {"confirmed": True, "expected_binding_id": None},
        {"confirmed": True, "expected_binding_id": s.binding["id"], "thread_id": "gone"},
    ):
        with pytest.raises(WorkbenchError):
            act("bind", **({"thread_id": "new-thread"} | args))
        assert (s.store.root / "wake/host-bindings.json").read_bytes() == original
    result = act(
        "bind", thread_id="new-thread", confirmed=True, expected_binding_id=s.binding["id"]
    )
    assert result["state"] == "connect_required"
    assert result["transport_online"] is True and result["receiver_online"] is False
    assert connection.load_bindings(s.store.root)[0]["host_session_id"] == "new-thread"
    assert s.act()["state"] == "connect_required"
    assert not (s.store.root / "wake/host-endpoints.json").exists()
    with pytest.raises(WorkbenchError, match="binding_changed"):
        act("bind", thread_id="new-thread", confirmed=True, expected_binding_id=s.binding["id"])
    assert s.act("connect")["state"] == "receiver_online"


@pytest.mark.parametrize("setup", ["app"], indirect=True)
def test_unbound_session_can_be_authorized_without_claiming_delivery(setup, monkeypatch):
    s = setup
    set_binding(s.store, s.employee["id"], s.project["id"])
    s.act("install")
    s.state.update(running=True, online="synthetic")
    monkeypatch.setattr(
        connection,
        "_sessions",
        lambda endpoint, thread_id: {
            "sessions": [{"id": thread_id, "name": "Synthetic", "cwd": "/test"}],
            "truncated": False,
        },
    )
    result = connection.connection(
        s.store,
        s.employee["id"],
        s.project["id"],
        "bind",
        thread_id="existing-thread",
        expected_binding_id=None,
        confirmed=True,
    )
    assert result["state"] == "connect_required"
    assert connection.load_bindings(s.store.root)[0]["host_session_id"] == "existing-thread"
    assert s.store.snapshot()["messages"] == []


def test_configuration_uses_registered_project_and_does_not_bind(setup, monkeypatch):
    s = setup
    set_binding(s.store, s.employee["id"], s.project["id"])
    s.act("install")
    s.state.update(running=True, online="synthetic")
    calls = []

    def inspect(endpoint, thread_id, project_path):
        calls.append((endpoint, thread_id, project_path))
        return {"state": "inspection_complete"}

    monkeypatch.setattr(connection, "_configuration", inspect)
    before = connection.load_bindings(s.store.root)
    result = connection.connection(
        s.store, s.employee["id"], s.project["id"], "configuration", thread_id="selected-thread"
    )
    assert result["state"] == "inspection_complete"
    assert calls == [("synthetic", "selected-thread", s.project["path"])]
    assert connection.load_bindings(s.store.root) == before
    assert not (s.store.root / "wake/host-endpoints.json").exists()
    assert s.store.snapshot()["messages"] == []


@pytest.mark.parametrize(
    "fault", ["none", "wrong_request", "wrong_thread", "private_extra", "incomplete"]
)
def test_private_control_socket_checks_identity_and_projects_response(fault):
    import socket
    import tempfile

    with tempfile.TemporaryDirectory(prefix="am-control-", dir="/private/tmp") as directory:
        endpoint = str(Path(directory) / "receiver.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(endpoint)
        listener.listen(1)
        errors = []

        def serve():
            try:
                stream, _ = listener.accept()
                with stream:
                    request = b""
                    while b"\n" not in request:
                        request += stream.recv(8192)
                    request = json.loads(request)
                    assert request["operation"] == "inspect" and request["thread_id"] == "expected"
                    item = {
                        "id": "wrong" if fault == "wrong_thread" else "expected",
                        "name": "Synthetic",
                        "cwd": "/test",
                    }
                    if fault == "private_extra":
                        item["preview"] = "PRIVATE CONTENT"
                    result = {
                        "protocol": request["protocol"],
                        "request_id": "wrong"
                        if fault == "wrong_request"
                        else request["request_id"],
                        "sessions": [item],
                        "truncated": False,
                    }
                    if fault != "incomplete":
                        stream.sendall(json.dumps(result).encode() + b"\n")
            except Exception as exc:  # noqa: BLE001 - capture any background server failure for the assertion thread
                errors.append(exc)
            finally:
                listener.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            if fault in {"wrong_request", "wrong_thread", "incomplete"}:
                with pytest.raises(ValueError):
                    connection._sessions(endpoint, "expected")
            else:
                result = connection._sessions(endpoint, "expected")
                assert result == {
                    "sessions": [{"id": "expected", "name": "Synthetic", "cwd": "/test"}],
                    "truncated": False,
                }
        finally:
            thread.join(3)
        assert not thread.is_alive() and errors == []
