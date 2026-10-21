"""Real store/session/delivery/wake boundaries, isolated homes and no models."""

import asyncio
import http.client as http_client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_mailbox.workbench_host_activity import PROTOCOL as HOST_ACTIVITY_PROTOCOL
from agent_mailbox.workbench_host_activity import start_host_activity_listener
from agent_mailbox.workbench_host_bindings import load_bindings, resolve_routes, set_binding
from agent_mailbox.workbench_mail_notifications import mark_host_received, pending_ids, ready_ids
from agent_mailbox.workbench_mail_sessions import create_session, invoke
from agent_mailbox.workbench_mail_tasks import accept_mail_task, create_mail_task, submit_mail_task
from agent_mailbox.workbench_store import SCHEMA_VERSION, WorkbenchError, WorkbenchStore
from agent_mailbox.workbench_wake import (
    DEFAULT_COOLDOWN_SECONDS,
    hook_deliver,
    load_state,
    run_once,
    save_state,
    state_lock,
    unread_ids_for_employee,
)


@pytest.fixture
def team(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Synthetic", tmp_path)
    worker = store.create_employee("Worker", "codex", project["id"])
    reviewer = store.create_employee("Reviewer", "claude", project["id"])
    session = create_session(store, worker["id"], project["id"], "Test")
    for employee in (worker, reviewer):
        set_binding(
            store,
            employee["id"],
            project["id"],
            host_kind="synthetic",
            host_session_id="synthetic-session-" + employee["id"],
        )
    return store, project, worker, reviewer, session


def test_mail_task_request_replay_creates_one_task_and_one_message(team):
    store, project, worker, _, _ = team
    first = create_mail_task(
        store, project["id"], "Read", "One document", worker["id"], request_id="mail-handoff-001"
    )
    second = create_mail_task(
        store, project["id"], "Read", "One document", worker["id"], request_id="mail-handoff-001"
    )
    assert second["id"] == first["id"]
    assert second["request_message_id"] == first["request_message_id"]
    assert len(store.snapshot()["tasks"]) == 1
    assert len(store.list_messages(project["id"])) == 1
    with pytest.raises(WorkbenchError) as error:
        create_mail_task(
            store,
            project["id"],
            "Read",
            "Different document",
            worker["id"],
            request_id="mail-handoff-001",
        )
    assert error.value.code == "TASK_REQUEST_CONFLICT"


def test_employee_wake_ids_exclude_revoked_project_but_keep_other_project(team, tmp_path):
    store, first_project, worker, _, _ = team
    second_path = tmp_path / "second-project"
    second_path.mkdir()
    second_project = store.create_project("Second", second_path)
    store.add_project_member(second_project["id"], worker["id"])
    first = store.send_message(first_project["id"], "First", "Synthetic", recipient_id=worker["id"])
    second = store.send_message(
        second_project["id"], "Second", "Synthetic", recipient_id=worker["id"]
    )
    assert set(unread_ids_for_employee(store, worker["id"])) == {first["id"], second["id"]}

    store.remove_project_member(first_project["id"], worker["id"])
    assert unread_ids_for_employee(store, worker["id"]) == [second["id"]]
    assert pending_ids(store, worker["id"]) == [second["id"]]


def test_one_employee_receives_two_projects_through_correct_project_bindings(team, tmp_path):
    """One agent can share a host session without leaking a project's mail to another route."""
    store, first_project, worker, _, _ = team
    second_path = tmp_path / "second-project"
    second_path.mkdir()
    second_project = store.create_project("Second", second_path)
    store.add_project_member(second_project["id"], worker["id"])
    first_binding = next(
        binding for binding in load_bindings(store.root) if binding["employee_id"] == worker["id"]
    )
    second_binding = set_binding(
        store,
        worker["id"],
        second_project["id"],
        host_kind="synthetic",
        host_session_id=first_binding["host_session_id"],
    )
    first = store.send_message(first_project["id"], "First", "Synthetic", recipient_id=worker["id"])
    second = store.send_message(
        second_project["id"], "Second", "Synthetic", recipient_id=worker["id"]
    )
    install_hook(store)
    delivered = []

    def host(command, **kwargs):
        payload = json.loads(
            (store.root / "wake/wake-outbox" / (command[-1] + ".json")).read_text()
        )
        delivered.append(
            (payload["binding"]["id"], payload["binding"]["project_id"], tuple(payload["fresh"]))
        )
        return host_receipt(store, command)

    drain(store, worker["id"], runner=host, event_driven=True)
    assert set(delivered) == {
        (first_binding["id"], first_project["id"], (first["id"],)),
        (second_binding["id"], second_project["id"], (second["id"],)),
    }
    assert pending_ids(store, worker["id"]) == []


def drain(store, employee, runner=None, now=10000, event_driven=False):
    return run_once(
        store,
        store.root,
        employee_ids=[employee],
        now=now,
        unread_provider=unread_ids_for_employee,
        pending_provider=pending_ids,
        acknowledge=mark_host_received,
        event_driven=event_driven,
        deliver=hook_deliver(
            store.root,
            runner=runner,
            require_receipt=True,
            binding_resolver=lambda eid, mids: resolve_routes(store, eid, mids),
        ),
    )


def host_receipt(store, command, status="received"):
    base = store.root / "wake"
    payload = json.loads((base / "wake-outbox" / (command[-1] + ".json")).read_text())
    receipts = base / "receipts"
    receipts.mkdir(exist_ok=True)
    (receipts / command[-1]).write_text(
        json.dumps(
            {
                "protocol": "agent-mailbox.host-receipt.v1",
                "delivery_id": command[-1],
                "employee_id": command[-2],
                "message_ids": payload["fresh"],
                "status": status,
                "host_session_id": payload["binding"]["host_session_id"],
                "binding_id": payload["binding"]["id"],
                "project_id": payload["binding"]["project_id"],
                "host_kind": payload["binding"]["host_kind"],
            }
        )
    )
    return SimpleNamespace(returncode=0 if status == "received" else 1)


def install_hook(store):
    if sys.platform == "win32":
        pytest.skip("POSIX fixture; Windows hook contract has a separate test suite")
    hook = store.root / "wake/wake-hook.sh"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o700)
    return hook


def assign(team):
    store, project, worker, reviewer, _ = team
    return create_mail_task(
        store,
        project["id"],
        "Task",
        "Synthetic instructions",
        worker["id"],
        review_recipient_id=reviewer["id"],
    )


def test_assignment_and_delivery_create_atomic_directed_intents(team):
    store, _, worker, reviewer, session = team
    task = assign(team)
    assert pending_ids(store, worker["id"]) == [task["request_message_id"]]
    assert pending_ids(store, reviewer["id"]) == []
    accept_mail_task(store, session["token"], task["id"])
    submitted = submit_mail_task(store, session["token"], task["id"], "Synthetic result")
    assert submitted["status"] == "review"
    mids = pending_ids(store, reviewer["id"])
    assert len(mids) == 1
    with store._transaction(readonly=True) as db:
        mail = dict(db.execute("SELECT * FROM messages WHERE id=?", (mids[0],)).fetchone())
    assert mail["recipient_id"] == reviewer["id"]
    assert mail["sender_id"] == worker["id"]
    assert mail["reply_to"] == task["request_message_id"]
    assert task["id"] in mail["body"]
    with pytest.raises(WorkbenchError):
        submit_mail_task(store, session["token"], task["id"], "Duplicate")
    assert pending_ids(store, reviewer["id"]) == mids


def test_explicit_open_settles_unsent_alert_but_preserves_cancel_notice(team):
    store, _, worker, _, session = team
    task = assign(team)
    mid = task["request_message_id"]
    assert pending_ids(store, worker["id"]) == [mid]
    invoke(store, session["token"], "messages", {"folder": "inbox"})
    assert pending_ids(store, worker["id"]) == [mid]
    claims = store.root / "wake" / "claims"
    claims.mkdir(parents=True, exist_ok=True)
    marker = claims / f"{mid}.pending"
    marker.write_text(worker["id"])

    opened = invoke(store, session["token"], "message_open", {"message_id": mid})
    assert opened["accepts_task"] is False
    assert store.get_task(task["id"])["status"] == "queued"
    assert pending_ids(store, worker["id"]) == []
    with store._transaction(readonly=True) as db:
        row = db.execute(
            "SELECT status,attempts FROM notifications WHERE kind='mailbox_wake' AND state_hash=?",
            (mid,),
        ).fetchone()
    assert (row["status"], row["attempts"]) == ("recipient_opened", 0)
    assert ready_ids(store, worker["id"]) == []
    assert not marker.exists()

    store.cancel_task(task["id"])
    assert store.get_task(task["id"])["status"] == "cancelled"
    cancellation = pending_ids(store, worker["id"])
    assert len(cancellation) == 1 and cancellation[0] != mid


def test_late_valid_host_receipt_can_confirm_an_already_opened_letter(team):
    store, project, worker, _, session = team
    letter = store.send_message(project["id"], "Race", "Synthetic", recipient_id=worker["id"])
    invoke(store, session["token"], "message_open", {"message_id": letter["id"]})
    mark_host_received(store, worker["id"], [letter["id"]])
    mark_host_received(store, worker["id"], [letter["id"]])
    with store._transaction(readonly=True) as db:
        row = db.execute(
            "SELECT status,attempts FROM notifications WHERE kind='mailbox_wake' AND state_hash=?",
            (letter["id"],),
        ).fetchone()
    assert (row["status"], row["attempts"]) == ("host_received", 1)


def test_open_after_unknown_host_effect_does_not_erase_uncertainty_or_cancel_notice(team):
    store, project, worker, _, session = team
    install_hook(store)
    task = create_mail_task(store, project["id"], "Uncertain", "Synthetic", worker["id"])
    mid = task["request_message_id"]
    assert (
        drain(store, worker["id"], runner=lambda command, **_: SimpleNamespace(returncode=1))[
            "woke"
        ]
        == []
    )
    unknown = store.root / "wake" / "claims" / f"{mid}.unknown"
    assert unknown.exists()

    opened = invoke(store, session["token"], "message_open", {"message_id": mid})
    assert opened["message"]["read_receipt"]["reader_id"] == worker["id"]
    assert store.get_task(task["id"])["status"] == "queued"
    assert pending_ids(store, worker["id"]) == []
    assert ready_ids(store, worker["id"]) == []
    assert unknown.exists()  # Reading does not manufacture a strict host receipt.
    with store._transaction(readonly=True) as db:
        row = db.execute(
            "SELECT status,attempts FROM notifications WHERE kind='mailbox_wake' AND state_hash=?",
            (mid,),
        ).fetchone()
    assert (row["status"], row["attempts"]) == ("recipient_opened", 0)

    store.cancel_task(task["id"])
    assert store.get_task(task["id"])["status"] == "cancelled"
    notice = pending_ids(store, worker["id"])
    assert len(notice) == 1 and notice[0] != mid
    assert unknown.exists()


def test_preexisting_open_is_reconciled_before_offline_mail_recovery(team):
    store, project, worker, _, session = team
    old = store.send_message(project["id"], "Old", "Opened elsewhere", recipient_id=worker["id"])
    invoke(store, session["token"], "message_open", {"message_id": old["id"]})
    # Simulate an older installation that recorded a read receipt but left its
    # durable host alert pending. A newly connected receiver must not get it.
    with store._transaction() as db:
        db.execute(
            "UPDATE notifications SET status='pending' WHERE kind='mailbox_wake' AND state_hash=?",
            (old["id"],),
        )
    new = store.send_message(project["id"], "New", "Unopened", recipient_id=worker["id"])
    install_hook(store)
    assert pending_ids(store, worker["id"]) == [new["id"]]
    assert ready_ids(store, worker["id"]) == [new["id"]]
    with store._transaction(readonly=True) as db:
        status = db.execute(
            "SELECT status FROM notifications WHERE kind='mailbox_wake' AND state_hash=?",
            (old["id"],),
        ).fetchone()["status"]
    assert status == "recipient_opened"


def test_cancel_after_preexisting_open_keeps_the_stop_notice(team):
    store, _, worker, _, session = team
    task = assign(team)
    mid = task["request_message_id"]
    invoke(store, session["token"], "message_open", {"message_id": mid})
    with store._transaction() as db:
        db.execute(
            "UPDATE notifications SET status='pending' WHERE kind='mailbox_wake' AND state_hash=?",
            (mid,),
        )

    store.cancel_task(task["id"])
    with store._transaction(readonly=True) as db:
        status = db.execute(
            "SELECT status FROM notifications WHERE kind='mailbox_wake' AND state_hash=?",
            (mid,),
        ).fetchone()["status"]
    assert status == "recipient_opened"
    notice = pending_ids(store, worker["id"])
    assert len(notice) == 1 and notice[0] != mid


@pytest.mark.parametrize("change", ["cancel", "pause_resume", "leave_rejoin"])
def test_cancelled_mail_task_does_not_rewake_an_offline_assignee(team, change):
    """Keep the historical letter, but retire an undelivered assignment intent."""
    store, project, worker, _, session = team
    task = create_mail_task(store, project["id"], "Withdrawn", "Synthetic", worker["id"])
    mid = task["request_message_id"]
    assert pending_ids(store, worker["id"]) == [mid]
    with store._transaction(readonly=True) as db:
        original_messages = db.execute("SELECT count(*) FROM messages").fetchone()[0]

    if change == "cancel":
        store.cancel_task(task["id"])
    elif change == "pause_resume":
        store.set_employee_lifecycle(worker["id"], "paused", "Synthetic")
        store.set_employee_lifecycle(worker["id"], "active", "Synthetic")
    else:
        store.remove_project_member(project["id"], worker["id"])
        store.add_project_member(project["id"], worker["id"])

    assert store.get_task(task["id"])["status"] == "cancelled"
    assert pending_ids(store, worker["id"]) == []
    assert mid not in unread_ids_for_employee(store, worker["id"])
    with store._transaction(readonly=True) as db:
        row = db.execute(
            "SELECT status,attempts FROM notifications WHERE kind='mailbox_wake' AND state_hash=?",
            (mid,),
        ).fetchone()
        assert row["status"] == "cancelled" and row["attempts"] == 0
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == original_messages
    with pytest.raises(WorkbenchError):
        accept_mail_task(store, session["token"], task["id"])


def test_cancel_withdraws_known_undelivered_marker_but_preserves_uncertain_claim(team):
    store, project, worker, _, _ = team
    known = create_mail_task(store, project["id"], "Not sent", "Synthetic", worker["id"])
    uncertain = create_mail_task(store, project["id"], "Uncertain", "Synthetic", worker["id"])
    claims = store.root / "wake" / "claims"
    claims.mkdir(parents=True)
    known_pending = claims / f"{known['request_message_id']}.pending"
    known_pending.write_text(worker["id"])
    unknown = claims / f"{uncertain['request_message_id']}.unknown"
    unknown.write_text("synthetic-delivery-id")

    store.cancel_task(known["id"])
    store.cancel_task(uncertain["id"])

    assert not known_pending.exists()
    assert unknown.exists()
    pending = pending_ids(store, worker["id"])
    assert pending[0] == uncertain["request_message_id"] and len(pending) == 2
    with store._transaction(readonly=True) as db:
        rows = dict(
            db.execute(
                "SELECT state_hash,status FROM notifications WHERE kind='mailbox_wake'"
            ).fetchall()
        )
    assert rows[known["request_message_id"]] == "cancelled"
    assert rows[uncertain["request_message_id"]] == "pending"


def test_legacy_cancelled_assignment_without_host_claim_is_not_replayed(team):
    store, project, worker, _, _ = team
    task = create_mail_task(store, project["id"], "Old", "Synthetic", worker["id"])
    mid = task["request_message_id"]
    store.cancel_task(task["id"])
    # Simulate the prior release: the task is cancelled, but its notification
    # was left pending. History remains, while neither wake entry replays it.
    with store._transaction() as db:
        db.execute(
            "UPDATE notifications SET status='pending' WHERE kind='mailbox_wake' AND state_hash=?",
            (mid,),
        )
    assert pending_ids(store, worker["id"]) == []
    assert mid not in unread_ids_for_employee(store, worker["id"])
    with store._transaction(readonly=True) as db:
        assert db.execute("SELECT 1 FROM messages WHERE id=?", (mid,)).fetchone()


def test_cancelled_assignment_with_unknown_host_effect_keeps_exact_receipt_path(team):
    store, project, worker, _, _ = team
    install_hook(store)
    task = create_mail_task(store, project["id"], "Uncertain", "Synthetic", worker["id"])
    mid = task["request_message_id"]
    calls = []

    def no_receipt(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1)

    assert drain(store, worker["id"], runner=no_receipt)["woke"] == []
    assert len(calls) == 1
    unknown = store.root / "wake" / "claims" / f"{mid}.unknown"
    assert unknown.exists()
    store.cancel_task(task["id"])
    pending = pending_ids(store, worker["id"])
    assert pending[0] == mid and len(pending) == 2
    cancellation_notice = pending[1]
    assert mid in unread_ids_for_employee(store, worker["id"])

    host_receipt(store, calls[0])  # Valid, bound late proof of the original delivery.

    def only_cancel_notice(command, **_kwargs):
        payload = json.loads((store.root / "wake/wake-outbox" / f"{command[-1]}.json").read_text())
        assert payload["fresh"] == [cancellation_notice]
        return host_receipt(store, command)

    assert drain(store, worker["id"], runner=only_cancel_notice, now=20000)["woke"] == [
        worker["id"]
    ]
    assert pending_ids(store, worker["id"]) == []
    assert store.get_task(task["id"])["status"] == "cancelled"


def test_cancel_after_host_receipt_sends_one_directed_control_notice(team):
    store, project, worker, _, _ = team
    install_hook(store)
    task = create_mail_task(store, project["id"], "Stop work", "Synthetic", worker["id"])
    original = task["request_message_id"]
    assert drain(store, worker["id"], runner=lambda command, **_: host_receipt(store, command))[
        "woke"
    ] == [worker["id"]]
    assert pending_ids(store, worker["id"]) == []
    with store._transaction(readonly=True) as db:
        count_before = db.execute("SELECT count(*) FROM messages").fetchone()[0]

    store.cancel_task(task["id"])
    with store._transaction(readonly=True) as db:
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == count_before + 1
        notice = db.execute(
            "SELECT * FROM messages WHERE recipient_id=? AND reply_to=? AND id!=?",
            (worker["id"], original, original),
        ).fetchone()
        assert notice is not None
        assert notice["request_work"] == 0
        assert task["id"] in notice["body"]
    assert pending_ids(store, worker["id"]) == [notice["id"]]
    with pytest.raises(WorkbenchError):
        store.cancel_task(task["id"])
    assert pending_ids(store, worker["id"]) == [notice["id"]]
    assert drain(
        store,
        worker["id"],
        runner=lambda command, **_: host_receipt(store, command),
        now=10001,
        event_driven=True,
    )["woke"] == [worker["id"]]
    assert pending_ids(store, worker["id"]) == []
    assert store.get_task(task["id"])["status"] == "cancelled"


def test_pause_queues_cancel_notice_until_employee_is_active_again(team):
    store, project, worker, _, _ = team
    install_hook(store)
    task = create_mail_task(store, project["id"], "Pause after receipt", "Synthetic", worker["id"])
    assert drain(store, worker["id"], runner=lambda command, **_: host_receipt(store, command))[
        "woke"
    ] == [worker["id"]]
    assert pending_ids(store, worker["id"]) == []

    store.set_employee_lifecycle(worker["id"], "paused", "Synthetic pause")
    assert store.get_task(task["id"])["status"] == "cancelled"
    assert pending_ids(store, worker["id"]) == []
    with store._transaction(readonly=True) as db:
        rows = db.execute(
            "SELECT id,request_work FROM messages WHERE task_id=? AND id!=?",
            (task["id"], task["request_message_id"]),
        ).fetchall()
    assert len(rows) == 1 and rows[0]["request_work"] == 0
    notice_id = rows[0]["id"]

    store.set_employee_lifecycle(worker["id"], "active", "Synthetic resume")
    assert pending_ids(store, worker["id"]) == [notice_id]
    assert drain(
        store,
        worker["id"],
        runner=lambda command, **_: host_receipt(store, command),
        now=20000,
        event_driven=True,
    )["woke"] == [worker["id"]]
    assert pending_ids(store, worker["id"]) == []


@pytest.mark.parametrize("change", ["leave", "retire"])
def test_revocation_after_host_receipt_flags_unconfirmed_external_stop(team, change):
    store, project, worker, _, _ = team
    install_hook(store)
    task = create_mail_task(
        store, project["id"], "Revoked after receipt", "Synthetic", worker["id"]
    )
    assert drain(store, worker["id"], runner=lambda command, **_: host_receipt(store, command))[
        "woke"
    ] == [worker["id"]]
    with store._transaction(readonly=True) as db:
        count_before = db.execute("SELECT count(*) FROM messages").fetchone()[0]

    if change == "leave":
        store.remove_project_member(project["id"], worker["id"])
    else:
        store.set_employee_lifecycle(worker["id"], "retired", "Synthetic retirement")

    assert store.get_task(task["id"])["status"] == "cancelled"
    assert pending_ids(store, worker["id"]) == []
    with store._transaction(readonly=True) as db:
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == count_before
    alerts = [
        row
        for row in store.governance_events(project["id"])
        if row["type"] == "mail_task_stop_unconfirmed" and row["task_id"] == task["id"]
    ]
    assert len(alerts) == 1
    assert "未确认" in alerts[0]["reason"]


@pytest.mark.parametrize("change", ["leave", "retire"])
def test_revocation_before_delivery_does_not_raise_false_stop_alert(team, change):
    store, project, worker, _, _ = team
    task = create_mail_task(store, project["id"], "Never delivered", "Synthetic", worker["id"])
    if change == "leave":
        store.remove_project_member(project["id"], worker["id"])
    else:
        store.set_employee_lifecycle(worker["id"], "retired", "Synthetic retirement")
    assert store.get_task(task["id"])["status"] == "cancelled"
    assert not [
        row
        for row in store.governance_events(project["id"])
        if row["type"] == "mail_task_stop_unconfirmed" and row["task_id"] == task["id"]
    ]


@pytest.mark.parametrize("change", ["leave", "retire"])
def test_revocation_with_unknown_delivery_requires_external_stop_check(team, change):
    store, project, worker, _, _ = team
    task = create_mail_task(store, project["id"], "Unknown delivery", "Synthetic", worker["id"])
    claim = store.root / "wake/claims" / f"{task['request_message_id']}.unknown"
    claim.parent.mkdir(parents=True)
    claim.write_text("synthetic-delivery-id")
    if change == "leave":
        store.remove_project_member(project["id"], worker["id"])
    else:
        store.set_employee_lifecycle(worker["id"], "retired", "Synthetic retirement")
    assert claim.exists()
    alerts = [
        row
        for row in store.governance_events(project["id"])
        if row["type"] == "mail_task_stop_unconfirmed" and row["task_id"] == task["id"]
    ]
    assert len(alerts) == 1


def test_cancel_after_explicit_accept_notifies_even_if_assignment_wake_was_pending(team):
    store, project, worker, _, session = team
    task = create_mail_task(store, project["id"], "Accepted", "Synthetic", worker["id"])
    accepted = accept_mail_task(store, session["token"], task["id"])
    assert accepted["status"] == "running"
    store.cancel_task(task["id"])
    pending = pending_ids(store, worker["id"])
    assert task["request_message_id"] not in pending
    assert len(pending) == 1
    with store._transaction(readonly=True) as db:
        notice = db.execute(
            "SELECT request_work,reply_to FROM messages WHERE id=?", (pending[0],)
        ).fetchone()
        assert notice["request_work"] == 0
        assert notice["reply_to"] == task["request_message_id"]


def test_cancel_notice_failure_rolls_back_task_status_and_message(team, monkeypatch):
    import agent_mailbox.workbench_mail_notifications as notices

    store, project, worker, _, _ = team
    install_hook(store)
    task = create_mail_task(store, project["id"], "Atomic", "Synthetic", worker["id"])
    assert drain(store, worker["id"], runner=lambda command, **_: host_receipt(store, command))[
        "woke"
    ] == [worker["id"]]
    with store._transaction(readonly=True) as db:
        count_before = db.execute("SELECT count(*) FROM messages").fetchone()[0]

    def fail_enqueue(*_args):
        raise RuntimeError("synthetic enqueue failure")

    monkeypatch.setattr(notices, "enqueue", fail_enqueue)
    with pytest.raises(RuntimeError, match="synthetic enqueue failure"):
        store.cancel_task(task["id"])
    assert store.get_task(task["id"])["status"] == "queued"
    with store._transaction(readonly=True) as db:
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == count_before


def test_cancel_refuses_to_run_outside_unavailable_wake_lock(team, monkeypatch):
    import contextlib

    import agent_mailbox.workbench_wake as wake

    store, project, worker, _, _ = team
    task = create_mail_task(store, project["id"], "Lock", "Synthetic", worker["id"])

    @contextlib.contextmanager
    def unavailable(_home):
        raise wake.WakeLockBusy("synthetic lock failure")
        yield

    monkeypatch.setattr(wake, "state_lock", unavailable)
    with pytest.raises(WorkbenchError) as caught:
        store.cancel_task(task["id"])
    assert caught.value.code == "wake_busy"
    assert store.get_task(task["id"])["status"] == "queued"
    assert pending_ids(store, worker["id"]) == [task["request_message_id"]]


def test_cancellation_waits_for_inflight_wake_lock(team):
    store, project, worker, _, _ = team
    task = create_mail_task(store, project["id"], "Waiting", "Synthetic", worker["id"])
    entered = threading.Event()
    release = threading.Event()

    def in_flight():
        with state_lock(store.root):
            entered.set()
            assert release.wait(3)

    with ThreadPoolExecutor(max_workers=2) as pool:
        holder = pool.submit(in_flight)
        assert entered.wait(3)
        canceller = pool.submit(store.cancel_task, task["id"])
        try:
            assert not canceller.done()
            assert not release.wait(0.1)
        finally:
            release.set()
        holder.result(timeout=3)
        canceller.result(timeout=3)
    assert pending_ids(store, worker["id"]) == []


def test_cancel_during_real_delivery_waits_for_receipt_then_sends_control(team):
    store, project, worker, _, _ = team
    install_hook(store)
    task = create_mail_task(store, project["id"], "In flight", "Synthetic", worker["id"])
    original = task["request_message_id"]
    entered = threading.Event()
    release = threading.Event()

    def blocked_host(command, **_kwargs):
        entered.set()
        assert release.wait(3)
        return host_receipt(store, command)

    with ThreadPoolExecutor(max_workers=2) as pool:
        delivery = pool.submit(drain, store, worker["id"], runner=blocked_host)
        assert entered.wait(3)
        cancellation = pool.submit(store.cancel_task, task["id"])
        try:
            assert not cancellation.done()
            assert not release.wait(0.1)
        finally:
            release.set()
        assert delivery.result(timeout=3)["woke"] == [worker["id"]]
        assert cancellation.result(timeout=3)["status"] == "cancelled"

    with store._transaction(readonly=True) as db:
        original_state = db.execute(
            "SELECT status FROM notifications WHERE kind='mailbox_wake' AND state_hash=?",
            (original,),
        ).fetchone()["status"]
        notices = db.execute(
            "SELECT id,request_work FROM messages WHERE task_id=? AND id!=?",
            (task["id"], original),
        ).fetchall()
    assert original_state == "host_received"
    assert len(notices) == 1 and notices[0]["request_work"] == 0
    assert pending_ids(store, worker["id"]) == [notices[0]["id"]]
    assert drain(
        store,
        worker["id"],
        runner=lambda command, **_: host_receipt(store, command),
        now=20000,
        event_driven=True,
    )["woke"] == [worker["id"]]
    assert pending_ids(store, worker["id"]) == []


def test_failure_after_mail_insert_rolls_back_task_message_intent_and_signal(team, monkeypatch):
    store, *_ = team
    store.mail_wake_event.clear()
    monkeypatch.setattr(
        store, "_governance", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash"))
    )
    with pytest.raises(RuntimeError):
        assign(team)
    with store._transaction(readonly=True) as db:
        for table in ("tasks", "messages", "notifications"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert not store.mail_wake_event.is_set()


def test_no_reviewer_does_not_broadcast_private_delivery(team):
    store, project, worker, reviewer, session = team
    task = create_mail_task(store, project["id"], "Private", "Read", worker["id"])
    accept_mail_task(store, session["token"], task["id"])
    submit_mail_task(store, session["token"], task["id"], "Private result")
    assert store.get_task(task["id"])["status"] == "review"
    assert not pending_ids(store, reviewer["id"])
    with store._transaction(readonly=True) as db:
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_invalid_or_self_reviewer_rejected_and_employee_cannot_override(team, tmp_path):
    store, project, worker, reviewer, session = team
    other = store.create_project("Other", tmp_path)
    outsider = store.create_employee("Outside", "codex", other["id"])
    for recipient in (worker["id"], outsider["id"], "missing"):
        with pytest.raises(WorkbenchError):
            create_mail_task(
                store, project["id"], "Task", "Read", worker["id"], review_recipient_id=recipient
            )
    task = assign(team)
    with pytest.raises(WorkbenchError):
        invoke(
            store,
            session["token"],
            "task_submit",
            {"task_id": task["id"], "result": "x", "review_recipient_id": reviewer["id"]},
        )


def test_revoked_reviewer_preserves_human_delivery_without_leak(team):
    store, project, _, reviewer, session = team
    task = assign(team)
    accept_mail_task(store, session["token"], task["id"])
    store.remove_project_member(project["id"], reviewer["id"])
    submit_mail_task(store, session["token"], task["id"], "Result survives")
    assert store.get_task(task["id"])["result"] == "Result survives"
    assert not pending_ids(store, reviewer["id"])
    assert any(
        e["type"] == "review_notification_blocked" for e in store.task_detail(task["id"])["events"]
    )


def test_restart_without_hook_preserves_intent_then_delivers_once(team):
    store, _, worker, _, _ = team
    task = assign(team)
    store = WorkbenchStore(store.root)  # New process state: no in-memory signal.
    assert drain(store, worker["id"])["woke"] == []
    assert pending_ids(store, worker["id"]) == [task["request_message_id"]]
    assert not load_state(store.root)["employees"][worker["id"]].get("wakes")
    before = list((store.root / "wake/wake-outbox").glob("*.json"))
    drain(store, worker["id"], now=10001)
    assert list((store.root / "wake/wake-outbox").glob("*.json")) == before
    install_hook(store)
    calls = []

    def host(command, **kwargs):
        payload = json.loads(
            (store.root / "wake/wake-outbox" / (command[-1] + ".json")).read_text()
        )
        assert command[-2] == worker["id"]
        assert payload["fresh"] == [task["request_message_id"]]
        calls.append(payload)
        return host_receipt(store, command)

    assert drain(store, worker["id"], host)["woke"] == [worker["id"]]
    assert pending_ids(store, worker["id"]) == []
    assert drain(store, worker["id"], host, now=20000)["woke"] == []
    assert len(calls) == 1
    assert store.get_task(task["id"])["status"] == "queued"


def test_failed_host_retries_without_claiming_success(team):
    store, _, worker, _, _ = team
    assign(team)
    install_hook(store)
    assert not drain(
        store, worker["id"], lambda command, **k: host_receipt(store, command, "not_received")
    )["woke"]
    assert pending_ids(store, worker["id"])
    assert drain(store, worker["id"], lambda command, **k: host_receipt(store, command))["woke"]


def test_host_receipt_before_db_ack_crash_does_not_repeat_host_call(team):
    store, _, worker, _, _ = team
    task = assign(team)
    install_hook(store)
    calls = []

    def host(command, **kwargs):
        calls.append(1)
        return host_receipt(store, command)

    assert hook_deliver(
        store.root,
        runner=host,
        require_receipt=True,
        binding_resolver=lambda eid, mids: resolve_routes(store, eid, mids),
    )(worker["id"], {"fresh": [task["request_message_id"]]})
    assert pending_ids(store, worker["id"])
    assert drain(WorkbenchStore(store.root), worker["id"], host)["woke"]
    assert len(calls) == 1
    assert pending_ids(store, worker["id"]) == []


def test_disabled_and_cooldown_do_not_discard_durable_intent(team):
    store, project, worker, _, _ = team
    assign(team)
    install_hook(store)
    host = lambda command, **k: host_receipt(store, command)
    drain(store, worker["id"], host)
    mail = store.send_message(project["id"], "Next", "Read", recipient_id=worker["id"])
    assert not drain(store, worker["id"], host, now=10001)["woke"]
    state = load_state(store.root)
    state["enabled"] = False
    save_state(store.root, state)
    assert not drain(store, worker["id"], host, now=20000)["woke"]
    assert pending_ids(store, worker["id"]) == [mail["id"]]
    state["enabled"] = True
    save_state(store.root, state)
    assert drain(store, worker["id"], host, now=10000 + DEFAULT_COOLDOWN_SECONDS + 1)["woke"]


def test_schema12_upgrade_preserves_old_tasks_and_backs_up(team):
    store, project, worker, _, _ = team
    task = create_mail_task(store, project["id"], "Old", "Read", worker["id"])
    with store._connection() as db:
        db.execute("ALTER TABLE tasks DROP COLUMN review_recipient_id")
        db.execute("PRAGMA user_version=12")
        db.commit()
    upgraded = WorkbenchStore(store.root)
    assert SCHEMA_VERSION >= 13
    assert upgraded.migration_backup_path.is_file()
    assert upgraded.get_task(task["id"])["review_recipient_id"] is None
    assert upgraded.get_task(task["id"])["status"] == "queued"


def test_followup_inherits_explicit_reviewer(team):
    store, _, _, reviewer, session = team
    task = assign(team)
    accept_mail_task(store, session["token"], task["id"])
    submit_mail_task(store, session["token"], task["id"], "First result")
    followup = store.follow_up_task(task["id"], "Please check this detail")
    assert followup["task"]["review_recipient_id"] == reviewer["id"]


def test_message_request_replay_has_one_notification(team):
    store, project, worker, *_ = team
    kwargs = {"recipient_id": worker["id"], "request_id": "stable-request"}
    first = store.send_message(project["id"], "Hello", "Synthetic", **kwargs)
    again = store.send_message(project["id"], "Hello", "Synthetic", **kwargs)
    assert first["id"] == again["id"]
    assert pending_ids(store, worker["id"]) == [first["id"]]


def test_real_isolated_hook_process_receives_only_ids(team):
    store, _, worker, _, _ = team
    task = assign(team)
    hook = install_hook(store)
    import shlex

    script = hook.parent / "synthetic_host.py"
    script.write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "base=Path(__file__).parent\n"
        "employee,delivery=sys.argv[1:]\n"
        "payload=json.loads((base/'wake-outbox'/f'{delivery}.json').read_text())\n"
        "(base/'receipt.txt').write_text(employee+'\\n'+delivery+'\\n')\n"
        "(base/'receipts').mkdir(exist_ok=True)\n"
        "(base/'receipts'/delivery).write_text(json.dumps({"
        "'protocol':'agent-mailbox.host-receipt.v1','delivery_id':delivery,"
        "'employee_id':employee,'message_ids':payload['fresh'],'status':'received',"
        "'host_session_id':payload['binding']['host_session_id'],"
        "'binding_id':payload['binding']['id'],'project_id':payload['binding']['project_id'],"
        "'host_kind':payload['binding']['host_kind']}))\n"
    )
    hook.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n'
    )
    assert drain(store, worker["id"])["woke"] == [worker["id"]]
    employee, delivery = (hook.parent / "receipt.txt").read_text().splitlines()
    assert employee == worker["id"]
    payload = json.loads((hook.parent / "wake-outbox" / f"{delivery}.json").read_text())
    assert payload["fresh"] == [task["request_message_id"]]
    assert pending_ids(store, worker["id"]) == []


def test_pending_mail_recovers_after_worker_store_restart_without_new_commit(team):
    """A durable failed delivery must retry when a fresh workbench worker starts."""
    import shlex

    import agent_mailbox.workbench as app

    store, project, worker, _, _ = team
    hook = install_hook(store)
    script = hook.parent / "restart_host.py"
    script.write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "base=Path(__file__).parent\n"
        "employee,delivery=sys.argv[1:]\n"
        "payload=json.loads((base/'wake-outbox'/f'{delivery}.json').read_text())\n"
        "attempts=base/'restart-attempts.txt'\n"
        "prior=attempts.read_text().splitlines() if attempts.exists() else []\n"
        "status='not_received' if not prior else 'received'\n"
        "attempts.write_text('\\n'.join([*prior,status])+'\\n')\n"
        "(base/'receipts').mkdir(exist_ok=True)\n"
        "(base/'receipts'/delivery).write_text(json.dumps({"
        "'protocol':'agent-mailbox.host-receipt.v1','delivery_id':delivery,"
        "'employee_id':employee,'message_ids':payload['fresh'],'status':status,"
        "'host_session_id':payload['binding']['host_session_id'],"
        "'binding_id':payload['binding']['id'],'project_id':payload['binding']['project_id'],"
        "'host_kind':payload['binding']['host_kind']}))\n"
        "sys.exit(0 if status=='received' else 1)\n"
    )
    hook.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n'
    )
    drain(store, worker["id"], event_driven=True)  # Establish the first-start baseline.
    letter = store.send_message(
        project["id"], "After restart", "Synthetic", recipient_id=worker["id"]
    )
    drain(store, worker["id"], event_driven=True)
    assert pending_ids(store, worker["id"]) == [letter["id"]]
    assert (hook.parent / "restart-attempts.txt").read_text().splitlines() == ["not_received"]
    # The normal 30-second retry backoff still applies after a crash. Model a
    # restart after that window rather than forcing an immediate duplicate.
    marker = hook.parent / "claims" / f"{letter['id']}.pending"
    expired = time.time() - 31
    os.utime(marker, (expired, expired))

    restarted = WorkbenchStore(store.root)
    stop = threading.Event()
    thread = threading.Thread(
        target=app.wake_redeem_loop,
        args=(restarted, restarted.root),
        kwargs={"interval": None, "stop": stop},
        daemon=True,
    )
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while pending_ids(restarted, worker["id"]) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pending_ids(restarted, worker["id"]) == []
        assert (hook.parent / "restart-attempts.txt").read_text().splitlines() == [
            "not_received",
            "received",
        ]
    finally:
        stop.set()
        restarted.mail_wake_event.set()
        thread.join(3)
    assert not thread.is_alive()


def test_commit_wakes_native_worker_without_waiting_for_recovery_interval(team, monkeypatch):
    import agent_mailbox.workbench as app
    import agent_mailbox.workbench_wake as wake

    store, project, worker, _, _ = team
    install_hook(store)
    ready, received, stop = threading.Event(), threading.Event(), threading.Event()
    real_run = wake.run_once

    def run(*args, **kwargs):
        result = real_run(*args, **kwargs)
        ready.set()
        return result

    def host(command, **kwargs):
        result = host_receipt(store, command)
        received.set()
        return result

    factory = hook_deliver
    monkeypatch.setattr(wake, "run_once", run)
    monkeypatch.setattr(
        wake, "hook_deliver", lambda home, **kwargs: factory(home, **{**kwargs, "runner": host})
    )
    thread = threading.Thread(
        target=app.wake_redeem_loop,
        args=(store, store.root),
        kwargs={"interval": 60, "stop": stop},
        daemon=True,
    )
    thread.start()
    try:
        assert ready.wait(3)
        store.send_message(project["id"], "Signal", "Read", recipient_id=worker["id"])
        assert received.wait(3), "Commit signal must wake the worker before its 60s recovery wait"
    finally:
        stop.set()
        store.mail_wake_event.set()
        thread.join(3)
    assert not thread.is_alive()


def test_reenable_from_separate_cli_signals_running_native_worker(team, monkeypatch):
    """Re-enabling must deliver held mail without waiting for a recovery scan."""
    import agent_mailbox.workbench as app
    import agent_mailbox.workbench_wake as wake

    store, project, worker, _, _ = team
    install_hook(store)
    ready, received, stop = threading.Event(), threading.Event(), threading.Event()
    calls = []
    real_run = wake.run_once

    def run(*args, **kwargs):
        result = real_run(*args, **kwargs)
        ready.set()
        return result

    def host(command, **kwargs):
        calls.append(command)
        result = host_receipt(store, command)
        received.set()
        return result

    factory = hook_deliver
    monkeypatch.setattr(wake, "run_once", run)
    monkeypatch.setattr(
        wake, "hook_deliver", lambda home, **kwargs: factory(home, **{**kwargs, "runner": host})
    )
    server = app.WorkbenchHTTP(store)
    http = threading.Thread(target=server.serve_forever, daemon=True)
    native = threading.Thread(
        target=app.wake_redeem_loop,
        args=(store, store.root),
        kwargs={"interval": None, "stop": stop},
        daemon=True,
    )
    http.start()
    native.start()
    try:
        assert ready.wait(3)
        assert wake.cli_main(["--disable"], home=store.root) == 0
        ready.clear()
        letter = store.send_message(
            project["id"], "Held while disabled", "Synthetic", recipient_id=worker["id"]
        )
        assert ready.wait(3)
        assert not received.is_set()
        assert pending_ids(store, worker["id"]) == [letter["id"]]

        unauthenticated = http_client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            unauthenticated.request(
                "POST",
                "/api/workbench/mailbox/wake/recheck",
                body=b"{}",
                headers={"Authorization": "Bearer wrong", "Content-Type": "application/json"},
            )
            response = unauthenticated.getresponse()
            assert response.status == 401
            response.read()
        finally:
            unauthenticated.close()

        ready.clear()
        code = (
            "from pathlib import Path; from agent_mailbox.workbench_wake import cli_main; "
            f"raise SystemExit(cli_main(['--enable'], home=Path({str(store.root)!r})))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert received.wait(3), "Separate CLI enable must signal the native worker"
        assert ready.wait(3), "Delivery state must be committed before inspection"
        assert len(calls) == 1
        assert pending_ids(store, worker["id"]) == []
    finally:
        stop.set()
        store.mail_wake_event.set()
        native.join(3)
        server.shutdown()
        http.join(3)
        server.close()
    assert not native.is_alive()


def test_codex_turn_complete_signal_retries_busy_pending_before_recovery_interval(
    team, monkeypatch
):
    """Worker goes from a negative host receipt to a new verified receipt on idle event."""
    import agent_mailbox.workbench as app
    import agent_mailbox.workbench_wake as wake

    store, project, worker, _, _ = team
    binding = set_binding(
        store, worker["id"], project["id"], host_kind="codex", host_session_id="original-thread"
    )
    install_hook(store)
    attempts = []
    received = threading.Event()
    stop = threading.Event()

    def host(command, **kwargs):
        status = "not_received" if not attempts else "received"
        attempts.append(status)
        result = host_receipt(store, command, status)
        if status == "received":
            received.set()
        return result

    factory = hook_deliver
    monkeypatch.setattr(
        wake, "hook_deliver", lambda home, **kwargs: factory(home, **{**kwargs, "runner": host})
    )
    with tempfile.TemporaryDirectory(prefix="am-idle-", dir="/private/tmp") as raw:
        endpoint = Path(raw) / "idle.sock"
        close = start_host_activity_listener(store, endpoint)
        thread = threading.Thread(
            target=app.wake_redeem_loop,
            args=(store, store.root),
            kwargs={"interval": 60, "stop": stop},
            daemon=True,
        )
        thread.start()
        try:
            letter = store.send_message(
                project["id"], "Busy recipient", "Synthetic", recipient_id=worker["id"]
            )
            mid = letter["id"]
            marker = store.root / "wake/claims" / f"{mid}.pending"
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert marker.exists() and attempts == ["not_received"]
            assert pending_ids(store, worker["id"]) == [mid]
            with socket.socket(socket.AF_UNIX) as client:
                client.connect(str(endpoint))
                client.sendall(
                    (
                        json.dumps(
                            {
                                "protocol": HOST_ACTIVITY_PROTOCOL,
                                "kind": "turn_completed",
                                "thread_id": binding["host_session_id"],
                            }
                        )
                        + "\n"
                    ).encode()
                )
                client.settimeout(3)
                answer = json.loads(client.makefile("rb").readline())
            assert answer == {
                "protocol": HOST_ACTIVITY_PROTOCOL,
                "thread_id": binding["host_session_id"],
                "processed": True,
            }
            assert pending_ids(store, worker["id"]) == []
            assert received.wait(3), "Host idle event must beat the 60s recovery scan"
            deadline = time.monotonic() + 3
            while pending_ids(store, worker["id"]) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert pending_ids(store, worker["id"]) == []
            assert attempts == ["not_received", "received"]
        finally:
            stop.set()
            store.mail_wake_event.set()
            thread.join(3)
            close()
    assert not thread.is_alive()


def test_codex_idle_ack_reports_lock_deferral_as_unprocessed(team, monkeypatch):
    """A skipped wake pass must never be acknowledged as completed to Desktop."""
    import agent_mailbox.workbench as app
    import agent_mailbox.workbench_wake as wake

    store, project, worker, _, _ = team
    binding = set_binding(
        store, worker["id"], project["id"], host_kind="codex", host_session_id="original-thread"
    )
    monkeypatch.setattr(wake, "run_once", lambda *args, **kwargs: {"deferred": "lock_busy"})
    stop = threading.Event()
    with tempfile.TemporaryDirectory(prefix="am-idle-deferred-", dir="/private/tmp") as raw:
        endpoint = Path(raw) / "idle.sock"
        close = start_host_activity_listener(store, endpoint)
        thread = threading.Thread(
            target=app.wake_redeem_loop,
            args=(store, store.root),
            kwargs={"interval": 60, "stop": stop},
            daemon=True,
        )
        thread.start()
        try:
            with socket.socket(socket.AF_UNIX) as client:
                client.connect(str(endpoint))
                client.sendall(
                    (
                        json.dumps(
                            {
                                "protocol": HOST_ACTIVITY_PROTOCOL,
                                "kind": "turn_completed",
                                "thread_id": binding["host_session_id"],
                            }
                        )
                        + "\n"
                    ).encode()
                )
                client.settimeout(3)
                answer = json.loads(client.makefile("rb").readline())
            assert answer == {
                "protocol": HOST_ACTIVITY_PROTOCOL,
                "thread_id": binding["host_session_id"],
                "processed": False,
            }
        finally:
            stop.set()
            store.mail_wake_event.set()
            thread.join(3)
            close()
    assert not thread.is_alive()


def test_two_stdio_clients_trigger_round_trip_without_periodic_scans(team, monkeypatch, tmp_path):
    """Real MCP subprocesses/HTTP/DB/worker; only the receiving host is synthetic."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    import agent_mailbox.workbench as app
    import agent_mailbox.workbench_wake as wake
    from agent_mailbox.workbench_private import private_mode

    store, project, alice, bob, alice_session = team
    bob_session = create_session(store, bob["id"], project["id"], "Bob")
    paths = []
    for employee, session in ((alice, alice_session), (bob, bob_session)):
        path = tmp_path / (employee["id"] + ".json")
        path.write_text(
            json.dumps(
                {
                    "home": str(store.root),
                    "token": session["token"],
                    "employee_id": employee["id"],
                    "project_id": project["id"],
                    "session_id": session["id"],
                }
            )
        )
        private_mode(path, 0o600)
        paths.append(path)

    install_hook(store)
    ready, stop = threading.Event(), threading.Event()
    received = {e["id"]: threading.Event() for e in (alice, bob)}
    deliveries = []
    real_run = wake.run_once

    def run(*args, **kwargs):
        result = real_run(*args, **kwargs)
        ready.set()
        return result

    def host(command, **kwargs):
        payload = json.loads(
            (store.root / "wake/wake-outbox" / (command[-1] + ".json")).read_text()
        )
        # The recipient only receives committed, correctly addressed messages.
        with store._transaction(readonly=True) as db:
            for message_id in payload["fresh"]:
                row = db.execute(
                    "SELECT recipient_id FROM messages WHERE id=?", (message_id,)
                ).fetchone()
                assert row is not None and row[0] == command[-2]
        result = host_receipt(store, command)
        deliveries.append((command[-2], payload["fresh"]))
        received[command[-2]].set()
        return result

    monkeypatch.setattr(wake, "run_once", run)
    monkeypatch.setattr(
        wake,
        "hook_deliver",
        lambda home, **kwargs: hook_deliver(home, **{**kwargs, "runner": host}),
    )
    server = app.WorkbenchHTTP(store)
    http = threading.Thread(target=server.serve_forever, daemon=True)
    worker = threading.Thread(
        target=app.wake_redeem_loop,
        args=(store, store.root),
        kwargs={"interval": None, "stop": stop},
        daemon=True,
    )
    http.start()
    worker.start()

    def params(path):
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "agent_mailbox", "mailbox-mcp", "--session-file", str(path)],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )

    async def exchange():
        async with (
            stdio_client(params(paths[0])) as (ar, aw),
            ClientSession(ar, aw) as a,
            stdio_client(params(paths[1])) as (br, bw),
            ClientSession(br, bw) as b,
        ):
            await a.initialize()
            await b.initialize()
            for client in (a, b):
                assert not (await client.call_tool("project_context", {})).is_error
            original = await a.call_tool(
                "project_message",
                {
                    "title": "Event only",
                    "body": "Read this synthetic message",
                    "recipient_id": bob["id"],
                    "request_id": "event-only-original",
                },
            )
            assert not original.is_error
            message = json.loads(original.content[0].text)
            assert await asyncio.to_thread(received[bob["id"]].wait, 3), (
                "No timer may deliver this mail"
            )
            reply = await b.call_tool(
                "project_message",
                {
                    "title": "Re: Event only",
                    "body": "Synthetic reply",
                    "recipient_id": alice["id"],
                    "reply_to": message["id"],
                    "request_id": "event-only-reply",
                },
            )
            assert not reply.is_error
            response = json.loads(reply.content[0].text)
            assert await asyncio.to_thread(received[alice["id"]].wait, 3), (
                "Reply must signal the original sender"
            )
            assert deliveries == [(bob["id"], [message["id"]]), (alice["id"], [response["id"]])]
            received[bob["id"]].clear()
            followup = await a.call_tool(
                "project_message",
                {
                    "title": "Next step",
                    "body": "Synthetic next step",
                    "recipient_id": bob["id"],
                    "reply_to": response["id"],
                    "request_id": "event-only-followup",
                },
            )
            assert not followup.is_error
            assert await asyncio.to_thread(received[bob["id"]].wait, 3), (
                "A new reply must not wait for the legacy 15-minute cooldown"
            )
            assert deliveries[-1] == (bob["id"], [json.loads(followup.content[0].text)["id"]])
            assert store.snapshot()["tasks"] == [], "A notification must not accept or create work"

    try:
        assert ready.wait(3)
        asyncio.run(exchange())
    finally:
        stop.set()
        store.mail_wake_event.set()
        worker.join(5)
        server.shutdown()
        http.join(5)
        server.close()
    assert not worker.is_alive()


def test_event_notifications_keep_off_switch_budget_and_duplicate_guard():
    from agent_mailbox.workbench_wake import (
        MAX_MAIL_EVENTS_PER_HOUR,
        decide,
        empty_state,
        record_wake,
    )

    state = empty_state()
    # More than the old 50-item history cap, so an exhausted budget cannot
    # accidentally become unlimited after trimming the successful receipts.
    for n in range(MAX_MAIL_EVENTS_PER_HOUR):
        mid = f"new-mail-{n}"
        assert decide(state, "receiver", [mid], now=1000, event_driven=True)
        record_wake(state, "receiver", [mid], now=1000)
        assert decide(state, "receiver", [mid], now=1000, event_driven=True) is None
    assert decide(state, "receiver", ["over-budget"], now=1000, event_driven=True) is None
    assert decide(state, "receiver", ["over-budget"], now=4600, event_driven=True)
    state["enabled"] = False
    assert decide(state, "receiver", ["over-budget"], now=4600, event_driven=True) is None


def test_native_stream_does_not_replay_old_mail_without_notification_intent(team):
    store, project, worker, _, _ = team
    install_hook(store)
    message = store.send_message(project["id"], "Old", "Historical mail", recipient_id=worker["id"])
    with store._transaction() as db:
        db.execute("DELETE FROM notifications WHERE state_hash=?", (message["id"],))
    calls = []
    result = run_once(
        store,
        store.root,
        employee_ids=[worker["id"]],
        cold_started=True,
        pending_provider=pending_ids,
        unread_provider=unread_ids_for_employee,
        event_driven=True,
        deliver=lambda *args: calls.append(args),
    )
    assert result["woke"] == []
    assert calls == []
