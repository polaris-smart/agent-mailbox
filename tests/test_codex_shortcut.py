"""Persistent launch icon: isolated files; never opens the real host."""

import json
import os
import plistlib
import shlex
import subprocess
import sys

import pytest

from agent_mailbox import workbench_codex_connection as receiver
from agent_mailbox import workbench_codex_shortcut as shortcut
from agent_mailbox.workbench_host_bindings import set_binding
from agent_mailbox.workbench_store import WorkbenchStore

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS native launcher compiler")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = WorkbenchStore(tmp_path / "home's $(not-a-command)")
    project = store.create_project("Synthetic shortcut", tmp_path)
    employee = store.create_employee(
        "Synthetic Codex", "codex", project["id"], connection_type="app"
    )
    root = store.directory / "receivers"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(receiver, "_paths", lambda home: (root, tmp_path / "ipc"))
    monkeypatch.setattr(receiver, "_installed", lambda root: root)
    binary = tmp_path / "binary's $(not-a-command)"
    captured = tmp_path / "captured.json"
    script = "import json,sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:])); print('receiver_online')"
    binary.write_text(
        "#!/bin/sh\nexec " + shlex.join([sys.executable, "-c", script, str(captured)]) + ' "$@"\n'
    )
    binary.chmod(0o700)
    monkeypatch.setattr(shortcut, "_command", lambda: str(binary))
    calls = []
    real = receiver._run

    def run(args):
        calls.append(args)
        if args[0] == "/usr/bin/open":
            assert args[1] == "-R" and len(args) == 3
            return None
        assert args[0] in {"/usr/bin/osacompile", "/usr/bin/codesign"}
        return real(args)

    monkeypatch.setattr(receiver, "_run", run)

    def act(**kwargs):
        return shortcut.shortcut(store, employee["id"], project["id"], **kwargs)

    return store, project, employee, calls, act


def test_real_native_bundle_creation_private_idempotent_reveal_and_no_business_changes(setup):
    store, project, employee, calls, act = setup
    before = store.snapshot()
    assert act() == {"available": False, "state": "shortcut_missing"}
    assert act(create=True, reveal=True) == {"available": True, "state": "shortcut_ready"}
    directory = shortcut._directory(store, employee["id"], project["id"])
    app = shortcut._verify(directory, employee["id"], project["id"], store.root)
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    assert info["CFBundleName"] == "Codex · Mailbox" and info["LSUIElement"] is False
    subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
        check=True,
        capture_output=True,
    )
    source = subprocess.check_output(
        ["/usr/bin/osadecompile", str(app / "Contents/Resources/Scripts/main.scpt")], text=True
    )
    assert "codex-receiver-start" in source and "--home" in source
    assert "CODEX_CLI_PATH" not in source and "token" not in source
    subprocess.run(
        ["/usr/bin/osascript", str(app / "Contents/Resources/Scripts/main.scpt")],
        check=True,
        capture_output=True,
        timeout=15,
    )
    assert json.loads((store.root.parent / "captured.json").read_text()) == [
        "codex-receiver-start",
        "--home",
        str(store.root),
        "--employee",
        employee["id"],
    ]
    assert not list(store.root.rglob("not-a-command"))
    record = (directory / "entry.json").read_bytes()
    assert act(create=True, reveal=True)["available"]
    assert (directory / "entry.json").read_bytes() == record
    assert sum(c[0] == "/usr/bin/osacompile" for c in calls) == 1
    assert sum(c[0] == "/usr/bin/open" for c in calls) == 2
    subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
        check=True,
        capture_output=True,
    )
    assert store.snapshot() == before
    assert not (store.root / "wake/host-endpoints.json").exists()


def test_one_employee_uses_one_shortcut_across_two_projects(setup):
    store, project, employee, calls, act = setup
    other = store.create_project("Second project", project["path"])
    store.add_project_member(other["id"], employee["id"])
    assert act(create=True)["available"]
    assert shortcut.shortcut(store, employee["id"], other["id"], create=True)["available"]
    assert shortcut._directory(store, employee["id"], project["id"]) == shortcut._directory(
        store, employee["id"], other["id"]
    )
    assert sum(call[0] == "/usr/bin/osacompile" for call in calls) == 1


def test_employee_scoped_start_selects_a_current_binding(setup, monkeypatch, capsys):
    store, project, employee, _, _ = setup
    set_binding(
        store, employee["id"], project["id"], host_kind="codex", host_session_id="original-thread"
    )
    other = store.create_project("Second project", project["path"])
    store.add_project_member(other["id"], employee["id"])
    set_binding(
        store, employee["id"], other["id"], host_kind="codex", host_session_id="second-thread"
    )
    calls = []
    monkeypatch.setattr(
        receiver,
        "connection",
        lambda s, e, p, action: (
            calls.append((e, p, action)) or {"state": "launch_requested", "receiver_online": False}
        ),
    )
    args = ["--home", str(store.root), "--employee", employee["id"]]
    assert shortcut.main(args) == 0
    assert capsys.readouterr().out.strip() == "launch_requested"
    assert calls == [(employee["id"], min(project["id"], other["id"]), "launch")]
    with store._transaction() as db:
        db.execute(
            "DELETE FROM memberships WHERE employee_id=? AND project_id=?",
            (employee["id"], project["id"]),
        )
    assert shortcut.main(args) == 0
    assert capsys.readouterr().out.strip() == "launch_requested"
    assert calls[-1] == (employee["id"], other["id"], "launch")
    with store._transaction() as db:
        db.execute("DELETE FROM memberships WHERE employee_id=?", (employee["id"],))
    assert shortcut.main(args) == 2
    assert capsys.readouterr().out.strip() == "receiver_start_unconfirmed"
    assert len(calls) == 2


def test_employee_scoped_start_prefers_existing_desktop_route(setup, monkeypatch, capsys):
    store, project, employee, _, _ = setup
    other = store.create_project("Second project", project["path"])
    store.add_project_member(other["id"], employee["id"])
    projects = sorted((project["id"], other["id"]))
    first = set_binding(
        store, employee["id"], projects[0], host_kind="codex", host_session_id="not-connected"
    )
    second = set_binding(
        store, employee["id"], projects[1], host_kind="codex", host_session_id="connected-thread"
    )
    route = store.root / "wake/host-endpoints.json"
    route.write_text(
        json.dumps(
            {
                "version": 1,
                "endpoints": [
                    {
                        "binding_id": second["id"],
                        "employee_id": employee["id"],
                        "host_kind": "codex",
                        "launcher_directory": str(receiver._paths(store.root)[1]),
                    }
                ],
            }
        )
    )
    route.chmod(0o600)
    calls = []
    monkeypatch.setattr(
        receiver,
        "connection",
        lambda s, e, p, action: (
            calls.append((e, p, action)) or {"state": "receiver_online", "receiver_online": True}
        ),
    )
    assert shortcut.main(["--home", str(store.root), "--employee", employee["id"]]) == 0
    assert capsys.readouterr().out.strip() == "receiver_online"
    assert calls == [(employee["id"], second["project_id"], "launch")]
    assert first["project_id"] != second["project_id"]


@pytest.mark.parametrize("fault", ["changed", "symlink", "public", "extra", "hardlink", "parent"])
def test_tampering_never_reveals_or_replaces_an_untrusted_icon(setup, fault):
    store, project, employee, calls, act = setup
    act(create=True)
    directory = shortcut._directory(store, employee["id"], project["id"])
    app = shortcut._verify(directory, employee["id"], project["id"], store.root)
    target = app / "Contents/Info.plist"
    if fault == "changed":
        target.write_bytes(target.read_bytes() + b" ")
    elif fault == "symlink":
        other = directory / "other"
        target.rename(other)
        target.symlink_to(other)
    elif fault == "public":
        target.chmod(0o644)
    elif fault == "extra":
        (app / "extra").write_text("unowned")
        (app / "extra").chmod(0o600)
    elif fault == "parent":
        app.parent.chmod(0o777)
    else:
        os.link(target, directory / "linked")
    count = len(calls)
    with pytest.raises(ValueError):
        act(create=True, reveal=True)
    assert len(calls) == count


def test_lost_descriptor_write_recovers_exact_owned_bundle_without_recompile(setup, monkeypatch):
    store, project, employee, calls, act = setup
    original = receiver._atomic

    def fail(path, data):
        if path.name == "entry.json":
            raise OSError("synthetic interrupted publication")
        return original(path, data)

    monkeypatch.setattr(receiver, "_atomic", fail)
    with pytest.raises(OSError):
        act(create=True)
    monkeypatch.setattr(receiver, "_atomic", original)
    assert act(create=True, reveal=True)["available"]
    assert sum(c[0] == "/usr/bin/osacompile" for c in calls) == 1
    directory = shortcut._directory(store, employee["id"], project["id"])
    assert shortcut._verify(directory, employee["id"], project["id"], store.root).is_dir()


def test_moved_mailbox_binary_creates_new_entry_and_keeps_prior_bundle(setup, monkeypatch):
    store, project, employee, _, act = setup
    act(create=True)
    directory = shortcut._directory(store, employee["id"], project["id"])
    before = shortcut._verify(directory, employee["id"], project["id"], store.root)
    monkeypatch.setattr(shortcut, "_command", lambda: "/synthetic-new/app")
    act(create=True)
    after = shortcut._verify(directory, employee["id"], project["id"], store.root)
    assert before != after and before.is_dir() and after.is_dir()
    assert (
        plistlib.loads((before / "Contents/Info.plist").read_bytes())["CFBundleIdentifier"]
        != plistlib.loads((after / "Contents/Info.plist").read_bytes())["CFBundleIdentifier"]
    )


def test_copied_home_cannot_reveal_an_icon_targeting_the_original_home(setup):
    store, project, employee, _, act = setup
    act(create=True)
    directory = shortcut._directory(store, employee["id"], project["id"])
    with pytest.raises(ValueError, match="record_invalid"):
        shortcut._verify(
            directory, employee["id"], project["id"], store.root.parent / "restored-home"
        )


def test_current_membership_and_confirmation_required_on_owner_action(setup):
    store, project, employee, calls, _ = setup
    with pytest.raises(Exception, match="explicit_receiver_authorization"):
        receiver.connection(store, employee["id"], project["id"], "shortcut")
    with store._transaction() as db:
        db.execute("DELETE FROM memberships WHERE employee_id=?", (employee["id"],))
    with pytest.raises(Exception, match="active local Codex"):
        receiver.connection(store, employee["id"], project["id"], "shortcut", confirmed=True)
    assert calls == []


def test_start_command_reuses_existing_launch_only_and_does_not_initialize_missing_home(
    setup, monkeypatch, capsys
):
    store, project, employee, _, _ = setup
    calls = []

    def launch(s, employee_id, project_id, action):
        calls.append((s.root, employee_id, project_id, action))
        return {"state": "restart_required", "receiver_online": False}

    monkeypatch.setattr(receiver, "connection", launch)
    args = ["--home", str(store.root), "--employee", employee["id"], "--project", project["id"]]
    assert shortcut.main(args) == 0
    assert capsys.readouterr().out.strip() == "restart_required"
    assert calls == [(store.root, employee["id"], project["id"], "launch")]
    absent = store.root.parent / "absent"
    args[1] = str(absent)
    assert shortcut.main(args) == 2 and not absent.exists()
    assert capsys.readouterr().out.strip() == "receiver_start_unconfirmed"


def test_source_runtime_does_not_offer_a_fragile_development_shortcut(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    with pytest.raises(ValueError, match="requires_packaged_app"):
        shortcut._command()
