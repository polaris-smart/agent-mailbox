"""Revision durability, attribution, isolation and safe database migration."""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_mailbox.workbench_store import MAX_TEXT, WorkbenchError, WorkbenchStore


@pytest.fixture
def resources(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Project", tmp_path)
    source = tmp_path / "PRD.md"
    source.write_text("original", encoding="utf-8")
    resource = store.add_resource(project["id"], "PRD", "prd", source)
    return store, project["id"], resource["id"], source


def test_snapshot_approval_and_live_remain_distinct(resources):
    store, pid, rid, source = resources
    version = store.capture_resource_version(pid, rid, "baseline")
    assert version["status"] == "approved"
    assert version["created_by"] == version["approved_by"] == "human"
    source.write_text("changed")
    assert store.read_resource(pid, rid)["content"] == "changed"
    snapshot = store.read_resource_version(pid, rid, version["id"])
    assert snapshot["content"] == "original"
    assert snapshot["provenance"]["mode"] == "snapshot"
    manifest = store.resource_manifest(pid)
    assert manifest["resources"][0]["version_id"] == version["id"]
    assert store.project_context(pid)["resource_manifest"] == manifest
    source.unlink()
    assert store.read_resource_version(pid, rid, version["id"])["content"] == "original"


def test_employee_proposal_does_not_replace_approved(resources):
    store, pid, rid, source = resources
    baseline = store.capture_resource_version(pid, rid)
    employee = store.create_employee("Codex", "codex", pid)
    source.write_text("proposal")
    proposal = store.capture_resource_version(pid, rid, employee_id=employee["id"])
    assert proposal["status"] == "proposed"
    assert proposal["created_by"] == employee["id"]
    assert proposal["approved_by"] is None
    assert store.resource_manifest(pid)["resources"][0]["version_id"] == baseline["id"]
    approved = store.approve_resource_version(pid, rid, proposal["id"])
    assert approved["status"] == "approved"
    assert approved["approved_by"] == "human"
    assert store.approve_resource_version(pid, rid, proposal["id"]) == approved
    assert store.resource_manifest(pid)["resources"][0]["version_id"] == proposal["id"]


def test_membership_and_resource_isolation(resources, tmp_path):
    store, pid, rid, source = resources
    other = store.create_project("Other", tmp_path)
    employee = store.create_employee("Other agent", "codex", other["id"])
    with pytest.raises(WorkbenchError, match="当前项目"):
        store.capture_resource_version(pid, rid, employee_id=employee["id"])
    version = store.capture_resource_version(pid, rid)
    for call in (
        lambda: store.resource_versions(other["id"], rid),
        lambda: store.read_resource_version(other["id"], rid, version["id"]),
        lambda: store.approve_resource_version(other["id"], rid, version["id"]),
    ):
        with pytest.raises(WorkbenchError):
            call()
    other_rid = store.add_resource(pid, "Other doc", "prd", source)["id"]
    with pytest.raises(WorkbenchError):
        store.read_resource_version(pid, other_rid, version["id"])
    assert store.resource_versions(pid, other_rid)["versions"] == []


def test_concurrent_duplicate_capture_is_idempotent(resources):
    store, pid, rid, _ = resources
    with ThreadPoolExecutor(max_workers=8) as pool:
        versions = list(pool.map(lambda _: store.capture_resource_version(pid, rid), range(16)))
    assert len({item["id"] for item in versions}) == 1
    assert len(store.resource_versions(pid, rid)["versions"]) == 1


def test_snapshot_scrubs_secrets_before_persistence_and_rechecks_return(resources):
    store, pid, rid, source = resources
    employee = store.create_employee("Agent", "codex", pid)
    with store._connection() as db:
        token = db.execute(
            "SELECT secret_token FROM employees WHERE id=?", (employee["id"],)
        ).fetchone()[0]
    source.write_text("known:" + token + "\nfuture-secret")
    version = store.capture_resource_version(pid, rid, summary=token)
    with store._connection() as db:
        row = db.execute("SELECT * FROM resource_versions").fetchone()
        assert token not in row["content"] and token not in row["summary"]
    snapshot = store.read_resource_version(pid, rid, version["id"])
    assert "[redacted]" in snapshot["content"]
    assert (
        snapshot["provenance"]["content_sha256"]
        == hashlib.sha256(snapshot["content"].encode()).hexdigest()
    )
    with store._transaction() as db:
        db.execute(
            "UPDATE employees SET secret_token=? WHERE id=?", ("future-secret", employee["id"])
        )
    snapshot = store.read_resource_version(pid, rid, version["id"])
    assert "future-secret" not in snapshot["content"]
    assert snapshot["provenance"]["redacted_since_capture"]
    assert snapshot["provenance"]["canonical_sha256"] == version["content_sha256"]
    assert (
        store.resource_manifest(pid)["resources"][0]["content_sha256"]
        == snapshot["provenance"]["content_sha256"]
    )


def test_overlong_capture_has_no_revision(resources):
    store, pid, rid, source = resources
    source.write_text("x" * (MAX_TEXT + 1))
    with pytest.raises(WorkbenchError):
        store.capture_resource_version(pid, rid)
    assert store.resource_versions(pid, rid)["versions"] == []


def test_revision_and_approval_are_immutable_and_tampering_detected(resources):
    store, pid, rid, _ = resources
    version = store.capture_resource_version(pid, rid)
    with sqlite3.connect(store.db_path) as db:
        for statement in (
            'UPDATE resource_versions SET content="tampered"',
            "DELETE FROM resource_versions",
            'UPDATE resource_approvals SET approved_at="tampered"',
            "DELETE FROM resource_approvals",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(statement)
        db.execute("DROP TRIGGER immutable_resource_versions_update")
        db.execute('UPDATE resource_versions SET content="tampered"')
    for call in (
        lambda: store.read_resource_version(pid, rid, version["id"]),
        lambda: store.approve_resource_version(pid, rid, version["id"]),
        lambda: store.resource_manifest(pid),
    ):
        with pytest.raises(WorkbenchError) as error:
            call()
        assert error.value.code == "resource_integrity_error"


def test_schema6_migration_backup_preserves_business_and_old_live(resources):
    store, pid, rid, _ = resources
    employee = store.create_employee("Agent", "codex", pid)
    with sqlite3.connect(store.db_path) as db:
        db.execute("DROP TABLE resource_approvals")
        db.execute("DROP TABLE resource_versions")
        db.execute("PRAGMA user_version=6")
    migrated = WorkbenchStore(store.root)
    backup = migrated.migration_backup_path
    assert backup and backup.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert db.execute("SELECT id FROM employees").fetchone()[0] == employee["id"]
        assert db.execute("SELECT id FROM resources").fetchone()[0] == rid
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name='resource_versions'"
        ).fetchone()
    assert migrated.read_resource(pid, rid)["content"] == "original"
    assert migrated.resource_versions(pid, rid)["versions"] == []
    assert WorkbenchStore(store.root).migration_backup_path is None


def test_claim_freezes_approved_resources_and_reports_unversioned(resources):
    store, pid, rid, source = resources
    employee = store.create_employee("Worker", "codex", pid)
    first = store.capture_resource_version(pid, rid)
    missing = store.add_resource(pid, "Not approved", "draft", source)["id"]
    task = store.create_task(pid, "Task", "Read project resources", employee["id"])
    assert store.claim_task(store.local_node()["id"])["id"] == task["id"]
    frozen = store.execution_resource_manifest(task["id"], task["run_id"])
    assert frozen["capture_mode"] == "at_claim"
    assert frozen["resources"][0]["version_id"] == first["id"]
    assert frozen["missing_resources"] == [
        {"resource_id": missing, "reason": "no_approved_version"}
    ]
    source.write_text("new approved version")
    second = store.capture_resource_version(pid, rid)
    assert store.resource_manifest(pid)["resources"][0]["version_id"] == second["id"]
    assert store.execution_resource_manifest(task["id"], task["run_id"]) == frozen
    default = store.read_execution_resource(pid, rid, task["id"], task["run_id"])
    assert default["content"] == "original"
    assert default["provenance"]["matches_frozen_content"]
    override = store.read_execution_resource(pid, rid, task["id"], task["run_id"], second["id"])
    assert override["content"] == "new approved version"
    assert override["provenance"]["explicit_override"]
    assert not override["provenance"]["matches_frozen_content"]
    assert (
        store.read_execution_resource(pid, rid, task["id"], task["run_id"], live=True)[
            "provenance"
        ]["mode"]
        == "live"
    )
    with pytest.raises(WorkbenchError) as error:
        store.read_execution_resource(pid, missing, task["id"], task["run_id"])
    assert error.value.code == "resource_unversioned"
    for call in (
        lambda: store.execution_resource_manifest(task["id"], "wrong"),
        lambda: store.read_execution_resource(pid, rid, task["id"], "wrong", live=True),
    ):
        with pytest.raises(WorkbenchError) as error:
            call()
        assert error.value.code == "permission_denied"
    with sqlite3.connect(store.db_path) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute('UPDATE task_resource_manifests SET manifest="{}"')
    assert (
        len(
            [
                event
                for event in store.task_detail(task["id"])["events"]
                if event["type"] == "resources_pinned"
            ]
        )
        == 1
    )


def test_old_active_tasks_are_not_silently_given_current_manifest(resources):
    store, pid, _, _ = resources
    employee = store.create_employee("Worker", "codex", pid)
    task = store.create_task(pid, "Task", "Prompt", employee["id"])
    with store._transaction() as db:
        db.execute('UPDATE tasks SET status="running" WHERE id=?', (task["id"],))
    with pytest.raises(WorkbenchError) as error:
        store.execution_resource_manifest(task["id"], task["run_id"])
    assert error.value.code == "resource_manifest_missing"


def test_governance_events_are_real_and_idempotent(resources):
    store, pid, rid, source = resources
    owner = store.capture_resource_version(pid, rid)
    assert store.capture_resource_version(pid, rid)["id"] == owner["id"]
    employee = store.create_employee("Worker", "codex", pid)
    source.write_text("employee proposal")
    proposal = store.capture_resource_version(pid, rid, employee_id=employee["id"])
    store.approve_resource_version(pid, rid, proposal["id"])
    store.approve_resource_version(pid, rid, proposal["id"])
    with store._connection() as db:
        events = db.execute(
            "SELECT * FROM governance_events WHERE type LIKE 'resource_version_%' ORDER BY created_at"
        ).fetchall()
    assert [row["type"] for row in events] == [
        "resource_version_created",
        "resource_version_approved",
        "resource_version_created",
        "resource_version_approved",
    ]
    assert events[2]["actor"] == f"employee:{employee['id']}"
    assert events[3]["actor"] == "human"
    assert all("employee proposal" not in row["payload"] for row in events)


def test_migration_failure_rolls_back_business_and_schema(resources, monkeypatch):
    from agent_mailbox import workbench_resources

    store, pid, rid, _ = resources
    with sqlite3.connect(store.db_path) as db:
        db.execute("DROP TABLE task_resource_manifests")
        db.execute("DROP TABLE resource_approvals")
        db.execute("DROP TABLE resource_versions")
        db.execute("PRAGMA user_version=6")
    monkeypatch.setattr(
        workbench_resources,
        "RESOURCE_SCHEMA",
        (*workbench_resources.RESOURCE_SCHEMA, "INVALID migration SQL"),
    )
    with pytest.raises(WorkbenchError):
        WorkbenchStore(store.root)
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert db.execute("SELECT id FROM resources").fetchone()[0] == rid
        assert db.execute("SELECT id FROM projects").fetchone()[0] == pid
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name='resource_versions'"
        ).fetchone()
    backup = list(store.directory.glob("state-v6-before-v7-*.sqlite"))
    assert len(backup) == 1
    with sqlite3.connect(backup[0]) as db:
        assert db.execute("SELECT id FROM resources").fetchone()[0] == rid


def test_recognizable_provider_credentials_are_not_persisted(resources):
    store, pid, rid, source = resources
    source.write_text(
        'API_KEY="provider-credential"\nOPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx\n'
        "password: longpassword\nAuthorization: Bearer abcdefghijklmnopqrstuvwxyz\n"
        "-----BEGIN PRIVATE KEY-----\nprivatekeycontent\n-----END PRIVATE KEY-----\n"
        "ordinary product text"
    )
    version = store.capture_resource_version(pid, rid)
    result = store.read_resource_version(pid, rid, version["id"])
    for secret in (
        "provider-credential",
        "sk-abcdefghijklmnopqrstuvwx",
        "longpassword",
        "abcdefghijklmnopqrstuvwxyz",
        "privatekeycontent",
    ):
        assert secret not in result["content"]
    assert "ordinary product text" in result["content"]
    assert result["provenance"]["content_sha256"] == version["content_sha256"]


def test_reverting_document_creates_new_approved_revision(resources):
    store, pid, rid, source = resources
    original = store.capture_resource_version(pid, rid)
    source.write_text("changed")
    changed = store.capture_resource_version(pid, rid)
    source.write_text("original")
    reverted = store.capture_resource_version(pid, rid)
    assert reverted["id"] not in (original["id"], changed["id"])
    assert reverted["ordinal"] == 3
    assert store.resource_manifest(pid)["resources"][0]["version_id"] == reverted["id"]


def test_employee_submitted_text_is_proposed_without_touching_coordinator_source(resources):
    store, pid, rid, source = resources
    employee = store.create_employee("Remote", "codex", pid)
    baseline = store.capture_resource_version(pid, rid)
    proposal = store.capture_resource_version(
        pid, rid, "Remote edit", employee["id"], content="remote employee changed this"
    )
    assert proposal["source"] == f"employee:{employee['id']}:submitted_text"
    assert proposal["status"] == "proposed"
    assert proposal["approved_by"] is None
    assert (
        store.read_resource_version(pid, rid, proposal["id"])["content"]
        == "remote employee changed this"
    )
    assert source.read_text() == "original"
    assert store.resource_manifest(pid)["resources"][0]["version_id"] == baseline["id"]
    assert (
        store.capture_resource_version(
            pid, rid, "Remote edit", employee["id"], content="remote employee changed this"
        )["id"]
        == proposal["id"]
    )
    source.unlink()
    next_proposal = store.capture_resource_version(
        pid, rid, employee_id=employee["id"], content="independent of source path"
    )
    assert next_proposal["status"] == "proposed"
    store.approve_resource_version(pid, rid, next_proposal["id"])
    assert store.resource_manifest(pid)["resources"][0]["version_id"] == next_proposal["id"]


def test_submitted_text_rejects_owner_nonmember_and_invalid_payload(resources, tmp_path):
    store, pid, rid, _ = resources
    employee = store.create_employee("Member", "codex", pid)
    other_pid = store.create_project("Other", tmp_path)["id"]
    other = store.create_employee("Outside", "codex", other_pid)
    with pytest.raises(WorkbenchError) as error:
        store.capture_resource_version(pid, rid, content="owner supplied text")
    assert error.value.code == "permission_denied"
    with pytest.raises(WorkbenchError):
        store.capture_resource_version(pid, rid, employee_id=other["id"], content="outside")
    for content in ("é" * (128 * 1024 + 1), "\x00", "\ud800", 123, "x" * (256 * 1024 + 1)):
        with pytest.raises(WorkbenchError):
            store.capture_resource_version(pid, rid, employee_id=employee["id"], content=content)
    assert store.resource_versions(pid, rid)["versions"] == []
    version = store.capture_resource_version(pid, rid, employee_id=employee["id"], content="")
    assert store.read_resource_version(pid, rid, version["id"])["content"] == ""


def test_submitted_text_scrubs_credentials_and_distinguishes_file_origin(resources):
    store, pid, rid, _ = resources
    employee = store.create_employee("Member", "codex", pid)
    file_version = store.capture_resource_version(pid, rid, employee_id=employee["id"])
    text_version = store.capture_resource_version(
        pid, rid, employee_id=employee["id"], content="original"
    )
    assert text_version["id"] != file_version["id"]
    assert text_version["source"].endswith(":submitted_text")
    proposal = store.capture_resource_version(
        pid, rid, employee_id=employee["id"], content='API_KEY="provider-secret"\nvisible'
    )
    snapshot = store.read_resource_version(pid, rid, proposal["id"])
    assert "provider-secret" not in snapshot["content"]
    assert snapshot["provenance"]["content_sha256"] == proposal["content_sha256"]
