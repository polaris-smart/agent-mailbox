"""Existing sessions exchange project mail without managed tasks or model calls."""

import json

import pytest

from agent_mailbox.workbench_mail_sessions import (
    create_session,
    invoke,
    list_sessions,
    revoke_session,
)
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


@pytest.fixture
def team(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Team", str(tmp_path))
    alice = store.create_employee("Alice", "codex", project["id"], connection_type="app")
    bob = store.create_employee("Bob", "claude", project["id"])
    sessions = [
        create_session(store, employee["id"], project["id"], "Existing conversation")
        for employee in (alice, bob)
    ]
    return store, project, alice, bob, sessions


def test_mail_exchange_without_tasks_and_session_attribution(team):
    store, _project, alice, bob, (a, b) = team
    first = invoke(
        store,
        a["token"],
        "message",
        {
            "title": "Review",
            "body": "Please review",
            "recipient_id": bob["id"],
            "request_id": "first",
        },
    )
    assert first["source_task_id"] is None and first["task_id"] is None
    assert first["sender_session_id"] == a["id"]
    assert first["source_session_id"] == a["id"]
    assert not first["request_work"] and not first["internal_actor_verified"]
    inbox = invoke(store, b["token"], "messages", {})
    assert inbox["messages"][0]["id"] == first["id"]
    assert not inbox["viewing_acknowledges"]
    reply = invoke(
        store,
        b["token"],
        "message",
        {
            "title": "Reply",
            "body": "Reviewed",
            "recipient_id": alice["id"],
            "reply_to": first["id"],
        },
    )
    assert reply["thread_id"] == first["id"] and reply["sender_id"] == bob["id"]
    assert store.snapshot()["tasks"] == []
    assert (
        invoke(store, a["token"], "context", {})["mailbox_policy"]["notification_mode"]
        == "manual_check"
    )
    assert (
        invoke(
            store,
            a["token"],
            "message",
            {
                "title": "Review",
                "body": "Please review",
                "recipient_id": bob["id"],
                "request_id": "first",
            },
        )["id"]
        == first["id"]
    )
    with pytest.raises(WorkbenchError, match="同一个请求编号"):
        invoke(
            store,
            a["token"],
            "message",
            {
                "title": "Review",
                "body": "Different",
                "recipient_id": bob["id"],
                "request_id": "first",
            },
        )


def test_identity_tool_and_cross_project_rejections(team, tmp_path):
    store, project, alice, bob, (a, _b) = team
    other = store.create_project("Other", str(tmp_path))
    outsider = store.create_employee("Outside", "codex", other["id"])
    for tool, args in (
        ("message", {"sender_id": bob["id"]}),
        ("context", {"project_id": other["id"]}),
        ("message", {"request_work": True}),
        ("team_message", {}),
        ("approve", {}),
    ):
        with pytest.raises(WorkbenchError):
            invoke(store, a["token"], tool, args)
    with pytest.raises(WorkbenchError):
        invoke(
            store,
            a["token"],
            "message",
            {"title": "Out", "body": "No", "recipient_id": outsider["id"]},
        )
    private = store.send_message(project["id"], "Private", "For Bob", recipient_id=bob["id"])
    with pytest.raises(WorkbenchError):
        invoke(
            store,
            a["token"],
            "message",
            {"title": "Snoop", "body": "No", "reply_to": private["id"]},
        )
    assert private["id"] not in {
        m["id"] for m in invoke(store, a["token"], "messages", {})["messages"]
    }
    for token in (
        store.employee_credentials(alice["id"], project["id"])["token"],
        "bad",
        "\ud800" * 30,
    ):
        with pytest.raises(WorkbenchError):
            invoke(store, token, "context", {})


def test_revocation_restart_and_membership_generation(team):
    store, project, alice, _bob, (a, b) = team
    second = create_session(store, alice["id"], project["id"], "Another App")
    revoked = revoke_session(store, a["id"])
    assert not revoked["active"] and revoked["revoked_at"]
    assert invoke(store, second["token"], "context", {})
    restarted = WorkbenchStore(store.root)
    with pytest.raises(WorkbenchError):
        invoke(restarted, a["token"], "context", {})
    store.remove_project_member(project["id"], alice["id"])
    store.add_project_member(project["id"], alice["id"])
    with pytest.raises(WorkbenchError):
        invoke(store, second["token"], "context", {})
    assert not list_sessions(store, alice["id"], project["id"])[1]["active"]
    assert invoke(store, b["token"], "context", {})


@pytest.mark.parametrize("lifecycle", ["paused", "retired"])
def test_inactive_employee_cannot_use_or_issue_session(team, lifecycle):
    store, project, alice, _, (a, _) = team
    store.set_employee_lifecycle(alice["id"], lifecycle, "Fixture")
    with pytest.raises(WorkbenchError):
        invoke(store, a["token"], "context", {})
    with pytest.raises(WorkbenchError):
        create_session(store, alice["id"], project["id"], "Forbidden")


def test_only_approved_snapshots_and_explicit_proposals(team, tmp_path):
    store, project, alice, _, (a, _) = team
    path = tmp_path / "PRD.md"
    path.write_text("Approved")
    resource = store.add_resource(project["id"], "PRD", "prd", path)
    with pytest.raises(WorkbenchError):
        invoke(store, a["token"], "resource_read", {"resource_id": resource["id"]})
    baseline = store.capture_resource_version(project["id"], resource["id"])
    path.write_text("Unapproved live source")
    read = invoke(store, a["token"], "resource_read", {"resource_id": resource["id"]})
    assert read["content"] == "Approved" and read["version"]["id"] == baseline["id"]
    with pytest.raises(WorkbenchError):
        invoke(store, a["token"], "resource_read", {"resource_id": resource["id"], "live": True})
    with pytest.raises(WorkbenchError):
        invoke(store, a["token"], "resource_propose", {"resource_id": resource["id"]})
    proposed = invoke(
        store,
        a["token"],
        "resource_propose",
        {"resource_id": resource["id"], "content": "Proposal " + a["token"]},
    )
    assert proposed["status"] == "proposed"
    with pytest.raises(WorkbenchError):
        invoke(
            store,
            a["token"],
            "resource_read",
            {"resource_id": resource["id"], "version_id": proposed["id"]},
        )
    assert (
        a["token"]
        not in store.read_resource_version(project["id"], resource["id"], proposed["id"])["content"]
    )
    note = invoke(store, a["token"], "note", {"title": "Log", "body": "Note " + a["token"]})
    assert "[redacted]" in note["body"]
    mail = invoke(store, a["token"], "message", {"title": "Mail", "body": "Token " + a["token"]})
    assert "[redacted]" in mail["body"]
    assert a["token"] not in json.dumps(
        [list_sessions(store, alice["id"], project["id"]), store.snapshot()]
    )
    with store._connection() as db:
        row = dict(db.execute("SELECT * FROM mailbox_sessions WHERE id=?", (a["id"],)).fetchone())
        assert a["token"] not in json.dumps(row)


def test_expiry_and_direct_store_cannot_bypass_identity(team):
    store, project, alice, bob, (a, _) = team
    with pytest.raises(WorkbenchError):
        store.send_message(
            project["id"], "Impersonate", "No", sender_id=bob["id"], mailbox_token=a["token"]
        )
    with pytest.raises(WorkbenchError):
        store.send_message(
            project["id"],
            "Wake",
            "No",
            sender_id=alice["id"],
            recipient_id=bob["id"],
            request_work=True,
            mailbox_token=a["token"],
        )
    with store._transaction() as db:
        db.execute(
            "UPDATE mailbox_sessions SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (a["id"],),
        )
    with pytest.raises(WorkbenchError):
        invoke(store, a["token"], "note", {"title": "Expired", "body": "No"})
    assert store.snapshot()["memories"] == []


def test_schema9_migration_preserves_existing_messages_and_membership(team):
    store, project, alice, _, _ = team
    old = store.send_message(project["id"], "Before", "Migration fixture")
    membership = store.employee_credentials(alice["id"], project["id"])
    with store._connection() as db:
        db.execute("ALTER TABLE messages DROP COLUMN source_session_id")
        db.execute("DROP TABLE mailbox_sessions")
        db.execute("PRAGMA user_version=9")
    upgraded = WorkbenchStore(store.root)
    assert upgraded.migration_backup_path.is_file()
    assert upgraded.list_messages(project["id"])[0]["id"] == old["id"]
    assert upgraded.list_messages(project["id"])[0]["source_session_id"] is None
    assert upgraded.employee_credentials(alice["id"], project["id"]) == membership
    session = create_session(upgraded, alice["id"], project["id"], "After migration")
    assert invoke(upgraded, session["token"], "context", {})


def test_session_authorization_and_writes_share_transaction(team, monkeypatch):
    import agent_mailbox.workbench_mail_sessions as domain

    store, project, _, _, (a, _) = team
    original = domain.validate_session
    transactions = []

    def validate(bound, db, token):
        assert db.in_transaction
        transactions.append(db)
        return original(bound, db, token)

    monkeypatch.setattr(domain, "validate_session", validate)
    invoke(store, a["token"], "message", {"title": "Atomic", "body": "One transaction"})
    assert len(transactions) == 2 and transactions[0] is transactions[1]
    assert len(store.list_messages(project["id"])) == 1
    revoke_session(store, a["id"])
    with pytest.raises(WorkbenchError):
        invoke(store, a["token"], "message", {"title": "After revoke", "body": "Forbidden"})
    assert len(store.list_messages(project["id"])) == 1


def test_session_cannot_read_other_project_or_bind_remote_employee(team, tmp_path):
    store, _project, _alice, _bob, (a, _) = team
    other = store.create_project("Other resources", str(tmp_path))
    path = tmp_path / "Private.md"
    path.write_text("Other project only")
    resource = store.add_resource(other["id"], "Private", "prd", path)
    store.capture_resource_version(other["id"], resource["id"])
    for tool in ("resource_read", "resource_versions"):
        with pytest.raises(WorkbenchError):
            invoke(store, a["token"], tool, {"resource_id": resource["id"]})
    remote = store.upsert_device("remote-fixture", "Remote fixture", "online")
    employee = store.create_employee("Remote employee", "codex", other["id"], node_id=remote["id"])
    with pytest.raises(WorkbenchError):
        create_session(store, employee["id"], other["id"], "Local grant forbidden")
