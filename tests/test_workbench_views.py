"""View marks: watermark feeds backlog folding only (roadmap 水位线第一步)."""

from __future__ import annotations

import pytest

from agent_mailbox import workbench_views as wv
from agent_mailbox.workbench_store import WorkbenchStore


@pytest.fixture
def scene(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    alice = store.create_employee("Alice", "codex", project["id"])
    bob = store.create_employee("Bob", "codex", project["id"])
    task = store.create_task(project["id"], "Build", "work", alice["id"])
    store.claim_task(store.local_node()["id"])
    store.set_status(task["id"], "running")
    return store, project, alice, bob, task


def _send(store, project, sender, recipient, task, body, n=1):
    out = []
    for i in range(n):
        out.append(
            store.send_message(
                project["id"],
                f"{body} {i}",
                body,
                recipient_id=recipient["id"],
                sender_id=sender["id"],
                source_task_id=task["id"],
            )
        )
    return out


def test_mark_advances_and_never_moves_backwards(scene):
    store, _project, alice, bob, _task = scene
    first = wv.record_view(store, bob["id"], alice["id"], "2026-10-04T10:00:00+00:00")
    assert first["last_viewed_at"] == "2026-10-04T10:00:00+00:00"

    # 单调不减（HS 的不变量）：更早的时间戳不得回退
    again = wv.record_view(store, bob["id"], alice["id"], "2026-10-04T09:00:00+00:00")
    assert again["last_viewed_at"] == "2026-10-04T10:00:00+00:00"
    assert wv.last_viewed_at(store, bob["id"], alice["id"]) == "2026-10-04T10:00:00+00:00"

    # 相同时刻也不动
    wv.record_view(store, bob["id"], alice["id"], "2026-10-04T10:00:00+00:00")
    assert wv.last_viewed_at(store, bob["id"], alice["id"]) == "2026-10-04T10:00:00+00:00"

    # 更晚则前进
    wv.record_view(store, bob["id"], alice["id"], "2026-10-04T11:00:00+00:00")
    assert wv.last_viewed_at(store, bob["id"], alice["id"]) == "2026-10-04T11:00:00+00:00"


def test_backlog_counts_only_after_the_mark(scene):
    store, project, alice, bob, task = scene
    _send(store, project, alice, bob, task, "第一批", n=3)
    assert wv.backlog_count(store, bob["id"], alice["id"]) == 3  # 没有水位线 ⇒ 全算

    with store._transaction() as db:
        rows = db.execute(
            "SELECT created_at FROM messages WHERE recipient_id=? ORDER BY created_at", (bob["id"],)
        ).fetchall()
    wv.record_view(store, bob["id"], alice["id"], rows[1]["created_at"])
    assert wv.backlog_count(store, bob["id"], alice["id"]) == 1  # 只看水位线之后的

    _send(store, project, alice, bob, task, "第二批", n=2)
    assert wv.backlog_count(store, bob["id"], alice["id"]) == 3


def test_viewing_records_a_mark_but_changes_no_state(scene):
    """核心不变量：查看永不改任何任务/消息状态（HS 要求钉成断言）。"""
    store, project, alice, bob, task = scene
    _send(store, project, alice, bob, task, "待看", n=2)

    def snapshot():
        with store._transaction() as db:
            return {
                # messages 表没有 status 列（实测）；不变量用 id/thread/task 归属即可验证
                "messages": [
                    tuple(r)
                    for r in db.execute(
                        "SELECT id,thread_id,task_id,created_at FROM messages ORDER BY id"
                    )
                ],
                "tasks": [tuple(r) for r in db.execute("SELECT id,status FROM tasks")],
                "governance": db.execute("SELECT count(*) AS c FROM governance_events").fetchone()[
                    "c"
                ],
            }

    before = snapshot()
    inbox = store.employee_messages(project["id"], bob["id"], "inbox")
    after = snapshot()
    assert inbox["viewing_acknowledges"] is False
    assert before == after, "读信改变了消息/任务状态"
    assert wv.last_viewed_at(store, bob["id"], alice["id"]) is not None  # 但水位线已记


def test_backlog_tier_folds_a_slow_drip(scene, monkeypatch):
    """速率档失效时的兜底：把速率阈值调到极大，只留积压档，慢滴也要被折叠。"""
    store, project, alice, bob, task = scene
    monkeypatch.setenv("AGENT_MAILBOX_FOLD_TIERS", "300:9999,21600:9999")
    monkeypatch.setenv("AGENT_MAILBOX_BACKLOG_LIMIT", "3")
    monkeypatch.delenv("AGENT_MAILBOX_BACKLOG_FOLD", raising=False)

    _send(store, project, alice, bob, task, "历史", n=4)
    store.employee_messages(project["id"], bob["id"], "inbox")  # bob 看过了 ⇒ 水位线建立
    assert wv.backlog_count(store, bob["id"], alice["id"]) == 0

    results = _send(store, project, alice, bob, task, "慢滴", n=4)
    assert all(not r.get("folded") for r in results[:3])  # 未达上限
    assert results[3].get("folded") is True  # 第 4 封触发积压折叠
    assert wv.backlog_count(store, bob["id"], alice["id"]) >= 3


def test_kill_switch_disables_backlog_folding(scene, monkeypatch):
    store, project, alice, bob, task = scene
    monkeypatch.setenv("AGENT_MAILBOX_FOLD_TIERS", "300:9999,21600:9999")
    monkeypatch.setenv("AGENT_MAILBOX_BACKLOG_LIMIT", "2")
    monkeypatch.setenv("AGENT_MAILBOX_BACKLOG_FOLD", "0")

    assert wv.backlog_folding_enabled() is False
    results = _send(store, project, alice, bob, task, "风暴", n=5)
    assert all(not r.get("folded") for r in results)  # 关了就不折


def test_backlog_trips_requires_enabled_and_limit(scene, monkeypatch):
    store, _project, alice, bob, _task = scene
    monkeypatch.setenv("AGENT_MAILBOX_BACKLOG_LIMIT", "1")
    assert wv.backlog_trips(store, bob["id"], alice["id"]) is False  # 空积压
    monkeypatch.setenv("AGENT_MAILBOX_BACKLOG_FOLD", "0")
    assert wv.backlog_trips(store, bob["id"], alice["id"]) is False  # 关了
