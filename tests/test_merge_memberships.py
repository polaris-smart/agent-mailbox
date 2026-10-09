"""合并 memberships：**先按项目去重** ✓（盲 UPDATE 撞 PK ✗ —— 评审预言 + 实测双证）。

夹具纪律（本轮吃过的红灯）：先 mkdir ✗ 用产品 API ✗ 期望值从场景代码**手推** ✓。
"""

from __future__ import annotations

import pytest

from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


def test_create_employee_auto_joins_the_project(tmp_path):
    """事实核查（本轮实测 ✓）：`create_employee(..., project_id)` **会自动建 membership** ⇒
    写夹具时必须把它算进去（我先前按"只 add 了才算成员"建模 ⇒ 期望值连错 ✗）。"""
    store = WorkbenchStore(tmp_path / "home")
    (tmp_path / "p").mkdir(exist_ok=True)
    project = store.create_project("P", tmp_path / "p")
    person = store.create_employee("A", "deepseek", project["id"])
    with store._transaction() as db:
        owners = [
            r["employee_id"]
            for r in db.execute(
                "SELECT employee_id FROM memberships WHERE project_id=?", (project["id"],)
            )
        ]
    assert owners == [person["id"]]


def _scene(tmp_path, *, split):
    """split=True：a∈P1,P2 且 b∈P1 ⇒ P1 共享（dropped）· P2 独占（moved）。

    split=False：两人都不在 P1（a 只在 P2）⇒ 全部 moved。
    """
    (tmp_path / "p1").mkdir(exist_ok=True)
    (tmp_path / "p2").mkdir(exist_ok=True)
    store = WorkbenchStore(tmp_path / "home")
    p1 = store.create_project("P1", tmp_path / "p1")
    p2 = store.create_project("P2", tmp_path / "p2")
    a = store.create_employee("A", "deepseek", p1["id"])
    b = store.create_employee("B", "deepseek", p1["id"])
    if split:
        store.add_project_member(p1["id"], a["id"])  # a∈P1
        store.add_project_member(p1["id"], b["id"])  # b∈P1  ⇒ P1 共享
        store.add_project_member(p2["id"], a["id"])  # a∈P2  ⇒ P2 独占
    else:
        store.add_project_member(p2["id"], a["id"])  # 只有 a∈P2 ⇒ 全部 moved
    return store, a, b, p1, p2


def test_dry_run_reports_without_writing(tmp_path):
    store, a, b, _p1, _p2 = _scene(tmp_path, split=True)
    report = store.merge_employee_memberships(a["id"], b["id"])  # 默认 dry-run ✓
    assert report["dry_run"] is True and report["dropped"] == 1 and report["moved"] == 1
    assert store.merge_employee_memberships(a["id"], b["id"])["dropped"] == 1  # 未写 ✓


def test_shared_project_row_dropped_and_sessions_revoked(tmp_path):
    store, a, b, p1, _p2 = _scene(tmp_path, split=True)
    report = store.merge_employee_memberships(a["id"], b["id"], dry_run=False)
    assert report["dropped"] == 1 and report["moved"] == 1
    assert store.merge_employee_memberships(a["id"], b["id"])["dropped"] == 0  # 旧行已去 ✓
    with store._transaction() as db:
        owners = [
            r["employee_id"]
            for r in db.execute(
                "SELECT employee_id FROM memberships WHERE project_id=?", (p1["id"],)
            )
        ]
    assert owners == [b["id"]], owners


def test_project_only_old_has_is_moved(tmp_path):
    store, a, b, _p1, p2 = _scene(tmp_path, split=False)
    report = store.merge_employee_memberships(a["id"], b["id"], dry_run=False)
    assert report["moved"] >= 1, report  # 至少 P2 是"只旧行有"⇒ 改指向 ✓
    with store._transaction() as db:
        owners = [
            r["employee_id"]
            for r in db.execute(
                "SELECT employee_id FROM memberships WHERE project_id=?", (p2["id"],)
            )
        ]
    assert owners == [b["id"]], owners


def test_same_id_is_refused(tmp_path):
    store, a, _b, _p1, _p2 = _scene(tmp_path, split=True)
    with pytest.raises(WorkbenchError):
        store.merge_employee_memberships(a["id"], a["id"])
