"""孤立引用体检：两类都要抓到（只查 foreign_key_check 会漏第二类 ✗）。"""

from __future__ import annotations

import sqlite3

from agent_mailbox.workbench_store import WorkbenchStore, _foreign_key_violations


def test_fresh_store_reports_ok(tmp_path):
    """新库 ok=True（覆盖 orphan_report 全路径 + 懒建表容忍 ✓）。"""
    report = WorkbenchStore(tmp_path / "home").orphan_report()
    assert report["ok"] is True
    assert report["foreign_key_violations"] == []
    assert report["ghost_view_marks"] == 0 and report["ghost_actors"] == 0


def test_no_fk_reference_is_invisible_to_foreign_key_check(tmp_path):
    """证据：**无 FK 的引用**对 foreign_key_check 永远不可见 ✗（这正是迁移的坑 ✓）。"""
    db = sqlite3.connect(tmp_path / "t.db")
    db.executescript(
        """
        CREATE TABLE employees(id TEXT PRIMARY KEY);
        CREATE TABLE mail_view_marks(employee_id TEXT, message_id TEXT);
        CREATE TABLE governance_events(actor TEXT, type TEXT);
        INSERT INTO employees VALUES('e1');
        INSERT INTO mail_view_marks VALUES('ghost','m1');
        INSERT INTO governance_events VALUES('employee:ghost','note');
        """
    )
    assert _foreign_key_violations(db) == [], "无 FK 引用不该被 foreign_key_check 看见"
    assert (
        db.execute(
            "SELECT count(*) FROM mail_view_marks WHERE employee_id NOT IN (SELECT id FROM employees)"
        ).fetchone()[0]
        == 1
    )
    assert (
        db.execute(
            "SELECT count(*) FROM governance_events WHERE actor LIKE 'employee:%' "
            "AND substr(actor, 10) NOT IN (SELECT id FROM employees)"
        ).fetchone()[0]
        == 1
    )
