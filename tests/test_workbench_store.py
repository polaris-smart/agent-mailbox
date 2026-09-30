"""Domain invariants: atomic claims, scoped credentials and durable lifecycle."""

import json
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_mailbox.workbench_store import SCHEMA_VERSION, WorkbenchError, WorkbenchStore


@pytest.fixture
def store(tmp_path):
    return WorkbenchStore(tmp_path / "data")


@pytest.fixture
def project(store, tmp_path):
    directory = tmp_path / "project"
    directory.mkdir()
    return store.create_project("Demo", directory)


@pytest.fixture
def employee(store, project):
    return store.create_employee("Codex", "codex", project["id"])


def queued(store, project, employee, **kwargs):
    return store.create_task(
        project["id"], "Build", "Make a useful change", employee["id"], **kwargs
    )


def running(store, project, employee):
    task = queued(store, project, employee)
    assert store.claim_task(store.local_node()["id"])["id"] == task["id"]
    return store.set_status(task["id"], "running")


def test_new_store_private_and_stable(store):
    assert store.directory.stat().st_mode & 0o777 == 0o700
    assert store.db_path.stat().st_mode & 0o777 == 0o600
    node = store.local_node()
    assert WorkbenchStore(store.root).local_node() == node
    assert store.snapshot()["devices"][0]["id"] == node["id"]
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_projects_require_real_directory(store, tmp_path):
    file = tmp_path / "file"
    file.write_text("not a directory")
    for path in (tmp_path / "missing", file, 123):
        with pytest.raises(WorkbenchError):
            store.create_project("Project", path)
    with pytest.raises(WorkbenchError):
        store.create_project("", tmp_path)
    assert store.snapshot()["projects"] == []


def test_employee_reused_across_projects_with_scoped_stable_tokens(
    store, project, employee, tmp_path
):
    other = store.create_project("Other", tmp_path)
    first = store.employee_credentials(employee["id"], project["id"])
    assert not store.validate_employee(first["token"], employee["id"], other["id"])
    second_employee = store.create_employee("Codex", "codex", other["id"])
    assert second_employee["id"] == employee["id"]
    assert second_employee["project_id"] == project["id"]
    assert second_employee["project_ids"] == [project["id"], other["id"]]
    second = store.employee_credentials(employee["id"], other["id"])
    assert second["token"] != first["token"]
    assert store.validate_employee(first["token"], employee["id"], project["id"])
    assert not store.validate_employee(first["token"], employee["id"], other["id"])
    assert not store.validate_employee(second["token"], employee["id"], project["id"])
    restarted = WorkbenchStore(store.root)
    assert restarted.employee_credentials(employee["id"], project["id"]) == first
    assert restarted.employee_credentials(employee["id"], other["id"]) == second
    assert len(store.snapshot()["employees"]) == 1
    assert not store.validate_employee("bad", "missing", project["id"])
    assert not store.validate_employee("\ud800", employee["id"], project["id"])
    assert not store.validate_employee([], employee["id"], project["id"])


def test_cross_project_assignment_and_credentials_rejected(store, project, employee, tmp_path):
    other = store.create_project("Other", tmp_path)
    with pytest.raises(WorkbenchError) as error:
        store.create_task(other["id"], "Bad", "Cross project", employee["id"])
    assert error.value.code == "permission_denied"
    with pytest.raises(WorkbenchError):
        store.employee_credentials(employee["id"], other["id"])
    assert store.snapshot()["tasks"] == []


def test_remote_device_routes_its_own_work(store, project):
    remote = store.upsert_device("remote-node", "Linux", "online", "2026-09-30T00:00:00Z")
    assert not remote["is_local"]
    employee = store.create_employee("Remote", "claude", project["id"], remote["id"])
    task = queued(store, project, employee)
    assert store.claim_task(store.local_node()["id"]) is None
    assert store.claim_task(remote["id"])["id"] == task["id"]
    changed = store.update_employee(employee["id"], "auth_required", "Please sign in")
    assert changed["status"] == "auth_required"
    assert changed["project_ids"] == [project["id"]]
    assert store.upsert_device(remote["id"], "Linux renamed", "offline")["name"] == "Linux renamed"


def test_atomic_multithread_claims_unique(store, project):
    expected = set()
    for i in range(30):
        employee = store.create_employee(f"Employee {i}", "codex", project["id"])
        expected.add(queued(store, project, employee)["id"])

    def drain(_):
        claimed = []
        own_store = WorkbenchStore(store.root)
        while task := own_store.claim_task(own_store.local_node()["id"]):
            claimed.append(task["id"])
        return claimed

    with ThreadPoolExecutor(max_workers=10) as pool:
        all_claims = [task_id for batch in pool.map(drain, range(10)) for task_id in batch]
    assert len(all_claims) == len(set(all_claims)) == 30
    assert set(all_claims) == expected
    assert {task["status"] for task in store.snapshot()["tasks"]} == {"starting"}


def test_same_employee_claim_serialized_across_projects(store, project, employee, tmp_path):
    other = store.create_project("Other", tmp_path)
    store.create_employee("Codex", "codex", other["id"])
    first = queued(store, project, employee)
    second = queued(store, other, employee)
    node = store.local_node()["id"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.claim_task(node), range(8)))
    claimed = [task for task in results if task is not None]
    assert [task["id"] for task in claimed] == [first["id"]]
    assert store.get_task(second["id"])["status"] == "queued"
    store.set_status(first["id"], "running")
    store.request_permission(first["id"], "request", [{"kind": "allow_once"}], {"title": "Write"})
    assert store.claim_task(node) is None
    store.resolve_permission(first["id"], "request", "deny")
    store.finish_task(first["id"], "review", "Complete")
    assert store.claim_task(node)["id"] == second["id"]


def test_review_requires_real_execution_then_explicit_accept(store, project, employee):
    task = queued(store, project, employee)
    assert task["status"] == "queued" and not task["cancel_requested"]
    for method in (
        lambda: store.set_status(task["id"], "done"),
        lambda: store.finish_task(task["id"], "review"),
        lambda: store.review_task(task["id"], "accept"),
    ):
        with pytest.raises(WorkbenchError):
            method()
    claimed = store.claim_task(store.local_node()["id"])
    assert claimed["run_id"] == task["run_id"]
    assert claimed["session_id"] == task["session_id"]
    store.set_status(task["id"], "running")
    assert store.finish_task(task["id"], "review", "Changed files")["status"] == "review"
    assert store.review_task(task["id"], "accept", "Looks good")["status"] == "done"
    with pytest.raises(WorkbenchError):
        store.set_status(task["id"], "running")


def test_review_rejection_recorded(store, project, employee):
    task = running(store, project, employee)
    store.finish_task(task["id"], "review", "Result")
    rejected = store.review_task(task["id"], "reject", "Missing tests")
    assert rejected["status"] == "failed"
    assert rejected["error"] == {"code": "REVIEW_REJECTED", "message": "Missing tests"}


def test_cancel_waits_for_active_worker_stop(store, project, employee):
    task = queued(store, project, employee)
    assert store.cancel_task(task["id"])["status"] == "cancelled"
    assert store.claim_task(store.local_node()["id"]) is None
    active = running(store, project, employee)
    cancelled = store.cancel_task(active["id"])
    assert cancelled["status"] == "running" and cancelled["cancel_requested"] is True
    with pytest.raises(WorkbenchError):
        store.request_permission(active["id"], "late", [{"kind": "allow_once"}], {})
    assert store.finish_task(active["id"], "review", "Late result")["status"] == "cancelled"


def test_restart_interrupts_without_requeue_and_expires_approval(store, project, employee):
    active = running(store, project, employee)
    store.request_permission(active["id"], "pending", [{"kind": "allow_once"}], {})
    second = queued(store, project, employee)
    restarted = WorkbenchStore(store.root)
    recovered = restarted.recover_runs(restarted.local_node()["id"])
    assert [task["id"] for task in recovered] == [active["id"]]
    assert recovered[0]["status"] == "interrupted"
    assert restarted.get_task(active["id"])["run_id"] == active["run_id"]
    assert restarted.get_permission(active["id"], "pending")["status"] == "expired"
    with pytest.raises(WorkbenchError):
        restarted.resolve_permission(active["id"], "pending", "allow_once")
    assert restarted.claim_task(restarted.local_node()["id"])["id"] == second["id"]


def test_permission_resolve_compare_and_swap(store, project, employee):
    task = running(store, project, employee)
    store.request_permission(task["id"], "prompt", [{"kind": "allow_once"}], {"title": "Read"})
    assert store.get_task(task["id"])["status"] == "waiting_approval"

    def answer(decision):
        try:
            return store.resolve_permission(task["id"], "prompt", decision)["decision"]
        except WorkbenchError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(answer, ["allow_once", "deny"]))
    assert answers.count("invalid_state") == 1
    assert store.get_permission(task["id"], "prompt")["decision"] in {"allow_once", "deny"}
    assert store.get_task(task["id"])["status"] == "running"


def test_permissions_cannot_allow_after_finish_or_cancel(store, project, employee):
    for finish in ("review", "failed", "cancelled", "interrupted", "cancel-request"):
        task = running(store, project, employee)
        store.request_permission(task["id"], "request", [{"kind": "allow_once"}], {})
        if finish == "cancel-request":
            store.cancel_task(task["id"])
        else:
            store.finish_task(task["id"], finish)
        with pytest.raises(WorkbenchError):
            store.resolve_permission(task["id"], "request", "allow_once")
        assert store.get_permission(task["id"], "request")["status"] == "expired"
        if finish == "cancel-request":
            store.finish_task(task["id"], "cancelled")


def test_multiple_permissions_must_all_resolve(store, project, employee):
    task = running(store, project, employee)
    for request_id in ("one", "two"):
        store.request_permission(task["id"], request_id, [{"kind": "allow_once"}], {})
    store.resolve_permission(task["id"], "one", "allow_once")
    assert store.get_task(task["id"])["status"] == "waiting_approval"
    with pytest.raises(WorkbenchError):
        store.set_status(task["id"], "running")
    store.resolve_permission(task["id"], "two", "deny")
    assert store.get_task(task["id"])["status"] == "running"


def test_snapshot_events_and_context_never_reveal_credentials(store, project, employee):
    token = store.employee_credentials(employee["id"], project["id"])["token"]
    task = queued(store, project, employee)
    event = store.add_event(
        task["id"],
        "output",
        f"Token {token}",
        {"nested": [{"token": token, "Authorization": "Bearer private"}], "text": token},
    )
    assert token not in json.dumps(event)
    assert event["payload"]["nested"][0]["Authorization"] == "[redacted]"
    store.add_memory(project["id"], "Secret", token)
    assert token not in json.dumps(store.snapshot())
    assert token not in json.dumps(store.project_context(project["id"]))
    assert token not in json.dumps(store.task_detail(task["id"]))
    assert "secret_token" not in json.dumps(store.snapshot())


def test_memory_search_sql_injection_and_wildcards_are_literal(store, project, tmp_path):
    injection = "' OR 1=1 --"
    store.add_memory(project["id"], "Rule", "Prefer tests")
    memory = store.add_memory(project["id"], "Untrusted", injection + " 50% _under_")
    other = store.create_project("Other", tmp_path)
    store.add_memory(other["id"], "Other rule", "Prefer tests")
    assert [r["id"] for r in store.search_memory(project["id"], injection)] == [memory["id"]]
    assert [r["id"] for r in store.search_memory(project["id"], "%")] == [memory["id"]]
    assert len(store.search_memory(project["id"], "tests")) == 1
    assert store.search_memory(project["id"], "x' UNION SELECT * FROM employees --") == []
    store.delete_memory(memory["id"])
    assert store.search_memory(project["id"], injection) == []


def test_resource_scoped_utf8_and_live_validation(store, project, tmp_path):
    file = tmp_path / "brief.md"
    file.write_text("中文资料\n' DROP TABLE tasks;", encoding="utf-8")
    resource = store.add_resource(project["id"], "Brief", "document", file)
    read = store.read_resource(project["id"], resource["id"])
    assert read["content"] == file.read_text(encoding="utf-8")
    assert read["source"] == str(file.resolve())
    other = store.create_project("Other", tmp_path)
    with pytest.raises(WorkbenchError) as error:
        store.read_resource(other["id"], resource["id"])
    assert error.value.code == "permission_denied"
    file.unlink()
    sensitive = tmp_path / "auth.json"
    sensitive.write_text('{"token":"secret"}')
    file.symlink_to(sensitive)
    with pytest.raises(WorkbenchError):
        store.read_resource(project["id"], resource["id"])


@pytest.mark.parametrize(
    "name,body",
    [
        (".env", b"API_KEY=secret"),
        (".env.local", b"KEY=secret"),
        ("auth.json", b"{}"),
        ("credentials.toml", b"key='secret'"),
        ("huge.md", b"x" * (1024 * 1024 + 1)),
        ("binary.md", b"x\0y"),
        ("not-utf8.md", b"\xff\xfe"),
    ],
)
def test_sensitive_oversized_and_binary_resources_rejected(store, project, tmp_path, name, body):
    file = tmp_path / name
    file.write_bytes(body)
    with pytest.raises(WorkbenchError):
        store.add_resource(project["id"], "Resource", "document", file)
    assert store.snapshot()["resources"] == []


def test_limits_invalid_types_and_invalid_states_are_domain_errors(store, project, employee):
    task = queued(store, project, employee)
    actions = [
        lambda: store.create_employee("X", [], project["id"]),
        lambda: store.update_employee(employee["id"], []),
        lambda: store.create_task(project["id"], "Task", "Text", employee["id"], "all-access"),
        lambda: store.create_task(project["id"], "Task", "x" * (1024 * 1024 + 1), employee["id"]),
        lambda: store.set_status(task["id"], []),
        lambda: store.finish_task(task["id"], "done"),
        lambda: store.review_task(task["id"], 1),
        lambda: store.add_event(task["id"], "output", "Text", {"x": float("nan")}),
        lambda: store.request_permission(task["id"], "id", [], {}),
        lambda: store.backup(123),
    ]
    for action in actions:
        with pytest.raises(WorkbenchError):
            action()


def test_backup_restores_consistent_identity_tasks_credentials(store, project, employee, tmp_path):
    task = running(store, project, employee)
    credentials = store.employee_credentials(employee["id"], project["id"])
    file = store.backup(tmp_path / "backup.sqlite")
    assert file.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(file) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    restored_root = tmp_path / "restored"
    restored_folder = restored_root / "workbench"
    restored_folder.mkdir(parents=True)
    shutil.copyfile(file, restored_folder / "state.db")
    restored = WorkbenchStore(restored_root)
    assert restored.local_node() == store.local_node()
    assert restored.get_task(task["id"]) == store.get_task(task["id"])
    assert restored.employee_credentials(employee["id"], project["id"]) == credentials
    with pytest.raises(WorkbenchError):
        store.backup(file)
    with pytest.raises(WorkbenchError):
        store.backup(store.db_path)


def test_version_one_migration_preserves_old_credentials_and_membership(tmp_path):
    root = tmp_path / "legacy"
    folder = root / "workbench"
    folder.mkdir(parents=True)
    with sqlite3.connect(folder / "state.db") as db:
        db.executescript("""
            CREATE TABLE devices(id TEXT PRIMARY KEY,name TEXT NOT NULL,is_local INTEGER NOT NULL,
                                 status TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,path TEXT NOT NULL,
                                  created_at TEXT NOT NULL);
            CREATE TABLE employees(id TEXT PRIMARY KEY,name TEXT NOT NULL,kind TEXT NOT NULL,
                                   project_id TEXT NOT NULL,node_id TEXT NOT NULL,status TEXT NOT NULL,
                                   detail TEXT NOT NULL,secret_token TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE memberships(employee_id TEXT NOT NULL,project_id TEXT NOT NULL,
                                     PRIMARY KEY(employee_id,project_id));
            INSERT INTO devices VALUES('node-old','Mac',1,'online','2026-09-30');
            INSERT INTO projects VALUES('project-old','Old','/tmp','2026-09-30');
            INSERT INTO employees VALUES('employee-old','Codex','codex','project-old','node-old',
                                         'unknown','','old-credential','2026-09-30');
            INSERT INTO memberships VALUES('employee-old','project-old');
            PRAGMA user_version=1;
        """)
    migrated = WorkbenchStore(root)
    assert migrated.local_node()["id"] == "node-old"
    assert migrated.employee_credentials("employee-old", "project-old")["token"] == "old-credential"
    assert migrated.validate_employee("old-credential", "employee-old", "project-old")
    assert migrated.snapshot()["employees"][0]["project_ids"] == ["project-old"]
    assert migrated.snapshot()["devices"][0]["last_seen"] is None
    assert "old-credential" not in json.dumps(migrated.snapshot())


def test_future_schema_rejected(tmp_path):
    root = tmp_path / "future"
    folder = root / "workbench"
    folder.mkdir(parents=True)
    with sqlite3.connect(folder / "state.db") as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(WorkbenchError) as error:
        WorkbenchStore(root)
    assert error.value.code == "incompatible_version"


def test_pause_blocks_new_work_and_claim_without_interrupting_active(store, project, employee):
    task = running(store, project, employee)
    pending = queued(store, project, employee)
    paused = store.set_employee_lifecycle(employee["id"], "paused", "暂时离岗")
    assert paused["employee"]["lifecycle"] == "paused"
    assert store.get_task(task["id"])["status"] == "running"
    with pytest.raises(WorkbenchError, match="暂停"):
        queued(store, project, employee)
    store.finish_task(task["id"], "review")
    assert store.claim_task(store.local_node()["id"]) is None
    store.set_employee_lifecycle(employee["id"], "active", "恢复")
    assert store.claim_task(store.local_node()["id"])["id"] == pending["id"]


def test_retirement_revokes_all_projects_cancels_queue_and_preserves_history(
    store, project, employee, tmp_path
):
    other = store.create_project("Second", tmp_path)
    store.create_employee("Codex", "codex", other["id"])
    credentials = [store.employee_credentials(employee["id"], p["id"]) for p in (project, other)]
    task = running(store, project, employee)
    pending = queued(store, other, employee)
    store.request_permission(task["id"], "approval", [{"kind": "allow_once"}], {})
    retired = store.set_employee_lifecycle(employee["id"], "retired", "项目结束")
    assert len(retired["tasks"]) == 2
    assert store.get_task(task["id"])["cancel_requested"]
    assert store.get_task(task["id"])["status"] == "waiting_approval"
    assert store.get_task(pending["id"])["status"] == "cancelled"
    assert store.get_permission(task["id"], "approval")["status"] == "expired"
    for c in credentials:
        assert not store.validate_employee(c["token"], employee["id"], c["project_id"])
        with pytest.raises(WorkbenchError):
            store.employee_credentials(employee["id"], c["project_id"])
    for action in (
        lambda: store.set_employee_lifecycle(employee["id"], "active"),
        lambda: store.create_employee("Codex", "codex", project["id"]),
        lambda: queued(store, project, employee),
        lambda: store.resolve_permission(task["id"], "approval", "allow_once"),
    ):
        with pytest.raises(WorkbenchError):
            action()
    store.finish_task(task["id"], "review", "late result")
    assert store.get_task(task["id"])["status"] == "cancelled"
    restarted = WorkbenchStore(store.root)
    assert restarted.snapshot()["employees"][0]["lifecycle"] == "retired"
    assert len(restarted.snapshot()["tasks"]) == 2
    fresh = store.create_employee("Codex new", "codex", project["id"])
    assert fresh["id"] != employee["id"]
    assert store.claim_task(store.local_node()["id"]) is None


def test_lifecycle_requires_reason_and_management_records_redact(store, project, employee):
    token = store.employee_credentials(employee["id"], project["id"])["token"]
    for reason in ("", "   ", None):
        with pytest.raises(WorkbenchError):
            store.set_employee_lifecycle(employee["id"], "retired", reason)
    store.set_employee_lifecycle(employee["id"], "paused", "Sensitive " + token)
    store.set_employee_lifecycle(employee["id"], "active")
    assert token not in json.dumps(store.governance_events())
    assert token not in json.dumps(store.snapshot())
    assert {e["actor"] for e in store.governance_events()} == {"human"}
    assert len([e for e in store.governance_events() if e["type"] == "employee_lifecycle"]) == 2


def test_creation_cohort_ledger_records_actual_dispatch_and_review_actor(store, project, employee):
    task = queued(store, project, employee, actor="employee:sender")
    store.claim_task(store.local_node()["id"])
    store.set_status(task["id"], "running")
    store.finish_task(task["id"], "review", "delivery")
    store.review_task(task["id"], "accept", "验收通过")
    records = store.governance_events(project["id"])
    dispatch = next(e for e in records if e["type"] == "task_dispatched")
    review = next(e for e in records if e["type"] == "task_reviewed")
    assert dispatch["actor"] == "employee:sender"
    assert review["actor"] == "human"
    assert review["task_id"] == task["id"]
    assert review["payload"]["run_id"] == task["run_id"]


def test_permission_deadline_is_persistent_and_late_approval_denied(store, project, employee):
    task = running(store, project, employee)
    first = store.request_permission(task["id"], "once", [{"kind": "allow_once"}], {})
    again = store.request_permission(task["id"], "once", [{"kind": "allow_once"}], {})
    assert first["expires_at"] == again["expires_at"]
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE permissions SET expires_at='2020-01-01T00:00:00+00:00'")
    with pytest.raises(WorkbenchError) as error:
        store.resolve_permission(task["id"], "once", "allow_once")
    assert error.value.code == "permission_expired"
    assert store.get_permission(task["id"], "once")["status"] == "expired"
    assert store.get_task(task["id"])["status"] == "running"
    assert store.permission_decision(task["id"], "once", task["run_id"])["decision"] == "deny"


def test_permission_decision_run_binding_and_cancel_override_saved_allow(store, project, employee):
    task = running(store, project, employee)
    store.request_permission(task["id"], "once", [{"kind": "allow_once"}], {})
    assert store.permission_decision(task["id"], "once", task["run_id"])["status"] == "pending"
    for method in (store.permission_decision, store.expire_permission):
        with pytest.raises(WorkbenchError):
            method(task["id"], "once", "stale-run")
    store.resolve_permission(task["id"], "once", "allow_once")
    assert store.permission_decision(task["id"], "once", task["run_id"])["decision"] == "allow_once"
    store.cancel_task(task["id"])
    assert store.permission_decision(task["id"], "once", task["run_id"])["decision"] == "deny"


def test_no_once_option_cannot_be_approved_or_upgraded_to_allow_always(store, project, employee):
    task = running(store, project, employee)
    store.request_permission(task["id"], "once", [{"kind": "allow_always"}], {})
    with pytest.raises(WorkbenchError):
        store.resolve_permission(task["id"], "once", "allow_once")
    assert store.get_permission(task["id"], "once")["status"] == "pending"
    expired = store.expire_permission(task["id"], "once", task["run_id"])
    assert expired["status"] == "expired"
    assert expired["decision"] == "deny"


def test_worker_abandonment_invalidates_a_saved_allow_and_cancellation_wins(
    store, project, employee
):
    task = running(store, project, employee)
    store.request_permission(task["id"], "once", [{"kind": "allow_once"}], {})
    store.resolve_permission(task["id"], "once", "allow_once")
    assert store.expire_permission(task["id"], "once", task["run_id"])["decision"] == "deny"
    assert store.permission_decision(task["id"], "once", task["run_id"])["status"] == "expired"
    store.cancel_task(task["id"])
    assert (
        store.finish_task(task["id"], "failed", error={"code": "PERMISSION_DENIED"})["status"]
        == "cancelled"
    )


def test_version_three_migration_keeps_projects_tokens_tasks_and_permission_deadline(
    store, project, employee
):
    token = store.employee_credentials(employee["id"], project["id"])["token"]
    task = running(store, project, employee)
    first = store.request_permission(task["id"], "once", [{"kind": "allow_once"}], {})
    with sqlite3.connect(store.db_path) as db:
        db.execute("DROP TABLE governance_events")
        for column in ("lifecycle", "lifecycle_reason", "lifecycle_changed_at"):
            db.execute("ALTER TABLE employees DROP COLUMN " + column)
        db.execute("ALTER TABLE permissions DROP COLUMN expires_at")
        db.execute("PRAGMA user_version=3")
    upgraded = WorkbenchStore(store.root)
    assert upgraded.snapshot()["employees"][0]["lifecycle"] == "active"
    assert upgraded.get_task(task["id"]) == store.get_task(task["id"])
    assert upgraded.employee_credentials(employee["id"], project["id"])["token"] == token
    assert upgraded.get_permission(task["id"], "once")["expires_at"] == first["expires_at"]
    assert upgraded.governance_events() == []


def test_claim_scope_filters_before_claiming_without_losing_other_work(
    store, project, employee, tmp_path
):
    other = store.create_project("Other", tmp_path)
    store.create_employee("Codex", "codex", other["id"])
    first = queued(store, project, employee)
    second = queued(store, other, employee)
    node = store.local_node()["id"]
    assert store.claim_task(node, project_ids=[]) is None
    assert store.claim_task(node, project_ids=[other["id"]])["id"] == second["id"]
    assert store.get_task(first["id"])["status"] == "queued"
    with pytest.raises(WorkbenchError):
        store.claim_task(node, project_ids="invalid")


def test_v2_migration_preserves_tasks_and_adds_optional_model(tmp_path):
    import sqlite3

    store = WorkbenchStore(tmp_path / "state")
    project = store.create_project("Migration", str(tmp_path))
    employee = store.create_employee("Codex", "codex", project["id"])
    task = store.create_task(project["id"], "Before", "Existing work", employee["id"])
    with sqlite3.connect(store.db_path) as db:
        db.execute("ALTER TABLE tasks DROP COLUMN model")
        db.execute("PRAGMA user_version=2")
    restored = WorkbenchStore(store.root)
    assert restored.get_task(task["id"])["model"] is None
    selected = restored.create_task(
        project["id"], "After", "New work", employee["id"], model="advertised-model"
    )
    assert selected["model"] == "advertised-model"


def test_version_four_to_five_adds_receipt_proofs_without_replacing_identity(
    store, project, employee
):
    import sqlite3

    task = store.create_task(project["id"], "Retained", "Unchanged", employee["id"])
    identity = store.local_node()["id"]
    credential = store.employee_credentials(employee["id"], project["id"])
    with sqlite3.connect(store.db_path) as db:
        db.execute("DROP TABLE remote_receipts")
        db.execute("PRAGMA user_version=4")
    restored = WorkbenchStore(store.root)
    assert restored.local_node()["id"] == identity
    assert restored.employee_credentials(employee["id"], project["id"]) == credential
    assert restored.get_task(task["id"])["status"] == "queued"
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert db.execute("SELECT count(*) FROM remote_receipts").fetchone()[0] == 0
