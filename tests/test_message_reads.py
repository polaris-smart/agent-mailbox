"""Recipient opens are evidence, not list views, work acceptance or another wake."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_mailbox.workbench_mail_sessions import create_session, invoke, revoke_session
from agent_mailbox.workbench_mail_tasks import create_mail_task
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


@pytest.fixture
def team(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Read receipt", str(tmp_path))
    people = [store.create_employee(n, "codex", project["id"]) for n in ("A", "B", "C")]
    sessions = [create_session(store, e["id"], project["id"], e["name"]) for e in people]
    message = invoke(
        store,
        sessions[0]["token"],
        "message",
        {"title": "One message", "body": "Pinned body", "recipient_id": people[1]["id"]},
    )
    return store, project, people, sessions, message


def counts(store):
    with store._transaction(readonly=True) as db:
        return {
            t: db.execute("SELECT count(*) FROM " + t).fetchone()[0]
            for t in ("messages", "notifications", "tasks")
        }


def opened(store, session, message):
    return invoke(store, session["token"], "message_open", {"message_id": message["id"]})


def test_list_and_delivery_do_not_read_then_open_is_idempotent(team):
    store, project, people, sessions, message = team
    before = counts(store)
    assert (
        invoke(store, sessions[1]["token"], "messages", {})["messages"][0]["read_receipt"] is None
    )
    assert (
        invoke(store, sessions[0]["token"], "messages", {"folder": "sent"})["messages"][0][
            "read_receipt"
        ]
        is None
    )
    first = opened(store, sessions[1], message)
    assert first["message"]["body"] == "Pinned body"
    receipt = first["message"]["read_receipt"]
    assert receipt["reader_id"] == people[1]["id"] and receipt["first_opened_at"]
    assert receipt["attribution_scope"] == "employee_session"
    assert first["accepts_task"] is False
    assert opened(store, sessions[1], message) == first
    assert counts(store) == {**before, "notifications": before["notifications"] + 1}
    sent = invoke(store, sessions[0]["token"], "messages", {"folder": "sent"})
    assert sent["messages"][0]["read_receipt"] == receipt
    assert all("read_receipt" not in m for m in store.list_messages(project["id"]))
    outsider_context = invoke(store, sessions[2]["token"], "context", {})
    assert all("read_receipt" not in m for m in outsider_context["messages"])


def test_only_recipient_can_open_and_errors_do_not_disclose(team):
    store, _, _, sessions, message = team
    failures = []
    for session, msg in [
        (sessions[0], message),
        (sessions[2], message),
        (sessions[1], {"id": "missing"}),
    ]:
        with pytest.raises(WorkbenchError) as err:
            opened(store, session, msg)
        failures.append((err.value.code, str(err.value)))
    assert len(set(failures)) == 1
    assert (
        invoke(store, sessions[1]["token"], "messages", {})["messages"][0]["read_receipt"] is None
    )


def test_owner_projection_displays_evidence_without_opening_or_leaking_to_context(team):
    store, _, _, sessions, message = team
    before = counts(store)
    assert store.snapshot(owner_read_receipts=True)["messages"][0]["read_receipt"] is None
    with store._transaction(readonly=True) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM governance_events WHERE type='message_opened'"
            ).fetchone()[0]
            == 0
        )
    receipt = opened(store, sessions[1], message)["message"]["read_receipt"]
    assert store.snapshot(owner_read_receipts=True)["messages"][0]["read_receipt"] == receipt
    assert "read_receipt" not in store.snapshot()["messages"][0]
    assert all(
        "read_receipt" not in m
        for m in invoke(store, sessions[2]["token"], "context", {})["messages"]
    )
    assert counts(store) == {**before, "notifications": before["notifications"] + 1}


def test_cross_project_and_identity_override_rejected(team, tmp_path):
    store, _, people, sessions, message = team
    other = store.create_project("Other", str(tmp_path))
    store.add_project_member(other["id"], people[1]["id"])
    wrong = create_session(store, people[1]["id"], other["id"], "Other")
    with pytest.raises(WorkbenchError) as err:
        opened(store, wrong, message)
    assert err.value.code == "MESSAGE_NOT_ACCESSIBLE"
    for key, value in [
        ("employee_id", people[1]["id"]),
        ("opened_at", "tomorrow"),
        ("project_id", other["id"]),
    ]:
        with pytest.raises(WorkbenchError) as err:
            invoke(
                store,
                sessions[1]["token"],
                "message_open",
                {"message_id": message["id"], key: value},
            )
        assert err.value.code == "MAILBOX_ARGUMENT_DENIED"


@pytest.mark.parametrize("invalidate", ["revoke", "expire", "remove"])
def test_current_authorization_checked_even_for_previous_open(team, invalidate):
    store, project, people, sessions, message = team
    receipt = opened(store, sessions[1], message)["message"]["read_receipt"]
    if invalidate == "revoke":
        revoke_session(store, sessions[1]["id"])
    else:
        with store._transaction() as db:
            if invalidate == "expire":
                db.execute(
                    "UPDATE mailbox_sessions SET expires_at='2000-01-01' WHERE id=?",
                    (sessions[1]["id"],),
                )
            else:
                db.execute(
                    "DELETE FROM memberships WHERE project_id=? AND employee_id=?",
                    (project["id"], people[1]["id"]),
                )
    with pytest.raises(WorkbenchError) as err:
        opened(store, sessions[1], message)
    assert err.value.code == "UNAUTHORIZED"
    assert (
        invoke(store, sessions[0]["token"], "messages", {"folder": "sent"})["messages"][0][
            "read_receipt"
        ]
        == receipt
    )


def test_concurrent_opens_have_one_first_receipt_and_one_retraction_intent(team):
    store, _, _, sessions, message = team
    before = counts(store)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: opened(store, sessions[1], message), range(8)))
    assert all(r == results[0] for r in results)
    with store._transaction(readonly=True) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM governance_events WHERE type='message_opened'"
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT count(*) FROM notifications WHERE kind='mailbox_queue_retract'"
            ).fetchone()[0]
            == 1
        )
    assert counts(store) == {**before, "notifications": before["notifications"] + 1}


def test_task_open_does_not_accept_and_accept_does_not_open(team):
    store, project, people, sessions, _ = team
    task = create_mail_task(store, project["id"], "Open", "No execution", people[1]["id"])
    first = opened(store, sessions[1], {"id": task["request_message_id"]})
    assert first["accepts_task"] is False and store.get_task(task["id"])["status"] == "queued"
    other = create_mail_task(store, project["id"], "Accept", "Separate ledger", people[1]["id"])
    invoke(store, sessions[1]["token"], "task_accept", {"task_id": other["id"]})
    row = next(
        m
        for m in invoke(store, sessions[1]["token"], "messages", {})["messages"]
        if m["id"] == other["request_message_id"]
    )
    assert row["read_receipt"] is None


def test_group_cannot_open_self_mail_can_open_and_reply_does_not_fabricate_read(team):
    store, project, people, sessions, message = team
    group = store.send_message(project["id"], "Group", "Public")
    myself = invoke(
        store,
        sessions[1]["token"],
        "message",
        {"title": "Self", "body": "Self", "recipient_id": people[1]["id"]},
    )
    with pytest.raises(WorkbenchError) as err:
        opened(store, sessions[1], group)
    assert err.value.code == "MESSAGE_NOT_ACCESSIBLE"
    assert (
        invoke(store, sessions[1]["token"], "messages", {})["messages"][-1]["read_receipt"] is None
    )
    with store._transaction(readonly=True) as db:
        assert (
            db.execute(
                "SELECT status FROM notifications WHERE kind='mailbox_wake' AND state_hash=?",
                (myself["id"],),
            ).fetchone()["status"]
            == "pending"
        )
    receipt = opened(store, sessions[1], myself)["message"]["read_receipt"]
    assert receipt["reader_id"] == people[1]["id"] and receipt["first_opened_at"]
    assert opened(store, sessions[1], myself)["message"]["read_receipt"] == receipt
    with store._transaction(readonly=True) as db:
        assert (
            db.execute(
                "SELECT status FROM notifications WHERE kind='mailbox_wake' AND state_hash=?",
                (myself["id"],),
            ).fetchone()["status"]
            == "recipient_opened"
        )
    assert (
        invoke(store, sessions[1]["token"], "messages", {"folder": "sent"})["messages"][-1][
            "read_receipt"
        ]
        == receipt
    )
    invoke(
        store,
        sessions[1]["token"],
        "message",
        {
            "title": "Reply",
            "body": "No open yet",
            "recipient_id": people[0]["id"],
            "reply_to": message["id"],
        },
    )
    row = next(
        m
        for m in invoke(store, sessions[0]["token"], "messages", {"folder": "sent"})["messages"]
        if m["id"] == message["id"]
    )
    assert row["read_receipt"] is None


def test_open_is_not_a_managed_readonly_autoapproval(team):
    from agent_mailbox.workbench_engine import read_only_tool_call

    assert not read_only_tool_call(
        {"name": "mcp__agent-mailbox__project_message_open", "kind": "read"}
    )
    store, _, _, sessions, message = team
    with store._transaction(readonly=True) as db:
        before = db.execute("PRAGMA user_version").fetchone()[0]
    opened(store, sessions[1], message)
    with store._transaction(readonly=True) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == before
