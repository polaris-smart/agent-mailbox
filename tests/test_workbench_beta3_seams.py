"""Verify cross-module seams with real HTTP, SQLite and Git, without provider calls."""

import json
import sqlite3
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agent_mailbox.workbench import WorkbenchHTTP
from agent_mailbox.workbench_engine import WorkbenchEngine
from agent_mailbox.workbench_execution_resources import (
    execution_project_context,
    project_task_delivery,
)
from agent_mailbox.workbench_onboarding import create_probe
from agent_mailbox.workbench_private import private_access
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore
from agent_mailbox.workbench_workspaces import apply_delivery, task_delivery


@pytest.fixture
def fixture(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    (path / "proof.txt").write_text("baseline\n")
    for args in [
        ("init",),
        ("add", "."),
        (
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "baseline",
        ),
    ]:
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Project", path)
    employee = store.create_employee("Codex", "codex", project["id"])
    return store, project, employee, path


def test_engine_context_delivery_followup_accept_apply(fixture):
    store, project, employee, source = fixture
    engine = WorkbenchEngine(store)
    seen = []

    def execute(task, cwd, tools, event, permission, cancelled, timeout):
        event({"type": "started"})
        context = execution_project_context(
            store, project["id"], employee["id"], task["id"], task["run_id"]
        )
        assert cwd["path"] == context["project"]["path"] != str(source)
        path = Path(cwd["path"]) / "proof.txt"
        seen.append(path.read_text())
        path.write_text("first\n" if len(seen) == 1 else "final\n")
        return {"status": "completed", "output_text": "Employee claims tests passed"}

    engine.execution.run = execute
    first = store.create_task(
        project["id"], "Edit", "Change proof", employee["id"], "workspace-write"
    )
    engine.execute(store.claim_task(store.local_node()["id"]))
    delivery = task_delivery(store, first["id"])
    first_record = store.get_task(first["id"])
    assert first_record["status"] == "review", {
        "error": first_record.get("error"),
        "capture_error": delivery.get("capture_error"),
        "execution_status": delivery.get("system", {}).get("execution_status"),
        "executed_steps": len(seen),
    }
    assert not delivery["system"]["tests_run"]
    child = store.follow_up_task(first["id"], "Use final wording")
    assert child["parent_task"]["status"] == "failed"
    assert store.task_detail(child["task"]["id"])["links"][0]["direction"] == "parent"
    engine.execute(store.claim_task(store.local_node()["id"]))
    assert seen == ["baseline\n", "first\n"]
    assert "first" in project_task_delivery(store, project["id"], first["id"])["diff"]
    assert "can_apply" not in project_task_delivery(store, project["id"], first["id"])
    store.review_task(child["task"]["id"], "accept", "Inspected diff")
    assert (source / "proof.txt").read_text() == "baseline\n"
    apply_delivery(store, child["task"]["id"])
    assert (source / "proof.txt").read_text() == "final\n"
    other = store.create_project("Other", source)
    with pytest.raises(WorkbenchError, match="当前授权项目"):
        project_task_delivery(store, other["id"], first["id"])


def test_http_probe_explicit_model_and_readonly_get(fixture, monkeypatch):
    store, project, employee, _ = fixture
    monkeypatch.setattr(
        "agent_mailbox.workbench_onboarding.discover_employees",
        lambda: [{"kind": "codex", "connection_type": "cli", "auth_status": "authenticated"}],
    )
    monkeypatch.setattr(
        "agent_mailbox.workbench_onboarding.runtime_status",
        lambda root: {"installed": True, "node_available": True},
    )
    server = WorkbenchHTTP(store, token="test-owner")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def api(route, body=None, token="test-owner"):
        req = urllib.request.Request(
            server.endpoint + "/api/workbench/" + route,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        return json.load(urllib.request.urlopen(req, timeout=5))

    try:
        status = api(f"employees/{employee['id']}/onboarding?project_id={project['id']}")
        assert status["can_verify"] and not store.snapshot()["tasks"]
        probe = api(
            f"employees/{employee['id']}/verify",
            {"project_id": project["id"], "model": "explicit-model"},
        )
        assert probe["task"]["model"] == "explicit-model"
        repeated = api(f"employees/{employee['id']}/verify", {"project_id": project["id"]})
        assert repeated["reused"] and repeated["task"]["id"] == probe["task"]["id"]
        with pytest.raises(urllib.error.HTTPError) as exc:
            api(f"employees/{employee['id']}/verify", {"project_id": project["id"]}, "wrong")
        assert exc.value.code == 401
    finally:
        server.shutdown()
        server.close()
        thread.join(3)


def test_probe_reuse_cannot_bypass_membership_revocation(fixture):
    store, project, employee, _ = fixture
    create_probe(store, employee["id"], project["id"])
    with store._transaction() as db:
        db.execute("DELETE FROM memberships WHERE employee_id=?", (employee["id"],))
    with pytest.raises(WorkbenchError):
        create_probe(store, employee["id"], project["id"])


def test_schema8_upgrade_preserves_rows_and_private_backup(fixture):
    store, _, _, _ = fixture
    before = store.snapshot()
    with sqlite3.connect(store.db_path) as db:
        for table in [
            "employee_probes",
            "task_links",
            "task_workspaces",
            "task_deliveries",
            "task_delivery_applications",
        ]:
            db.execute("DROP TABLE " + table)
        db.execute("PRAGMA user_version=8")
    upgraded = WorkbenchStore(store.root)
    assert upgraded.snapshot()["projects"] == before["projects"]
    assert upgraded.snapshot()["employees"] == before["employees"]
    assert private_access(upgraded.migration_backup_path, 0o600)
    with sqlite3.connect(upgraded.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 11
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_followup_keeps_human_requirements_from_large_original(fixture):
    store, project, employee, _ = fixture
    task = store.create_task(project["id"], "Large", "a" * 50000, employee["id"])
    task = store.claim_task(store.local_node()["id"])
    store.set_status(task["id"], "running")
    store.finish_task(task["id"], "review", "b" * 50000)
    child = store.follow_up_task(task["id"], "HUMAN-MUST-KEEP")
    assert "HUMAN-MUST-KEEP" in child["task"]["prompt"]
    assert "project_delivery" in child["task"]["prompt"]


def test_remote_write_rejected_and_legacy_queue_not_executed(fixture):
    store, project, _, _ = fixture
    store.upsert_device("fixture-remote", "Remote", "online")
    employee = store.create_employee(
        "Remote Codex", "codex", project["id"], node_id="fixture-remote"
    )
    with pytest.raises(WorkbenchError) as exc:
        store.create_task(project["id"], "Write", "Edit", employee["id"], "workspace-write")
    assert exc.value.code == "WORKSPACE_REMOTE_UNSUPPORTED"
    legacy = store.create_task(project["id"], "Legacy", "Edit", employee["id"])
    with store._transaction() as db:
        db.execute("UPDATE tasks SET permission_mode='workspace-write' WHERE id=?", (legacy["id"],))
    assert store.claim_task("fixture-remote", [project["id"]]) is None
    assert store.get_task(legacy["id"])["error"]["code"] == "WORKSPACE_REMOTE_UNSUPPORTED"
    assert store.get_task(legacy["id"])["status"] == "failed"
