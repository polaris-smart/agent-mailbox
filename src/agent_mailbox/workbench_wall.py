"""Team wall and audit chain: the observability layer (roadmap U7 + 第四成败手).

Two questions this answers, both measured from today's storm:

1. **"Team wall"** — during 353 letters of storm nobody could see the whole
   board: the human had to grep logs and walk directories. The wall is a
   **pull-only** aggregate of existing state: what is running, who is waited on,
   what is stuck, and the health lines (folded messages, frozen threads,
   search index sync).
2. **"Audit chain"** — given any message, reconstruct its whole life:
   sent → received → claimed → receipt → folded/frozen events → terminal state.
   The success criterion agreed with HS: sample 20 letters and every one must be
   reconstructable, 20/20.

Hard constraints (agreed with HS, 2026-10-04):
  * **pull-only** — no push, no notifications, no background work;
  * **no new writer** — nothing here inserts/updates/deletes; data comes from
    ``tasks`` / ``messages`` / ``events`` / ``governance_events`` only, so the
    wall cannot itself become a new storm source;
  * **denoised aggregation** — counts and states, never a message firehose.
"""

from __future__ import annotations

import json
from typing import Any

from . import workbench_policy, workbench_search
from .workbench_proof import PROOF_EVENT, VERIFY_EVENT

# 「在跑」= 真的在执行；等授权属于"等人"（第 10 轮 [中] ④-5 指出前端/后端口径不一致）
ACTIVE_STATUSES = ("starting", "running")
WAITING_STATUSES = ("waiting_approval", "review")
STUCK_STATUSES = ("failed", "interrupted", "cancelled")
FOLD_EVENT = "message_folded"
FREEZE_EVENT = "thread_frozen"
UNFREEZE_EVENT = "thread_unfrozen"


def _rows(db, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [{k: row[k] for k in row.keys()} for row in db.execute(sql, params)]  # noqa: SIM118


def _parse_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        import json

        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def wall(store, project_id: str | None = None) -> dict[str, Any]:
    """Snapshot of 「what is running / who is waited on / what is stuck」 (read-only)."""
    if project_id:
        store.ensure_project(project_id)
    scope_t = " AND t.project_id = ?" if project_id else ""  # 查询带别名 t
    scope_p = (
        " AND t.project_id = ?" if project_id else ""
    )  # permissions 经 join 到 tasks（该表无 project_id 列）
    scope_msg = (
        " AND (project_id = ? OR project_id IS NULL)" if project_id else ""
    )  # 无归属事件=全局
    scope_g = " AND (project_id = ? OR project_id IS NULL)" if project_id else ""  # 治理事件同上
    params = (project_id,) if project_id else ()
    with store._transaction(readonly=True) as db:
        running = _rows(
            db,
            "SELECT t.id, t.title, t.status, t.assignee_id, t.updated_at, e.name AS assignee "
            "FROM tasks t LEFT JOIN employees e ON e.id = t.assignee_id "
            f"WHERE t.status IN ({','.join('?' * len(ACTIVE_STATUSES))}){scope_t} "
            "ORDER BY t.updated_at",
            (*ACTIVE_STATUSES, *params),
        )
        waiting_on_human = _rows(
            db,
            "SELECT t.id, t.title, t.status, t.assignee_id, e.name AS assignee, t.updated_at "
            "FROM tasks t LEFT JOIN employees e ON e.id = t.assignee_id "
            f"WHERE t.status IN ({','.join('?' * len(WAITING_STATUSES))}){scope_t} "
            "ORDER BY t.updated_at",
            (*WAITING_STATUSES, *params),
        )
        pending_approvals = _rows(
            db,
            "SELECT p.task_id, p.status, p.created_at FROM permissions p "
            "JOIN tasks t ON t.id = p.task_id "
            f"WHERE p.status = 'pending'{scope_p}",
            params,
        )
        stuck = _rows(
            db,
            "SELECT t.id, t.title, t.status, t.updated_at, t.error FROM tasks t "
            f"WHERE t.status IN ({','.join('?' * len(STUCK_STATUSES))}){scope_t} "
            "ORDER BY t.updated_at DESC LIMIT 20",
            (*STUCK_STATUSES, *params),
        )
        # 一屏三问要分清「等交付人交证明」与「等同行复验」——两件不同的事，
        # 所以三类行（等验收/在跑/卡住）都带证明三态 + 同行校验状态。
        proof_state: dict[str, dict[str, Any]] = {}
        for event in db.execute(
            "SELECT task_id, type, payload FROM governance_events WHERE task_id IS NOT NULL "
            "AND type IN (?, ?) ORDER BY created_at, rowid",
            (PROOF_EVENT, VERIFY_EVENT),
        ).fetchall():
            state = proof_state.setdefault(
                event["task_id"], {"has_proof": False, "proof_verdict": None}
            )
            if event["type"] == PROOF_EVENT:
                state["has_proof"] = True
            else:
                payload = event["payload"]
                if isinstance(payload, str) and payload.strip():
                    try:
                        payload = json.loads(payload)
                    except ValueError:
                        payload = {}
                state["proof_verdict"] = (payload or {}).get("verdict")
        for rows in (waiting_on_human, running, stuck):
            for row in rows:
                state = proof_state.get(row["id"]) or {"has_proof": False, "proof_verdict": None}
                row["has_proof"] = state["has_proof"]
                row["proof_verdict"] = state["proof_verdict"]
                row["peer_verified"] = workbench_policy.peer_verified_in(
                    db, row["id"], row.get("assignee_id")
                )
        folded = db.execute(
            f"SELECT count(*) AS c FROM governance_events WHERE type = ?{scope_g}",
            (FOLD_EVENT, *params),
        ).fetchone()["c"]
        freeze_events = _rows(
            db,
            "SELECT type, payload, reason, created_at FROM governance_events "
            f"WHERE type IN (?, ?){scope_g} ORDER BY created_at",
            (FREEZE_EVENT, UNFREEZE_EVENT, *params),
        )
        event_types = _rows(
            db,
            "SELECT type, count(*) AS c, max(created_at) AS last_at FROM events "
            + (
                "WHERE task_id IN (SELECT id FROM tasks WHERE project_id = ?) "
                if project_id
                else ""
            )
            + "GROUP BY type ORDER BY c DESC",
            params,
        )
        counts = {
            "tasks": db.execute(
                f"SELECT count(*) AS c FROM tasks WHERE 1=1{scope_msg}", params
            ).fetchone()["c"],
            "messages": db.execute(
                f"SELECT count(*) AS c FROM messages WHERE 1=1{scope_msg}", params
            ).fetchone()["c"],
            "governance_events": db.execute(
                f"SELECT count(*) AS c FROM governance_events WHERE 1=1{scope_msg}", params
            ).fetchone()["c"],
        }

    frozen: dict[str, dict[str, Any]] = {}
    for row in freeze_events:
        payload = _parse_payload(row.get("payload"))
        thread = payload.get("thread_id") or ""
        if not thread:
            continue
        if row["type"] == FREEZE_EVENT:
            frozen[thread] = {
                "thread_id": thread,
                "reason": row.get("reason") or "",
                "frozen_at": row.get("created_at"),
                "unfrozen_at": None,
            }
        elif thread in frozen:
            frozen[thread]["unfrozen_at"] = row.get("created_at")

    return {
        "scope": {"project_id": project_id},
        "running": running,
        "waiting_on_human": waiting_on_human,
        "pending_approvals": pending_approvals,
        "stuck": stuck,
        "frozen_threads": [t for t in frozen.values() if not t["unfrozen_at"]],
        "counts": {**counts, "folded_messages": folded},
        "recent_event_types": event_types,
        "health": {
            "search_index": workbench_search.readonly_index_status(store),
        },
    }


def audit_chain(store, message_id: str) -> dict[str, Any]:
    """Reconstruct one message's whole life (the fourth success criterion)."""
    with store._transaction(readonly=True) as db:
        message = db.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if message is None:
            raise ValueError(f"找不到消息 {message_id}")
        message = {k: message[k] for k in message.keys()}  # noqa: SIM118
        task_ids = [tid for tid in (message.get("task_id"), message.get("source_task_id")) if tid]
        request_task = db.execute(
            "SELECT id FROM tasks WHERE request_message_id = ?", (message_id,)
        ).fetchone()
        if request_task:
            task_ids.append(request_task["id"])
        task_rows, task_events = [], []
        for task_id in dict.fromkeys(task_ids):
            row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                continue
            task_rows.append({k: row[k] for k in row.keys()})  # noqa: SIM118
            task_events.extend(
                _rows(
                    db,
                    "SELECT id, task_id, type, message, created_at FROM events "
                    "WHERE task_id = ? ORDER BY created_at",
                    (task_id,),
                )
            )
        replies = _rows(
            db,
            "SELECT id, title, sender_id, recipient_id, created_at FROM messages "
            "WHERE reply_to = ? ORDER BY created_at",
            (message_id,),
        )
        governance = _rows(
            db,
            "SELECT id, type, actor, reason, payload, created_at FROM governance_events "
            "WHERE task_id IN (SELECT id FROM tasks WHERE request_message_id = ?) "
            "   OR payload LIKE ? ORDER BY created_at",
            (message_id, f"%{message_id}%"),
        )
    governance = [{**row, "payload": _parse_payload(row.get("payload"))} for row in governance]
    terminal = task_rows[-1]["status"] if task_rows else "no-task"
    return {
        "message": message,
        "tasks": task_rows,
        "task_events": sorted(task_events, key=lambda r: r["created_at"]),
        "replies": replies,
        "governance": governance,
        "terminal_state": terminal,
        "steps": [
            {"step": "sent", "at": message.get("created_at"), "by": message.get("sender_id")},
            {
                "step": "received",
                "at": message.get("created_at"),
                "by": message.get("recipient_id"),
            },
            *[{"step": r["type"], "at": r["created_at"], "by": r.get("actor")} for r in governance],
            *[
                {"step": f"reply:{r['id']}", "at": r["created_at"], "by": r.get("sender_id")}
                for r in replies
            ],
            {
                "step": f"terminal:{terminal}",
                "at": (task_rows[-1]["updated_at"] if task_rows else message.get("created_at")),
                "by": None,
            },
        ],
    }
