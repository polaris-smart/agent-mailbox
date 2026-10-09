"""重指向机制：默认 dry-run ✓ 覆盖无外键引用 ✓ **排除 memberships**（不自撞主键 ✗）。"""

from __future__ import annotations

import sqlite3

import pytest

from agent_mailbox import workbench_enroll as enroll_mod
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


def _scene(tmp_path):
    (tmp_path / "repo").mkdir(exist_ok=True)
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("P", tmp_path / "repo")
    a = enroll_mod.enroll(store, project["id"], "deepseek", name="A", connection_type="app")
    b = enroll_mod.enroll(store, project["id"], "deepseek", name="B", connection_type="app")
    return store, project, a["employee"]["id"], b["employee"]["id"]


def test_dry_run_counts_without_writing(tmp_path):
    store, _project, a, b = _scene(tmp_path)
    report = store.repoint_employee_references(a, b)  # 默认 dry-run ✓
    assert report["dry_run"] is True
    assert report["affected"].get("mailbox_sessions.employee_id", 0) >= 1, report
    assert report["memberships_skipped"] >= 1, "memberships 必须被**跳过** ✓（否则撞主键 ✗）"
    assert "memberships.employee_id" not in report["affected"]
    assert store.repoint_employee_references(a, b)["total"] == report["total"]  # 未写 ✓


def test_execute_moves_sessions_and_actor_but_not_memberships(tmp_path):
    store, project, a, b = _scene(tmp_path)
    with store._transaction() as db:  # 治理日志原语 ⇒ actor 字符串引用 ✓
        store._governance(
            db,
            "note",
            employee_id=a,
            project_id=project["id"],
            actor=f"employee:{a}",
            reason="fixture",
        )
    with store._transaction() as db:
        before = [
            r["employee_id"]
            for r in db.execute(
                "SELECT employee_id FROM memberships WHERE project_id=?", (project["id"],)
            )
        ]
    report = store.repoint_employee_references(a, b, dry_run=False)
    assert report["dry_run"] is False and report["total"] >= 2, report
    assert store.repoint_employee_references(a, b)["total"] == 0  # 已无引用 ✓
    db = sqlite3.connect(store.db_path)
    sessions = [r[0] for r in db.execute("SELECT employee_id FROM mailbox_sessions")]
    actors = [
        r[0]
        for r in db.execute("SELECT actor FROM governance_events WHERE actor LIKE 'employee:%'")
    ]
    after = [
        r[0]
        for r in db.execute(
            "SELECT employee_id FROM memberships WHERE project_id=?", (project["id"],)
        )
    ]
    db.close()
    assert a not in sessions and all(x == b for x in sessions), sessions
    assert actors and all(x == f"employee:{b}" for x in actors), actors
    assert after == before, "memberships **不许**被本方法改动 ✓（交给专门的去重方法 ✓）"


def test_same_id_and_unknown_id_are_refused(tmp_path):
    store, _project, a, _b = _scene(tmp_path)
    with pytest.raises(WorkbenchError):
        store.repoint_employee_references(a, a)
    with pytest.raises(WorkbenchError):
        store.repoint_employee_references(a, "employee_nope")
