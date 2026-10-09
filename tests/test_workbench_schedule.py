"""Four primitives: run / schedule / watch / inspect (roadmap T32)."""

from __future__ import annotations

import pytest

from agent_mailbox import workbench_schedule as ws
from agent_mailbox.workbench_mail_tasks import create_mail_task
from agent_mailbox.workbench_store import WorkbenchStore

PAST = "2020-01-01T00:00:00+00:00"
FUTURE = "2099-01-01T00:00:00+00:00"


@pytest.fixture
def scene(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    alice = store.create_employee("Alice", "codex", project["id"])
    mailbox_task = create_mail_task(store, project["id"], "信箱任务", "prompt", alice["id"])
    managed_task = store.create_task(project["id"], "受管任务", "work", alice["id"])
    return store, project, alice, mailbox_task, managed_task


def test_schedule_and_due_are_derived(scene):
    store, project, _alice, task, _managed = scene
    assert ws.due_tasks(store, project["id"]) == []  # 未排期 ⇒ 不 due
    ws.schedule(store, task["id"], FUTURE)
    assert ws.due_tasks(store, project["id"]) == []  # 未来 ⇒ 不 due
    ws.schedule(store, task["id"], PAST)
    due = ws.due_tasks(store, project["id"])
    assert [row["id"] for row in due] == [task["id"]]  # 过期 ⇒ due
    ws.schedule(store, task["id"], FUTURE)  # 改期到未来 ⇒ 最新胜
    assert ws.due_tasks(store, project["id"]) == []
    with pytest.raises(ValueError):
        ws.schedule(store, task["id"], "not-a-time")


def test_finished_or_other_project_tasks_are_not_due(scene, tmp_path):
    store, project, _alice, task, _managed = scene
    ws.schedule(store, task["id"], PAST)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = store.create_project("Other", other_dir)
    assert ws.due_tasks(store, other["id"]) == []  # 别的项目看不到
    assert len(ws.due_tasks(store, project["id"])) == 1
    other_emp = store.create_employee("Bob", "codex", other["id"])
    other_task = create_mail_task(store, other["id"], "别项目", "p", other_emp["id"])
    ws.schedule(store, other_task["id"], PAST)
    assert [row["id"] for row in ws.due_tasks(store, other["id"])] == [other_task["id"]]


def test_watch_returns_immediately_when_due_and_times_out_otherwise(scene):
    store, project, _alice, task, _managed = scene
    ws.schedule(store, task["id"], PAST)
    quick = ws.watch(store, project["id"], timeout_seconds=1.0, poll_seconds=0.01)
    assert quick["timed_out"] is False and [row["id"] for row in quick["due"]] == [task["id"]]

    ws.schedule(store, task["id"], FUTURE)
    empty = ws.watch(store, project["id"], timeout_seconds=0.05, poll_seconds=0.01)
    assert empty["timed_out"] is True and empty["due"] == []
    assert empty["waited_seconds"] >= 0.05


def test_inspect_reports_history(scene):
    store, _project, _alice, task, _managed = scene
    ws.schedule(store, task["id"], PAST, note="早上做")
    history = ws.inspect(store, task["id"])
    assert history["status"] == "queued"
    assert history["schedules"] and history["schedules"][-1]["note"] == "早上做"
    assert any(event["type"] == "mail_task_assigned" for event in history["governance"])
    with pytest.raises(ValueError):
        ws.inspect(store, "task_missing")


def test_run_now_is_honest_about_mailbox_tasks(scene):
    store, _project, _alice, task, managed = scene
    out = ws.run_now(store, task["id"])
    assert out["action"] == "needs_employee_accept"  # 平台不替员工接单
    assert "接受" in out["hint"]

    claimed = ws.run_now(store, managed["id"])
    assert claimed["action"] in ("claimed", "claimed_other")
    again = ws.run_now(store, managed["id"])
    assert again["action"] == "noop"  # 已不在 queued


def test_due_and_inspect_do_not_write(scene):
    store, project, _alice, task, _managed = scene
    ws.schedule(store, task["id"], PAST)
    with store._transaction() as db:
        before = db.execute("SELECT count(*) AS c FROM governance_events").fetchone()["c"]
    ws.due_tasks(store, project["id"])
    ws.inspect(store, task["id"])
    with store._transaction() as db:
        assert db.execute("SELECT count(*) AS c FROM governance_events").fetchone()["c"] == before
