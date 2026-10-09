"""迁移前置闸：坏库在**写任何 schema 之前**被拒；legacy 版本保持既有保证（裁决 a）。"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from agent_mailbox.workbench_store import (
    WorkbenchError,
    _assert_no_orphans,
    _assert_no_orphans_for_version,
    _foreign_key_violations,
)

STORE = Path(__file__).resolve().parents[1] / "src" / "agent_mailbox" / "workbench_store.py"


def _db_with_orphan(tmp_path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(tmp_path / "t.db")
    db.executescript(
        """
        PRAGMA foreign_keys=OFF;
        CREATE TABLE employees(id TEXT PRIMARY KEY);
        CREATE TABLE messages(id TEXT PRIMARY KEY, employee_id TEXT REFERENCES employees(id));
        INSERT INTO employees VALUES('e1');
        INSERT INTO messages VALUES('m1','e1');
        INSERT INTO messages VALUES('m2','ghost');
        """
    )
    return db


def test_clean_db_passes(tmp_path):
    db = sqlite3.connect(tmp_path / "clean.db")
    db.executescript("CREATE TABLE a(id TEXT PRIMARY KEY); INSERT INTO a VALUES('1');")
    assert _foreign_key_violations(db) == []
    _assert_no_orphans(db)


def test_orphan_blocks_with_table_and_anti_deadloop_advice(tmp_path):
    db = _db_with_orphan(tmp_path)
    assert _foreign_key_violations(db)[0][0] == "messages"
    with pytest.raises(WorkbenchError) as caught:
        _assert_no_orphans(db)
    message = str(caught.value)
    assert "尚未开始" in message and "messages" in message
    assert re.search(r"不要[^。]*备份", message)


def test_legacy_versions_keep_the_existing_guarantee(tmp_path):
    """v<=10 不加闸 ✓（既有保证：corrupt v5 ⇒ migration_failed + 可恢复 ✓ 老板裁决 a）。"""
    db = _db_with_orphan(tmp_path)
    _assert_no_orphans_for_version(db, 5)  # 不抛 ✓
    _assert_no_orphans_for_version(db, 10)  # 不抛 ✓
    with pytest.raises(WorkbenchError) as caught:
        _assert_no_orphans_for_version(db, 11)  # v>=11 才拦 ✓
    assert caught.value.code == "migration_blocked"


def test_gate_runs_before_the_backup_and_any_schema_statement():
    """闸必须在**建备份之前** ✓（评审原建议 ✓ 现在做得到：闸只对 v>=11 生效 ⇒ 不影响 legacy ✓）。

    两条都要防：闸晚于备份 ⇒ 失败迁移留**冗余备份**（评审中危"同名堆积"✗）；
    闸晚于 schema ⇒ 半迁移状态 ✗。
    """
    source = STORE.read_text()
    gate = source.index("_assert_no_orphans_for_version(db, version)")
    backup = source.index("state-v{version}-before-v{SCHEMA_VERSION}")
    schema = source.index('for statement in SCHEMA.split(";")')
    assert gate < backup, "闸必须在建备份之前 ✗"
    assert gate < schema, "闸必须在写 schema 之前 ✗"


def test_misleading_restore_advice_is_gone():
    assert "请恢复迁移前备份" not in STORE.read_text()
