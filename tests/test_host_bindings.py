"""Project/session routing and revocation with real isolated stores, no real hosts."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent_mailbox import workbench_host_bindings as bindings
from agent_mailbox import workbench_wake as wake
from agent_mailbox.workbench_mail_notifications import mark_host_received, pending_ids
from agent_mailbox.workbench_private import private_access
from agent_mailbox.workbench_store import WorkbenchStore


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = WorkbenchStore(tmp_path / "home")
    p1 = store.create_project("Private", tmp_path)
    p2 = store.create_project("Shared", tmp_path)
    employee = store.create_employee("Worker", "codex", p1["id"])
    store.add_project_member(p2["id"], employee["id"])
    monkeypatch.setattr(wake, "hook_command", lambda home: ["synthetic-host"])
    return store, employee["id"], p1["id"], p2["id"]


def bind(store, employee, project, session="session-one"):
    return bindings.set_binding(
        store, employee, project, host_kind="synthetic", host_session_id=session
    )


def mail(store, employee, project):
    return store.send_message(project, "Synthetic", "No private data", recipient_id=employee)["id"]


def adapter(store, calls, **overrides):
    def run(command, **kwargs):
        base = store.root / "wake"
        payload = json.loads((base / "wake-outbox" / f"{command[-1]}.json").read_text())
        calls.append(payload)
        binding = payload["binding"]
        receipt = {
            "protocol": "agent-mailbox.host-receipt.v1",
            "delivery_id": command[-1],
            "employee_id": command[-2],
            "message_ids": payload["fresh"],
            "status": "received",
            "binding_id": binding["id"],
            "project_id": binding["project_id"],
            "host_kind": binding["host_kind"],
            "host_session_id": binding["host_session_id"],
            **overrides,
        }
        (base / "receipts").mkdir(exist_ok=True)
        (base / "receipts" / command[-1]).write_text(json.dumps(receipt))
        return SimpleNamespace(returncode=0)

    return run


def sender(store, runner):
    return wake.hook_deliver(
        store.root,
        runner=runner,
        require_receipt=True,
        binding_resolver=lambda eid, mids: bindings.resolve_routes(store, eid, mids),
    )


def drain(store, employee, runner, now=10000):
    return wake.run_once(
        store,
        store.root,
        employee_ids=[employee],
        now=now,
        unread_provider=wake.unread_ids_for_employee,
        pending_provider=pending_ids,
        acknowledge=mark_host_received,
        deliver=sender(store, runner),
    )


def test_bind_replace_revoke_preserves_other_project_and_private_file(setup):
    store, employee, p1, p2 = setup
    first = bind(store, employee, p1)
    second = bind(store, employee, p2, "session-two")
    replacement = bind(store, employee, p1, "session-three")
    assert replacement["id"] != first["id"]
    assert bindings.load_bindings(store.root) == [second, replacement]
    assert private_access(store.root / "wake" / bindings.FILENAME, 0o600)
    bindings.set_binding(store, employee, p1)
    assert bindings.load_bindings(store.root) == [second]


def test_one_host_session_cannot_route_two_employee_mailboxes(setup):
    store, employee, p1, p2 = setup
    other = store.create_employee("Other", "codex", p2)
    first = bind(store, employee, p1, "shared-host-session")
    # The same employee may use one host session for several projects.
    second = bind(store, employee, p2, "shared-host-session")
    assert bindings.load_bindings(store.root) == [first, second]
    with pytest.raises(ValueError, match="host_session_already_owned"):
        bind(store, other["id"], p2, "shared-host-session")
    assert bindings.load_bindings(store.root) == [first, second]


def test_invalid_binding_cannot_replace_current_route(setup, monkeypatch):
    store, employee, p1, _ = setup
    original = bind(store, employee, p1)
    for session in (None, "", "\n", "x" * 513):
        with pytest.raises(ValueError):
            bind(store, employee, p1, session)
        assert bindings.load_bindings(store.root) == [original]
    monkeypatch.setattr(bindings.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        bind(store, employee, p1, "replacement")
    assert bindings.load_bindings(store.root) == [original]
    assert not list((store.root / "wake").glob(".host-bindings-*"))


def test_receiver_surface_metadata_is_codex_only_and_validated(setup):
    store, employee, p1, _ = setup
    with wake.state_lock(store.root):
        tagged = bindings._set_binding_locked(
            store,
            employee,
            p1,
            host_kind="codex",
            host_session_id="desktop-thread",
            receiver_surface="desktop",
        )
    assert bindings.load_bindings(store.root)[0]["receiver_surface"] == "desktop"
    path = store.root / "wake" / bindings.FILENAME
    original = path.read_bytes()
    for surface in ([], "unknown"):
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "bindings": [
                        {**tagged, "receiver_surface": surface},
                    ],
                }
            )
        )
        with pytest.raises(ValueError, match="Invalid receiver surface"):
            bindings.load_bindings(store.root)
    path.write_bytes(original)
    with wake.state_lock(store.root), pytest.raises(ValueError, match="Invalid receiver surface"):
        bindings._set_binding_locked(
            store,
            employee,
            p1,
            host_kind="synthetic",
            host_session_id="other",
            receiver_surface="desktop",
        )
    assert path.read_bytes() == original


def test_membership_required_and_rechecked_before_delivery(setup):
    store, employee, p1, _ = setup
    bind(store, employee, p1)
    mid = mail(store, employee, p1)
    store.remove_project_member(p1, employee)
    with pytest.raises(ValueError):
        bind(store, employee, p1)
    calls = []
    assert not sender(store, adapter(store, calls))(employee, {"fresh": [mid]})
    assert calls == []
    bindings.set_binding(
        store, employee, p1
    )  # Revocation remains possible after membership removal.
    assert bindings.load_bindings(store.root) == []


def test_corrupt_or_ambiguous_configuration_fails_closed(setup):
    store, employee, p1, _ = setup
    current = bind(store, employee, p1)
    path = store.root / "wake" / bindings.FILENAME
    for content in (
        "{",
        "[]",
        json.dumps({"version": 1, "bindings": [current, current]}),
        json.dumps({"version": 1, "bindings": [current, {**current, "project_id": "other"}]}),
        json.dumps(
            {
                "version": 1,
                "bindings": [
                    current,
                    {
                        **current,
                        "id": "another-binding",
                        "employee_id": "another-employee",
                        "project_id": "other",
                    },
                ],
            }
        ),
    ):
        path.write_text(content)
        with pytest.raises(ValueError):
            bind(store, employee, p1)
        assert path.read_text() == content


def test_project_batches_never_mix_and_partial_success_only_acks_confirmed(setup):
    store, employee, p1, p2 = setup
    bind(store, employee, p1)
    m1, m2 = mail(store, employee, p1), mail(store, employee, p2)
    calls = []
    assert drain(store, employee, adapter(store, calls))["woke"] == [employee]
    assert calls[0]["fresh"] == [m1]
    assert calls[0]["binding"]["project_id"] == p1
    assert pending_ids(store, employee) == [m2]
    bind(store, employee, p2, "session-two")
    assert not drain(store, employee, adapter(store, calls), now=10001)["woke"]  # Cooldown applies.
    assert drain(store, employee, adapter(store, calls), now=20000)["woke"] == [employee]
    assert calls[1]["fresh"] == [m2]
    assert calls[1]["binding"]["host_session_id"] == "session-two"
    assert not pending_ids(store, employee)
    assert not drain(WorkbenchStore(store.root), employee, adapter(store, calls), now=30000)["woke"]
    assert len(calls) == 2


def test_missing_or_revoked_binding_never_calls_host_then_rebind_recovers(setup):
    store, employee, p1, _ = setup
    mid = mail(store, employee, p1)
    calls = []
    for _ in range(2):
        assert not drain(store, employee, adapter(store, calls))["woke"]
    bind(store, employee, p1)
    bindings.set_binding(store, employee, p1)
    assert not drain(store, employee, adapter(store, calls))["woke"]
    assert calls == [] and pending_ids(store, employee) == [mid]
    bind(store, employee, p1)
    assert drain(store, employee, adapter(store, calls))["woke"] == [employee]
    assert len(calls) == 1


@pytest.mark.parametrize("field", ["binding_id", "project_id", "host_kind", "host_session_id"])
def test_wrong_binding_receipt_never_acks_or_retries(setup, field):
    store, employee, p1, _ = setup
    bind(store, employee, p1)
    mid = mail(store, employee, p1)
    calls = []
    for _ in range(3):
        assert not drain(store, employee, adapter(store, calls, **{field: "wrong"}))["woke"]
    assert pending_ids(store, employee) == [mid]
    assert len(calls) == 1
    assert (store.root / "wake/claims" / f"{mid}.unknown").exists()


def test_rebinding_cannot_use_old_receipt_after_ack_crash(setup):
    store, employee, p1, _ = setup
    bind(store, employee, p1)
    mid = mail(store, employee, p1)
    calls = []
    assert sender(store, adapter(store, calls))(employee, {"fresh": [mid]})
    bind(store, employee, p1, "new-session")
    assert not drain(store, employee, adapter(store, calls))["woke"]
    assert len(calls) == 1 and pending_ids(store, employee) == [mid]


def test_cli_home_routing_and_readonly_listing(setup, tmp_path):
    store, employee, p1, _ = setup
    prefix = [sys.executable, "-m", "agent_mailbox", "wake", "--home", str(store.root)]
    result = subprocess.run(
        prefix
        + [
            "--bind",
            employee,
            "--project",
            p1,
            "--host",
            "synthetic",
            "--session",
            "existing-session",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert bindings.load_bindings(store.root)[0]["host_session_id"] == "existing-session"
    result = subprocess.run(
        prefix + ["--unbind", employee, "--project", p1], check=False, capture_output=True
    )
    assert result.returncode == 0
    absent = tmp_path / "never-created"
    result = subprocess.run(
        [sys.executable, "-m", "agent_mailbox", "wake", "--bindings", "--home", str(absent)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0 and json.loads(result.stdout) == []
    assert not absent.exists()


@pytest.mark.parametrize("change", ["inactive", "remote"])
def test_ineligible_employee_cannot_bind_or_reuse_existing_route(setup, change):
    store, employee, p1, _ = setup
    bind(store, employee, p1)
    mid = mail(store, employee, p1)
    with store._transaction() as db:
        if change == "inactive":
            db.execute("UPDATE employees SET lifecycle='left' WHERE id=?", (employee,))
        else:
            # A real nonlocal device row preserves FK constraints.
            device = dict(db.execute("SELECT * FROM devices WHERE is_local=1").fetchone())
            device.update(id="node-remote", is_local=0)
            db.execute(
                f"INSERT INTO devices ({','.join(device)}) VALUES ({','.join('?' for _ in device)})",
                tuple(device.values()),
            )
            db.execute("UPDATE employees SET node_id='node-remote' WHERE id=?", (employee,))
    with pytest.raises(ValueError):
        bind(store, employee, p1)
    assert bindings.resolve_routes(store, employee, [mid]) == ([], [mid])


def test_two_bound_projects_in_one_round_have_independent_batches(setup):
    store, employee, p1, p2 = setup
    bind(store, employee, p1, "one")
    bind(store, employee, p2, "two")
    m1, m2 = mail(store, employee, p1), mail(store, employee, p2)
    calls = []
    assert drain(store, employee, adapter(store, calls))["woke"] == [employee]
    assert {p["binding"]["host_session_id"]: p["fresh"] for p in calls} == {
        "one": [m1],
        "two": [m2],
    }
    assert not pending_ids(store, employee)
