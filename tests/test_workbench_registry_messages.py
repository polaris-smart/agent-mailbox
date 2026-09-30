"""Real SQLite contracts: global identities, scoped runs, and project messages."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_mailbox.workbench_store import (
    EMPLOYEE_KINDS,
    SCHEMA_VERSION,
    WorkbenchError,
    WorkbenchStore,
)


@pytest.fixture
def store(tmp_path):
    return WorkbenchStore(tmp_path / "isolated")


@pytest.fixture
def project(store, tmp_path):
    return store.create_project("Isolated registry contracts", tmp_path)


def member(store, project, name="Worker", kind="codex", **kwargs):
    return store.create_employee(name, kind, project["id"], **kwargs)


def started(store, project, employee):
    task = store.create_task(
        project["id"], "Owned execution fixture", "No real AI is executed.", employee["id"]
    )
    assert store.claim_task(employee["node_id"])["id"] == task["id"]
    return task


def credentials_valid(store, credentials, **overrides):
    return store.validate_execution(**{**credentials, **overrides})


@pytest.mark.parametrize("kind", sorted(EMPLOYEE_KINDS))
def test_global_registry_has_no_synthetic_project_or_execution_claim(store, kind):
    employee = store.create_employee("Registered " + kind, kind)
    assert employee["project_id"] is None and employee["project_ids"] == []
    assert employee["execution_supported"] is (kind in {"codex", "claude"})
    assert employee["execution_verified"] is False
    assert employee["auth_status"] == "unknown"
    assert store.snapshot()["projects"] == []
    assert "secret_token" not in employee


@pytest.mark.parametrize(
    "kind,connection_type", [("gemini", "cli"), ("codex", "app"), ("claude", "endpoint")]
)
def test_unimplemented_connections_can_join_but_cannot_run(store, project, kind, connection_type):
    employee = store.create_employee(
        "Unsupported", kind, connection_type=connection_type, entrypoint="fixture-entry"
    )
    store.add_project_member(project["id"], employee["id"])
    with pytest.raises(WorkbenchError) as error:
        store.create_task(project["id"], "Must not queue", "Unsupported adapter", employee["id"])
    assert error.value.code == "ADAPTER_UNSUPPORTED"
    assert store.snapshot()["tasks"] == []
    assert store.snapshot()["employees"][0]["execution_verified"] is False


def test_discovery_auth_is_distinct_and_empty_legacy_entry_can_be_filled_once(store):
    employee = store.create_employee("Codex", "codex", status="available")
    refreshed = store.create_employee(
        "Codex", "codex", entrypoint="/fixture/codex", auth_status="authenticated"
    )
    assert refreshed["id"] == employee["id"] and refreshed["entrypoint"] == "/fixture/codex"
    assert refreshed["auth_status"] == "authenticated" and refreshed["execution_verified"] is False
    changed = store.update_employee(
        employee["id"], "available", "Fixture", auth_status="auth_required"
    )
    assert changed["auth_status"] == "auth_required" and changed["execution_verified"] is False
    assert store.update_employee(employee["id"], "unknown")["auth_status"] == "auth_required"
    with pytest.raises(WorkbenchError) as error:
        store.create_employee("Codex", "codex", entrypoint="/another/connection")
    assert error.value.code == "IDENTITY_CONNECTION_CONFLICT"


def test_membership_removal_revokes_only_one_project_and_cancels_queue(store, project, tmp_path):
    other = store.create_project("Other project", tmp_path)
    employee = store.create_employee("Global", "codex")
    store.add_project_member(project["id"], employee["id"])
    store.add_project_member(other["id"], employee["id"])
    first = store.employee_credentials(employee["id"], project["id"])
    second = store.employee_credentials(employee["id"], other["id"])
    assert store.add_project_member(project["id"], employee["id"])["project_ids"] == [
        project["id"],
        other["id"],
    ]
    assert store.employee_credentials(employee["id"], project["id"]) == first
    queued = store.create_task(project["id"], "Queued", "No execution", employee["id"])
    remaining = store.remove_project_member(project["id"], employee["id"])
    assert remaining["project_ids"] == [other["id"]]
    assert not store.validate_employee(first["token"], employee["id"], project["id"])
    assert store.validate_employee(second["token"], employee["id"], other["id"])
    assert store.get_task(queued["id"])["status"] == "cancelled"
    store.add_project_member(project["id"], employee["id"])
    assert store.employee_credentials(employee["id"], project["id"])["token"] != first["token"]


def test_cannot_remove_executing_member_or_rejoin_retired_identity(store, project):
    employee = member(store, project)
    task = started(store, project, employee)
    with pytest.raises(WorkbenchError) as error:
        store.remove_project_member(project["id"], employee["id"])
    assert error.value.code == "MEMBER_HAS_ACTIVE_TASK"
    assert store.get_task(task["id"])["status"] == "starting"
    store.set_employee_lifecycle(employee["id"], "retired", "Contract fixture")
    with pytest.raises(WorkbenchError):
        store.add_project_member(project["id"], employee["id"])
    with pytest.raises(WorkbenchError):
        store.create_employee(employee["name"], employee["kind"])


def test_execution_token_is_run_scoped_terminal_revoked_and_redacted(store, project, tmp_path):
    employee = member(store, project)
    task = started(store, project, employee)
    scoped = store.execution_credentials(task["id"])
    membership = store.employee_credentials(employee["id"], project["id"])
    assert scoped["token"] != membership["token"]
    assert credentials_valid(store, scoped)
    assert not credentials_valid(store, scoped, token=membership["token"])
    assert not credentials_valid(store, scoped, run_id="other-run")
    assert not credentials_valid(store, scoped, task_id="other-task")
    assert not credentials_valid(store, scoped, project_id="other-project")
    assert not credentials_valid(store, scoped, employee_id="other-employee")
    assert not credentials_valid(store, scoped, token="\ud800")
    store.add_event(
        task["id"],
        "Fixture",
        "Token " + scoped["token"],
        {"tool_token": scoped["token"], "nested": scoped["token"]},
    )
    store.set_status(task["id"], "running")
    store.finish_task(task["id"], "review", "Result " + scoped["token"])
    assert not credentials_valid(store, scoped)
    with pytest.raises(WorkbenchError):
        store.execution_credentials(task["id"])
    public = json.dumps(
        [
            store.snapshot(),
            store.get_task(task["id"]),
            store.task_detail(task["id"]),
            store.project_context(project["id"]),
        ]
    )
    assert scoped["token"] not in public and "tool_token" not in store.get_task(task["id"])
    assert store.snapshot()["employees"][0]["execution_verified"] is True
    assert WorkbenchStore(store.root).snapshot()["employees"][0]["execution_verified"] is True


def test_cancel_and_retire_immediately_invalidate_run_capability(store, project):
    employee = member(store, project)
    task = started(store, project, employee)
    scoped = store.execution_credentials(task["id"])
    store.cancel_task(task["id"])
    assert not credentials_valid(store, scoped)
    store.finish_task(task["id"], "cancelled")
    task = started(store, project, employee)
    scoped = store.execution_credentials(task["id"])
    store.set_employee_lifecycle(employee["id"], "retired", "Stop fixture")
    assert not credentials_valid(store, scoped)
    assert store.snapshot()["employees"][0]["execution_verified"] is False


def test_normal_group_messages_and_replies_do_not_wake(store, project):
    employee = member(store, project)
    initial = store.send_message(project["id"], "Group update", "Project context")
    assert (
        initial["task_id"] is None
        and initial["request_work"] is False
        and initial["sender"] is None
    )
    assert store.snapshot()["tasks"] == []
    task = started(store, project, employee)
    reply = store.send_message(
        project["id"],
        "Reply",
        "Recorded, no dispatch",
        sender_id=employee["id"],
        source_task_id=task["id"],
        reply_to=initial["id"],
    )
    assert reply["thread_id"] == initial["thread_id"] and reply["reply_to"] == initial["id"]
    assert reply["attribution"] == {"scope": "employee_session", "internal_actor_verified": False}
    assert reply["attribution_scope"] == "employee_session"
    assert reply["internal_actor_verified"] is False
    assert reply["sender_session_id"] == task["session_id"]
    assert reply["source_run_id"] == task["run_id"]
    assert initial["sender_session_id"] is None and initial["source_run_id"] is None
    assert reply["sender"]["id"] == employee["id"] and reply["task_id"] is None
    assert len(store.snapshot()["tasks"]) == 1
    assert store.snapshot()["messages"] == store.list_messages(project["id"])
    context = store.project_context(project["id"])
    assert {m["id"] for m in context["messages"]} == {initial["id"], reply["id"]}


def test_request_work_atomic_link_dedupe_and_log(store, project):
    sender = member(store, project, "Sender")
    recipient = member(store, project, "Recipient")
    source = started(store, project, sender)
    arguments = {
        "recipient_id": recipient["id"],
        "sender_id": sender["id"],
        "source_task_id": source["id"],
        "request_work": True,
        "request_id": "request-fixture",
    }
    message = store.send_message(
        project["id"], "Please review", "Read-only collaboration", **arguments
    )
    task = store.get_task(message["task_id"])
    assert task["permission_mode"] == "read-only" and task["status"] == "queued"
    assert task["request_message_id"] == message["id"] and task["source_task_id"] == source["id"]
    assert task["assignee_id"] == recipient["id"]
    assert (
        store.send_message(project["id"], "Please review", "Read-only collaboration", **arguments)
        == message
    )
    assert len(store.snapshot()["tasks"]) == 2 and len(store.list_messages(project["id"])) == 1
    event = store.task_detail(task["id"])["events"][-1]
    assert event["type"] == "team_message" and event["payload"]["from_id"] == sender["id"]
    with pytest.raises(WorkbenchError) as error:
        store.send_message(project["id"], "Changed request", "Different semantics", **arguments)
    assert error.value.code == "MESSAGE_REQUEST_CONFLICT"


def test_concurrent_human_request_retry_queues_exactly_once(store, project):
    employee = member(store, project)

    def send(_):
        return store.send_message(
            project["id"],
            "Human request",
            "Once only",
            recipient_id=employee["id"],
            request_work=True,
            request_id="human-once",
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        messages = list(pool.map(send, range(12)))
    assert len({m["id"] for m in messages}) == 1
    assert len(store.snapshot()["tasks"]) == 1
    assert len(store.list_messages(project["id"])) == 1


def test_invalid_work_request_rolls_back_message_and_governance(store, project):
    unsupported = member(store, project, kind="gemini")
    before = store.governance_events()
    with pytest.raises(WorkbenchError) as error:
        store.send_message(
            project["id"],
            "Cannot execute",
            "Should rollback",
            recipient_id=unsupported["id"],
            request_work=True,
        )
    assert error.value.code == "ADAPTER_UNSUPPORTED"
    assert store.list_messages(project["id"]) == [] and store.snapshot()["tasks"] == []
    assert store.governance_events() == before


def test_message_cross_project_sender_and_source_are_rejected(store, project, tmp_path):
    other = store.create_project("Other", tmp_path)
    employee = member(store, project)
    outsider = member(store, other, "Outsider")
    source = started(store, project, employee)
    parent = store.send_message(other["id"], "Other thread", "Cannot cross projects")
    invalid = [
        {"reply_to": parent["id"]},
        {"recipient_id": outsider["id"]},
        {"sender_id": employee["id"]},
        {"sender_id": outsider["id"], "source_task_id": source["id"]},
        {
            "sender_id": employee["id"],
            "source_task_id": source["id"],
            "recipient_id": employee["id"],
            "request_work": True,
        },
        {"source_task_id": source["id"]},
    ]
    for kwargs in invalid:
        with pytest.raises(WorkbenchError):
            store.send_message(project["id"], "Invalid", "Must reject", **kwargs)
    store.finish_task(source["id"], "failed")
    with pytest.raises(WorkbenchError):
        store.send_message(
            project["id"],
            "Old run",
            "Terminal origin",
            sender_id=employee["id"],
            source_task_id=source["id"],
        )
    assert store.list_messages(project["id"]) == []


def test_paused_sender_or_recipient_cannot_create_new_message(store, project):
    sender = member(store, project, "Sender")
    target = member(store, project, "Target")
    source = started(store, project, sender)
    store.set_employee_lifecycle(target["id"], "paused")
    with pytest.raises(WorkbenchError):
        store.send_message(project["id"], "Normal", "New direct message", recipient_id=target["id"])
    store.set_employee_lifecycle(sender["id"], "paused")
    with pytest.raises(WorkbenchError):
        store.send_message(
            project["id"],
            "Normal",
            "Session message",
            sender_id=sender["id"],
            source_task_id=source["id"],
        )


def test_employee_request_cycle_blocked_but_normal_reply_does_not_trigger(store, project):
    a, b = member(store, project, "A"), member(store, project, "B")
    source = started(store, project, a)
    first = store.send_message(
        project["id"],
        "A to B",
        "Work",
        recipient_id=b["id"],
        sender_id=a["id"],
        source_task_id=source["id"],
        request_work=True,
    )
    assert store.claim_task(b["node_id"])["id"] == first["task_id"]
    with pytest.raises(WorkbenchError) as error:
        store.send_message(
            project["id"],
            "B to A",
            "Would recurse",
            recipient_id=a["id"],
            sender_id=b["id"],
            source_task_id=first["task_id"],
            request_work=True,
        )
    assert error.value.code == "MESSAGE_REQUEST_CYCLE"
    reply = store.send_message(
        project["id"],
        "B reply",
        "No trigger",
        recipient_id=a["id"],
        sender_id=b["id"],
        source_task_id=first["task_id"],
        reply_to=first["id"],
    )
    assert reply["task_id"] is None and len(store.snapshot()["tasks"]) == 2


def test_employee_request_chain_has_four_trigger_limit(store, project):
    employees = [member(store, project, name) for name in "ABCDEF"]
    source = started(store, project, employees[0])
    for index in range(1, 5):
        message = store.send_message(
            project["id"],
            f"Link {index}",
            "Bounded work",
            recipient_id=employees[index]["id"],
            sender_id=employees[index - 1]["id"],
            source_task_id=source["id"],
            request_work=True,
        )
        source = store.claim_task(employees[index]["node_id"])
        assert source["id"] == message["task_id"]
    with pytest.raises(WorkbenchError) as error:
        store.send_message(
            project["id"],
            "Fifth trigger",
            "Must stop",
            recipient_id=employees[5]["id"],
            sender_id=employees[4]["id"],
            source_task_id=source["id"],
            request_work=True,
        )
    assert error.value.code == "MESSAGE_REQUEST_LIMIT"
    assert len(store.list_messages(project["id"])) == 4 and len(store.snapshot()["tasks"]) == 5


# Literal pre-registry v5 fixture, not a v6 database with its version lowered.
V5_SCHEMA = """
CREATE TABLE devices(id TEXT PRIMARY KEY,name TEXT NOT NULL,is_local INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'online',created_at TEXT NOT NULL,last_seen TEXT);
CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,path TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE employees(id TEXT PRIMARY KEY,name TEXT NOT NULL,kind TEXT NOT NULL,project_id TEXT NOT NULL REFERENCES projects(id),node_id TEXT NOT NULL REFERENCES devices(id),status TEXT NOT NULL,detail TEXT NOT NULL,secret_token TEXT NOT NULL,created_at TEXT NOT NULL,lifecycle TEXT NOT NULL DEFAULT 'active',lifecycle_reason TEXT NOT NULL DEFAULT '',lifecycle_changed_at TEXT);
CREATE TABLE memberships(employee_id TEXT NOT NULL REFERENCES employees(id),project_id TEXT NOT NULL REFERENCES projects(id),secret_token TEXT NOT NULL,PRIMARY KEY(employee_id,project_id));
CREATE TABLE tasks(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),title TEXT NOT NULL,prompt TEXT NOT NULL,assignee_id TEXT NOT NULL REFERENCES employees(id),node_id TEXT NOT NULL REFERENCES devices(id),permission_mode TEXT NOT NULL,run_id TEXT NOT NULL UNIQUE,session_id TEXT NOT NULL UNIQUE,status TEXT NOT NULL DEFAULT 'queued',cancel_requested INTEGER NOT NULL DEFAULT 0,result TEXT NOT NULL DEFAULT '',error TEXT,model TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE events(id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),type TEXT NOT NULL,message TEXT NOT NULL,payload TEXT,created_at TEXT NOT NULL);
CREATE TABLE permissions(task_id TEXT NOT NULL REFERENCES tasks(id),request_id TEXT NOT NULL,run_id TEXT NOT NULL,options TEXT NOT NULL,tool_call TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',decision TEXT,created_at TEXT NOT NULL,resolved_at TEXT,expires_at TEXT,PRIMARY KEY(task_id,request_id));
CREATE TABLE memories(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),title TEXT NOT NULL,body TEXT NOT NULL,source TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE resources(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),name TEXT NOT NULL,kind TEXT NOT NULL,path TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE governance_events(id TEXT PRIMARY KEY,employee_id TEXT REFERENCES employees(id),project_id TEXT REFERENCES projects(id),task_id TEXT REFERENCES tasks(id),type TEXT NOT NULL,actor TEXT NOT NULL,reason TEXT NOT NULL,payload TEXT,created_at TEXT NOT NULL);
CREATE TABLE remote_receipts(task_id TEXT NOT NULL REFERENCES tasks(id),run_id TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(task_id,run_id));
PRAGMA user_version=5;
"""


def legacy_v5(root, *, corrupt=False):
    folder = root / "workbench"
    folder.mkdir(parents=True)
    path = folder / "state.db"
    with sqlite3.connect(path) as db:
        db.executescript(V5_SCHEMA)
        db.execute("INSERT INTO devices VALUES('node-old','Old Mac',1,'online','2026-09-30',NULL)")
        db.execute(
            "INSERT INTO projects VALUES('project-old','Existing project','/tmp','2026-09-30')"
        )
        db.execute(
            "INSERT INTO employees VALUES('employee-old','Existing Codex','codex','project-old','node-old','available','','fixture-employee-secret','2026-09-30','active','',NULL)"
        )
        db.execute(
            "INSERT INTO memberships VALUES('employee-old','project-old','fixture-project-secret')"
        )
        db.execute(
            "INSERT INTO tasks VALUES('task-old','project-old','Historic accepted work','Historic prompt','employee-old','node-old','read-only','run-old','session-old','done',0,'Historic result',NULL,NULL,'2026-09-30','2026-09-30')"
        )
        db.execute(
            "INSERT INTO events VALUES('event-old','task-old','done','Historic outcome','{}','2026-09-30')"
        )
        db.execute(
            "INSERT INTO memories VALUES('memory-old','project-old','Decision','Keep context','human','2026-09-30')"
        )
        db.execute(
            "INSERT INTO governance_events VALUES('ledger-old','employee-old','project-old','task-old','task_reviewed','human','Accepted','{}','2026-09-30')"
        )
        db.execute("INSERT INTO remote_receipts VALUES('task-old','run-old','retained-proof')")
        if corrupt:
            db.execute(
                "INSERT INTO memberships VALUES('missing-employee','project-old','orphan-fixture')"
            )
    return path


def test_literal_v5_migration_preserves_identity_tokens_history_and_private_backup(tmp_path):
    root = tmp_path / "old-home"
    legacy_v5(root)
    store = WorkbenchStore(root)
    assert store.local_node()["id"] == "node-old"
    assert store.validate_employee("fixture-project-secret", "employee-old", "project-old")
    employee = store.snapshot()["employees"][0]
    assert employee["id"] == "employee-old" and employee["project_ids"] == ["project-old"]
    assert employee["auth_status"] == "unknown" and employee["execution_verified"] is False
    assert store.get_task("task-old")["session_id"] == "session-old"
    assert store.get_task("task-old")["result"] == "Historic result"
    assert store.task_detail("task-old")["events"][0]["id"] == "event-old"
    assert store.project_context("project-old")["memories"][0]["id"] == "memory-old"
    assert store.governance_events()[0]["id"] == "ledger-old"
    assert (
        store.migration_backup_path and store.migration_backup_path.stat().st_mode & 0o777 == 0o600
    )
    with sqlite3.connect(store.migration_backup_path) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 5
        assert {r[1]: r[3] for r in backup.execute("PRAGMA table_info(employees)")}[
            "project_id"
        ] == 1
        assert "auth_status" not in [r[1] for r in backup.execute("PRAGMA table_info(employees)")]
        assert (
            backup.execute("SELECT secret_token FROM memberships").fetchone()[0]
            == "fixture-project-secret"
        )
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert {r[1]: r[3] for r in db.execute("PRAGMA table_info(employees)")}["project_id"] == 0
        assert db.execute("SELECT digest FROM remote_receipts").fetchone()[0] == "retained-proof"
        assert db.execute("SELECT tool_token FROM tasks").fetchone()[0]
    orphan = store.create_employee("New global employee", "gemini")
    assert orphan["project_id"] is None and len(store.snapshot()["projects"]) == 1
    backup = store.migration_backup_path
    reopened = WorkbenchStore(root)
    assert reopened.migration_backup_path is None
    assert len(list(store.directory.glob("state-v5-before-v6-*.sqlite"))) == 1
    assert backup.exists()


def test_failed_migration_restores_v5_schema_and_rows_with_prechange_backup(tmp_path):
    root = tmp_path / "corrupt-v5"
    path = legacy_v5(root, corrupt=True)
    with pytest.raises(WorkbenchError) as error:
        WorkbenchStore(root)
    assert error.value.code == "migration_failed"
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert {r[1]: r[3] for r in db.execute("PRAGMA table_info(employees)")}["project_id"] == 1
        assert "auth_status" not in [r[1] for r in db.execute("PRAGMA table_info(employees)")]
        assert db.execute("SELECT id,result FROM tasks").fetchone() == (
            "task-old",
            "Historic result",
        )
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='messages'").fetchone()
    backups = list(path.parent.glob("state-v5-before-v6-*.sqlite"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 5
        assert backup.execute("SELECT count(*) FROM memberships").fetchone()[0] == 2
