"""Ledger event ordering: one rule, shared by every derived view.

``governance_events()`` returns rows ordered ``created_at DESC, rowid DESC``, but
derived views scan that list themselves to find the **latest** (or earliest) event of
a kind. On a same-second tie the winner used to fall out of a random uuid or of
Python's scan order, so "最新一条胜" was not deterministic — an independent review hit
exactly this on 2026-10-05.

Rule: compare ``(created_at, _seq)`` where ``_seq`` is the row's insertion order
(``rowid``, added to every event by the store).
"""

from __future__ import annotations

from typing import Any


def event_order(event: dict[str, Any]) -> tuple[str, int]:
    """Sort key: timestamp first, insertion order breaks ties."""
    return (str(event.get("created_at") or ""), int(event.get("_seq") or 0))


def latest(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    return max(events, key=event_order) if events else None


def earliest(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    return min(events, key=event_order) if events else None
