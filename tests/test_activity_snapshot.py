"""教室视图数据底座：不要假指标，要有产出。"""

from __future__ import annotations

import pytest

from agent_mailbox.workbench_store import WorkbenchStore


@pytest.fixture
def store(tmp_path):
    s = WorkbenchStore(tmp_path / "data")
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    s.create_project("Demo", project_dir)
    return s


def test_devices_never_expose_a_fake_heartbeat(store):
    """`devices.last_seen` 没有写入方 ⇒ 必须显式声明不可用，而不是给个假值。（ux-ia2/eng-verify）"""
    snap = store.activity_snapshot()
    assert snap["devices"], "至少应有一台本机设备"
    for device in snap["devices"]:
        assert device["heartbeat_available"] is False
        assert "last_seen" not in device, "不许把没有写入方的列当指标展示"


def test_employee_row_carries_outcome_not_just_activity(store):
    """四问全绿仍可能空转 ⇒ 每行必须带「最近一件产出」字段（可为空，但字段要在）。"""
    snap = store.activity_snapshot()
    assert set(snap) == {"employees", "devices"}
    for row in snap["employees"]:  # 空库也允许（本机可能还没登记员工）
        assert set(row) >= {"active_tasks", "live_sessions", "wired", "last_action", "last_outcome"}
        assert isinstance(row["wired"], bool)
