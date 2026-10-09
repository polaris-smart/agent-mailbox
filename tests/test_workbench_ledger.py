"""Task ledger: derived view, zero writes (roadmap T11)."""

from __future__ import annotations

import json

import pytest

from agent_mailbox import workbench_ledger as wl
from agent_mailbox import workbench_proof as wp
from agent_mailbox.workbench_mail_tasks import create_mail_task
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


@pytest.fixture
def scene(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    alice = store.create_employee("Alice", "codex", project["id"])  # 可受管执行
    bob = store.create_employee("Bob", "hermes", project["id"])  # 不可受管执行
    return store, project, alice, bob


def _run(store, project, employee, title):
    task = store.create_task(project["id"], title, "work", employee["id"])
    store.claim_task(store.local_node()["id"])
    store.set_status(task["id"], "running")
    return task


def _snapshot(store):
    with store._transaction() as db:
        return {
            table: db.execute(f"SELECT count(*) AS c FROM {table}").fetchone()["c"]
            for table in ("tasks", "events", "governance_events", "messages")
        }


def test_task_rows_carry_duration_events_messages_and_error(scene):
    store, project, alice, _bob = scene
    ok = _run(store, project, alice, "正常单")
    store.finish_task(ok["id"], "review", "结果")
    bad = _run(store, project, alice, "坏单")
    store.finish_task(bad["id"], "failed", "", {"code": "UPSTREAM_TIMEOUT", "message": "配额烧尽"})
    store.send_message(project["id"], "配套信", "正文", recipient_id=alice["id"])

    view = wl.ledger(store, project["id"])
    rows = {row["task_id"]: row for row in view["tasks"]}
    assert set(rows) == {ok["id"], bad["id"]}
    assert rows[bad["id"]]["error_code"] == "UPSTREAM_TIMEOUT"
    assert "配额烧尽" in rows[bad["id"]]["error_message"]
    assert rows[ok["id"]]["events"] >= 1  # queued/accepted/… 来自 events
    assert all(row["duration_seconds"] is not None for row in view["tasks"])


def _mailbox_task(store, project, employee, title):
    """mailbox 任务：人派活给非受管员工（store 对它**不**要适配器）。"""
    return create_mail_task(store, project["id"], title, "work", employee["id"])


def _mark_failed(store, task_id, code, message):
    with store._transaction() as db:
        db.execute(
            "UPDATE tasks SET status='failed', error=? WHERE id=?",
            (json.dumps({"code": code, "message": message}), task_id),
        )


def test_managed_task_is_refused_for_unsupported_employee(scene):
    """发现（对 T29/T30 有用）：受管路径**已经**拒绝无适配器员工；mailbox 路径按设计允许。"""
    store, project, _alice, bob = scene
    with pytest.raises(WorkbenchError) as excinfo:
        store.create_task(project["id"], "managed", "work", bob["id"])
    assert excinfo.value.code == "ADAPTER_UNSUPPORTED"
    assert _mailbox_task(store, project, bob, "信箱派活")["execution_mode"] == "mailbox"


def test_upstream_rollup_surfaces_the_unhealthy_one(scene):
    """今天三起'上游不健康'没有统一视图 —— 这里一次看全。"""
    store, project, alice, bob = scene
    good = _run(store, project, alice, "成功单")
    store.finish_task(good["id"], "review", "结果")
    for i in range(2):
        bad = _mailbox_task(store, project, bob, f"失败单{i}")
        _mark_failed(store, bad["id"], "UPSTREAM_DOWN", "登录态失效")

    upstream = {row["name"]: row for row in wl.ledger(store, project["id"])["upstream"]}
    assert upstream["Bob"]["failed"] == 2 and upstream["Bob"]["success_rate"] == 0.0
    assert upstream["Bob"]["last_failure"]["code"] == "UPSTREAM_DOWN"
    assert upstream["Alice"]["success_rate"] == 1.0
    assert upstream["Alice"]["execution_supported"] is True
    assert upstream["Bob"]["execution_supported"] is False  # hermes 不是受管执行引擎
    # 排序：失败多的排前面（一眼看到谁不健康）
    assert wl.ledger(store, project["id"])["upstream"][0]["name"] == "Bob"


def test_ledger_shows_proof_and_verdict(scene, tmp_path):
    store, project, alice, _bob = scene
    task = _run(store, project, alice, "带证明的单")
    store.finish_task(task["id"], "review", "结果")
    artifact = tmp_path / "out.md"
    artifact.write_text("交付物", encoding="utf-8")
    wp.build_proof(
        store,
        task["id"],
        artifacts=[str(artifact)],
        criteria=[{"criterion": "x", "self_check": "pass"}],
    )
    wp.verify_proof(store, task["id"], record=True)

    row = next(r for r in wl.ledger(store, project["id"])["tasks"] if r["task_id"] == task["id"])
    assert row["has_proof"] is True
    assert row["proof_verdict"] == "verified"


def test_ledger_never_writes(scene):
    """约束：账本是视图 —— 零写、无新表。"""
    store, project, alice, _bob = scene
    task = _run(store, project, alice, "只读检查")
    store.finish_task(task["id"], "review", "结果")
    before = _snapshot(store)
    wl.ledger(store, project["id"])
    wl.ledger(store)
    after = _snapshot(store)
    assert before == after, "账本产生了写操作"
    with store._transaction() as db:
        names = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not any(name.startswith("ledger") for name in names), "账本不该新建表"


def test_ledger_is_scoped_by_project(scene, tmp_path):
    store, project, alice, _bob = scene
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = store.create_project("Other", other_dir)
    other_emp = store.create_employee("Carol", "codex", other["id"])
    _run(store, project, alice, "本项目单")
    _run(store, other, other_emp, "别项目单")

    assert len(wl.ledger(store, project["id"])["tasks"]) == 1
    assert len(wl.ledger(store, other["id"])["tasks"]) == 1
    assert len(wl.ledger(store)["tasks"]) == 2  # 不限定 = 全部


def test_ledger_on_empty_store_is_quiet(scene):
    store, _project, _alice, _bob = scene
    view = wl.ledger(store)
    assert view["tasks"] == []
    assert all(row["tasks_total"] == 0 and row["success_rate"] is None for row in view["upstream"])


def test_upstream_hides_idle_employees_by_default(scene):
    """去噪：默认不列零活动员工（否则 21 个空行淹掉真正不健康的那一个）。"""
    store, project, alice, _bob = scene
    task = _run(store, project, alice, "唯一有单的")
    store.finish_task(task["id"], "review", "结果")

    active = wl.ledger(store, project["id"])["upstream"]
    assert [row["name"] for row in active] == ["Alice"]
    everything = wl.ledger(store, project["id"], include_idle=True)["upstream"]
    assert len(everything) >= 2 and "Bob" in [row["name"] for row in everything]
