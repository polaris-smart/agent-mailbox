"""Opened mail can dismiss only its own pending Host alert, without a model."""

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from agent_mailbox.workbench import wake_redeem_loop
from agent_mailbox.workbench_host_bindings import set_binding
from agent_mailbox.workbench_host_retractions import (
    reconcile_opened_queue,
    status_counts,
    status_counts_file,
)
from agent_mailbox.workbench_host_transport import retract_dsh_queue
from agent_mailbox.workbench_mail_notifications import mark_host_received
from agent_mailbox.workbench_mail_sessions import create_session, invoke
from agent_mailbox.workbench_store import WorkbenchStore
from agent_mailbox.workbench_wake import cli_main, state_lock


def team(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Synthetic", str(tmp_path))
    employee = store.create_employee("Receiver", "deepseek", project["id"])
    session = create_session(store, employee["id"], project["id"], "Isolated")
    binding = set_binding(
        store,
        employee["id"],
        project["id"],
        host_kind="deepseek",
        host_session_id="existing-session",
    )
    return store, project, employee, session, binding


def prepare_delivery(store, employee, binding, messages):
    delivery_id = "wake-synthetic"
    wake = store.root / "wake"
    claims = wake / "claims"
    outbox = wake / "wake-outbox"
    receipts = wake / "receipts"
    claims.mkdir(parents=True, exist_ok=True)
    outbox.mkdir(parents=True, exist_ok=True)
    receipts.mkdir(parents=True, exist_ok=True)
    ids = [message["id"] for message in messages]
    payload = {
        "receipt_protocol": "agent-mailbox.host-receipt.v1",
        "delivery_id": delivery_id,
        "binding": binding,
        "employee_id": employee["id"],
        "fresh": ids,
    }
    outbox_file = outbox / f"{delivery_id}.json"
    outbox_file.write_text(json.dumps(payload))
    outbox_file.chmod(0o600)
    receipt = {
        "protocol": "agent-mailbox.host-receipt.v1",
        "delivery_id": delivery_id,
        "employee_id": employee["id"],
        "message_ids": ids,
        "binding_id": binding["id"],
        "project_id": binding["project_id"],
        "host_kind": binding["host_kind"],
        "host_session_id": binding["host_session_id"],
        "status": "received",
    }
    receipt_file = receipts / delivery_id
    receipt_file.write_text(json.dumps(receipt))
    receipt_file.chmod(0o600)
    for mid in ids:
        marker = claims / mid
        marker.write_text(delivery_id)
        marker.chmod(0o600)
        ok = Path(f"{marker}.ok")
        ok.touch()
        ok.chmod(0o600)
    mark_host_received(store, employee["id"], ids)
    return payload


def pending_retractions(store):
    with store._transaction(readonly=True) as db:
        return [
            tuple(row)
            for row in db.execute(
                "SELECT state_hash,status FROM notifications "
                "WHERE kind='mailbox_queue_retract' ORDER BY state_hash"
            ).fetchall()
        ]


def test_retraction_diagnostics_are_scoped_to_employee_and_project(tmp_path):
    store, project, employee, session, _ = team(tmp_path)
    other_path = tmp_path / "other"
    other_path.mkdir()
    other_project = store.create_project("Other", str(other_path))
    other_employee = store.create_employee("Other receiver", "deepseek", other_project["id"])
    other_session = create_session(store, other_employee["id"], other_project["id"], "Other")
    first = store.send_message(project["id"], "First", "Synthetic", recipient_id=employee["id"])
    second = store.send_message(
        other_project["id"], "Second", "Synthetic", recipient_id=other_employee["id"]
    )
    invoke(store, session["token"], "message_open", {"message_id": first["id"]})
    invoke(store, other_session["token"], "message_open", {"message_id": second["id"]})
    assert status_counts(store)["pending"] == 2
    assert status_counts(store, employee_id=employee["id"], project_id=project["id"]) == {
        "pending": 1,
    }
    assert status_counts(store, employee_id=employee["id"], project_id=other_project["id"]) == {}


def test_actual_cli_status_reads_existing_retraction_without_store_initialization(tmp_path, capsys):
    store, project, employee, session, _ = team(tmp_path)
    message = store.send_message(project["id"], "Opened", "Synthetic", recipient_id=employee["id"])
    invoke(store, session["token"], "message_open", {"message_id": message["id"]})
    before = {path.name: path.read_bytes() for path in store.directory.iterdir()}
    assert status_counts_file(store.root) == {"pending": 1}
    assert cli_main(["--status"], home=store.root, store=None) == 0
    assert "已读提醒撤队：待处理 1" in capsys.readouterr().out
    assert {path.name: path.read_bytes() for path in store.directory.iterdir()} == before


def test_cli_diagnostic_reads_uncheckpointed_wal_without_misreporting_zero(tmp_path):
    store, project, employee, session, _ = team(tmp_path)
    message = store.send_message(project["id"], "Opened", "Synthetic", recipient_id=employee["id"])
    invoke(store, session["token"], "message_open", {"message_id": message["id"]})
    with sqlite3.connect(store.db_path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            "UPDATE notifications SET status='unknown' WHERE kind='mailbox_queue_retract'"
        )
        writer.commit()
        assert (store.directory / "state.db-wal").exists()
        before = {path.name: path.read_bytes() for path in store.directory.iterdir()}
        assert status_counts_file(store.root) == {"unknown": 1}
        assert {path.name: path.read_bytes() for path in store.directory.iterdir()} == before


def test_cli_diagnostic_does_not_trust_a_symlinked_database(tmp_path):
    home = tmp_path / "home"
    directory = home / "workbench"
    directory.mkdir(parents=True, mode=0o700)
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"not a trusted store")
    (directory / "state.db").symlink_to(outside)
    assert status_counts_file(home) is None


def test_one_open_retracts_only_original_delivery_once(tmp_path, monkeypatch):
    store, project, employee, session, binding = team(tmp_path)
    message = store.send_message(project["id"], "One", "Synthetic", recipient_id=employee["id"])
    original = prepare_delivery(store, employee, binding, [message])
    calls = []
    monkeypatch.setattr(
        "agent_mailbox.workbench_host_transport.endpoint_for",
        lambda home, candidate: "/synthetic/private.sock",
    )
    monkeypatch.setattr(
        "agent_mailbox.workbench_host_transport.retract_dsh_queue",
        lambda endpoint, payload: calls.append((endpoint, payload)) or "retracted",
    )
    invoke(store, session["token"], "message_open", {"message_id": message["id"]})
    with state_lock(store.root):
        assert reconcile_opened_queue(store, store.root) == [(original["delivery_id"], "retracted")]
        assert reconcile_opened_queue(store, store.root) == []
    assert calls == [
        (
            "/synthetic/private.sock",
            {
                **original,
                "operation": "retract_opened",
                "receipt_protocol": "agent-mailbox.host-retraction.v1",
            },
        )
    ]
    assert pending_retractions(store) == [(message["id"], "retracted")]
    with store._transaction(readonly=True) as db:
        assert (
            db.execute(
                "SELECT status FROM notifications WHERE kind='mailbox_wake' AND state_hash=?",
                (message["id"],),
            ).fetchone()[0]
            == "host_received"
        )


def test_batch_waits_until_every_message_is_opened(tmp_path, monkeypatch):
    store, project, employee, session, binding = team(tmp_path)
    messages = [
        store.send_message(project["id"], str(n), "Synthetic", recipient_id=employee["id"])
        for n in (1, 2)
    ]
    prepare_delivery(store, employee, binding, messages)
    calls = []
    monkeypatch.setattr(
        "agent_mailbox.workbench_host_transport.endpoint_for",
        lambda home, candidate: "/synthetic/private.sock",
    )
    monkeypatch.setattr(
        "agent_mailbox.workbench_host_transport.retract_dsh_queue",
        lambda endpoint, payload: calls.append(payload) or "retracted",
    )
    invoke(store, session["token"], "message_open", {"message_id": messages[0]["id"]})
    with state_lock(store.root):
        assert reconcile_opened_queue(store, store.root) == []
    assert calls == []
    invoke(store, session["token"], "message_open", {"message_id": messages[1]["id"]})
    with state_lock(store.root):
        assert reconcile_opened_queue(store, store.root) == [("wake-synthetic", "retracted")]
    assert len(calls) == 1
    assert {status for _, status in pending_retractions(store)} == {"retracted"}


def test_open_before_host_admission_needs_no_retraction(tmp_path, monkeypatch):
    store, project, employee, session, _ = team(tmp_path)
    message = store.send_message(project["id"], "Early", "Synthetic", recipient_id=employee["id"])
    monkeypatch.setattr(
        "agent_mailbox.workbench_host_transport.retract_dsh_queue",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not call host")),
    )
    invoke(store, session["token"], "message_open", {"message_id": message["id"]})
    with state_lock(store.root):
        assert reconcile_opened_queue(store, store.root) == [(message["id"], "not_needed")]
    assert pending_retractions(store) == [(message["id"], "not_needed")]


def test_retraction_never_uses_old_delivery_wire_protocol(tmp_path):
    store, project, employee, _, binding = team(tmp_path)
    message = store.send_message(
        project["id"], "Version fence", "Synthetic", recipient_id=employee["id"]
    )
    old_delivery = prepare_delivery(store, employee, binding, [message])
    with pytest.raises(ValueError, match="invalid_retraction_envelope"):
        retract_dsh_queue(tmp_path / "absent.sock", {**old_delivery, "operation": "retract_opened"})


def test_uncertain_batch_response_stays_unknown_without_second_removal(
    tmp_path, monkeypatch, capsys
):
    store, project, employee, session, binding = team(tmp_path)
    messages = [
        store.send_message(project["id"], str(n), "Synthetic", recipient_id=employee["id"])
        for n in (1, 2)
    ]
    prepare_delivery(store, employee, binding, messages)
    monkeypatch.setattr(
        "agent_mailbox.workbench_host_transport.endpoint_for",
        lambda home, candidate: "/synthetic/private.sock",
    )
    calls = []

    def lost_reply(endpoint, payload):
        calls.append(payload["delivery_id"])
        raise TimeoutError("response lost after Host may have removed the item")

    monkeypatch.setattr("agent_mailbox.workbench_host_transport.retract_dsh_queue", lost_reply)
    for message in messages:
        invoke(store, session["token"], "message_open", {"message_id": message["id"]})
    with state_lock(store.root):
        assert reconcile_opened_queue(store, store.root) == [("wake-synthetic", "unknown")]
        assert reconcile_opened_queue(store, store.root) == []
    assert calls == ["wake-synthetic"]
    assert {status for _, status in pending_retractions(store)} == {"unknown"}
    assert cli_main(["--status"], home=store.root, store=store) == 0
    assert "已读提醒撤队：待处理 0 · 结果不确定 2" in capsys.readouterr().out


def test_changed_host_binding_cannot_retract_old_session_queue(tmp_path, monkeypatch):
    store, project, employee, session, binding = team(tmp_path)
    message = store.send_message(
        project["id"], "Old session", "Synthetic", recipient_id=employee["id"]
    )
    prepare_delivery(store, employee, binding, [message])
    invoke(store, session["token"], "message_open", {"message_id": message["id"]})
    set_binding(
        store,
        employee["id"],
        project["id"],
        host_kind="deepseek",
        host_session_id="replacement-session",
    )
    monkeypatch.setattr(
        "agent_mailbox.workbench_host_transport.retract_dsh_queue",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not call old Host")),
    )
    with state_lock(store.root):
        assert reconcile_opened_queue(store, store.root) == [("wake-synthetic", "binding_changed")]
    assert pending_retractions(store) == [(message["id"], "binding_changed")]


def test_symlinked_claim_cannot_redirect_retraction(tmp_path, monkeypatch):
    store, project, employee, session, binding = team(tmp_path)
    message = store.send_message(
        project["id"], "Untrusted marker", "Synthetic", recipient_id=employee["id"]
    )
    prepare_delivery(store, employee, binding, [message])
    claim = store.root / "wake" / "claims" / message["id"]
    claim.unlink()
    claim.symlink_to(tmp_path / "outside")
    monkeypatch.setattr(
        "agent_mailbox.workbench_host_transport.retract_dsh_queue",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not call Host")),
    )
    invoke(store, session["token"], "message_open", {"message_id": message["id"]})
    with state_lock(store.root):
        assert reconcile_opened_queue(store, store.root) == [(message["id"], "unknown")]
    assert pending_retractions(store) == [(message["id"], "unknown")]


def test_changed_original_receipt_cannot_authorize_retraction(tmp_path, monkeypatch):
    store, project, employee, session, binding = team(tmp_path)
    message = store.send_message(
        project["id"], "Tampered receipt", "Synthetic", recipient_id=employee["id"]
    )
    prepare_delivery(store, employee, binding, [message])
    receipt = store.root / "wake" / "receipts" / "wake-synthetic"
    value = json.loads(receipt.read_text())
    value["message_ids"] = ["another_message"]
    receipt.write_text(json.dumps(value))
    monkeypatch.setattr(
        "agent_mailbox.workbench_host_transport.retract_dsh_queue",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not call Host")),
    )
    invoke(store, session["token"], "message_open", {"message_id": message["id"]})
    with state_lock(store.root):
        assert reconcile_opened_queue(store, store.root) == [(message["id"], "unknown")]
    assert pending_retractions(store) == [(message["id"], "unknown")]


def test_open_commit_signals_retraction_worker_without_timer(tmp_path, monkeypatch):
    store, project, employee, session, binding = team(tmp_path)
    message = store.send_message(
        project["id"], "Busy Host", "Synthetic", recipient_id=employee["id"]
    )
    prepare_delivery(store, employee, binding, [message])
    entered, removed, completed, stop = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    from agent_mailbox import workbench_host_retractions

    original = workbench_host_retractions.reconcile_opened_queue

    def observed_reconcile(store, home):
        entered.set()
        result = original(store, home)
        if any(status == "retracted" for _, status in result):
            completed.set()
        return result

    monkeypatch.setattr(workbench_host_retractions, "reconcile_opened_queue", observed_reconcile)
    monkeypatch.setattr(
        "agent_mailbox.workbench_host_transport.endpoint_for",
        lambda home, candidate: "/synthetic/private.sock",
    )
    monkeypatch.setattr(
        "agent_mailbox.workbench_host_transport.retract_dsh_queue",
        lambda endpoint, payload: removed.set() or "retracted",
    )
    thread = threading.Thread(
        target=wake_redeem_loop,
        args=(store, store.root),
        kwargs={"interval": None, "stop": stop},
        daemon=True,
    )
    thread.start()
    try:
        assert entered.wait(3), "worker must reach its event wait"
        invoke(store, session["token"], "message_open", {"message_id": message["id"]})
        assert removed.wait(3), "open commit must wake the worker for exact retraction"
        assert completed.wait(3)
        assert pending_retractions(store) == [(message["id"], "retracted")]
    finally:
        stop.set()
        store.mail_wake_event.set()
        thread.join(3)
    assert not thread.is_alive()
