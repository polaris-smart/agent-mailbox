"""Explicit recipient opens; never infer reading from delivery or folder listing."""

from __future__ import annotations

import hashlib
import json

from .workbench_store import WorkbenchError, _now


def _event_id(message):
    identity = [message["project_id"], message["id"], message["recipient_id"]]
    return "governance_read_" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def receipt_in(db, message, viewer_id):
    """Project only to the sender/recipient; the owner's generic views stay separate."""
    if message["recipient_id"] is None or viewer_id not in (
        message["sender_id"],
        message["recipient_id"],
    ):
        return None
    event = db.execute(
        "SELECT created_at FROM governance_events WHERE id=? AND type='message_opened' "
        "AND project_id=? AND employee_id=?",
        (_event_id(message), message["project_id"], message["recipient_id"]),
    ).fetchone()
    if event is None:
        return None
    return {
        "reader_id": message["recipient_id"],
        "first_opened_at": event["created_at"],
        "attribution_scope": "employee_session",
    }


def open_in(store, db, session, message_id):
    """Called inside the transaction that has validated the current employee session.

    The server returned content to this registered session. This does not prove
    model comprehension or successful client display after a network failure.
    """
    message = None
    if isinstance(message_id, str) and 0 < len(message_id) <= 512:
        message = db.execute(
            "SELECT * FROM messages WHERE id=? AND project_id=? AND recipient_id=?",
            (message_id, session["project_id"], session["employee_id"]),
        ).fetchone()
    if message is None:
        raise WorkbenchError("MESSAGE_NOT_ACCESSIBLE", "该信件不在本人当前项目收件箱。")
    db.execute(
        "INSERT OR IGNORE INTO governance_events "
        "(id,employee_id,project_id,task_id,type,actor,reason,payload,created_at) "
        "VALUES(?,?,?,NULL,'message_opened',?,?,?,?)",
        (
            _event_id(message),
            session["employee_id"],
            session["project_id"],
            "employee:" + session["employee_id"],
            "收件人员工会话明确打开单封邮件；不代表接单。",
            json.dumps({"message_id": message_id, "session_id": session["id"]}),
            _now(),
        ),
    )
    from .workbench_host_retractions import enqueue_in
    from .workbench_mail_notifications import settle_opened_notification

    settle_opened_notification(db, message)
    enqueue_in(db, message)
    value = store._message(db, message)
    value["read_receipt"] = receipt_in(db, message, session["employee_id"])
    return {"message": value, "accepts_task": False}
