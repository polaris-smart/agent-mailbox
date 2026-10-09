"""Four primitives: run · schedule · watch · inspect (roadmap T32).

Absorbed as a method from the competitor read-through (``run/schedule/watch/
inspect`` — "it works while you sleep"). Ours keeps the house rules: **no daemon,
no new table, no new writer**. So:

* ``schedule`` is a governance event (``task_scheduled``); the due set is *derived*
  (latest schedule wins, only queued tasks qualify);
* ``watch`` is a **pull loop with a timeout**, not a listener — the caller blocks,
  the machine stays quiet;
* ``inspect`` is the history we already keep (task events + governance events);
* ``run`` is honest about the product: a **mailbox** task cannot be "run" by the
  platform (the employee must accept it in its own session), so it says so instead
  of pretending.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from . import workbench_events

SCHEDULED_EVENT = "task_scheduled"


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _payload(event: dict) -> dict:
    value = event.get("payload")
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def schedule(
    store, task_id: str, due_at: str, *, actor: str = "human", note: str = ""
) -> dict[str, Any]:
    """Record when a task should be worked on (auditable, latest wins)."""
    when = _parse_time(due_at)
    if when is None:
        raise ValueError("due_at 需要 ISO 时间（如 2026-10-05T09:00:00+08:00）")
    with store._transaction() as db:
        task = store._required(db, "tasks", task_id)
        store._governance(
            db,
            SCHEDULED_EVENT,
            employee_id=task["assignee_id"],
            project_id=task["project_id"],
            task_id=task_id,
            actor=actor,
            reason=f"排期至 {when.isoformat()}",
            payload={"due_at": when.isoformat(), "note": note},
        )
    return {"task_id": task_id, "due_at": when.isoformat(), "note": note}


def schedules(store, task_id: str) -> list[dict[str, Any]]:
    rows = [
        {
            "due_at": _payload(event).get("due_at"),
            "note": _payload(event).get("note") or "",
            "at": event.get("created_at"),
            "_seq": event.get("_seq"),
        }
        for event in store.governance_events(limit=None)
        if event.get("type") == SCHEDULED_EVENT and event.get("task_id") == task_id
    ]
    rows.sort(key=lambda row: (str(row.get("at") or ""), int(row.get("_seq") or 0)))
    return rows


def due_tasks(
    store, project_id: str | None = None, *, now: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Queued tasks whose **latest** schedule is at or before ``now`` (derived)."""
    moment = _parse_time(now) or datetime.now(timezone.utc)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in store.governance_events(limit=None):
        if event.get("type") != SCHEDULED_EVENT or not event.get("task_id"):
            continue
        grouped.setdefault(str(event["task_id"]), []).append(event)
    # 最新排期 = (created_at, 插入序) 最大者；同秒也确定（独立审查指出过）
    latest = {
        task_key: {
            "due_at": _payload(newest).get("due_at"),
            "at": newest.get("created_at"),
            "project_id": newest.get("project_id"),
        }
        for task_key, events in grouped.items()
        if (newest := workbench_events.latest(events)) is not None
    }
    out = []
    with store._transaction(readonly=True) as db:
        if project_id:
            store._required(db, "projects", project_id)
        for task_id, item in latest.items():
            due = _parse_time(item.get("due_at"))
            if due is None or due > moment:
                continue
            row = db.execute(
                "SELECT id, title, status, project_id, assignee_id FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None or row["status"] != "queued":
                continue
            if project_id is not None and row["project_id"] != project_id:
                continue
            out.append({**{k: row[k] for k in row.keys()}, "due_at": item["due_at"]})  # noqa: SIM118
    out.sort(key=lambda row: str(row.get("due_at") or ""))
    return out[:limit]


def watch(
    store,
    project_id: str | None = None,
    *,
    timeout_seconds: float = 30.0,
    poll_seconds: float = 0.5,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Block until something is due or the timeout expires. **Pull, no daemon.**

    Injecting ``clock``/``now`` keeps this testable without real waiting.
    """
    started = clock()
    stamp = now() if now else None
    while True:
        due = due_tasks(store, project_id, now=stamp)
        if due:
            return {"due": due, "timed_out": False, "waited_seconds": round(clock() - started, 3)}
        if clock() - started >= timeout_seconds:
            return {"due": [], "timed_out": True, "waited_seconds": round(clock() - started, 3)}
        time.sleep(min(poll_seconds, max(0.0, timeout_seconds)))


def inspect(store, task_id: str) -> dict[str, Any]:
    """History of one task: row + task events + governance + schedule trail (read-only)."""
    with store._transaction(readonly=True) as db:
        task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task is None:
            raise ValueError(f"找不到任务 {task_id}")
        task_row = {k: task[k] for k in task.keys()}  # noqa: SIM118
        events = [
            {k: row[k] for k in row.keys()}  # noqa: SIM118
            for row in db.execute(
                "SELECT type, message, created_at FROM events WHERE task_id = ? ORDER BY created_at",
                (task_id,),
            ).fetchall()
        ]
        governance = [
            {k: row[k] for k in row.keys()}  # noqa: SIM118
            for row in db.execute(
                "SELECT type, actor, reason, created_at FROM governance_events WHERE task_id = ? ORDER BY created_at",
                (task_id,),
            ).fetchall()
        ]
    return {
        "task": task_row,
        "events": events,
        "governance": governance,
        "schedules": schedules(store, task_id),
        "status": task_row["status"],
    }


def run_now(store, task_id: str, *, actor: str = "human") -> dict[str, Any]:
    """Try to start a task **now**. Honest about mailbox tasks (employee must accept)."""
    with store._transaction() as db:
        task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task is None:
            raise ValueError(f"找不到任务 {task_id}")
        row = {k: task[k] for k in task.keys()}  # noqa: SIM118
    if row["status"] != "queued":
        return {"action": "noop", "reason": f"status_is_{row['status']}"}
    if row["execution_mode"] == "mailbox":
        return {
            "action": "needs_employee_accept",
            "reason": "mailbox_task_runs_in_the_employees_own_session",
            "hint": f"请该员工用自己的会话接受：agent-mailbox mailbox-mcp …（任务 {task_id}）",
        }
    # 先**只读**窥视会被认领的是哪一张：盲目 claim 会顺手启动别的任务，且没有回退路径
    would = store.peek_claim(store.local_node()["id"])
    if would is None:
        return {"action": "not_eligible", "reason": "no_claimable_task_for_this_node"}
    if would["id"] != task_id:
        return {
            "action": "not_eligible",
            "reason": "another_task_would_be_claimed_first",
            "would_claim": would["id"],
            "hint": "认领按排队顺序取第一张；请先处理它，或另开一张（同一位员工同时只能有一张活跃受管卡）",
        }
    claimed = store.claim_task(store.local_node()["id"])
    if claimed is None or claimed["id"] != task_id:
        return {"action": "not_eligible", "reason": "claim_raced_or_refused"}
    return {"action": "claimed", "task_id": task_id}
