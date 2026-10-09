"""权限语义：只读工具不打断人 + 「本任务内允许」一次批准覆盖同一 run。

真机实测：一个小任务打断人 6 次，且**只读**调用（`project_context`）也要批；
选项只有 `allow-once`/`reject`，没有"本任务内允许"。

这里钉住三个构件：
* `read_only_tool_call()` —— 只读识别（保守口径，不做模糊匹配）；
* `resolve_permission(..., "allow_run")` —— 新选项可被接受，且**未提供该选项时拒绝**；
* `run_has_allow_run()` —— 同一 run 的后续请求据此自动放行。

`permission()` 闭包内的接线（自动放行 + 选项注入）不在单测范围内，靠下一次真机执行复验。
"""

from __future__ import annotations

import pytest

from agent_mailbox.workbench_engine import read_only_tool_call
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


@pytest.fixture
def scene(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    alice = store.create_employee("Alice", "codex", project["id"])
    # 走真实路径拿到一个可请求权限的执行：派单 → 认领(starting) → running
    task = store.create_task(project["id"], "活", "说明", alice["id"])
    claimed = store.claim_task(store.local_node()["id"])
    assert claimed is not None and claimed["id"] == task["id"]
    store.set_status(task["id"], "running")
    return store, project, alice, claimed


def test_read_only_tools_are_recognised():
    for name in (
        "project_context",
        "project_messages",
        "project_tasks",
        "project_delivery",
        "project_memory_search",
        "project_resource_read",
        "project_resource_versions",
    ):
        assert read_only_tool_call({"name": f"mcp__agent-mailbox-project__{name}"}), name


def test_write_tools_are_still_gated():
    assert not read_only_tool_call({"name": "project_message"})
    assert not read_only_tool_call({"name": "project_note"})
    assert not read_only_tool_call({"name": "project_task_submit"})
    assert not read_only_tool_call({"title": "Preparing file…", "kind": "other"})
    assert not read_only_tool_call({})
    assert not read_only_tool_call(None)  # type: ignore[arg-type]


def test_kind_alone_is_never_enough():
    """`kind` 是员工自报字段：单独命中不得放行（第 10 轮 [高]）。"""
    assert not read_only_tool_call({"title": "anything", "kind": "read"})
    assert not read_only_tool_call(
        {"name": "shell", "kind": "read", "rawInput": {"command": "rm -rf /tmp/x"}}
    )
    assert not read_only_tool_call({"name": "Write", "kind": "read"})


def test_builtin_read_needs_exact_name_and_kind():
    assert read_only_tool_call({"name": "Read", "kind": "read"})
    assert not read_only_tool_call({"name": "Read", "kind": "write"})
    assert not read_only_tool_call({"name": "read", "kind": "read"})  # 大小写不折叠


def test_self_reported_metadata_cannot_grant_read_only():
    """第 10 轮 [高]：子串匹配 + title 拼接 + 大小写折叠都曾放行写操作。"""
    for attack in (
        {"name": "project_resource_read_write"},
        {"title": "project_context / project_message"},
        {"name": "PROJECT_CONTEXT"},
        {"name": "mcp__agent-mailbox-project__project_message"},
        {"name": "project_context_evil"},
    ):
        assert not read_only_tool_call(attack), attack


def _pending_permission(store, task, options):
    store.request_permission(task["id"], "req-1", options, {"name": "project_message"})
    return store


def test_allow_run_is_accepted_only_when_offered(scene):
    store, _project, _alice, task = scene
    options = [
        {"optionId": "allow-once", "name": "Yes", "kind": "allow_once"},
        {"optionId": "allow-run", "name": "本任务内允许", "kind": "allow_run"},
    ]
    _pending_permission(store, task, options)
    store.resolve_permission(task["id"], "req-1", "allow_run")
    assert store.run_has_allow_run(task["id"], task["run_id"]) is True


def test_allow_run_is_refused_when_the_agent_did_not_offer_it(scene):
    store, _project, _alice, task = scene
    _pending_permission(
        store, task, [{"optionId": "allow-once", "name": "Yes", "kind": "allow_once"}]
    )
    with pytest.raises(WorkbenchError) as caught:
        store.resolve_permission(task["id"], "req-1", "allow_run")
    assert caught.value.code == "invalid_field"
    assert store.run_has_allow_run(task["id"], task["run_id"]) is False


def test_run_has_allow_run_is_scoped_to_the_run(scene):
    store, _project, _alice, task = scene
    options = [{"optionId": "allow-run", "name": "本任务内允许", "kind": "allow_run"}]
    _pending_permission(store, task, options)
    store.resolve_permission(task["id"], "req-1", "allow_run")
    assert store.run_has_allow_run(task["id"], task["run_id"]) is True
    assert store.run_has_allow_run(task["id"], "run_some_other") is False


def test_expired_allow_run_no_longer_authorises(scene):
    """第 10 轮 [中] ②-2：过期的「本任务内允许」不得继续自动放行。"""
    store, _project, _alice, task = scene
    options = [{"optionId": "allow-run", "name": "本任务内允许", "kind": "allow_run"}]
    _pending_permission(store, task, options)
    store.resolve_permission(task["id"], "req-1", "allow_run")
    assert store.run_has_allow_run(task["id"], task["run_id"]) is True

    with store._transaction() as db:
        db.execute(
            "UPDATE permissions SET expires_at='2000-01-01T00:00:00+00:00' "
            "WHERE task_id=? AND request_id='req-1'",
            (task["id"],),
        )
    assert store.run_has_allow_run(task["id"], task["run_id"]) is False


def test_namespaced_and_spoofed_names_are_never_read_only():
    """第二轮评审 eng-verify 实测的两个残余绕过（第 10 轮修复没封死）。"""
    # 异命名空间：只取末段会放行 ✗ ⇒ 必须要求我们自己的 server 前缀
    assert not read_only_tool_call({"name": "mcp__x__project_context", "kind": "read"})
    assert not read_only_tool_call({"name": "mcp__x__Read", "kind": "read"})
    assert not read_only_tool_call({"name": "mcp__a__b__c", "kind": "read"})
    # 我们的真名仍然放行 ✓，写操作仍然拒绝 ✓
    assert read_only_tool_call({"name": "mcp__agent-mailbox-project__project_context"})
    assert not read_only_tool_call({"name": "mcp__agent-mailbox-project__project_message"})
