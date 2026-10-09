"""Observation window: criteria as counts, not a calendar (roadmap §4)."""

from __future__ import annotations

import pytest

from agent_mailbox import workbench_observe as wo
from agent_mailbox.workbench_store import WorkbenchStore


@pytest.fixture
def scene(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    alice = store.create_employee("Alice", "codex", project["id"])
    store.send_message(project["id"], "第一封", "正文", recipient_id=alice["id"])
    store.send_message(project["id"], "第二封", "正文", recipient_id=alice["id"])
    return store, project, alice


def test_start_is_recorded_once_and_idempotent(scene):
    store, _project, _alice = scene
    assert wo.window(store)["started_at"] is None
    first = wo.start(store)
    assert first["started_at"] and first["days"] is not None
    second = wo.start(store)  # 再调不覆盖起点
    assert second["started_at"] == first["started_at"]


def test_evaluate_counts_storms_and_acceptances(scene):
    store, project, alice = scene
    wo.start(store)
    from agent_mailbox import workbench_policy as wp

    wp.set_policy(store, {"escalation_budget": 0}, project_id=project["id"])
    wp.record_escalation(store, project["id"], task_id=None, reason="想升级")
    task = store.create_task(project["id"], "要验收", "work", alice["id"])
    store.claim_task(store.local_node()["id"])
    store.set_status(task["id"], "running")
    store.finish_task(task["id"], "review", "结果")

    result = wo.evaluate(store)
    assert result["counters"]["escalations_suppressed"] == 1
    assert result["criteria"]["storms"] == 1 and result["criteria"]["storms_zero"] is False
    assert result["criteria"]["acceptances"] == 0  # review ≠ done（人验收才算）
    assert result["verdict"] == "观察中"

    with store._transaction() as db:
        db.execute("UPDATE tasks SET status='done' WHERE id=?", (task["id"],))
    assert wo.evaluate(store)["criteria"]["acceptances"] == 1


def test_audit_sample_measures_reconstructability(scene):
    store, project, alice = scene
    wo.start(store)
    # 只统计"窗口内"发生的信件（fixture 里那两封在开窗之前，天然不计入）
    store.send_message(project["id"], "窗口内一", "正文", recipient_id=alice["id"])
    store.send_message(project["id"], "窗口内二", "正文", recipient_id=alice["id"])
    result = wo.evaluate(store, sample=20)
    assert result["criteria"]["audit_sampled"] == 2
    assert result["criteria"]["audit_reconstructable"] == 2  # 两个都还原得出
    assert result["criteria"]["audit_full"] is True
    assert all(item["ok"] for item in result["sample"])


def test_evaluate_before_start_is_honest(scene):
    store, _project, _alice = scene
    result = wo.evaluate(store)
    assert result["window"]["started_at"] is None
    assert result["verdict"].startswith("未开始")


def test_evaluate_does_not_write(scene):
    store, _project, _alice = scene
    wo.start(store)
    with store._transaction() as db:
        before = db.execute("SELECT count(*) AS c FROM governance_events").fetchone()["c"]
    wo.evaluate(store)
    wo.window(store)
    with store._transaction() as db:
        assert db.execute("SELECT count(*) AS c FROM governance_events").fetchone()["c"] == before
