"""Upgrade contract, end to end (Codex AM-06; boss question 2026-10-08).

Contract: replacing the binary must NOT cost the user data, keys or wake watermarks.

Codex review of the first version (correct): it could pass vacuously -- it neither
forced a real migration (12 to 12 is not a migration) nor required any usable key.
This version removes both vacuities:
  1. it forces a real migration by pinning the copy back to SCHEMA_VERSION - 1;
  2. it requires a non-zero baseline AND proves a real token still validates.

Notes for editors: docstrings stay ASCII; Chinese quoting uses the corner brackets.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import sqlite3

import pytest

from agent_mailbox.workbench_mail_sessions import list_sessions, validate_session
from agent_mailbox.workbench_store import SCHEMA_VERSION, WorkbenchError, WorkbenchStore

REAL_HOME = pathlib.Path.home() / ".agent-mailbox-v08"


def _digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_home(tmp_path: pathlib.Path) -> pathlib.Path:
    home = tmp_path / "home"
    shutil.copytree(REAL_HOME, home, ignore=shutil.ignore_patterns("*.log"))
    return home


@pytest.mark.skipif(not REAL_HOME.is_dir(), reason="no real home on this machine")
def test_upgrade_migrates_really_and_keeps_data_keys_and_watermarks(tmp_path):
    """Force a real migration on a copied home; data, keys and watermarks must survive."""
    home = _copy_home(tmp_path)
    db_path = home / "workbench" / "state.db"

    with sqlite3.connect(db_path) as db:
        before = {
            table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("employees", "projects", "memberships", "mailbox_sessions", "tasks")
        }
        real_version = db.execute("PRAGMA user_version").fetchone()[0]
        # Non-vacuity guard 1: the baseline must actually contain the things we claim to protect.
        assert before["mailbox_sessions"] > 0, "基线必须真有钥匙, 否则断言会空过"
        assert before["employees"] > 0, "基线必须真有员工, 否则断言会空过"
        # Non-vacuity guard 2: pin back so opening the store MUST migrate.
        db.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION) - 1}")
        db.commit()

    wake = home / "wake" / "wake-state.json"
    wake_before = _digest(wake) if wake.is_file() else None

    store = WorkbenchStore(home)  # migration and version checks happen here
    snapshot = store.snapshot()

    assert len(snapshot["employees"]) == before["employees"], "升级不得丢员工"
    assert len(snapshot["projects"]) == before["projects"], "升级不得丢项目"
    assert store.orphan_report()["ok"] is True, "迁移后不得产生孤儿引用"

    session_rows = sum(
        len(list_sessions(store, employee["id"], project["id"]))
        for employee in snapshot["employees"]
        for project in snapshot["projects"]
    )
    assert session_rows >= before["mailbox_sessions"], "钥匙必须还在, 升级不得要求重签"

    # Keys must remain *usable*, not merely countable: validate a real token from the copy.
    tokens = []
    for path in sorted((home / "workbench" / "mail-sessions").glob("*.json"))[:8]:
        try:
            value = json.loads(path.read_text(encoding="utf-8")).get("token")
        except (OSError, ValueError):
            continue
        if isinstance(value, str) and len(value) >= 20:
            tokens.append(value)
    assert tokens, "基线必须真带会话文件, 否则无法证明钥匙可用"
    usable = 0
    for token in tokens:
        try:
            with store._transaction() as db:
                validate_session(store, db, token)
            usable += 1
        except WorkbenchError:
            continue
    assert usable > 0, "升级后至少一把原钥匙仍可用, 否则用户就得全部重签"

    with sqlite3.connect(db_path) as db:
        migrated = db.execute("PRAGMA user_version").fetchone()[0]
    assert migrated == SCHEMA_VERSION, f"必须真的从 {real_version} 迁到 {SCHEMA_VERSION}"
    assert migrated != real_version or real_version == SCHEMA_VERSION, "迁移必须被真正触发"

    if wake_before is not None:
        assert _digest(wake) == wake_before, "水位线不得被升级清空"


@pytest.mark.skipif(not REAL_HOME.is_dir(), reason="no real home on this machine")
def test_future_schema_is_refused_loudly(tmp_path):
    """A newer schema must be refused, never silently downgraded."""
    home = tmp_path / "home"
    (home / "workbench").mkdir(parents=True)
    shutil.copy2(REAL_HOME / "workbench" / "state.db", home / "workbench" / "state.db")
    with sqlite3.connect(home / "workbench" / "state.db") as db:
        db.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION) + 1}")
    with pytest.raises(WorkbenchError):
        WorkbenchStore(home).snapshot()
