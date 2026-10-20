"""The release wake gate is scoped to one employee and one project."""

import json
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from agent_mailbox import workbench_wake_gate
from agent_mailbox.cli import _dispatch
from agent_mailbox.workbench_host_bindings import set_binding
from agent_mailbox.workbench_mail_notifications import enqueue, mark_host_received
from agent_mailbox.workbench_store import WorkbenchStore


def _check(home, employee_id, project_id, capsys):
    code = _dispatch(
        [
            "wake-check",
            "--home",
            str(home),
            "--employee",
            employee_id,
            "--project",
            project_id,
            "--json",
        ]
    )
    return code, json.loads(capsys.readouterr().out)


def test_gate_requires_exact_route_and_host_receipt(tmp_path, monkeypatch, capsys):
    store = WorkbenchStore(tmp_path / "home")
    first = store.create_project("First", tmp_path)
    second = store.create_project("Second", tmp_path)
    employee = store.create_employee("Worker", "codex", first["id"])
    store.add_project_member(second["id"], employee["id"])
    route = set_binding(
        store, employee["id"], first["id"], host_kind="codex", host_session_id="first-thread"
    )
    endpoint = store.root / "wake" / "host-endpoints.json"
    endpoint.write_text(
        json.dumps(
            {
                "version": 1,
                "endpoints": [
                    {
                        "binding_id": route["id"],
                        "employee_id": employee["id"],
                        "host_kind": "codex",
                        "socket_path": "/private/tmp/am-wake-gate-synthetic.sock",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    endpoint.chmod(0o600)
    monkeypatch.setattr(workbench_wake_gate, "receiver_online", lambda _home, _route: True)
    with store._transaction() as db:
        enqueue(db, "first-mail", employee["id"], first["id"])
        enqueue(db, "other-project-mail", employee["id"], second["id"])
        db.execute(
            "UPDATE notifications SET created_at='2020-01-01T00:00:00+00:00' "
            "WHERE state_hash='other-project-mail'"
        )
    mark_host_received(store, employee["id"], ["first-mail"])

    code, result = _check(store.root, employee["id"], first["id"], capsys)
    assert code == 0, result
    assert result["verdict"] == "pass"
    assert result["fresh_original_session_probe_verified"] is False
    assert result["receipt_scope"] == "historical_employee_project"
    assert all(item["passed"] for item in result["checks"].values())
    assert result["host_receipts"] == 1

    code, result = _check(store.root, employee["id"], second["id"], capsys)
    assert code == 1
    assert result["checks"]["G1_declared"]["passed"] is False
    assert result["checks"]["G2_listener"]["passed"] is False
    assert result["checks"]["G5_receipt"]["passed"] is False
    assert result["host_receipts"] == 0
    assert result["checks"]["G4_backlog"]["passed"] is False


def test_gate_reports_held_without_releasing_it(tmp_path, capsys):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Synthetic", tmp_path)
    employee = store.create_employee("Worker", "codex", project["id"])
    with store._transaction() as db:
        enqueue(db, "old-mail", employee["id"], project["id"])
        db.execute(
            "UPDATE notifications SET status='reauthorization_held' WHERE state_hash='old-mail'"
        )
    before = (store.root / "workbench" / "state.db").stat().st_mtime_ns
    code, result = _check(store.root, employee["id"], project["id"], capsys)
    assert code == 1
    assert result["checks"]["G4_backlog"]["passed"] is False
    assert result["held"] == 1
    with store._transaction(readonly=True) as db:
        assert (
            db.execute("SELECT status FROM notifications WHERE state_hash='old-mail'").fetchone()[
                "status"
            ]
            == "reauthorization_held"
        )
    assert (store.root / "workbench" / "state.db").stat().st_mtime_ns == before


def test_gate_distinguishes_fresh_pending_from_stale_backlog(tmp_path, capsys):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Synthetic", tmp_path)
    employee = store.create_employee("Worker", "codex", project["id"])
    with store._transaction() as db:
        enqueue(db, "fresh-mail", employee["id"], project["id"])
        enqueue(db, "older-mail", employee["id"], project["id"])
        db.execute(
            "UPDATE notifications SET created_at='2020-01-01T00:00:00+00:00' "
            "WHERE state_hash='older-mail'"
        )
    code, result = _check(store.root, employee["id"], project["id"], capsys)
    assert code == 1
    assert result["pending"] == 2
    assert result["checks"]["G4_backlog"]["passed"] is False
    with store._transaction() as db:
        db.execute(
            "UPDATE notifications SET status='recipient_opened' WHERE state_hash='older-mail'"
        )
    code, result = _check(store.root, employee["id"], project["id"], capsys)
    assert code == 1  # No route or host receipt, even though the backlog is fresh.
    assert result["pending"] == 1
    assert result["checks"]["G4_backlog"]["passed"] is True
    assert result["checks"]["G5_receipt"]["passed"] is False


def test_gate_missing_home_is_indeterminate_and_creates_nothing(tmp_path, capsys):
    home = tmp_path / "absent"
    code, result = _check(home, "employee_missing", "project_missing", capsys)
    assert code == 2
    assert result["verdict"] == "indeterminate"
    assert not home.exists()


def test_gate_does_not_disclose_invalid_endpoint_content(tmp_path, capsys):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Synthetic", tmp_path)
    employee = store.create_employee("Worker", "codex", project["id"])
    set_binding(store, employee["id"], project["id"], host_kind="codex", host_session_id="thread")
    endpoint = store.root / "wake" / "host-endpoints.json"
    endpoint.write_text("private host content is invalid JSON", encoding="utf-8")
    endpoint.chmod(0o600)
    code, result = _check(store.root, employee["id"], project["id"], capsys)
    assert code == 2
    assert result == {"verdict": "indeterminate", "reason": "check_unconfirmed"}


@pytest.mark.parametrize(
    ("gateway_available", "route_available", "consistent"),
    [(True, False, False), (False, True, False), (True, True, True)],
)
def test_hermes_listener_and_exact_route_are_separate_checks(
    tmp_path, capsys, gateway_available, route_available, consistent
):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Synthetic", tmp_path)
    employee = store.create_employee("Hermes", "hermes", project["id"])
    binding = set_binding(
        store, employee["id"], project["id"], host_kind="hermes", host_session_id="original"
    )
    with tempfile.TemporaryDirectory(prefix="am-gate-", dir="/private/tmp") as directory:
        endpoint = Path(directory) / "receiver.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(endpoint))
        endpoint.chmod(0o600)
        listener.listen(1)
        config = store.root / "wake" / "host-endpoints.json"
        config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "endpoints": [
                        {
                            "binding_id": binding["id"],
                            "employee_id": employee["id"],
                            "host_kind": "hermes",
                            "socket_path": str(endpoint),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        config.chmod(0o600)
        errors = []

        def answer():
            try:
                channel, _ = listener.accept()
                with channel:
                    channel.recv(8192)
                    channel.sendall(
                        json.dumps(
                            {
                                "protocol": "agent-mailbox.host-health.v1",
                                "host_kind": "hermes",
                                "gateway_injection_available": gateway_available,
                                "route_available": route_available,
                            }
                        ).encode()
                        + b"\n"
                    )
            except Exception as exc:  # noqa: BLE001 — surface socket worker failures
                errors.append(exc)

        worker = threading.Thread(target=answer, daemon=True)
        worker.start()
        try:
            code, result = _check(store.root, employee["id"], project["id"], capsys)
        finally:
            listener.close()
            worker.join(3)
        assert result["checks"]["G1_declared"]["passed"] is True
        assert result["checks"]["G2_listener"]["passed"] is True
        assert result["checks"]["G3_consistent"]["passed"] is consistent
        assert not worker.is_alive() and not errors
        assert code == 1  # A health check alone is not a fresh host receipt.
