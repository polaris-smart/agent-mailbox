"""Backpressure: fold a flooding sender's messages into one digest (rules S2/S4).

Why this exists (2026-10-04 replay): the loudest pair in one day was **70 messages
in 30 minutes, all one direction** — the recipient's inbox grew by one row per
message and every message produced its own notification. Rate limiting alone
cannot fix that: a *slow drip* (~1/min) stays under every sane threshold, and
tightening thresholds would hurt normal collaboration.

So the control is not a wall but a **funnel**: once one sender exceeds the limit
inside a window, later messages stop becoming rows of their own and are folded
into a single digest (titles preserved, bodies collapsed, one notification). The
recipient still learns *what* happened; the inbox stops growing linearly.

Design notes:
  * titles are preserved, bodies are dropped — the honest trade for a flood;
  * the digest carries its own ``thread_id`` marker so it never counts as a
    normal message (and cannot fold itself);
  * every folded message still has to be recorded as a governance event by the
    caller, so nothing is silently swallowed.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

DEFAULT_FOLD_LIMIT = 10
DEFAULT_FOLD_WINDOW_SECONDS = 300
# 双窗分层：短窗治"快爆"（2.4 封/分），长窗治"慢滴"（~1 封/分，任何 5 分钟窗都低于阈值）。
# 2026-10-04 回放：只有短窗时 219 封→189 行（只省快爆那一对）；加长窗后慢滴也进折叠。
DEFAULT_TIERS: tuple[tuple[int, int], ...] = ((300, 10), (21600, 30))
DIGEST_MARK = "digest:"
DIGEST_TITLE = "【折叠】{sender} → {recipient}（{count} 条）"
DIGEST_HEADER = "【折叠摘要】{sender} → {recipient}：以下 {count} 条消息已合并（只保留标题）"
MAX_FOLDED_TITLES = 50  # bounded: a digest must not itself become unbounded


def fold_limit(value: int | None = None) -> int:
    """Fold limit: explicit > ``AGENT_MAILBOX_FOLD_LIMIT`` > default."""
    if value is not None:
        return max(1, int(value))
    raw = os.environ.get("AGENT_MAILBOX_FOLD_LIMIT")
    if not raw:
        return DEFAULT_FOLD_LIMIT
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_FOLD_LIMIT


def tiers() -> tuple[tuple[int, int], ...]:
    """Return ``((window_seconds, max_messages), ...)``; env ``AGENT_MAILBOX_FOLD_TIERS`` overrides."""
    raw = os.environ.get("AGENT_MAILBOX_FOLD_TIERS")
    if not raw:
        # 单值覆写 AGENT_MAILBOX_FOLD_LIMIT ⇒ 派生双档（短=limit，长=3×limit），
        # 保持"一个旋钮"的直观，同时不丢长窗治慢滴的能力。
        legacy = os.environ.get("AGENT_MAILBOX_FOLD_LIMIT")
        if legacy:
            try:
                base = max(1, int(legacy))
            except ValueError:
                return DEFAULT_TIERS
            return tiers_for(base, base * 3)
        return DEFAULT_TIERS
    parsed = []
    for chunk in raw.split(","):
        window, _, limit = chunk.partition(":")
        try:
            parsed.append((int(window), int(limit)))
        except ValueError:
            return DEFAULT_TIERS
    return tuple(parsed) or DEFAULT_TIERS


def tiers_for(short: int, long: int) -> tuple[tuple[int, int], ...]:
    """Convenience for callers that want two explicit tiers (used by tests)."""
    return ((DEFAULT_FOLD_WINDOW_SECONDS, short), (21600, long))


def should_fold_tiered(counts: list[int], limit: int | None = None) -> bool:
    """True when any tier is exceeded: ``counts[i]`` is the count inside ``tiers()[i]``.

    Tier 0 is the burst window, tier 1 the slow-drip window. A slow drip that
    never exceeds the burst limit still exceeds the long window (今天 HS→dsh
    66 封/3h 就是这种)。
    """
    spec = tiers()
    if limit is not None:
        spec = tiers_for(limit, limit * 3)
    return any(count + 1 > cap for count, (_window, cap) in zip(counts, spec))


def fold_window(value: float | None = None) -> float:
    if value is not None:
        return max(1.0, float(value))
    raw = os.environ.get("AGENT_MAILBOX_FOLD_WINDOW")
    if not raw:
        return float(DEFAULT_FOLD_WINDOW_SECONDS)
    try:
        return max(1.0, float(raw))
    except ValueError:
        return float(DEFAULT_FOLD_WINDOW_SECONDS)


def should_fold(recent: int, limit: int | None = None) -> bool:
    """True when one more message would exceed the limit inside the window."""
    return recent + 1 > fold_limit(limit)


def digest_thread_id(sender_id: str | None, recipient_id: str | None) -> str:
    return f"{DIGEST_MARK}{sender_id or 'human'}->{recipient_id or 'project'}"


def is_digest(thread_id: str | None) -> bool:
    return bool(thread_id) and str(thread_id).startswith(DIGEST_MARK)


def digest_header(sender_id: str | None, recipient_id: str | None, count: int) -> str:
    return DIGEST_HEADER.format(
        sender=sender_id or "人工", recipient=recipient_id or "项目", count=count
    )


def digest_title(sender_id: str | None, recipient_id: str | None, count: int) -> str:
    return DIGEST_TITLE.format(
        sender=sender_id or "人工", recipient=recipient_id or "项目", count=count
    )


def count_folded(body: str) -> int:
    """Folded item count: the number of ``- `` list lines in a digest body."""
    return sum(1 for line in str(body).splitlines() if line.startswith("- "))


def append_folded(body: str, title: str, sender_id: str | None, recipient_id: str | None) -> str:
    """Append one folded title and refresh the header count (bounded, no parsing games)."""
    lines = str(body).splitlines()
    items = [line for line in lines if line.startswith("- ")]
    items.append(f"- {title}")
    items = items[:MAX_FOLDED_TITLES]
    return "\n".join([digest_header(sender_id, recipient_id, len(items)), *items])


def new_digest_body(title: str, sender_id: str | None, recipient_id: str | None) -> str:
    return append_folded("", title, sender_id, recipient_id)


def since(now_iso: str, window: float) -> str:
    """ISO cutoff for a window ending at ``now_iso``, shaped like the store's stamps.

    ``created_at`` is written by the store as ``datetime.isoformat()`` with a
    UTC offset, so a string comparison against this cutoff is valid. The filter
    is a coarse guard only: exact boundaries are not security relevant.
    """
    text = str(now_iso).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed - timedelta(seconds=window)).isoformat()
