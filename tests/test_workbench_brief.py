"""Session-start brief: bounded, read-only, denoised (roadmap T31)."""

from __future__ import annotations

import pytest

from agent_mailbox import workbench_brief as wb
from agent_mailbox.workbench_store import WorkbenchStore


@pytest.fixture
def scene(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    alice = store.create_employee("Alice", "codex", project["id"])
    bob = store.create_employee("Bob", "codex", project["id"])
    store.send_message(project["id"], "第一封未读", "正文", recipient_id=alice["id"])
    store.send_message(project["id"], "第二封未读", "正文", recipient_id=alice["id"])
    task = store.create_task(project["id"], "等我验收", "work", alice["id"])
    store.claim_task(store.local_node()["id"])
    store.set_status(task["id"], "running")
    store.finish_task(task["id"], "review", "结果")
    return store, project, alice, bob


def _snapshot(store):
    with store._transaction() as db:
        return {
            table: db.execute(f"SELECT count(*) AS c FROM {table}").fetchone()["c"]
            for table in ("messages", "tasks", "governance_events")
        }


def test_brief_reports_the_three_questions(scene):
    store, project, alice, _bob = scene
    data = wb.brief(store, employee_id=alice["id"], project_id=project["id"])
    assert data["unread"]["count"] == 2
    assert [row["title"] for row in data["waiting_on_human"]] == ["等我验收"]
    assert data["running"] == []
    assert data["policy"]["notification_mode"] == "manual_check"


def test_brief_is_read_only(scene):
    store, project, alice, _bob = scene
    before = _snapshot(store)
    wb.brief(store, employee_id=alice["id"], project_id=project["id"])
    assert _snapshot(store) == before


def test_brief_never_exceeds_the_budget(scene):
    store, project, alice, _bob = scene
    for i in range(20):
        store.send_message(project["id"], f"很多未读 {i}" * 5, "正文", recipient_id=alice["id"])
    data = wb.brief(store, employee_id=alice["id"], project_id=project["id"], budget=200)
    text = wb.render(data, budget=200)
    assert len(text.encode("utf-8")) <= 200
    assert "截断" in text or len(text.encode("utf-8")) <= 200
    with pytest.raises(ValueError):
        wb.brief(store, budget=0)
    with pytest.raises(ValueError):
        wb.brief(store, budget=10**9)


def test_brief_flags_quiet_but_abnormal(scene):
    """安静不等于无事：简报必须把"被折叠/被抑制"顶上来。"""
    store, project, alice, _bob = scene
    from agent_mailbox import workbench_policy as wp

    wp.set_policy(store, {"escalation_budget": 0}, project_id=project["id"])
    wp.record_escalation(store, project["id"], task_id=None, reason="想升级")
    data = wb.brief(store, employee_id=alice["id"], project_id=project["id"])
    assert data["signals"]["escalations_suppressed"] == 1
    assert "升级被抑制" in wb.render(data)


def test_empty_scope_still_renders(scene):
    store, _project, _alice, _bob = scene
    # 不限定项目时简报给全局视图（有意为之：先看得见，再收敛）
    data = wb.brief(store)
    assert data["unread"]["count"] == 0
    text = wb.render(data)
    assert text.startswith("【交接简报】") and "待我处理" in text
