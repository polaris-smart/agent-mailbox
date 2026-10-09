"""Team wall + audit chain: pull-only, no new writer, denoised (roadmap U7)."""

from __future__ import annotations

import pytest

from agent_mailbox import echo_guard as eg
from agent_mailbox import workbench_wall as ww
from agent_mailbox.workbench_store import WorkbenchStore


@pytest.fixture
def scene(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    alice = store.create_employee("Alice", "codex", project["id"])
    bob = store.create_employee("Bob", "codex", project["id"])
    return store, project, alice, bob


def _running_task(store, project, employee, title="Build"):
    task = store.create_task(project["id"], title, "work", employee["id"])
    assert store.claim_task(store.local_node()["id"])["id"] == task["id"]
    return store.set_status(task["id"], "running")


def _snapshot(store):
    with store._transaction() as db:
        counts = {
            table: db.execute(f"SELECT count(*) AS c FROM {table}").fetchone()["c"]
            for table in ("messages", "tasks", "events", "governance_events")
        }
        counts["fts_present"] = db.execute(
            "SELECT count(*) AS c FROM sqlite_master WHERE type='table' AND name='fts_messages'"
        ).fetchone()["c"]
    return counts


def test_wall_separates_running_waiting_and_stuck(scene):
    store, project, alice, bob = scene
    running = _running_task(store, project, alice, "正在跑")
    # 结束任务必须先 claim→running（store 会拒绝未开始就 finish）
    waiting = _running_task(store, project, bob, "待验收")
    store.finish_task(waiting["id"], "review", "结果在此")
    broken = _running_task(store, project, bob, "坏掉的")
    store.finish_task(broken["id"], "failed", "", {"code": "X", "message": "boom"})

    view = ww.wall(store, project["id"])
    assert [t["id"] for t in view["running"]] == [running["id"]]
    assert [t["id"] for t in view["waiting_on_human"]] == [waiting["id"]]
    assert [t["id"] for t in view["stuck"]] == [broken["id"]]
    assert view["counts"]["tasks"] == 3
    assert all("body" not in row for row in view["running"])  # 去噪：不搬消息流水


def test_wall_shows_folded_and_frozen(scene, monkeypatch):
    store, project, alice, bob = scene
    monkeypatch.setenv("AGENT_MAILBOX_FOLD_LIMIT", "2")
    task = _running_task(store, project, alice, "承载折叠")
    first = store.send_message(
        project["id"],
        "第一封",
        "正文",
        recipient_id=bob["id"],
        sender_id=alice["id"],
        source_task_id=task["id"],
    )
    for i in range(4):
        store.send_message(
            project["id"],
            f"洪水 {i}",
            "正文",
            recipient_id=bob["id"],
            sender_id=alice["id"],
            source_task_id=task["id"],
        )

    view = ww.wall(store, project["id"])
    assert view["counts"]["folded_messages"] >= 1

    thread = first["thread_id"]
    eg.freeze_thread(store, thread, reason="合成回声环", project_id=project["id"])
    assert [t["thread_id"] for t in ww.wall(store, project["id"])["frozen_threads"]] == [thread]

    eg.unfreeze_thread(store, thread, reason="人工确认", project_id=project["id"])
    assert ww.wall(store, project["id"])["frozen_threads"] == []


def test_wall_and_audit_never_write(scene):
    """约束'不引入新写者'：墙与审计链零写，也不许顺手建检索索引。"""
    store, project, _alice, bob = scene
    message = store.send_message(project["id"], "只读检查", "正文", recipient_id=bob["id"])
    before = _snapshot(store)
    ww.wall(store, project["id"])
    ww.audit_chain(store, message["id"])
    after = _snapshot(store)
    assert before == after, "墙/审计链产生了写操作"
    assert after["fts_present"] == 0, "墙不应该顺手建检索索引（那是写）"


def test_audit_chain_covers_a_task_life(scene):
    store, project, _alice, bob = scene
    # 产品路径：人发工作请求 ⇒ 生成请求消息 + 任务卡
    request = store.send_message(
        project["id"], "请干活", "正文", recipient_id=bob["id"], request_work=True
    )
    with store._transaction() as db:
        task_id = db.execute(
            "SELECT id FROM tasks WHERE request_message_id = ?", (request["id"],)
        ).fetchone()["id"]
    store.claim_task(store.local_node()["id"])
    store.set_status(task_id, "running")
    store.finish_task(task_id, "review", "交付结果")

    chain = ww.audit_chain(store, request["id"])
    steps = [s["step"] for s in chain["steps"]]
    assert steps[0] == "sent" and steps[1] == "received"
    assert any(s.startswith("terminal:") for s in steps)
    assert chain["terminal_state"] == "review"
    assert [t["id"] for t in chain["tasks"]] == [task_id]
    assert any(e["type"] for e in chain["task_events"])


def test_audit_chain_on_a_plain_message(scene):
    store, project, _alice, bob = scene
    message = store.send_message(project["id"], "普通信", "正文", recipient_id=bob["id"])
    chain = ww.audit_chain(store, message["id"])
    assert chain["message"]["id"] == message["id"]
    assert chain["terminal_state"] == "no-task"
    assert [s["step"] for s in chain["steps"]][:2] == ["sent", "received"]


def test_audit_chain_unknown_message_raises(scene):
    store, _project, _alice, _bob = scene
    with pytest.raises(ValueError):
        ww.audit_chain(store, "message_does_not_exist")
