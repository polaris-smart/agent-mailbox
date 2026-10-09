"""Observation window: turn the completion criteria into something countable (roadmap §4).

The criteria agreed with HS are **event counts, not a calendar**:

    0 风暴 · 0 丢失 · ≥10 次真验收 · 审计抽样 20/20

So this module records when the window started (one governance event) and evaluates
the four counts from the ledger. "0 丢失" is expressed as the audit criterion: if
every sampled letter can still be reconstructed end-to-end, nothing went missing
silently — which is the failure mode we actually fear.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import workbench_events
from .workbench_wall import audit_chain

STARTED_EVENT = "observation_started"
DEFAULT_SAMPLE = 20
TARGET_ACCEPTANCES = 10


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def start(store, *, at: str | None = None, actor: str = "human") -> dict[str, Any]:
    """Record the window start once; later calls return the original start (idempotent)."""
    moment = _parse_time(at) or datetime.now(timezone.utc)
    existing = window(store)
    if existing["started_at"]:
        return existing
    with store._transaction() as db:
        store._governance(
            db,
            STARTED_EVENT,
            actor=actor,
            reason=f"观察窗开始于 {moment.isoformat()}",
            payload={"started_at": moment.isoformat()},
        )
    return window(store)


def window(store) -> dict[str, Any]:
    """Earliest recorded start (read-only)."""
    starts: list[dict[str, Any]] = []
    for event in store.governance_events(limit=None):
        if event.get("type") != STARTED_EVENT:
            continue
        payload = event.get("payload")
        if isinstance(payload, str):
            import json

            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        starts.append(
            {
                **event,
                "started_at": str(
                    (payload or {}).get("started_at") or event.get("created_at") or ""
                ),
            }
        )
    earliest_start = workbench_events.earliest([row for row in starts if row["started_at"]])
    if earliest_start is None:
        return {"started_at": None, "days": None}
    stamps = [earliest_start["started_at"]]
    started = _parse_time(stamps[0])
    days = (
        round((datetime.now(timezone.utc) - started).total_seconds() / 86400, 2)
        if started
        else None
    )
    return {"started_at": stamps[0], "days": days}


def evaluate(store, *, sample: int = DEFAULT_SAMPLE) -> dict[str, Any]:
    """Count the four criteria from the ledger (read-only)."""
    scope = window(store)
    since = scope["started_at"] or ""
    counters: dict[str, int] = {}
    with store._transaction(readonly=True) as db:
        for name, event_type in (
            ("folds", "message_folded"),
            ("freezes", "thread_frozen"),
            ("escalations_suppressed", "escalation_suppressed"),
            ("escalations", "escalation"),
            ("policies_changed", "policy_updated"),
            ("letters_projected", "letter_projected"),
        ):
            counters[name] = db.execute(
                "SELECT count(*) AS c FROM governance_events WHERE type=? AND created_at >= ?",
                (event_type, since),
            ).fetchone()["c"]
        acceptances = db.execute(
            "SELECT count(*) AS c FROM tasks WHERE status='done' AND updated_at >= ?", (since,)
        ).fetchone()["c"]
        message_ids = [
            row["id"]
            for row in db.execute(
                "SELECT id FROM messages WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
                (since, max(1, int(sample))),
            ).fetchall()
        ]
    reconstructable = 0
    details = []
    for message_id in message_ids:
        try:
            chain = audit_chain(store, message_id)
        except ValueError:
            details.append({"message_id": message_id, "ok": False, "reason": "not_found"})
            continue
        steps = [step["step"] for step in chain.get("steps", [])]
        ok = len(steps) >= 2 and any(step.startswith("terminal:") for step in steps)
        reconstructable += 1 if ok else 0
        details.append({"message_id": message_id, "ok": ok, "steps": len(steps)})
    sampled = len(message_ids)
    storms = counters["folds"] + counters["freezes"] + counters["escalations_suppressed"]
    return {
        "window": scope,
        "criteria": {
            "storms": storms,
            "storms_zero": storms == 0,
            "acceptances": acceptances,
            "acceptances_met": acceptances >= TARGET_ACCEPTANCES,
            "audit_sampled": sampled,
            "audit_reconstructable": reconstructable,
            "audit_full": sampled > 0 and reconstructable == sampled,
        },
        "counters": counters,
        "sample": details,
        "verdict": (
            "未开始（先执行 observe --start）"
            if not scope["started_at"]
            else (
                "达标"
                if storms == 0
                and acceptances >= TARGET_ACCEPTANCES
                and sampled > 0
                and reconstructable == sampled
                else "观察中"
            )
        ),
    }
