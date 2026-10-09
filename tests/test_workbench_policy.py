"""Review policy: notify policy, escalation budget, peer review (roadmap T30)."""

from __future__ import annotations

import pytest

from agent_mailbox import workbench_policy as wp
from agent_mailbox import workbench_proof as wpf
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore
from agent_mailbox.workbench_workspaces import apply_delivery


@pytest.fixture
def scene(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    alice = store.create_employee("Alice", "codex", project["id"])
    bob = store.create_employee("Bob", "codex", project["id"])
    task = store.create_task(project["id"], "写能力交付", "work", alice["id"])
    store.claim_task(store.local_node()["id"])
    store.set_status(task["id"], "running")
    return store, project, alice, bob, task


def _snapshot(store):
    with store._transaction() as db:
        return {
            "tasks": [tuple(r) for r in db.execute("SELECT id,status FROM tasks ORDER BY id")],
            "governance": db.execute("SELECT count(*) AS c FROM governance_events").fetchone()["c"],
        }


def test_defaults_and_validation(scene):
    store, project, _alice, _bob, _task = scene
    policy = wp.effective_policy(store, project["id"])
    assert policy["notification_mode"] == "manual_check"
    assert policy["escalation_budget"] == 3
    assert policy["peer_review"] == "off"  # 默认不改既有行为
    with pytest.raises(ValueError):
        wp.set_policy(store, {"nope": 1})
    with pytest.raises(ValueError):
        wp.set_policy(store, {"peer_review": "maybe"})
    with pytest.raises(ValueError):
        wp.set_policy(store, {})


def test_global_then_project_override(scene, tmp_path):
    store, project, _alice, _bob, _task = scene
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = store.create_project("Other", other_dir)

    wp.set_policy(store, {"escalation_budget": 7})  # 全局
    assert wp.effective_policy(store, other["id"])["escalation_budget"] == 7
    wp.set_policy(store, {"escalation_budget": 1}, project_id=project["id"])  # 项目覆盖
    assert wp.effective_policy(store, project["id"])["escalation_budget"] == 1
    assert wp.effective_policy(store, other["id"])["escalation_budget"] == 7
    wp.set_policy(store, {"escalation_budget": 2}, project_id=project["id"])  # 最新覆盖
    assert wp.effective_policy(store, project["id"])["escalation_budget"] == 2


def test_escalation_budget_spends_then_suppresses_with_audit(scene):
    store, project, _alice, _bob, _task = scene
    wp.set_policy(store, {"escalation_budget": 2}, project_id=project["id"])
    assert (
        wp.record_escalation(store, project["id"], task_id=None, reason="第一次")["escalated"]
        is True
    )
    assert (
        wp.record_escalation(store, project["id"], task_id=None, reason="第二次")["escalated"]
        is True
    )
    third = wp.record_escalation(store, project["id"], task_id=None, reason="第三次")
    assert third["escalated"] is False and third["reason"] == "escalation_budget_exhausted"
    types = [e["type"] for e in store.governance_events() if "escalation" in e["type"]]
    assert types.count(wp.ESCALATION_EVENT) == 2
    assert types.count(wp.ESCALATION_SUPPRESSED_EVENT) == 1  # 抑制也留痕：安静不等于看不见


def test_peer_review_off_keeps_existing_behaviour(scene):
    """默认策略下门是关的：仍然走原有的『无工作区』拒绝，而不是策略拒绝。"""
    store, _project, _alice, _bob, task = scene
    store.finish_task(task["id"], "review", "结果")
    with pytest.raises(WorkbenchError) as excinfo:
        apply_delivery(store, task["id"])
    assert excinfo.value.code != "PEER_REVIEW_REQUIRED"


def test_peer_review_required_refuses_until_a_different_employee_verifies(scene, tmp_path):
    store, project, alice, bob, task = scene
    store.finish_task(task["id"], "review", "结果")
    artifact = tmp_path / "out.md"
    artifact.write_text("交付物", encoding="utf-8")
    wpf.build_proof(store, task["id"], artifacts=[str(artifact)])

    wp.set_policy(store, {"peer_review": "required_for_write"}, project_id=project["id"])
    with pytest.raises(WorkbenchError) as excinfo:
        apply_delivery(store, task["id"])
    assert excinfo.value.code == "PEER_REVIEW_REQUIRED"

    # 自己验自己：直接拒绝（不是"记录但不计数"）
    with pytest.raises(ValueError):
        wpf.verify_proof(store, task["id"], record=True, verifier_id=alice["id"])
    assert wp.peer_verified(store, task["id"], alice["id"]) is False
    with pytest.raises(WorkbenchError):
        apply_delivery(store, task["id"])

    # 另一名员工验过才算
    wpf.verify_proof(store, task["id"], record=True, verifier_id=bob["id"])
    assert wp.peer_verified(store, task["id"], alice["id"]) is True
    with pytest.raises(WorkbenchError) as excinfo:
        apply_delivery(store, task["id"])
    assert excinfo.value.code != "PEER_REVIEW_REQUIRED"  # 已过策略门，落到原有拒绝


def test_policy_changes_do_not_touch_tasks(scene):
    store, project, _alice, _bob, _task = scene
    before = _snapshot(store)
    wp.set_policy(store, {"notification_mode": "folded_digest"}, project_id=project["id"])
    wp.record_escalation(store, project["id"], task_id=None, reason="x")
    after = _snapshot(store)
    assert before["tasks"] == after["tasks"]  # 策略不推进任何任务状态
    assert after["governance"] > before["governance"]  # 但必须留痕


def test_policy_view_keeps_historic_keys(scene):
    """MCP 的 mailbox_policy 响应不能回归：历史键必须在。"""
    store, project, _alice, _bob, _task = scene
    view = wp.effective_policy(store, project["id"])
    for key in (
        "ordinary_mail_starts_work",
        "reading_acknowledges",
        "notification_mode",
        "managed_execution",
    ):
        assert key in view
    assert view["reading_acknowledges"] is False  # 看 ≠ 接单，永不变
