"""View marks: a watermark that feeds backlog-based folding (roadmap: 水位线第一步).

Scope is deliberately narrow — agreed with HS on 2026-10-04:

* **feeds folding only.** Nothing here is exposed as an API concept: there is no
  read receipt, no "read" state on a message, and no effect on any task or
  message status. ``查看 ≠ 接单`` stays literally true.
* **monotonic non-decreasing.** A mark only ever moves forward (HS's addition),
  so clock skew or out-of-order views cannot shrink the folding window.
* **kill-switch.** ``AGENT_MAILBOX_BACKLOG_FOLD=0`` disables the backlog tier
  entirely; the marks themselves then have no effect anywhere.
* **no schema migration.** The table is created lazily and idempotently, like
  the search index, so existing databases gain it without a version bump.

Why it exists at all: the two-tier rate folding could not see a **slow drip**
(measured: 67 letters at ~1/min never exceed any 5-minute window). Backlog —
"how many did this peer send that I have not looked at" — is the only signal
that catches that shape without tightening rate limits and hurting normal work.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any

MARKS_TABLE = "mail_view_marks"
DEFAULT_BACKLOG_LIMIT = 20
ENV_ENABLE = "AGENT_MAILBOX_BACKLOG_FOLD"
ENV_LIMIT = "AGENT_MAILBOX_BACKLOG_LIMIT"

_DDL = (
    f"""CREATE TABLE IF NOT EXISTS {MARKS_TABLE} (
  employee_id TEXT NOT NULL,
  counterpart_id TEXT NOT NULL,
  last_viewed_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (employee_id, counterpart_id)
)""",
)


def ensure_marks(db: sqlite3.Connection) -> None:
    """Create the marks table if missing (idempotent, no writes beyond DDL)."""
    for statement in _DDL:
        db.execute(statement)


def marks_present(db: sqlite3.Connection) -> bool:
    """Is the marks table already there? **Read-only** — never creates it.

    读路径不建表（与检索索引同一条纪律）：没建过就当"没有标记"，
    建表交给写路径（record_views_in）。
    """
    return (
        db.execute(
            "SELECT count(*) AS c FROM sqlite_master WHERE type='table' AND name=?", (MARKS_TABLE,)
        ).fetchone()["c"]
        > 0
    )


def backlog_folding_enabled(value: bool | None = None) -> bool:
    """Kill-switch: ``AGENT_MAILBOX_BACKLOG_FOLD=0`` turns the backlog tier off."""
    if value is not None:
        return bool(value)
    raw = os.environ.get(ENV_ENABLE)
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def backlog_limit(value: int | None = None) -> int:
    if value is not None:
        return max(1, int(value))
    raw = os.environ.get(ENV_LIMIT)
    if not raw:
        return DEFAULT_BACKLOG_LIMIT
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_BACKLOG_LIMIT


def record_views_in(db: sqlite3.Connection, employee_id: str, pairs: list[tuple[str, str]]) -> int:
    """Upsert several marks on an existing connection (monotonic guard applies).

    Callers that already hold a connection/transaction use this instead of
    :func:`record_view` so we never open a second connection while writing.
    """
    ensure_marks(db)
    changed = 0
    for counterpart_id, viewed_at in pairs:
        if not counterpart_id or not viewed_at or counterpart_id == employee_id:
            continue
        row = db.execute(
            f"SELECT last_viewed_at FROM {MARKS_TABLE} WHERE employee_id=? AND counterpart_id=?",
            (employee_id, counterpart_id),
        ).fetchone()
        current = row["last_viewed_at"] if row else None
        if current is not None and str(viewed_at) <= str(current):
            continue
        db.execute(
            f"INSERT INTO {MARKS_TABLE}(employee_id, counterpart_id, last_viewed_at, updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(employee_id, counterpart_id) "
            "DO UPDATE SET last_viewed_at=excluded.last_viewed_at, updated_at=excluded.updated_at",
            (employee_id, counterpart_id, viewed_at, viewed_at),
        )
        changed += 1
    return changed


def backlog_count_in(db: sqlite3.Connection, employee_id: str, counterpart_id: str) -> int:
    """Backlog count on an existing connection (no extra connection while writing)."""
    ensure_marks(db)
    row = db.execute(
        f"SELECT last_viewed_at FROM {MARKS_TABLE} WHERE employee_id=? AND counterpart_id=?",
        (employee_id, counterpart_id),
    ).fetchone()
    mark = row["last_viewed_at"] if row else None
    if mark is None:
        return db.execute(
            "SELECT count(*) AS c FROM messages WHERE recipient_id=? AND sender_id=?",
            (employee_id, counterpart_id),
        ).fetchone()["c"]
    return db.execute(
        "SELECT count(*) AS c FROM messages WHERE recipient_id=? AND sender_id=? AND created_at>?",
        (employee_id, counterpart_id, mark),
    ).fetchone()["c"]


def record_view(
    store, employee_id: str, counterpart_id: str, viewed_at: str
) -> dict[str, Any] | None:
    """Advance the watermark for one (viewer, counterpart) pair.

    Returns the stored mark, or None when the incoming timestamp is not newer
    (monotonic non-decreasing: never move backwards).
    """
    if not employee_id or not counterpart_id or not viewed_at:
        return None
    with store._transaction() as db:
        ensure_marks(db)
        row = db.execute(
            f"SELECT last_viewed_at FROM {MARKS_TABLE} WHERE employee_id=? AND counterpart_id=?",
            (employee_id, counterpart_id),
        ).fetchone()
        current = row["last_viewed_at"] if row else None
        if current is not None and str(viewed_at) <= str(current):
            return {
                "employee_id": employee_id,
                "counterpart_id": counterpart_id,
                "last_viewed_at": current,
            }
        db.execute(
            f"INSERT INTO {MARKS_TABLE}(employee_id, counterpart_id, last_viewed_at, updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(employee_id, counterpart_id) "
            "DO UPDATE SET last_viewed_at=excluded.last_viewed_at, updated_at=excluded.updated_at",
            (employee_id, counterpart_id, viewed_at, viewed_at),
        )
        return {
            "employee_id": employee_id,
            "counterpart_id": counterpart_id,
            "last_viewed_at": viewed_at,
        }


def record_views_for(store, employee_id: str, pairs: list[tuple[str, str]]) -> int:
    """Convenience for callers holding several ``(counterpart_id, created_at)`` pairs."""
    changed = 0
    for counterpart_id, viewed_at in pairs:
        if record_view(store, employee_id, counterpart_id, viewed_at):
            changed += 1
    return changed


def last_viewed_at(store, employee_id: str, counterpart_id: str) -> str | None:
    with store._transaction(readonly=True) as db:
        if not marks_present(db):
            return None
        row = db.execute(
            f"SELECT last_viewed_at FROM {MARKS_TABLE} WHERE employee_id=? AND counterpart_id=?",
            (employee_id, counterpart_id),
        ).fetchone()
    return row["last_viewed_at"] if row else None


def backlog_count(store, employee_id: str, counterpart_id: str) -> int:
    """How many messages this counterpart sent that the viewer has not looked at."""
    mark = last_viewed_at(store, employee_id, counterpart_id)
    with store._transaction(readonly=True) as db:
        if mark is None or not marks_present(db):
            return db.execute(
                "SELECT count(*) AS c FROM messages WHERE recipient_id=? AND sender_id=?",
                (employee_id, counterpart_id),
            ).fetchone()["c"]
        return db.execute(
            "SELECT count(*) AS c FROM messages WHERE recipient_id=? AND sender_id=? AND created_at>?",
            (employee_id, counterpart_id, mark),
        ).fetchone()["c"]


def backlog_trips(store, employee_id: str, counterpart_id: str, limit: int | None = None) -> bool:
    """True when the backlog tier should fold: enabled **and** backlog at limit."""
    if not backlog_folding_enabled():
        return False
    return backlog_count(store, employee_id, counterpart_id) >= backlog_limit(limit)
