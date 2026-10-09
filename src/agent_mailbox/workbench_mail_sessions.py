"""Revocable project mail capabilities for existing App and CLI sessions.

The capability identifies a registered session, not its internal/subagent author.
It grants no task execution, approval, management or operating-system sandbox.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import workbench_policy
from .workbench_store import WorkbenchError, _id, _now, _text

_READ_ONLY_KNOWLEDGE = {
    "graft_ask": {"task"},
    "graft_callers": {"symbol"},
    "aoci_doctor": set(),
    "aoci_status": set(),
    "aoci_check": set(),
}

TOOLS = {
    "aoci_check": set(),
    "aoci_status": set(),
    "aoci_doctor": set(),
    "graft_callers": {"symbol"},
    "graft_ask": {"task"},
    "context": set(),
    "messages": {"folder", "limit"},
    "message": {"title", "body", "recipient_id", "reply_to", "request_id"},
    "note": {"title", "body"},
    "memory_search": {"query"},
    "resource_read": {"resource_id", "version_id"},
    "resource_versions": {"resource_id"},
    "resource_propose": {"resource_id", "summary", "content"},
    "delivery": {"target_task_id"},
    "tasks": set(),
    "task_accept": {"task_id"},
    "task_submit": {"task_id", "result"},
}


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _membership(store, db, employee_id, project_id):
    row = db.execute(
        "SELECT m.secret_token,e.lifecycle,e.node_id FROM memberships m "
        "JOIN employees e ON e.id=m.employee_id WHERE m.employee_id=? AND m.project_id=?",
        (employee_id, project_id),
    ).fetchone()
    if not row or row["lifecycle"] != "active" or row["node_id"] != store.local_node_id(db):
        raise WorkbenchError("UNAUTHORIZED", "员工必须处于正常状态并属于本机项目组。")
    return row


def validate_session(store, db, token):
    """Validate inside the caller's transaction, including current membership generation."""
    if not isinstance(token, str) or not 20 <= len(token) <= 200:
        raise WorkbenchError("UNAUTHORIZED", "项目邮箱凭据无效或已撤销。")
    try:
        token_hash = _digest(token)
    except UnicodeEncodeError:
        raise WorkbenchError("UNAUTHORIZED", "项目邮箱凭据无效或已撤销。") from None
    row = db.execute("SELECT * FROM mailbox_sessions WHERE token_hash=?", (token_hash,)).fetchone()
    if not row or row["revoked_at"] is not None or row["expires_at"] <= _now():
        raise WorkbenchError("UNAUTHORIZED", "项目邮箱凭据无效或已撤销。")
    membership = _membership(store, db, row["employee_id"], row["project_id"])
    if not hmac.compare_digest(row["membership_hash"], _digest(membership["secret_token"])):
        raise WorkbenchError("UNAUTHORIZED", "项目成员权限已变更，请重新接入邮箱。")
    return row


def _public(store, db, row):
    result = {
        key: row[key]
        for key in (
            "id",
            "employee_id",
            "project_id",
            "label",
            "created_at",
            "expires_at",
            "revoked_at",
        )
    }
    result["active"] = False
    if row["revoked_at"] is None and row["expires_at"] > _now():
        try:
            membership = _membership(store, db, row["employee_id"], row["project_id"])
            result["active"] = hmac.compare_digest(
                row["membership_hash"], _digest(membership["secret_token"])
            )
        except WorkbenchError:
            pass
    result["attribution"] = {"scope": "employee_session", "internal_actor_verified": False}
    return store._scrub(db, result)


def create_session(store, employee_id, project_id, label):
    """Owner-only caller: issue once, store only a digest, expire after 30 days."""
    label = _text(label, "接入会话名称", 200).strip()
    with store._transaction() as db:
        membership = _membership(store, db, employee_id, project_id)
        token = secrets.token_urlsafe(32)
        session_id = _id("mail_session")
        created_at = _now()
        expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(
            timespec="microseconds"
        )
        db.execute(
            "INSERT INTO mailbox_sessions VALUES(?,?,?,?,?,?,?,?,NULL)",
            (
                session_id,
                employee_id,
                project_id,
                label,
                _digest(token),
                _digest(membership["secret_token"]),
                created_at,
                expires_at,
            ),
        )
        store._governance(
            db,
            "mailbox_session_created",
            employee_id=employee_id,
            project_id=project_id,
            actor="human",
            payload={"session_id": session_id},
        )
        result = _public(store, db, store._required(db, "mailbox_sessions", session_id))
        return {**result, "token": token}


def list_sessions(store, employee_id, project_id):
    with store._transaction(readonly=True) as db:
        store._required(db, "employees", employee_id)
        store._required(db, "projects", project_id)
        return [
            _public(store, db, row)
            for row in db.execute(
                "SELECT * FROM mailbox_sessions WHERE employee_id=? AND project_id=? ORDER BY created_at,id",
                (employee_id, project_id),
            )
        ]


def revoke_session(store, session_id):
    with store._transaction() as db:
        row = store._required(db, "mailbox_sessions", session_id)
        if row["revoked_at"] is None:
            db.execute("UPDATE mailbox_sessions SET revoked_at=? WHERE id=?", (_now(), session_id))
            store._governance(
                db,
                "mailbox_session_revoked",
                employee_id=row["employee_id"],
                project_id=row["project_id"],
                actor="human",
                payload={"session_id": session_id},
            )
        return _public(store, db, store._required(db, "mailbox_sessions", session_id))


def _bound_store(store, db):
    """Reuse existing domain methods in one authorization/write transaction."""
    bound = copy.copy(store)

    @contextmanager
    def connection(readonly: bool = False):
        # 绑定态复用调用方已开的事务；readonly 在此无意义（事务已由外层决定），
        # 但必须接受该参数——否则读辅助统一改用 readonly=True 后会 TypeError
        yield db

    bound._connection = connection
    bound._transaction = connection
    return bound


def _redact(value, token):
    if isinstance(value, str):
        return value.replace(token, "[redacted]")
    if isinstance(value, dict):
        return {key: _redact(item, token) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, token) for item in value]
    return value


def invoke(store, token, tool, args):
    if not isinstance(tool, str) or tool not in TOOLS:
        raise WorkbenchError("MAILBOX_TOOL_DENIED", "邮箱接入不提供该操作。")
    if not isinstance(args, dict) or set(args) - TOOLS[tool]:
        raise WorkbenchError("MAILBOX_ARGUMENT_DENIED", "不能指定额外身份、项目或执行参数。")
    with store._transaction() as db:
        session = validate_session(store, db, token)
        bound = _bound_store(store, db)
        project, employee = session["project_id"], session["employee_id"]
        args = _redact(args, token)
        try:
            if tool in _READ_ONLY_KNOWLEDGE:  # 只读诊断：不触发受管执行、不写状态
                from . import workbench_aoci as _aoci
                from . import workbench_graft as _graft

                path = bound.project_path_for(project)  # 服务端解析 ✓ agent 不传路径 ✓
                dispatch = {
                    "graft_ask": lambda: _graft.graft_ask(path, args["task"]),
                    "graft_callers": lambda: _graft.graft_callers(path, args["symbol"]),
                    "aoci_doctor": lambda: _aoci.aoci_doctor(path),
                    "aoci_status": lambda: _aoci.aoci_status(path),
                    "aoci_check": lambda: _aoci.aoci_check(path),
                }
                result = dispatch[tool]()

            elif tool == "context":
                result = bound.project_context(project, employee_id=employee)
                result["mailbox_session"] = _public(store, db, session)
                result["mailbox_policy"] = workbench_policy.effective_policy_in(db, project)
            elif tool == "messages":
                result = bound.employee_messages(
                    project, employee, args.get("folder", "inbox"), args.get("limit", 100)
                )
            elif tool == "message":
                # schema 只保证"参数存在"；空串/缺参必须在**真派发层**被拒，
                # 否则 HTTP/fleet/invoke 三条真路径仍会把缺参翻译成"全项目群发"
                if not str(args.get("recipient_id") or "").strip():
                    raise WorkbenchError(
                        "MAILBOX_ARGUMENT_DENIED",
                        "发消息必须指定收件人（recipient_id 非空）；员工不能群发全项目。",
                    )
                result = bound.send_message(
                    project,
                    args["title"],
                    args["body"],
                    recipient_id=args.get("recipient_id") or None,
                    sender_id=employee,
                    reply_to=args.get("reply_to") or None,
                    request_id=args.get("request_id") or None,
                    mailbox_token=token,
                )
            elif tool == "note":
                result = bound.add_memory(
                    project, args["title"], args["body"], source=f"employee:{employee}"
                )
                store._governance(
                    db,
                    "mailbox_note_created",
                    employee_id=employee,
                    project_id=project,
                    actor=f"employee:{employee}",
                    payload={"session_id": session["id"], "memory_id": result["id"]},
                )
            elif tool == "memory_search":
                result = {"memories": bound.search_memory(project, args["query"])}
            elif tool == "resource_versions":
                result = bound.resource_versions(project, args["resource_id"])
            elif tool == "resource_read":
                resource_id = args["resource_id"]
                versions = bound.resource_versions(project, resource_id)["versions"]
                version_id = args.get("version_id")
                version = (
                    next((v for v in versions if v["id"] == version_id), None)
                    if version_id
                    else next((v for v in versions if v["status"] == "approved"), None)
                )
                if not version or version["status"] != "approved":
                    raise WorkbenchError(
                        "RESOURCE_NOT_APPROVED", "请由 human 批准资料版本后再共享。"
                    )
                result = bound.read_resource_version(project, resource_id, version["id"])
            elif tool == "resource_propose":
                if not isinstance(args.get("content"), str):
                    raise WorkbenchError(
                        "EXPLICIT_CONTENT_REQUIRED", "邮箱提案必须提供文本，不能读取本地文件。"
                    )
                result = bound.capture_resource_version(
                    project,
                    args["resource_id"],
                    args.get("summary", ""),
                    employee,
                    content=args["content"],
                )
            elif tool in {"tasks", "task_accept", "task_submit"}:
                from .workbench_mail_tasks import (
                    accept_mail_task,
                    list_mail_tasks,
                    submit_mail_task,
                )

                if tool == "tasks":
                    result = list_mail_tasks(bound, token)
                elif tool == "task_accept":
                    result = accept_mail_task(bound, token, args["task_id"])
                else:
                    result = submit_mail_task(bound, token, args["task_id"], args["result"])
            else:
                from .workbench_execution_resources import project_task_delivery

                result = project_task_delivery(bound, project, args["target_task_id"])
        except KeyError:
            raise WorkbenchError("invalid_field", "邮箱操作缺少必要参数。") from None
        except (
            Exception
        ) as exc:  # 只读诊断的路径约束等：统一结构化（评审 #10：两面错误形状曾不一致 ✗）
            if type(exc).__name__ == "ProjectPathError":
                raise WorkbenchError("BAD_PROJECT_PATH", str(exc)) from None
            raise
        return _redact(store._scrub(db, result), token)


def ensure_member_session(
    store, project_id: str, employee_id: str, label: str | None = None
) -> dict:
    """**入组即自动签发** ✓（老板 2026-10-08 拍：项目建好、agent 拉进来，自然就有钥匙 ✓）。

    设计要点 ✗：
    · **先建成员关系** ✓ 再尝试签发 ✓ —— 签发失败**绝不影响**成员关系 ✓
    · 已有有效钥匙 ⇒ 幂等跳过 ✓（不重复签 ✗）
    · 签发失败 ⇒ **显式给原因** ✓（不在岗 / 非本机节点 / 权限 ✗）—— 不许静默 ✗
    · **不返回 token** ✗（只回 issued / expires_at / reason ✓ 凭据不进浏览器 ✓）
    """
    store.add_project_member(project_id, employee_id)
    # **AM-04 修复** ✗：按**有效**判（`active` = 未撤销 **且未过期** ✓），不是只看 `revoked_at` ✗
    # （Codex 实测：过期会话仍被当"已有钥匙"⇒ `issued=false` ✓ 而有效数为 0 ✗ ⇒ 用户表面入组成功、实际用不了 ✓）
    existing = [s for s in list_sessions(store, employee_id, project_id) if s.get("active")]
    if existing:
        return {
            "issued": False,
            "reason": "already_has_key",
            "expires_at": existing[0].get("expires_at"),
        }
    try:
        created = create_session(store, employee_id, project_id, label or f"auto-{project_id[-6:]}")
    except (WorkbenchError, OSError) as exc:  # 只捕可预期失败 ✓ 不盲捕 ✗
        return {
            "issued": False,
            "reason": type(exc).__name__,
            "detail": str(exc)[:200],
            "expires_at": None,
        }
    return {"issued": True, "reason": None, "expires_at": created.get("expires_at")}
