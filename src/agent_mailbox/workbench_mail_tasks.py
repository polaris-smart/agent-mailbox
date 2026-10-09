"""Explicit task receipts from existing employee sessions; never launch a model."""

from __future__ import annotations

import json
import secrets

from .workbench_mail_sessions import _bound_store, validate_session
from .workbench_store import WorkbenchError, _id, _now, _text


def create_mail_task(store, project_id, title, prompt, assignee_id):
    """Human assigns an ordinary-mail task, not a managed execution request."""
    title = _text(title, "任务名称", 300).strip()
    prompt = _text(prompt, "任务说明")
    with store._transaction() as db:
        store._required(db, "projects", project_id)
        employee = store._message_member(db, project_id, assignee_id)
        if employee["node_id"] != store.local_node_id(db):
            raise WorkbenchError("REMOTE_MAILBOX_UNAVAILABLE", "现有会话邮箱目前只支持本机员工。")
        if db.execute("SELECT paused FROM update_settings WHERE id=1").fetchone()[0]:
            raise WorkbenchError("UPDATE_PAUSED", "更新准备期间暂停创建任务。")
        title, prompt = store._scrub(db, title), store._scrub(db, prompt)
        task_id, timestamp = _id("task"), _now()
        db.execute(
            "INSERT INTO tasks(id,project_id,title,prompt,assignee_id,node_id,permission_mode,"
            "run_id,session_id,created_at,updated_at,tool_token,execution_mode) "
            "VALUES(?,?,?,?,?,?,'read-only',?,?,?,?,?,'mailbox')",
            (
                task_id,
                project_id,
                title,
                prompt,
                assignee_id,
                employee["node_id"],
                _id("mail_run"),
                _id("mail_task_session"),
                timestamp,
                timestamp,
                secrets.token_urlsafe(32),
            ),
        )
        message = _bound_store(store, db).send_message(
            project_id,
            title,
            f"任务 / Task: {task_id}\n{prompt}\n请明确接受任务；提交结果后仍需 Human 验收。",
            recipient_id=assignee_id,
        )
        db.execute("UPDATE messages SET task_id=? WHERE id=?", (task_id, message["id"]))
        db.execute("UPDATE tasks SET request_message_id=? WHERE id=?", (message["id"], task_id))
        store._event(db, task_id, "queued", "邮件任务已分配，等待员工明确接受；不会启动 CLI。")
        store._governance(
            db,
            "mail_task_assigned",
            employee_id=assignee_id,
            project_id=project_id,
            task_id=task_id,
            payload={"message_id": message["id"], "execution_mode": "mailbox"},
        )
        return store._scrub(db, store._entity(store._required(db, "tasks", task_id)))


def _authorized_task(store, db, token, task_id):
    session = validate_session(store, db, token)
    task = store._required(db, "tasks", task_id)
    if (
        task["execution_mode"] != "mailbox"
        or task["assignee_id"] != session["employee_id"]
        or task["project_id"] != session["project_id"]
    ):
        raise WorkbenchError("UNAUTHORIZED", "只能操作当前项目分配给本员工的邮件任务。")
    if task["cancel_requested"]:
        raise WorkbenchError("invalid_state", "任务已请求取消，不能继续提交。")
    return task, session


def list_mail_tasks(store, token):
    with store._transaction(readonly=True) as db:
        session = validate_session(store, db, token)
        rows = db.execute(
            "SELECT * FROM tasks WHERE project_id=? AND assignee_id=? "
            "AND execution_mode='mailbox' ORDER BY created_at,id",
            (session["project_id"], session["employee_id"]),
        )
        tasks = [store._entity(row) for row in rows]
        for task in tasks:
            saved = db.execute(
                "SELECT manifest FROM task_resource_manifests WHERE task_id=? AND run_id=?",
                (task["id"], task["run_id"]),
            ).fetchone()
            if saved:
                task["resource_manifest"] = json.loads(saved[0])
        return {"tasks": store._scrub(db, tasks), "reading_accepts": False}


def accept_mail_task(store, token, task_id):
    with store._transaction() as db:
        task, session = _authorized_task(store, db, token, task_id)
        if task["status"] != "queued":
            raise WorkbenchError("invalid_state", "只有待接受的邮件任务可以接受。")
        from .workbench_resources import freeze

        freeze(store, db, task)
        db.execute("UPDATE tasks SET status='running',updated_at=? WHERE id=?", (_now(), task_id))
        store._event(
            db,
            task_id,
            "mail_task_accepted",
            "员工已明确接受，尚未提交结果。",
            {"session_id": session["id"]},
        )
        store._governance(
            db,
            "mail_task_accepted",
            task_id=task_id,
            employee_id=session["employee_id"],
            project_id=session["project_id"],
            actor=f"employee:{session['employee_id']}",
            payload={"session_id": session["id"]},
        )
        accepted = store._scrub(db, store._entity(store._required(db, "tasks", task_id)))
        accepted["resource_manifest"] = _bound_store(store, db).execution_resource_manifest(
            task_id, task["run_id"]
        )
        return accepted


def submit_mail_task(store, token, task_id, result):
    result = _text(result, "任务结果")
    with store._transaction() as db:
        task, session = _authorized_task(store, db, token, task_id)
        if task["status"] != "running":
            raise WorkbenchError("invalid_state", "请先明确接受任务，再提交结果等待验收。")
        result = store._scrub(db, result.replace(token, "[redacted]"))
        db.execute(
            "UPDATE tasks SET status='review',result=?,updated_at=? WHERE id=?",
            (result, _now(), task_id),
        )
        _bound_store(store, db).send_message(
            session["project_id"],
            "交付 / Delivery: " + task["title"][:270],
            result,
            sender_id=session["employee_id"],
            reply_to=task["request_message_id"],
            mailbox_token=token,
        )
        store._event(
            db,
            task_id,
            "mail_task_submitted",
            "员工已提交结果，等待 Human 验收。",
            {"session_id": session["id"]},
        )
        store._governance(
            db,
            "mail_task_submitted",
            task_id=task_id,
            employee_id=session["employee_id"],
            project_id=session["project_id"],
            actor=f"employee:{session['employee_id']}",
            payload={"session_id": session["id"]},
        )
        return store._scrub(db, store._entity(store._required(db, "tasks", task_id)))


def set_project_role(store, project_id, employee_id, role):
    """Owner-only descriptive responsibility; grants no additional permission."""
    role = _text(role, "项目职责", 1000, empty=True).strip()
    with store._transaction() as db:
        store._required(db, "projects", project_id)
        store._message_member(db, project_id, employee_id)
        role = store._scrub(db, role)
        db.execute(
            "UPDATE memberships SET role=? WHERE project_id=? AND employee_id=?",
            (role, project_id, employee_id),
        )
        store._governance(
            db,
            "project_role_changed",
            employee_id=employee_id,
            project_id=project_id,
            payload={"role": role},
        )
        return {"project_id": project_id, "employee_id": employee_id, "role": role}
