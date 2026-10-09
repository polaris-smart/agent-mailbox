"""schema 11→12：只加两表（**不碰既有数据** ✓）+ 迁移前自动备份 + 数字对账 ✓。"""

from __future__ import annotations

import sqlite3

from agent_mailbox.workbench_store import SCHEMA_VERSION, WorkbenchStore


def _v11_with_data(tmp_path):
    """建当前版本库并灌数据，再把 user_version 压回 11 ⇒ 模拟"旧库待迁移" ✓（不手造反 schema ✗）。"""
    store = WorkbenchStore(tmp_path / "home")
    (tmp_path / "repo").mkdir(exist_ok=True)
    project = store.create_project("P", tmp_path / "repo")
    store.create_employee("A", "deepseek", project["id"])
    db = sqlite3.connect(store.db_path)
    db.execute("PRAGMA user_version=11")
    db.commit()
    before = {
        table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "employees",
            "projects",
            "memberships",
            "governance_events",
            "mailbox_sessions",
        )
    }
    db.close()
    return before


def test_migration_adds_tables_and_preserves_data(tmp_path):
    before = _v11_with_data(tmp_path)
    reopened = WorkbenchStore(tmp_path / "home")  # 触发 11→12 迁移 ✓
    assert SCHEMA_VERSION == 12
    db = sqlite3.connect(reopened.db_path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 12
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"notifications", "identity_links"} <= names
    after = {
        table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "employees",
            "projects",
            "memberships",
            "governance_events",
            "mailbox_sessions",
        )
    }
    db.close()
    assert after == before, f"迁移不得改动既有数据 ✗：{before} → {after}"


def test_migration_creates_a_backup(tmp_path):
    _v11_with_data(tmp_path)
    WorkbenchStore(tmp_path / "home")
    backups = list((tmp_path / "home" / "workbench").glob("state-v11-before-v12-*.sqlite"))
    assert backups, "迁移前必须自动备份 ✓（回滚路径）"


def test_fresh_store_is_already_v12_and_has_the_tables(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    db = sqlite3.connect(store.db_path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    db.close()
    assert {"notifications", "identity_links"} <= names
