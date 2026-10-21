"""Owner-reviewed release of one old ordinary Hermes mail alert.

No alert is replayed by linking a project or repairing a session. The Owner
must select an exact held message, inspect its scope, and confirm a fingerprint.
The database becomes pending before the private hold is renamed to a scoped
release marker; an interrupted operation therefore remains fail-closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re

from . import workbench_hermes_catalog as catalog
from .workbench_host_bindings import load_bindings
from .workbench_host_transport import endpoint_for, endpoints
from .workbench_mail_notifications import KIND, _recipient_opened
from .workbench_reauthorization_holds import (
    _exists,
    _private_json,
    completed_history,
    gated,
    hold_path,
    latest_completed_plan,
    release_path,
)
from .workbench_store import _now
from .workbench_wake import state_lock

MESSAGE_ID = re.compile(r"message_[0-9a-f]{32}\Z")


def list_held(store, employee_id, project_id):
    """Show only old ordinary alerts for Owner review; never expose host secrets."""
    catalog._member(store, employee_id, project_id)
    with store._transaction(readonly=True) as db:
        rows = db.execute(
            "SELECT m.id,m.title,m.created_at,m.sender_id,e.name AS sender_name "
            "FROM notifications n "
            "JOIN messages m ON m.id=n.state_hash AND m.recipient_id=n.target_id "
            "AND m.project_id=n.project_id "
            "LEFT JOIN employees e ON e.id=m.sender_id "
            "WHERE n.kind=? AND n.target_id=? AND n.project_id=? "
            "AND n.status IN ('reauthorization_held','pending') AND n.attempts=0 "
            "AND m.request_work=0 AND m.task_id IS NULL AND m.source_task_id IS NULL "
            "ORDER BY n.created_at,n.id",
            (KIND, employee_id, project_id),
        )
        items = []
        for row in rows:
            # Pending with its private hold still present is an interrupted
            # release, not an ordinary pending delivery. Keep it reviewable.
            if not _exists(hold_path(store.root, row["id"])) or _recipient_opened(
                db, project_id, row["id"], employee_id
            ):
                continue
            items.append(dict(row))
            if len(items) > 100:
                break
    return {
        "state": "held_mail_list",
        "items": items[:100],
        "has_more": len(items) > 100,
        "delivery_verified": False,
    }


def _candidate(store, employee_id, project_id, message_id):
    if not isinstance(message_id, str) or not MESSAGE_ID.fullmatch(message_id):
        raise ValueError("held_mail_unavailable")
    catalog._member(store, employee_id, project_id)
    binding = next(
        (
            row
            for row in load_bindings(store.root)
            if row["employee_id"] == employee_id
            and row["project_id"] == project_id
            and row["host_kind"] == "hermes"
        ),
        None,
    )
    if binding is None or gated(store.root, binding["id"]):
        raise ValueError("held_mail_route_unconfirmed")
    configured = next(
        (row for row in endpoints(store.root) if row["binding_id"] == binding["id"]), None
    )
    socket_path = endpoint_for(store.root, binding)
    if (
        configured is None
        or configured["employee_id"] != employee_id
        or configured["host_kind"] != "hermes"
        or not socket_path
        or configured.get("socket_path") != socket_path
    ):
        raise ValueError("held_mail_route_unconfirmed")
    _plans, cutoff, pending = completed_history(store.root, binding["id"])
    plan_id = latest_completed_plan(store.root, binding["id"])
    if cutoff is None or pending is not None or plan_id is None:
        raise ValueError("held_mail_route_unconfirmed")
    with store._transaction(readonly=True) as db:
        row = db.execute(
            "SELECT m.id,m.title,m.body,m.created_at,m.sender_id,e.name AS sender_name,"
            "m.request_work,m.task_id,m.source_task_id,"
            "n.id AS notification_id,n.status,n.attempts,n.updated_at "
            "FROM messages m JOIN notifications n ON n.state_hash=m.id "
            "AND n.kind=? AND n.target_id=m.recipient_id AND n.project_id=m.project_id "
            "LEFT JOIN employees e ON e.id=m.sender_id "
            "WHERE m.id=? AND m.recipient_id=? AND m.project_id=?",
            (KIND, message_id, employee_id, project_id),
        ).fetchone()
        if row is None or row["status"] not in {"reauthorization_held", "pending"}:
            raise ValueError("held_mail_unavailable")
        if row["request_work"] or row["task_id"] or row["source_task_id"]:
            raise ValueError("ordinary_mail_required")
        if row["attempts"] != 0 or _recipient_opened(db, project_id, message_id, employee_id):
            raise ValueError("held_mail_unavailable")
        note = dict(row)
    hold = hold_path(store.root, message_id)
    expected = {
        "version": 1,
        "plan_id": plan_id,
        "binding_id": binding["id"],
        "employee_id": employee_id,
        "project_id": project_id,
    }
    try:
        if _private_json(hold) != expected:
            raise ValueError("held_mail_marker_changed")
    except OSError:
        raise ValueError("held_mail_marker_changed") from None
    claims = store.root / "wake" / "claims"
    if any(_exists(claims / f"{message_id}{suffix}") for suffix in ("", ".ok", ".unknown")):
        raise ValueError("held_mail_effect_unconfirmed")
    released = release_path(store.root, message_id, binding["id"], plan_id)
    if _exists(released):
        raise ValueError("held_mail_release_changed")
    facts = {
        "message_id": message_id,
        "employee_id": employee_id,
        "project_id": project_id,
        "binding_id": binding["id"],
        "host_session_id": binding["host_session_id"],
        "socket_path": socket_path,
        "plan_id": plan_id,
        "cutoff": cutoff,
        "content_sha256": hashlib.sha256(
            json.dumps([note["title"], note["body"]], ensure_ascii=False).encode()
        ).hexdigest(),
        "sender_id": note["sender_id"],
        "notification_id": note["notification_id"],
        "status": note["status"],
        "updated_at": note["updated_at"],
    }
    fingerprint = hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()
    return note, binding, plan_id, hold, released, fingerprint


def _publish_release(hold, released, payload):
    """Rename only a verified private hold after the pending DB commit."""
    if _exists(released) or _private_json(hold) != payload:
        raise ValueError("held_mail_marker_changed")
    os.rename(hold, released)
    directory = os.open(released.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def operation(
    store,
    employee_id,
    project_id,
    message_id,
    action,
    *,
    expected_fingerprint=None,
    confirmed=False,
):
    """Preview or confirm exactly one old ordinary alert, never a task or unknown effect."""
    if action not in {"preview", "apply"} or type(confirmed) is not bool:
        raise ValueError("invalid_held_mail_action")
    if action == "preview" and (confirmed or expected_fingerprint is not None):
        raise ValueError("invalid_held_mail_preview")
    if action == "apply" and (
        confirmed is not True
        or not isinstance(expected_fingerprint, str)
        or not re.fullmatch(r"[a-f0-9]{64}", expected_fingerprint)
    ):
        raise ValueError("confirmation_required")
    with state_lock(store.root):
        note, binding, plan_id, hold, released, fingerprint = _candidate(
            store, employee_id, project_id, message_id
        )
        if action == "preview":
            return {
                "state": "confirmation_required",
                "fingerprint": fingerprint,
                "message_id": message_id,
                "title": note["title"],
                "body": note["body"],
                "created_at": note["created_at"],
                "sender_id": note["sender_id"],
                "sender_name": note["sender_name"],
                "delivery_verified": False,
            }
        if fingerprint != expected_fingerprint:
            raise ValueError("configuration_changed")
        if note["status"] == "reauthorization_held":
            with store._transaction() as db:
                changed = db.execute(
                    "UPDATE notifications SET status='pending',updated_at=? "
                    "WHERE id=? AND kind=? AND target_id=? AND project_id=? "
                    "AND state_hash=? AND status='reauthorization_held' AND attempts=0 "
                    "AND updated_at=?",
                    (
                        _now(),
                        note["notification_id"],
                        KIND,
                        employee_id,
                        project_id,
                        message_id,
                        note["updated_at"],
                    ),
                ).rowcount
                if changed != 1:
                    raise ValueError("held_mail_changed_refresh_required")
        _publish_release(
            hold,
            released,
            {
                "version": 1,
                "plan_id": plan_id,
                "binding_id": binding["id"],
                "employee_id": employee_id,
                "project_id": project_id,
            },
        )
    event = getattr(store, "mail_wake_event", None)
    if event is not None:
        event.set()
    return {"state": "old_mail_pending", "delivery_verified": False}
