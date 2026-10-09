"""派单是特权：只有人能派 · 每小时上限 · 同目标去重（默认关，按项目开）。

2026-10-06 老板关注"不是所有 agent 都能当车间主任，否则乱建任务 ⇒ 风暴"。
员工侧本就没有建/派任务工具；这里把"人能派"固化为不变量，并给出上限与去重两道可配闸。
"""

from __future__ import annotations

import pytest

from agent_mailbox import workbench_enroll as we
from agent_mailbox.workbench_policy import set_policy
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore

FAKE = [{"kind": "claude", "name": "Claude Code", "path": "/x/claude"}]


@pytest.fixture
def scene(tmp_path, monkeypatch):
    from agent_mailbox import workbench_runtime

    monkeypatch.setattr(workbench_runtime, "discover_employees", lambda: [dict(r) for r in FAKE])
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    return store, store.create_project("Demo", directory)


def _code(exc: WorkbenchError) -> str:
    return getattr(exc, "code", "") or str(exc)


def test_employee_actor_needs_a_whitelist(scene):
    """发起方解析成员工时，默认拒绝 —— "不是所有 agent 都能当车间主任"。"""
    store, project = scene
    me = we.enroll(store, project["id"], "claude", host="generic", binary="/x/agent-mailbox")
    employee_id, employee_name = me["employee"]["id"], me["employee"]["name"]

    store.create_task(project["id"], "人派的单", "正文", employee_id)  # actor 默认 human ✓
    with pytest.raises(WorkbenchError) as caught:
        store.create_task(project["id"], "员工派的单", "正文", employee_id, actor=employee_name)
    assert _code(caught.value) == "dispatch_not_allowed", _code(caught.value)

    set_policy(
        store, {"dispatch": {"employees_may_dispatch": [employee_name]}}, project_id=project["id"]
    )
    store.create_task(
        project["id"], "白名单内的单", "正文", employee_id, actor=employee_name
    )  # 放行 ✓


def test_hourly_cap_blocks_a_dispatch_storm(scene):
    """防风暴的主力闸是每小时上限（可配）。"""
    store, project = scene
    me = we.enroll(store, project["id"], "claude", host="generic", binary="/x/agent-mailbox")
    employee_id = me["employee"]["id"]
    set_policy(store, {"dispatch": {"max_tasks_per_hour": 1}}, project_id=project["id"])

    store.create_task(project["id"], "第一单", "正文", employee_id)
    with pytest.raises(WorkbenchError) as caught:
        store.create_task(project["id"], "第二单", "正文", employee_id)
    assert _code(caught.value) == "dispatch_rate_limited", _code(caught.value)


def test_same_goal_retry_is_allowed_by_default(scene):
    """同一 title+prompt 重派 = **正常重试**，故去重默认关（硬默认会误伤）。"""
    store, project = scene
    me = we.enroll(store, project["id"], "claude", host="generic", binary="/x/agent-mailbox")
    employee_id = me["employee"]["id"]

    store.create_task(project["id"], "同一目标", "同一正文", employee_id)
    store.create_task(project["id"], "同一目标", "同一正文", employee_id)  # 默认不拦 ✓


def test_dedupe_window_blocks_duplicates_when_enabled(scene):
    """项目显式开启去重后，窗口内同目标必须拒绝，并指向原任务。"""
    store, project = scene
    me = we.enroll(store, project["id"], "claude", host="generic", binary="/x/agent-mailbox")
    employee_id = me["employee"]["id"]
    set_policy(store, {"dispatch": {"dedupe_window_hours": 24}}, project_id=project["id"])

    store.create_task(project["id"], "同一目标", "同一正文", employee_id)
    with pytest.raises(WorkbenchError) as caught:
        store.create_task(project["id"], "同一目标", "同一正文", employee_id)
    assert _code(caught.value) == "duplicate_dispatch", _code(caught.value)


def test_dispatch_policy_values_are_validated(scene):
    store, project = scene
    with pytest.raises(ValueError):
        set_policy(store, {"dispatch": {"max_tasks_per_hour": -1}}, project_id=project["id"])
    with pytest.raises(TypeError):
        set_policy(store, {"dispatch": "不是对象"}, project_id=project["id"])


def test_employee_collaboration_path_is_explicitly_exempt(scene):
    """`actor="employee:<id>"`（request_work 的合作路径）是**显式豁免**，不是静默绕过。

    第四轮评审 flow2 实测：守卫按 id/name 查不到 `employee:<id>` ⇒ 员工分支被整段跳过 ✗。
    豁免本身保留（该路径有跳数≤3 + 发送侧限速自己的护栏 ✓），但要**写明 + 单测** ✓。
    """
    store, project = scene
    me = we.enroll(store, project["id"], "claude", host="generic", binary="/x/agent-mailbox")
    employee_id, employee_name = me["employee"]["id"], me["employee"]["name"]

    # ① 协作路径：豁免（与既有 request_work 能力一致）✓
    store.create_task(
        project["id"], "同事求助", "正文", employee_id, actor=f"employee:{employee_id}"
    )

    # ② 同一员工用**裸名**发起（非协作路径）⇒ 仍然需要白名单 ✓ 守卫没被豁免带偏
    with pytest.raises(WorkbenchError) as caught:
        store.create_task(project["id"], "裸名派单", "正文", employee_id, actor=employee_name)
    assert _code(caught.value) == "dispatch_not_allowed", _code(caught.value)

    set_policy(
        store, {"dispatch": {"employees_may_dispatch": [employee_name]}}, project_id=project["id"]
    )
    store.create_task(project["id"], "白名单内的裸名派单", "正文", employee_id, actor=employee_name)
