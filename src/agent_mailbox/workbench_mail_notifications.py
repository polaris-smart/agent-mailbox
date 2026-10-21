"""Durable host notification intents, committed with their directed messages.

The existing notifications outbox is the queue; no model or host is called in
the message transaction. A host receipt is not task acceptance or human review.
"""

import hashlib
from pathlib import Path

from .workbench_store import _id, _now

KIND = "mailbox_wake"


def _recipient_opened(db, project_id, message_id, recipient_id):
    from .workbench_message_reads import _event_id

    event_id = _event_id(
        {
            "project_id": project_id,
            "id": message_id,
            "recipient_id": recipient_id,
        }
    )
    return (
        db.execute(
            "SELECT 1 FROM governance_events WHERE id=? AND type='message_opened' "
            "AND project_id=? AND employee_id=?",
            (event_id, project_id, recipient_id),
        ).fetchone()
        is not None
    )


def settle_opened_notification(db, message):
    """An explicit recipient open dismisses pending or held mail without a host receipt."""
    return db.execute(
        "UPDATE notifications SET status='recipient_opened',updated_at=? "
        "WHERE kind=? AND target_id=? AND project_id=? AND state_hash=? "
        "AND status IN ('pending','reauthorization_held')",
        (_now(), KIND, message["recipient_id"], message["project_id"], message["id"]),
    ).rowcount


def settle_preexisting_opens(store, employee_id):
    """Reconcile read receipts recorded before this dismissal rule existed."""
    with store._transaction(readonly=True) as db:
        rows = db.execute(
            "SELECT m.id,m.project_id,m.recipient_id FROM notifications n "
            "JOIN messages m ON m.id=n.state_hash AND m.recipient_id=n.target_id "
            "WHERE n.kind=? AND n.target_id=? "
            "AND n.status IN ('pending','reauthorization_held')",
            (KIND, employee_id),
        ).fetchall()
        opened = [
            message
            for message in rows
            if _recipient_opened(db, message["project_id"], message["id"], employee_id)
        ]
    settled = []
    if opened:
        with store._transaction() as db:
            for message in opened:
                if _recipient_opened(
                    db, message["project_id"], message["id"], employee_id
                ) and settle_opened_notification(db, message):
                    settled.append(message["id"])
    # The wake worker calls this under its state lock. Clean offline markers
    # also for letters opened by the current version before this worker pass.
    claims = store.root / "wake" / "claims"
    with store._transaction(readonly=True) as db:
        for marker in claims.glob("*.pending"):
            mid = marker.name[: -len(".pending")]
            row = db.execute(
                "SELECT status FROM notifications WHERE kind=? AND target_id=? AND state_hash=?",
                (KIND, employee_id, mid),
            ).fetchone()
            if row is not None and row["status"] == "recipient_opened":
                marker.unlink(missing_ok=True)
    return settled


def host_effect_possible(home, message_id):
    """A surviving claim means a cancelled task may still have reached a host."""
    claim = Path(home) / "wake" / "claims" / message_id
    return any(path.is_file() for path in (claim, Path(f"{claim}.ok"), Path(f"{claim}.unknown")))


def host_may_know_assignment(db, home, task):
    """Use durable receipt or claim evidence before declaring external stop unknown."""
    message_id = task["request_message_id"]
    if task["execution_mode"] != "mailbox" or not message_id:
        return False
    if task["status"] == "running" or host_effect_possible(home, message_id):
        return True
    notification = db.execute(
        "SELECT status FROM notifications WHERE kind=? AND target_id=? AND state_hash=?",
        (KIND, task["assignee_id"], message_id),
    ).fetchone()
    return (
        notification is not None and notification["status"] in {"host_received", "recipient_opened"}
    ) or _recipient_opened(db, task["project_id"], message_id, task["assignee_id"])


def enqueue(db, message_id, recipient_id, project_id):
    if recipient_id is None:
        return
    now = _now()
    db.execute(
        "INSERT OR IGNORE INTO notifications "
        "(id,kind,channel,target_kind,target_id,project_id,state_hash,created_at,updated_at) "
        "VALUES(?,?,'host','employee',?,?,?,?,?)",
        (_id("note"), KIND, recipient_id, project_id, message_id, now, now),
    )


def retire_undelivered_assignment(db, home, task):
    """Withdraw only an assignment that has no possible host-side effect.

    Call with the wake directory lock held before the task transaction begins.
    A claim, confirmed delivery, or unknown delivery is evidence of a possible
    external effect and remains available for receipt reconciliation.
    """
    message_id = task["request_message_id"]
    if task["execution_mode"] != "mailbox" or not message_id:
        return False
    if _recipient_opened(db, task["project_id"], message_id, task["assignee_id"]):
        settle_opened_notification(
            db,
            {
                "id": message_id,
                "project_id": task["project_id"],
                "recipient_id": task["assignee_id"],
            },
        )
        return False
    if host_effect_possible(home, message_id):
        return False
    changed = db.execute(
        "UPDATE notifications SET status='cancelled',updated_at=? "
        "WHERE kind=? AND target_id=? AND state_hash=? "
        "AND status='pending' AND attempts=0",
        (_now(), KIND, task["assignee_id"], message_id),
    ).rowcount
    if changed:
        # This marker records a known non-delivery, so it has no effect to reconcile.
        claim = Path(home) / "wake" / "claims" / message_id
        Path(f"{claim}.pending").unlink(missing_ok=True)
    return bool(changed)


def enqueue_cancel_notice(db, home, task):
    """One directed control letter when the assignee could know the old task.

    The message is a Human-owned state notice, never a work request or an
    automatic reply. It is committed with its host intent in the same task
    cancellation transaction.
    """
    if task["execution_mode"] != "mailbox" or not task["request_message_id"]:
        return None
    original = db.execute(
        "SELECT thread_id FROM messages WHERE id=?", (task["request_message_id"],)
    ).fetchone()
    if original is None:
        return None
    if not host_may_know_assignment(db, home, task):
        return None
    request_id = f"mailbox_cancel:{task['id']}"
    prior = db.execute(
        "SELECT id FROM messages WHERE project_id=? AND sender_id IS NULL AND request_id=?",
        (task["project_id"], request_id),
    ).fetchone()
    if prior is not None:
        return prior["id"]
    message_id, now = _id("message"), _now()
    body = (
        f"任务 / Task: {task['id']}\n"
        "Human 已撤销此任务。请停止后续工作；若已有外部效果，请报告当前状态，"
        "不要自动重试。请以任务当前状态为准。\n"
        "A human cancelled this task. Stop further work. If an external effect "
        "already occurred, report its current state; do not retry automatically."
    )
    db.execute(
        "INSERT INTO messages(id,project_id,title,body,sender_id,recipient_id,reply_to,"
        "thread_id,request_work,task_id,request_id,request_digest,created_at) "
        "VALUES(?,?,?, ?,NULL,?,?, ?,0,?,?,?,?)",
        (
            message_id,
            task["project_id"],
            "任务已撤销 / Task cancelled",
            body,
            task["assignee_id"],
            task["request_message_id"],
            original["thread_id"],
            task["id"],
            request_id,
            hashlib.sha256(body.encode()).hexdigest(),
            now,
        ),
    )
    enqueue(db, message_id, task["assignee_id"], task["project_id"])
    return message_id


def pending_ids(store, employee_id):
    from .workbench_reauthorization_holds import held

    with store._transaction(readonly=True) as db:
        rows = db.execute(
            "SELECT n.state_hash,m.project_id,t.cancel_requested FROM notifications n "
            "JOIN messages m ON m.id=n.state_hash AND m.recipient_id=n.target_id "
            "LEFT JOIN tasks t ON t.id=m.task_id AND t.request_message_id=m.id "
            "AND t.execution_mode='mailbox' "
            "JOIN memberships p ON p.employee_id=n.target_id AND p.project_id=m.project_id "
            "JOIN employees e ON e.id=n.target_id AND e.lifecycle='active' "
            "WHERE n.kind=? AND n.target_id=? AND n.status='pending' "
            "ORDER BY n.created_at,n.id",
            (KIND, employee_id),
        ).fetchall()
        return [
            row["state_hash"]
            for row in rows
            if (not row["cancel_requested"] or host_effect_possible(store.root, row["state_hash"]))
            and not held(store.root, row["state_hash"])
            and not _recipient_opened(db, row["project_id"], row["state_hash"], employee_id)
        ]


def ready_ids(store, employee_id, *, ready_threads=frozenset()):
    """Only offer durable intents to a route that can currently receive them.

    A missing receiver stays in the DB outbox; the recovery scan notices it when
    the original host returns, without making a new delivery file every five seconds.
    """
    import time

    from .workbench_host_bindings import resolve_routes
    from .workbench_host_transport import configured_for_binding, receiver_online
    from .workbench_wake import _claim_receipt, _unknown_hermes_envelope, hook_command

    settle_preexisting_opens(store, employee_id)
    pending = pending_ids(store, employee_id)
    if not pending:
        return []
    routes, _missing = resolve_routes(store, employee_id, pending)
    eligible = set()
    immediate = set()
    claims = store.root / "wake" / "claims"
    legacy_hook = hook_command(store.root) is not None
    for route in routes:
        binding = route["binding"]
        # Unknown delivery belongs to one message, not its whole recipient batch.
        # Only a matching, already-persisted host receipt may settle it; the
        # other messages remain independently eligible for a new delivery.
        unknown = {mid for mid in route["messages"] if (claims / f"{mid}.unknown").is_file()}
        eligible.update(
            mid
            for mid in unknown
            if _claim_receipt(store.root / "wake", employee_id, claims / f"{mid}.unknown", binding)
        )
        try:
            available = (
                receiver_online(store.root, binding)
                if configured_for_binding(store.root, binding)
                else legacy_hook
            )
        except (OSError, ValueError):
            # Reject this route without blocking other projects or employees.
            # Never fall back to a legacy hook for an invalid native endpoint.
            print(f"[wake] receiver readiness unconfirmed: {binding['id']}")
            available = False
        if available:
            eligible.update(mid for mid in route["messages"] if mid not in unknown)
            if binding["host_kind"] == "hermes":
                # The host may have durable turn evidence even when its receipt
                # mirror is missing. Only the original delivery for this exact
                # binding may reach the journal query; older project claims may
                # not become ready just because the new receiver is online.
                eligible.update(
                    mid
                    for mid in unknown
                    if _unknown_hermes_envelope(
                        store.root / "wake", employee_id, mid, binding
                    )
                    is not None
                )
            if binding["host_kind"] == "codex" and binding["host_session_id"] in ready_threads:
                immediate.update(mid for mid in route["messages"] if mid not in unknown)
    now = time.time()
    ready = []
    for mid in pending:
        if mid not in eligible:
            continue
        if mid in immediate:
            ready.append(mid)
            continue
        try:
            last_attempt = (claims / f"{mid}.pending").stat().st_mtime
        except FileNotFoundError:
            ready.append(mid)
            continue
        if now - last_attempt >= 30:
            ready.append(mid)
    return ready


def mark_host_received(store, employee_id, message_ids):
    """The native worker calls this only after validating the adapter receipt."""
    with store._transaction() as db:
        db.executemany(
            "UPDATE notifications SET status='host_received',attempts=attempts+1,updated_at=? "
            "WHERE kind=? AND target_id=? AND state_hash=? "
            "AND status IN ('pending','recipient_opened')",
            [(_now(), KIND, employee_id, message_id) for message_id in message_ids],
        )
