"""Task ledger: a derived view for troubleshooting and trust (roadmap T11).

Agreed scope with HS (2026-10-04): **a view, not a billing subsystem.**
Everything is derived from existing tables — ``tasks`` / ``events`` /
``governance_events`` / ``messages`` / ``task_deliveries`` — and this module
**never writes**: no new table, no counters, no background job. The reason is our
own incident review: every "helpful" subsystem we added became a new place where
a storm could start (all four of 2026-10-04's storm sources were such features).

Why it exists: on 2026-10-04 three separate "upstream unhealthy" incidents —
dsh's 353 failed runs, WB's timeouts, codex exiting non-zero — had **no unified
view**. Counting per task and rolling up per employee is what turns that into one
screen: who is failing, since when, and with what error.

Capability fields (``execution_supported`` / ``execution_verified``) are read
through the store's own computation so the ledger can never disagree with the
product about what an employee can actually do.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import workbench_policy
from .workbench_proof import PROOF_EVENT, VERIFY_EVENT

FINISHED = ("review", "done", "failed", "cancelled", "interrupted")
RUNNING = ("starting", "running", "waiting_approval")
FAILED = ("failed", "cancelled", "interrupted")


def _rows(db, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [{k: row[k] for k in row.keys()} for row in db.execute(sql, params)]  # noqa: SIM118


def _seconds_between(start: Any, end: Any) -> float | None:
    def parse(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    first, last = parse(start), parse(end)
    if first is None or last is None:
        return None
    return round((last - first).total_seconds(), 3)


def _error_of(raw: Any) -> tuple[str | None, str | None]:
    if not raw:
        return None, None
    import json

    if isinstance(raw, dict):
        return raw.get("code"), raw.get("message")
    try:
        parsed = json.loads(str(raw))
    except ValueError:
        return None, str(raw)[:200]
    if isinstance(parsed, dict):
        return parsed.get("code"), parsed.get("message")
    return None, str(parsed)[:200]


def _payload_of(value: Any) -> dict[str, Any]:
    import json

    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def ledger(
    store, project_id: str | None = None, limit: int = 50, include_idle: bool = False
) -> dict[str, Any]:
    if project_id:
        store.ensure_project(project_id)
    """Per-task rows plus a per-employee rollup. Read-only, always.

    ``include_idle=False`` (default) hides employees with no activity: a view that
    lists 21 idle employees next to the one that is failing is the same as no view
    (rule: 聚合去噪).
    """
    size = max(1, min(500, int(limit)))
    scope_t = " AND t.project_id = ?" if project_id else ""
    scope_g = " AND (project_id = ? OR project_id IS NULL)" if project_id else ""
    params = (project_id,) if project_id else ()
    with store._transaction(readonly=True) as db:
        tasks = _rows(
            db,
            "SELECT t.* , e.name AS assignee_name, e.kind AS assignee_kind, "
            "e.connection_type AS assignee_connection "
            "FROM tasks t LEFT JOIN employees e ON e.id = t.assignee_id "
            f"WHERE 1=1{scope_t} ORDER BY t.updated_at DESC LIMIT ?",
            (*params, size),
        )
        event_counts = {
            row["task_id"]: (row["c"], row["last_at"])
            for row in _rows(
                db,
                "SELECT task_id, count(*) AS c, max(created_at) AS last_at FROM events GROUP BY task_id",
            )
        }
        event_types: dict[str, dict[str, int]] = {}
        for row in _rows(
            db, "SELECT task_id, type, count(*) AS c FROM events GROUP BY task_id, type"
        ):
            event_types.setdefault(row["task_id"], {})[row["type"]] = row["c"]
        message_counts = {
            row["task_id"]: row["c"]
            for row in _rows(
                db,
                "SELECT task_id, count(*) AS c FROM messages WHERE task_id IS NOT NULL GROUP BY task_id",
            )
        }
        governance = _rows(
            db,
            "SELECT task_id, type, payload, created_at FROM governance_events "
            f"WHERE task_id IS NOT NULL{scope_g} ORDER BY created_at",
            params,
        )
        # 能力字段复用 store 自己的计算（同一事务内），保证账本与产品口径一致
        employees = [store._employee(db, row) for row in db.execute("SELECT * FROM employees")]
        # 同行校验也必须在**本事务内**算（连接出了 with 就关了）
        peer_verified = {
            task["id"]: workbench_policy.peer_verified_in(db, task["id"], task.get("assignee_id"))
            for task in tasks
        }

    proofs: dict[str, dict[str, Any]] = {}
    for event in governance:
        task_id = event.get("task_id")
        if not task_id:
            continue
        if event["type"] == PROOF_EVENT:
            proofs.setdefault(task_id, {})["has_proof"] = True
            proofs[task_id]["proof_at"] = event.get("created_at")
        elif event["type"] == VERIFY_EVENT:
            payload = _payload_of(event.get("payload"))
            proofs.setdefault(task_id, {})["verdict"] = payload.get("verdict")
            proofs[task_id]["verified_at"] = event.get("created_at")

    rows = []
    for task in tasks:
        code, message = _error_of(task.get("error"))
        count, last_event = event_counts.get(task["id"], (0, None))
        rows.append(
            {
                "task_id": task["id"],
                "title": task["title"],
                "status": task["status"],
                "mode": task["execution_mode"],
                "assignee": task.get("assignee_name") or task["assignee_id"],
                "assignee_kind": task.get("assignee_kind"),
                "created_at": task["created_at"],
                "updated_at": task["updated_at"],
                "duration_seconds": _seconds_between(task["created_at"], task["updated_at"]),
                "events": count,
                "event_types": event_types.get(task["id"], {}),
                "last_event_at": last_event,
                "messages": message_counts.get(task["id"], 0),
                "has_proof": proofs.get(task["id"], {}).get("has_proof", False),
                "proof_verdict": proofs.get(task["id"], {}).get("verdict"),
                # 把「系统校验过」与「同行校验过」分开：门禁要的是后者，
                # 账本若只显示 verified 会让人误以为两人规则已满足（第三轮复查指出）
                "peer_verified": peer_verified.get(task["id"], False),
                "error_code": code,
                "error_message": (message or None) and str(message)[:200],
            }
        )

    upstream = []
    for row in employees:
        mine = [t for t in tasks if t["assignee_id"] == row["id"]]
        finished = [t for t in mine if t["status"] in FINISHED]
        failed = [t for t in mine if t["status"] in FAILED]
        last_failure = None
        for task in sorted(failed, key=lambda t: str(t["updated_at"]), reverse=True):
            code, message = _error_of(task.get("error"))
            last_failure = {
                "task_id": task["id"],
                "at": task["updated_at"],
                "code": code,
                "message": (message or None) and str(message)[:200],
            }
            break
        upstream.append(
            {
                "employee_id": row["id"],
                "name": row["name"],
                "kind": row["kind"],
                "connection_type": row["connection_type"],
                "lifecycle": row["lifecycle"],
                "execution_supported": bool(row.get("execution_supported")),
                "execution_verified": bool(row.get("execution_verified")),
                "tasks_total": len(mine),
                "running": len([t for t in mine if t["status"] in RUNNING]),
                "waiting_review": len([t for t in mine if t["status"] == "review"]),
                "failed": len(failed),
                "finished": len(finished),
                "success_rate": (
                    round((len(finished) - len(failed)) / len(finished), 3) if finished else None
                ),
                "last_failure": last_failure,
                "last_activity_at": max([str(t["updated_at"]) for t in mine], default=None),
            }
        )
    if not include_idle:
        upstream = [e for e in upstream if (e["tasks_total"] or 0) > 0]
    upstream.sort(key=lambda e: (-(e["failed"] or 0), -(e["tasks_total"] or 0), e["name"] or ""))
    return {"scope": {"project_id": project_id}, "tasks": rows, "upstream": upstream}
