"""Traffic guards: stop A↔B echo loops and one-way floods (rules S2/S3).

Why this exists (2026-10-04 incident): our worker answered a failed task with an
automated failure receipt; the peer treated that receipt as a new event, replied,
and the exchange kept going — one thread produced **15 "处理失败" letters** plus
four escalation letters from the peer before a human stopped it. A failure
receipt is content: automation must never treat it as a retryable signal.

Two layers, deliberately separate:

* :func:`detect_echo` — pure detection over a thread's messages (no I/O), so it
  is cheap, testable, and can run on any host;
* :func:`freeze_thread` / :func:`thread_frozen` — record the freeze as a
  governance event so the decision is auditable and reversible, and so future
  automation can refuse to auto-send on a frozen thread.

"Progress" gate: a loop that changes task state is real work, not an echo, so a
freeze requires **no observed state change** across the window.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from . import workbench_events

DEFAULT_MAX_ROUNDS = 3
DEFAULT_WINDOW_SECONDS = 3600
DEFAULT_FLOOD_MAX = 10
DEFAULT_FLOOD_WINDOW_SECONDS = 300
FREEZE_EVENT = "thread_frozen"
UNFREEZE_EVENT = "thread_unfrozen"

_SENDER_KEYS = ("sender_id", "sender", "from")
_RECIPIENT_KEYS = ("recipient_id", "recipient", "to")
_TIME_KEYS = ("created_at", "ts", "date", "time")


def _first(message: dict, keys: Sequence[str]) -> Any:
    for key in keys:
        value = message.get(key)
        if value:
            return value
    return None


def direction(message: dict) -> tuple[str, str] | None:
    """Return ``(sender, recipient)`` when both sides are known."""
    sender = _first(message, _SENDER_KEYS)
    recipient = _first(message, _RECIPIENT_KEYS)
    if not sender or not recipient:
        return None
    return str(sender), str(recipient)


def _timestamp(message: dict) -> str:
    return str(_first(message, _TIME_KEYS) or "")


@dataclass(frozen=True)
class EchoFinding:
    """A suspected echo loop inside one thread."""

    turns: int  # alternating turns (reversals + the opener)
    exchanges: int  # how many times the direction reversed
    rounds: float  # full round trips (turns / 2)
    messages: int  # messages considered inside the window
    first_at: str
    last_at: str
    participants: tuple[str, ...]


def count_exchanges(messages: Iterable[dict]) -> int:
    """Count adjacent direction reversals (A→B then B→A counts as one)."""
    directions = [d for d in (direction(m) for m in messages) if d]
    return sum(1 for prev, cur in pairwise(directions) if cur == (prev[1], prev[0]))


def count_turns(messages: Iterable[dict]) -> int:
    """Alternating turns in the thread: a reversal is a reply, plus the opener.

    This is the number a human counts as "几条一来一往": six messages
    (A→B→A→B→A→B) are six turns == three round trips.
    """
    return count_exchanges(messages) + 1 if any(direction(m) for m in messages) else 0


def detect_echo(
    messages: Iterable[dict],
    *,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    window_seconds: float | None = DEFAULT_WINDOW_SECONDS,
    now: str | None = None,
) -> EchoFinding | None:
    """Detect an A↔B echo inside one thread.

    Returns a finding when the thread reversed direction at least
    ``2 * max_rounds`` times (i.e. ``max_rounds`` complete round trips) **and**
    no message reported a different ``state`` (a state change means progress).

    ``window_seconds=None`` disables the time filter (useful for replays).
    """
    rows = [m for m in messages if isinstance(m, dict)]
    rows = [m for m in rows if direction(m)]
    rows.sort(key=_timestamp)
    if window_seconds is not None:
        stamps = [_timestamp(m) for m in rows if _timestamp(m)]
        if stamps:
            anchor = now or stamps[-1]
            keep = {
                s for s in stamps if s and (not anchor or s >= _iso_minus(anchor, window_seconds))
            }
            rows = [m for m in rows if _timestamp(m) in keep]
    if len(rows) < 3:
        return None

    states = {m.get("state") for m in rows if "state" in m}
    if len(states) > 1:
        return None  # state changed -> real progress, not an echo

    exchanges = count_exchanges(rows)
    turns = exchanges + 1  # opener + every reversal
    if turns < 2 * max_rounds:
        return None

    people: list[str] = []
    for m in rows:
        pair = direction(m)
        for name in pair:  # type: ignore[union-attr]
            if name not in people:
                people.append(name)
    return EchoFinding(
        turns=turns,
        exchanges=exchanges,
        rounds=turns / 2,
        messages=len(rows),
        first_at=_timestamp(rows[0]),
        last_at=_timestamp(rows[-1]),
        participants=tuple(people),
    )


def _iso_minus(anchor: str, seconds: float) -> str:
    """Return ``anchor - seconds`` as an ISO string the same shape as inputs.

    Kept dependency-free and tolerant: if the stamp cannot be parsed the filter
    is skipped (a missing window must never hide a loop).
    """
    from datetime import datetime, timedelta, timezone

    text = anchor.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    shifted = parsed.astimezone(timezone.utc) - timedelta(seconds=seconds)
    return shifted.strftime("%Y-%m-%dT%H:%M:%S")


def freeze_thread(
    store,
    thread_id: str,
    *,
    finding: EchoFinding | None = None,
    reason: str = "",
    actor: str = "storm-breaker",
    project_id: str | None = None,
) -> dict:
    """Record a thread freeze as a governance event (auditable + reversible)."""
    payload = {"thread_id": thread_id}
    if finding is not None:
        payload.update(
            {
                "turns": finding.turns,
                "exchanges": finding.exchanges,
                "rounds": finding.rounds,
                "messages": finding.messages,
                "first_at": finding.first_at,
                "last_at": finding.last_at,
                "participants": list(finding.participants),
            }
        )
    if not reason:
        reason = (
            f"回声环：{finding.turns} 轮交替（{finding.rounds:.1f} 个往返）无状态变化"
            if finding
            else ""
        )
    text = reason
    with store._transaction() as db:  # same package: the store owns the ledger
        return store._governance(
            db, FREEZE_EVENT, project_id=project_id, actor=actor, reason=text, payload=payload
        )


def unfreeze_thread(
    store, thread_id: str, *, reason: str = "", actor: str = "human", project_id: str | None = None
) -> dict:
    with store._transaction() as db:  # same package: the store owns the ledger
        return store._governance(
            db,
            UNFREEZE_EVENT,
            project_id=project_id,
            actor=actor,
            reason=reason or f"人工解除冻结：{thread_id}",
            payload={"thread_id": thread_id},
        )


def thread_frozen(store, thread_id: str) -> bool:
    """True when the latest freeze/unfreeze event for ``thread_id`` is a freeze."""
    matches: list[dict] = []
    # 冻结状态必须由**全量**账本推导：默认只回最近 200 条，账本一长就会"忘掉"冻结
    for event in store.governance_events(limit=None):
        if event.get("type") not in (FREEZE_EVENT, UNFREEZE_EVENT):
            continue
        payload = event.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        if (payload or {}).get("thread_id") == thread_id:
            matches.append(event)
    if not matches:
        return False
    # 显式按时序取最新：store 的返回顺序不保证是时间序（2026-10-04 实测踩到）
    latest = workbench_events.latest(matches)  # 同秒靠插入序（uuid 不能当平局键）
    return latest.get("type") == FREEZE_EVENT


@dataclass(frozen=True)
class FloodFinding:
    """One sender hammering one recipient (not an echo: no alternation)."""

    sender: str
    recipient: str
    count: int
    window_seconds: float
    first_at: str
    last_at: str


def detect_flood(
    messages: Iterable[dict],
    *,
    sender: str | None = None,
    recipient: str | None = None,
    max_messages: int = DEFAULT_FLOOD_MAX,
    window_seconds: float = DEFAULT_FLOOD_WINDOW_SECONDS,
) -> FloodFinding | None:
    """Detect one direction sending too many messages inside a window.

    Today's replay (2026-10-04) made the distinction unavoidable: the loudest
    thread was **HS→WB 70 messages, all one direction** — an echo guard can
    never see that (no alternation), so floods need their own control.

    Optionally pin ``sender``/``recipient``; otherwise the busiest direction in
    the given messages is measured.
    """
    rows = [m for m in messages if isinstance(m, dict) and direction(m)]
    rows = [
        m
        for m in rows
        if (sender is None or direction(m)[0] == sender)
        and (recipient is None or direction(m)[1] == recipient)
    ]
    if len(rows) < max_messages:
        return None
    rows.sort(key=_timestamp)
    if window_seconds is not None and rows:
        cutoff = _iso_minus(_timestamp(rows[-1]), window_seconds)
        if cutoff:
            rows = [m for m in rows if _timestamp(m) >= cutoff]
    if len(rows) < max_messages:
        return None
    busiest: dict[tuple[str, str], list[dict]] = {}
    for m in rows:
        busiest.setdefault(direction(m), []).append(m)  # type: ignore[arg-type]
    (who, to), group = max(busiest.items(), key=lambda kv: len(kv[1]))
    if len(group) < max_messages:
        return None
    return FloodFinding(
        sender=who,
        recipient=to,
        count=len(group),
        window_seconds=float(window_seconds or 0),
        first_at=_timestamp(group[0]),
        last_at=_timestamp(group[-1]),
    )


def fold_notice(finding: FloodFinding) -> str:
    """One line a human can act on — never N lines (rule S4: notifications fold)."""
    return (
        f"{finding.sender} → {finding.recipient} 在 {finding.window_seconds:.0f}s 内发了 "
        f"{finding.count} 封（上限 {DEFAULT_FLOOD_MAX}）⇒ 建议合并成一条摘要，并让发送方自查"
    )
