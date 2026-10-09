"""Message search: FTS5 index over project messages (roadmap T5).

Why: 3960 letters were only reachable by scanning directories — today's storm
debugging was grep plus directory walks because nothing could be searched. The
index is **derived data**: it never becomes a second source of truth.

Design decisions (each one measured, not assumed):

* **trigram tokenizer** — the default ``unicode61`` tokenizer does not segment
  Chinese at all, so a Chinese query matches nothing. ``trigram`` works for CJK
  *but needs at least three characters* (measured: ``风暴`` (2 chars) matches 0,
  ``收件箱`` (3 chars) matches 1). Since most high-frequency Chinese words are two
  characters, :func:`search` falls back to a ``LIKE`` scan for queries shorter
  than three characters — honest about its cost, and fine at this corpus size.
* **triggers keep it in sync** — inserts/updates/deletes on ``messages`` update
  the index inside the same transaction, so the index can never silently drift.
* **bodies are not returned by default** — search follows the token-burn aware
  rule from the competitor read-through: return a snippet, not the whole letter.

The index is created lazily and idempotently, so existing databases gain search
without a schema-version migration (``轻量``/no migration burden).
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

FTS_TABLE = "fts_messages"
MIN_TRIGRAM_CHARS = 3
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

_DDL = (
    (
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5("
        "message_id UNINDEXED, title, body, tokenize='trigram')"
    ),
    f"""CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO {FTS_TABLE}(message_id, title, body) VALUES (new.id, new.title, new.body);
END""",
    f"""CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  DELETE FROM {FTS_TABLE} WHERE message_id = old.id;
END""",
    f"""CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
  DELETE FROM {FTS_TABLE} WHERE message_id = old.id;
  INSERT INTO {FTS_TABLE}(message_id, title, body) VALUES (new.id, new.title, new.body);
END""",
)

_BACKFILL = (
    f"INSERT INTO {FTS_TABLE}(message_id, title, body) "
    "SELECT m.id, m.title, m.body FROM messages m "
    f"WHERE NOT EXISTS (SELECT 1 FROM {FTS_TABLE} f WHERE f.message_id = m.id)"
)

# FTS5 treats many characters as syntax; a phrase query in double quotes is the
# safe form (internal quotes are doubled, per FTS5 string rules).
_UNSAFE = re.compile(r"[^\w\s\u4e00-\u9fff\-.,:/@]+")


def ensure_index(db: sqlite3.Connection) -> None:
    """Create the FTS table/triggers if missing and backfill any gap (idempotent)."""
    for statement in _DDL:
        db.execute(statement)
    db.execute(_BACKFILL)


def _index_present(db: sqlite3.Connection) -> bool:
    return (
        db.execute(
            "SELECT count(*) AS c FROM sqlite_master WHERE type='table' AND name=?", (FTS_TABLE,)
        ).fetchone()["c"]
        > 0
    )


def build_index(store) -> dict[str, Any]:
    """**显式**建索引（唯一会写库的入口）：建表 + 触发器 + 回填。

    检索路径刻意不自动建 —— 一次"只读查询"顺手建 6 张表 + 3 个触发器，
    会让"零写"承诺失真（2026-10-05 独立审查指出）。
    """
    with store._transaction() as db:
        ensure_index(db)
    return readonly_index_status(store)


def normalize_query(query: str) -> str:
    """Turn user input into a safe FTS5 phrase query (whitespace = AND)."""
    cleaned = _UNSAFE.sub(" ", str(query or "")).strip()
    parts = [p for p in cleaned.split() if p]
    if not parts:
        return ""
    return " AND ".join('"' + p.replace('"', '""') + '"' for p in parts)


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int):
        return DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, limit))


def _row_to_result(row: sqlite3.Row, *, include_bodies: bool) -> dict[str, Any]:
    # sqlite3.Row 的 `in` 判的是"值"不是"键"，所以这里必须显式 .keys()
    data = {key: row[key] for key in row.keys()}  # noqa: SIM118
    if not include_bodies:
        data.pop("body", None)
    return data


def search(
    store,
    q: str,
    *,
    project_id: str | None = None,
    employee_id: str | None = None,
    folder: str | None = None,
    limit: int | None = None,
    include_bodies: bool = False,
) -> dict[str, Any]:
    """Search messages; returns ``{"query", "mode", "count", "results"}``.

    ``mode`` is ``"fts"`` or ``"like"`` — the short-query fallback is reported
    rather than hidden, so callers can see which path answered.
    """
    text = str(q or "").strip()
    if not text:
        raise ValueError("检索词不能为空。")
    size = _clamp_limit(limit)
    use_like = len(text) < MIN_TRIGRAM_CHARS

    with store._transaction(readonly=True) as db:
        if project_id:
            store._required(db, "projects", project_id)
        indexed = _index_present(db)  # 只读探测：检索路径绝不建表
        where = ["1=1"]
        params: list[Any] = []
        if project_id is not None:
            where.append("m.project_id = ?")
            params.append(project_id)
        if employee_id is not None:
            column = {"inbox": "m.recipient_id", "sent": "m.sender_id"}.get(folder or "inbox", None)
            if column is None:
                where.append("(m.recipient_id = ? OR m.sender_id = ? OR m.recipient_id IS NULL)")
                params.extend([employee_id, employee_id])
            else:
                where.append(f"({column} = ? OR ({column} IS NULL AND m.project_id IS NOT NULL))")
                params.append(employee_id)
        clause = " AND ".join(where)

        if use_like or not indexed:
            # LIKE 里 %/_ 是通配符：不转义则查 "%" 会命中全部（独立复查指出）
            escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            sql = (
                f"SELECT m.id AS message_id, m.title, m.body, m.created_at, m.thread_id, m.task_id, "
                f"m.sender_id, m.recipient_id, 0.0 AS score "
                f"FROM messages m WHERE {clause} AND (m.title LIKE ? OR m.body LIKE ?) "
                f"ORDER BY m.created_at DESC LIMIT ?"
            )
            rows = db.execute(sql, (*params, like, like, size)).fetchall()
            mode = "like" if use_like else "like_no_index"
        else:
            match = normalize_query(text)
            sql = (
                f"SELECT m.id AS message_id, m.title, m.body, m.created_at, m.thread_id, m.task_id, "
                f"m.sender_id, m.recipient_id, bm25({FTS_TABLE}) AS score, "
                f"snippet({FTS_TABLE}, 1, '«', '»', '…', 12) AS snippet "
                f"FROM {FTS_TABLE} f JOIN messages m ON m.id = f.message_id "
                f"WHERE {clause} AND {FTS_TABLE} MATCH ? "
                f"ORDER BY score, m.created_at DESC LIMIT ?"
            )
            rows = db.execute(sql, (*params, match, size)).fetchall()
            mode = "fts"

        results = [_row_to_result(row, include_bodies=include_bodies) for row in rows]
    answer: dict[str, Any] = {
        "query": text,
        "mode": mode,
        "count": len(results),
        "results": results,
    }
    if mode == "like_no_index":
        answer["hint"] = (
            "尚未建立检索索引（检索只读、不建表）；要更快请先执行：agent-mailbox search --index"
        )
    return answer


def index_stats(store) -> dict[str, Any]:
    """Indexed vs stored — **read-only** (never creates the index)."""
    with store._transaction(readonly=True) as db:
        present = _index_present(db)
        indexed = (
            db.execute(f"SELECT count(*) AS c FROM {FTS_TABLE}").fetchone()["c"] if present else 0
        )
        total = db.execute("SELECT count(*) AS c FROM messages").fetchone()["c"]
    return {
        "indexed": indexed,
        "messages": total,
        "index_built": present,
        "in_sync": bool(present) and indexed == total,
    }


def readonly_index_status(store) -> dict[str, Any]:
    """Index status **without writing** — the wall must never mutate anything (rule: 不引入新写者).

    Unlike :func:`index_stats` this never creates the FTS table; a missing index
    is reported as ``indexed: 0, present: False`` instead of being built.
    """
    with store._transaction(readonly=True) as db:
        present = (
            db.execute(
                "SELECT count(*) AS c FROM sqlite_master WHERE type='table' AND name=?",
                (FTS_TABLE,),
            ).fetchone()["c"]
            > 0
        )
        indexed = (
            db.execute(f"SELECT count(*) AS c FROM {FTS_TABLE}").fetchone()["c"] if present else 0
        )
        total = db.execute("SELECT count(*) AS c FROM messages").fetchone()["c"]
    return {
        "present": bool(present),
        "indexed": indexed,
        "messages": total,
        "in_sync": present and indexed == total,
    }
