"""Existing App sessions explicitly accept and deliver; human alone accepts delivery."""

import pytest

from agent_mailbox.workbench_mail_sessions import create_session, invoke, revoke_session
from agent_mailbox.workbench_mail_tasks import (
    accept_mail_task,
    create_mail_task,
    list_mail_tasks,
    set_project_role,
    submit_mail_task,
)
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


@pytest.fixture
def team(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Project", str(tmp_path))
    alice = store.create_employee("Alice App", "codex", project["id"], connection_type="app")
    bob = store.create_employee("Bob CLI", "claude", project["id"])
    a = create_session(store, alice["id"], project["id"], "Existing App")
    b = create_session(store, bob["id"], project["id"], "Existing CLI")
    return store, project, alice, bob, a, b


def new_task(team, employee=None):
    store, project, alice, *_ = team
    return create_mail_task(
        store, project["id"], "Review", "Review the document", (employee or alice)["id"]
    )


def test_mail_task_complete_without_launch_or_execution_grant(team):
    store, project, alice, _, a, _ = team
    task = new_task(team)
    assert task["execution_mode"] == "mailbox" and task["status"] == "queued"
    message = store.list_messages(project["id"])[0]
    assert message["task_id"] == task["id"] and not message["request_work"]
    assert not invoke(store, a["token"], "messages", {})["viewing_acknowledges"]
    assert list_mail_tasks(store, a["token"])["tasks"][0]["status"] == "queued"
    assert store.claim_task(store.local_node()["id"]) is None
    accepted = accept_mail_task(store, a["token"], task["id"])
    assert accepted["status"] == "running" and accepted["result"] == ""
    with pytest.raises(WorkbenchError):
        store.execution_credentials(task["id"])
    with store._connection() as db:
        private = store._required(db, "tasks", task["id"])
        assert not store.validate_execution(
            private["tool_token"], alice["id"], project["id"], task["id"], task["run_id"]
        )
    assert store.recover_runs(store.local_node()["id"]) == []
    result = submit_mail_task(store, a["token"], task["id"], "Checked " + a["token"])
    assert result["status"] == "review" and a["token"] not in result["result"]
    delivery = invoke(store, a["token"], "delivery", {"target_task_id": task["id"]})
    assert delivery["summary"] == result["result"]
    assert delivery["verification"]["status"] == "employee_report"
    assert delivery["files"] == [] and delivery["workspace"] is None
    assert "can_apply" not in delivery
    assert store.get_task(task["id"])["status"] != "done"
    assert not store.snapshot()["employees"][0]["execution_verified"]
    assert store.review_task(task["id"], "accept")["status"] == "done"
    with pytest.raises(WorkbenchError):
        submit_mail_task(store, a["token"], task["id"], "Overwrite acceptance")


def test_accepted_task_keeps_approved_resource_version_when_project_changes(team, tmp_path):
    store, project, _, _, a, _ = team
    source = tmp_path / "PRD.md"
    source.write_text("Approved baseline", encoding="utf-8")
    resource = store.add_resource(project["id"], "PRD", "prd", source)
    first = store.capture_resource_version(project["id"], resource["id"])
    task = new_task(team)
    accepted = accept_mail_task(store, a["token"], task["id"])
    pinned = accepted["resource_manifest"]
    assert pinned["resources"][0]["version_id"] == first["id"]
    source.write_text("New approved instructions", encoding="utf-8")
    second = store.capture_resource_version(project["id"], resource["id"])
    assert second["id"] != first["id"]
    resumed = list_mail_tasks(store, a["token"])["tasks"][0]
    assert resumed["resource_manifest"]["manifest_sha256"] == pinned["manifest_sha256"]
    assert resumed["resource_manifest"]["resources"][0]["version_id"] == first["id"]
    content = invoke(
        store,
        a["token"],
        "resource_read",
        {"resource_id": resource["id"], "version_id": first["id"]},
    )
    assert content["content"] == "Approved baseline"


def test_identity_revocation_and_submit_requires_accept(team):
    store, _, _, _, a, b = team
    task = new_task(team)
    for operation in (accept_mail_task,):
        with pytest.raises(WorkbenchError):
            operation(store, b["token"], task["id"])
    with pytest.raises(WorkbenchError):
        submit_mail_task(store, a["token"], task["id"], "Premature")
    revoke_session(store, a["id"])
    with pytest.raises(WorkbenchError):
        accept_mail_task(store, a["token"], task["id"])
    assert store.get_task(task["id"])["status"] == "queued"


@pytest.mark.parametrize("action", ["cancel", "leave", "paused", "retired"])
def test_running_manual_tasks_end_immediately(team, action):
    store, project, alice, _, a, _ = team
    task = new_task(team)
    accept_mail_task(store, a["token"], task["id"])
    if action == "cancel":
        store.cancel_task(task["id"])
    elif action == "leave":
        store.remove_project_member(project["id"], alice["id"])
    else:
        store.set_employee_lifecycle(alice["id"], action, "Stop")
    assert store.get_task(task["id"])["status"] == "cancelled"
    with pytest.raises(WorkbenchError):
        submit_mail_task(store, a["token"], task["id"], "Late result")


def test_manual_cli_does_not_block_managed_claim_and_followup_preserves_mode(team):
    store, project, _, bob, _, b = team
    mail = new_task(team, bob)
    accept_mail_task(store, b["token"], mail["id"])
    managed = store.create_task(project["id"], "Managed", "Read", bob["id"])
    assert store.claim_task(store.local_node()["id"])["id"] == managed["id"]
    submit_mail_task(store, b["token"], mail["id"], "First delivery")
    follow = store.follow_up_task(mail["id"], "Check another detail")
    assert follow["task"]["execution_mode"] == "mailbox"
    assert follow["task"]["status"] == "queued"
    assert follow["parent_task"]["status"] == "failed"


def test_roles_describe_responsibility_without_granting_execution(team):
    store, project, alice, _, a, _ = team
    role = set_project_role(store, project["id"], alice["id"], "Lead reviewer")
    assert role["role"] == "Lead reviewer"
    context = invoke(store, a["token"], "context", {})
    employee = next(e for e in context["employees"] if e["id"] == alice["id"])
    assert employee["project_role"] == "Lead reviewer"
    assert not employee["execution_supported"]
    store.pause_updates(True)
    with pytest.raises(WorkbenchError):
        new_task(team)
    assert store.snapshot()["tasks"] == []


def test_concurrent_accept_is_single_transition(team):
    from concurrent.futures import ThreadPoolExecutor

    store, _, _, _, a, _ = team
    task = new_task(team)

    def accept_once():
        try:
            return accept_mail_task(store, a["token"], task["id"])["status"]
        except WorkbenchError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: accept_once(), range(2)))
    assert sorted(outcomes) == ["invalid_state", "running"]
    events = store.task_detail(task["id"])["events"]
    assert len([event for event in events if event["type"] == "mail_task_accepted"]) == 1


def test_schema10_migration_preserves_session_and_legacy_task(team):
    store, project, _, bob, _, b = team
    managed = store.create_task(project["id"], "Legacy", "Read only", bob["id"])
    with store._connection() as db:
        db.execute("ALTER TABLE memberships DROP COLUMN role")
        db.execute("ALTER TABLE tasks DROP COLUMN execution_mode")
        db.execute("PRAGMA user_version=10")
        db.commit()
    upgraded = WorkbenchStore(store.root)
    assert upgraded.migration_backup_path.is_file()
    assert upgraded.get_task(managed["id"])["execution_mode"] == "managed"
    assert invoke(upgraded, b["token"], "context", {})["project"]["id"] == project["id"]
    assert set_project_role(upgraded, project["id"], bob["id"], "Reviewer")["role"] == "Reviewer"


def test_cross_project_and_remote_mail_rejected(team, tmp_path):
    store, _, alice, _, _, _ = team
    task = new_task(team)
    other = store.create_project("Other", str(tmp_path))
    store.add_project_member(other["id"], alice["id"])
    other_session = create_session(store, alice["id"], other["id"], "Other session")
    with pytest.raises(WorkbenchError):
        accept_mail_task(store, other_session["token"], task["id"])
    remote = store.upsert_device("remote-test", "Other device", "online")
    foreign = store.create_employee("Remote", "codex", other["id"], node_id=remote["id"])
    with pytest.raises(WorkbenchError) as error:
        create_mail_task(store, other["id"], "Impossible", "No", foreign["id"])
    assert error.value.code == "REMOTE_MAILBOX_UNAVAILABLE"
