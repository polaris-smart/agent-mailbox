"""Synthetic migration contracts; never read a developer's real home or keys.

Lowering a synthetic database's schema version tests the migration branch and
preservation contracts. It does not replace upgrading an original released app.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
from contextlib import closing

import pytest

from agent_mailbox.workbench_mail_sessions import (
    create_session,
    list_sessions,
    revoke_session,
    validate_session,
)
from agent_mailbox.workbench_store import SCHEMA_VERSION, WorkbenchError, WorkbenchStore
from agent_mailbox.workbench_wake import empty_state, record_wake, save_state


def _digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def synthetic_home(tmp_path: pathlib.Path):
    home = tmp_path / "home"
    store = WorkbenchStore(home)
    project = store.create_project("Upgrade fixture", str(tmp_path))
    employee = store.create_employee("Fixture member", "codex", project["id"])
    active = create_session(store, employee["id"], project["id"], "Active fixture")
    revoked = create_session(store, employee["id"], project["id"], "Revoked fixture")
    revoke_session(store, revoked["id"])
    store.send_message(
        project["id"],
        "Fixture task",
        "Synthetic content",
        recipient_id=employee["id"],
        request_work=True,
    )
    state = empty_state()
    record_wake(state, employee["id"], ["fixture-seen-message"], now=1000)
    save_state(home, state)
    return home, active["token"], revoked["token"]


def test_upgrade_migrates_really_and_keeps_data_keys_and_watermarks(synthetic_home):
    """Exercise schema migration with non-empty, isolated synthetic data."""
    home, active_token, revoked_token = synthetic_home
    db_path = home / "workbench" / "state.db"

    with closing(sqlite3.connect(db_path)) as db:
        before = {
            table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("employees", "projects", "memberships", "mailbox_sessions", "tasks")
        }
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

    with store._transaction() as db:
        assert validate_session(store, db, active_token)
        with pytest.raises(WorkbenchError):
            validate_session(store, db, revoked_token)
        after = {
            table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in before
        }
    assert after == before, "migration must preserve every seeded entity count"

    with closing(sqlite3.connect(db_path)) as db:
        migrated = db.execute("PRAGMA user_version").fetchone()[0]
    assert migrated == SCHEMA_VERSION

    assert wake_before is not None
    assert _digest(wake) == wake_before, "水位线不得被升级清空"
    assert json.loads(wake.read_text(encoding="utf-8"))["employees"]


def test_future_schema_is_refused_loudly(synthetic_home):
    """A newer schema must be refused, never silently downgraded."""
    home, _, _ = synthetic_home
    with closing(sqlite3.connect(home / "workbench" / "state.db")) as db:
        db.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION) + 1}")
        db.commit()
    with pytest.raises(WorkbenchError):
        WorkbenchStore(home).snapshot()
