"""Human and space ownership for project authorization.

This module is a data-layer boundary. The legacy owner HTTP capability remains
local-only; remote Human sessions must use a separately authenticated API.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .workbench_store import WorkbenchError, _id, _now, _text

HUMAN_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS humans (
    id TEXT PRIMARY KEY, display_name TEXT NOT NULL, created_at TEXT NOT NULL, disabled_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS spaces (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('private','shared')),
    owner_human_id TEXT NOT NULL REFERENCES humans(id), created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS space_memberships (
    space_id TEXT NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    human_id TEXT NOT NULL REFERENCES humans(id), role TEXT NOT NULL CHECK(role IN ('owner','member')),
    created_at TEXT NOT NULL, revoked_at TEXT, PRIMARY KEY(space_id,human_id))""",
    """CREATE TABLE IF NOT EXISTS project_spaces (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    space_id TEXT NOT NULL REFERENCES spaces(id), created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS project_human_grants (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    human_id TEXT NOT NULL REFERENCES humans(id),
    role TEXT NOT NULL CHECK(role IN ('owner','editor','viewer')),
    created_at TEXT NOT NULL, revoked_at TEXT, PRIMARY KEY(project_id,human_id))""",
    """CREATE TABLE IF NOT EXISTS project_human_reviewers (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    human_id TEXT NOT NULL REFERENCES humans(id),
    created_at TEXT NOT NULL, revoked_at TEXT, PRIMARY KEY(project_id,human_id))""",
    """CREATE TABLE IF NOT EXISTS human_bootstrap (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    owner_human_id TEXT NOT NULL REFERENCES humans(id),
    private_space_id TEXT NOT NULL REFERENCES spaces(id))""",
    """CREATE TABLE IF NOT EXISTS human_sessions (
    id TEXT PRIMARY KEY, human_id TEXT NOT NULL REFERENCES humans(id),
    token_sha256 TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL, revoked_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS human_enrollments (
    human_id TEXT PRIMARY KEY REFERENCES humans(id),
    invitation_sha256 TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL,
    claimed_at TEXT, recovery_sha256 TEXT UNIQUE)""",
    "CREATE INDEX IF NOT EXISTS project_spaces_space ON project_spaces(space_id)",
    "CREATE INDEX IF NOT EXISTS project_human_grants_human ON project_human_grants(human_id,revoked_at)",
    "CREATE INDEX IF NOT EXISTS project_human_reviewers_human ON project_human_reviewers(human_id,revoked_at)",
    "CREATE INDEX IF NOT EXISTS human_sessions_human ON human_sessions(human_id,revoked_at)",
)


def backfill_legacy(db):
    """Privately assign every pre-0.8.3 project without sharing it."""
    row = db.execute(
        "SELECT owner_human_id,private_space_id FROM human_bootstrap WHERE singleton=1"
    ).fetchone()
    if row is None:
        now = _now()
        human_id, space_id = _id("human"), _id("space")
        db.execute("INSERT INTO humans VALUES(?,?,?,NULL)", (human_id, "本机所有者", now))
        db.execute(
            "INSERT INTO spaces VALUES(?,?,?,?,?)", (space_id, "私人空间", "private", human_id, now)
        )
        db.execute(
            "INSERT INTO space_memberships VALUES(?,?,?,?,NULL)", (space_id, human_id, "owner", now)
        )
        db.execute("INSERT INTO human_bootstrap VALUES(1,?,?)", (human_id, space_id))
    else:
        human_id, space_id = row
    for (project_id,) in db.execute(
        "SELECT p.id FROM projects p LEFT JOIN project_spaces ps ON ps.project_id=p.id "
        "WHERE ps.project_id IS NULL"
    ).fetchall():
        now = _now()
        db.execute("INSERT INTO project_spaces VALUES(?,?,?)", (project_id, space_id, now))
        db.execute(
            "INSERT INTO project_human_grants VALUES(?,?,?,?,NULL)",
            (project_id, human_id, "owner", now),
        )


def assign_owner_project(db, project_id):
    """Call in the same transaction that inserts a legacy-owner project."""
    row = db.execute(
        "SELECT owner_human_id,private_space_id FROM human_bootstrap WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise WorkbenchError("HUMAN_BOOTSTRAP_REQUIRED", "本机所有者身份尚未初始化。")
    now = _now()
    db.execute("INSERT INTO project_spaces VALUES(?,?,?)", (project_id, row[1], now))
    db.execute(
        "INSERT INTO project_human_grants VALUES(?,?,?,?,NULL)", (project_id, row[0], "owner", now)
    )


def legacy_owner_view_crosses_human_boundary(store):
    """Old full-store routes cannot serve foreign or unmapped Human projects."""
    with store._transaction(readonly=True) as db:
        return (
            db.execute(
                "SELECT 1 FROM projects p LEFT JOIN project_spaces ps ON ps.project_id=p.id "
                "LEFT JOIN spaces s ON s.id=ps.space_id "
                "JOIN human_bootstrap hb ON hb.singleton=1 "
                "WHERE s.id IS NULL OR s.owner_human_id!=hb.owner_human_id LIMIT 1"
            ).fetchone()
            is not None
        )


def _active_human(db, human_id):
    row = db.execute(
        "SELECT id FROM humans WHERE id=? AND disabled_at IS NULL", (human_id,)
    ).fetchone()
    if row is None:
        raise WorkbenchError("HUMAN_UNAVAILABLE", "Human 身份不存在或已停用。")
    return row


def _project_scope(db, project_id):
    row = db.execute(
        "SELECT s.id AS space_id,s.kind,s.owner_human_id FROM project_spaces ps "
        "JOIN spaces s ON s.id=ps.space_id WHERE ps.project_id=?",
        (project_id,),
    ).fetchone()
    if row is None:
        raise WorkbenchError("PROJECT_SCOPE_REQUIRED", "项目没有有效的空间归属。")
    return row


def project_role(db, human_id, project_id):
    """The intersection of active Human, space membership, and project grant."""
    try:
        scope = _project_scope(db, project_id)
    except WorkbenchError:
        return None
    row = db.execute(
        "SELECT g.role FROM project_human_grants g "
        "JOIN humans h ON h.id=g.human_id AND h.disabled_at IS NULL "
        "JOIN space_memberships sm ON sm.space_id=? AND sm.human_id=g.human_id "
        "AND sm.revoked_at IS NULL "
        "WHERE g.project_id=? AND g.human_id=? AND g.revoked_at IS NULL",
        (scope["space_id"], project_id, human_id),
    ).fetchone()
    if row is None or (scope["kind"] == "private" and scope["owner_human_id"] != human_id):
        return None
    return row["role"]


def require_project_role(db, human_id, project_id, allowed=("owner", "editor", "viewer")):
    role = project_role(db, human_id, project_id)
    if role not in allowed:
        # Do not distinguish nonexistent projects from projects hidden from this Human.
        raise WorkbenchError("PROJECT_NOT_FOUND", "项目不存在或没有访问权限。")
    return role


def require_review_authority(db, human_id, project_id):
    """Review is an explicit project capability, not implied by edit access."""
    role = require_project_role(db, human_id, project_id)
    if role == "owner":
        return
    if (
        db.execute(
            "SELECT 1 FROM project_human_reviewers WHERE project_id=? AND human_id=? "
            "AND revoked_at IS NULL",
            (project_id, human_id),
        ).fetchone()
        is None
    ):
        raise WorkbenchError("PROJECT_NOT_FOUND", "项目不存在或没有验收权限。")


def list_projects_for_human(store, human_id):
    with store._transaction(readonly=True) as db:
        _active_human(db, human_id)
        rows = db.execute(
            "SELECT p.id,p.name,ps.space_id,s.kind,g.role,"
            "CASE WHEN g.role='owner' OR r.human_id IS NOT NULL THEN 1 ELSE 0 END AS can_review "
            "FROM projects p "
            "JOIN project_spaces ps ON ps.project_id=p.id "
            "JOIN spaces s ON s.id=ps.space_id "
            "JOIN project_human_grants g ON g.project_id=p.id AND g.human_id=? "
            "AND g.revoked_at IS NULL "
            "JOIN space_memberships sm ON sm.space_id=s.id AND sm.human_id=? "
            "AND sm.revoked_at IS NULL "
            "LEFT JOIN project_human_reviewers r ON r.project_id=p.id AND r.human_id=? "
            "AND r.revoked_at IS NULL "
            "WHERE (s.kind='shared' OR s.owner_human_id=?) "
            "ORDER BY p.created_at,p.id",
            (human_id, human_id, human_id, human_id),
        ).fetchall()
        # No path, task count, notification or resource content leaves this seam.
        return [dict(row) for row in rows]


def list_spaces_for_human(store, human_id):
    with store._transaction(readonly=True) as db:
        _active_human(db, human_id)
        rows = db.execute(
            "SELECT s.id,s.name,s.kind,sm.role FROM spaces s "
            "JOIN space_memberships sm ON sm.space_id=s.id "
            "WHERE sm.human_id=? AND sm.revoked_at IS NULL "
            "AND (s.kind='shared' OR s.owner_human_id=?) "
            "ORDER BY s.created_at,s.id",
            (human_id, human_id),
        ).fetchall()
        return [dict(row) for row in rows]


def list_space_members_for_human(store, human_id, space_id):
    """List names only for a shared space the caller currently belongs to."""
    with store._transaction(readonly=True) as db:
        _active_human(db, human_id)
        space = db.execute(
            "SELECT 1 FROM spaces s JOIN space_memberships sm ON sm.space_id=s.id "
            "WHERE s.id=? AND s.kind='shared' AND sm.human_id=? "
            "AND sm.revoked_at IS NULL",
            (space_id, human_id),
        ).fetchone()
        if space is None:
            raise WorkbenchError("SPACE_NOT_FOUND", "空间不存在或没有访问权限。")
        rows = db.execute(
            "SELECT h.id,h.display_name,sm.role FROM space_memberships sm "
            "JOIN humans h ON h.id=sm.human_id AND h.disabled_at IS NULL "
            "WHERE sm.space_id=? AND sm.revoked_at IS NULL "
            "ORDER BY sm.created_at,h.id",
            (space_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def list_project_grants_for_human(store, human_id, project_id):
    """Show active grants only to this project's Human owner."""
    with store._transaction(readonly=True) as db:
        require_project_role(db, human_id, project_id, ("owner",))
        scope = _project_scope(db, project_id)
        rows = db.execute(
            "SELECT h.id AS human_id,h.display_name,g.role,"
            "CASE WHEN g.role='owner' OR r.human_id IS NOT NULL THEN 1 ELSE 0 END AS can_review "
            "FROM project_human_grants g "
            "JOIN humans h ON h.id=g.human_id AND h.disabled_at IS NULL "
            "JOIN space_memberships sm ON sm.space_id=? AND sm.human_id=g.human_id "
            "AND sm.revoked_at IS NULL "
            "LEFT JOIN project_human_reviewers r ON r.project_id=g.project_id "
            "AND r.human_id=g.human_id AND r.revoked_at IS NULL "
            "WHERE g.project_id=? AND g.revoked_at IS NULL "
            "ORDER BY g.created_at,h.id",
            (scope["space_id"], project_id),
        ).fetchall()
        return [dict(row) for row in rows]


def list_task_summaries_for_human(store, human_id, project_id):
    """Return only one authorized project's non-sensitive task index fields."""
    with store._transaction(readonly=True) as db:
        require_project_role(db, human_id, project_id)
        rows = db.execute(
            "SELECT id,title,status,assignee_id,created_at FROM tasks "
            "WHERE project_id=? ORDER BY created_at,id",
            (project_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def visible_task_state_for_human(store, human_id):
    """Internal change marker for tasks in projects this Human can still read."""
    with store._transaction(readonly=True) as db:
        _active_human(db, human_id)
        rows = db.execute(
            "SELECT t.project_id,t.id,t.title,t.status,t.assignee_id,t.created_at "
            "FROM tasks t "
            "JOIN project_spaces ps ON ps.project_id=t.project_id "
            "JOIN spaces s ON s.id=ps.space_id "
            "JOIN project_human_grants g ON g.project_id=t.project_id "
            "AND g.human_id=? AND g.revoked_at IS NULL "
            "JOIN space_memberships sm ON sm.space_id=s.id "
            "AND sm.human_id=? AND sm.revoked_at IS NULL "
            "WHERE s.kind='shared' OR s.owner_human_id=? "
            "ORDER BY t.project_id,t.created_at,t.id",
            (human_id, human_id, human_id),
        ).fetchall()
        return tuple(tuple(row) for row in rows)


def visible_group_message_state_for_human(store, human_id):
    """Internal arrival marker for group mail in currently readable projects."""
    with store._transaction(readonly=True) as db:
        _active_human(db, human_id)
        rows = db.execute(
            "SELECT m.project_id,count(*) AS message_count,max(m.rowid) AS latest_rowid "
            "FROM messages m "
            "JOIN project_spaces ps ON ps.project_id=m.project_id "
            "JOIN spaces s ON s.id=ps.space_id "
            "JOIN project_human_grants g ON g.project_id=m.project_id "
            "AND g.human_id=? AND g.revoked_at IS NULL "
            "JOIN space_memberships sm ON sm.space_id=s.id "
            "AND sm.human_id=? AND sm.revoked_at IS NULL "
            "WHERE m.recipient_id IS NULL AND (s.kind='shared' OR s.owner_human_id=?) "
            "GROUP BY m.project_id ORDER BY m.project_id",
            (human_id, human_id, human_id),
        ).fetchall()
        return tuple(tuple(row) for row in rows)


def visible_approved_resource_state_for_human(store, human_id):
    """Internal change marker for approved versions in readable projects."""
    with store._transaction(readonly=True) as db:
        _active_human(db, human_id)
        rows = db.execute(
            "SELECT v.project_id,count(*) AS approval_count,max(a.rowid) AS latest_approval "
            "FROM resource_approvals a "
            "JOIN resource_versions v ON v.id=a.version_id "
            "JOIN project_spaces ps ON ps.project_id=v.project_id "
            "JOIN spaces s ON s.id=ps.space_id "
            "JOIN project_human_grants g ON g.project_id=v.project_id "
            "AND g.human_id=? AND g.revoked_at IS NULL "
            "JOIN space_memberships sm ON sm.space_id=s.id "
            "AND sm.human_id=? AND sm.revoked_at IS NULL "
            "WHERE s.kind='shared' OR s.owner_human_id=? "
            "GROUP BY v.project_id ORDER BY v.project_id",
            (human_id, human_id, human_id),
        ).fetchall()
        return tuple(tuple(row) for row in rows)


def task_counts_for_human(store, human_id, project_id):
    """Calculate one project's counts only after checking the same snapshot's ACL."""
    with store._transaction(readonly=True) as db:
        require_project_role(db, human_id, project_id)
        rows = db.execute(
            "SELECT status,count(*) AS count FROM tasks WHERE project_id=? GROUP BY status",
            (project_id,),
        ).fetchall()
        return {"counts": {row["status"]: row["count"] for row in rows}}


def review_material_for_human(store, human_id, project_id, task_id):
    """Expose one pending delivery to a current project Human reviewer."""
    with store._transaction(readonly=True) as db:
        require_review_authority(db, human_id, project_id)
        task = db.execute(
            "SELECT id,title,status,result,execution_mode FROM tasks WHERE id=? AND project_id=?",
            (task_id, project_id),
        ).fetchone()
        if task is None:
            raise WorkbenchError("TASK_NOT_FOUND", "任务不存在或没有访问权限。")
        if task["status"] != "review":
            raise WorkbenchError("INVALID_STATE", "任务尚未提交交付，不能验收。")
        delivery = (
            db.execute("SELECT payload FROM task_deliveries WHERE task_id=?", (task_id,)).fetchone()
            if task["execution_mode"] == "managed"
            else None
        )
        payload = json.loads(delivery["payload"]) if delivery else {}
        return store._scrub(
            db,
            {
                "task_id": task_id,
                "title": task["title"],
                "status": task["status"],
                "summary": payload.get("summary", task["result"]),
                "verification": payload.get(
                    "verification",
                    {
                        "status": "employee_report"
                        if task["execution_mode"] == "mailbox"
                        else "not_verified",
                    },
                ),
                "diff": payload.get("diff", ""),
                "files": payload.get("files", []),
                "capture_error": payload.get("capture_error"),
            },
        )


def list_team_messages_for_human(store, human_id, project_id):
    """Show only project-wide content, without private mail or host internals."""
    with store._transaction(readonly=True) as db:
        require_project_role(db, human_id, project_id)
        rows = db.execute(
            "SELECT id,title,body,sender_id,reply_to,thread_id,task_id,created_at "
            "FROM messages WHERE project_id=? AND recipient_id IS NULL "
            "ORDER BY created_at DESC,rowid DESC LIMIT 100",
            (project_id,),
        ).fetchall()
        messages = [dict(row) for row in rows]
        return {"messages": store._scrub(db, messages), "viewing_acknowledges": False}


def search_memories_for_human(store, human_id, project_id, query):
    """Search literal text in exactly one authorized project, with a hard row cap."""
    q = _text(query, "搜索内容", 1000, empty=True)
    needle = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with store._transaction(readonly=True) as db:
        require_project_role(db, human_id, project_id)
        rows = db.execute(
            "SELECT * FROM memories WHERE project_id=? "
            "AND (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\') "
            "ORDER BY created_at,rowid LIMIT 100",
            (project_id, needle, needle),
        ).fetchall()
        return store._scrub(db, [store._entity(row) for row in rows])


def resource_manifest_for_human(store, human_id, project_id):
    """Expose approved versions, not draft IDs or canonical unredacted hashes."""
    from .workbench_resources import manifest

    with store._transaction(readonly=True) as db:
        require_project_role(db, human_id, project_id)
        approved = manifest(store, db, project_id)
        return {
            "resources": [
                {
                    "resource_id": item["resource_id"],
                    "version_id": item["version_id"],
                    "content_sha256": item["content_sha256"],
                    "redacted_since_capture": item["redacted_since_capture"],
                }
                for item in approved["resources"]
            ]
        }


def approved_resource_version_for_human(store, human_id, project_id, resource_id, version_id):
    """Read an approved immutable snapshot, never a live local path or draft."""
    from .workbench_resources import digest

    with store._transaction(readonly=True) as db:
        require_project_role(db, human_id, project_id)
        row = db.execute(
            "SELECT v.content,v.content_sha256 FROM resource_versions v "
            "JOIN resource_approvals a ON a.version_id=v.id "
            "WHERE v.project_id=? AND v.resource_id=? AND v.id=?",
            (project_id, resource_id, version_id),
        ).fetchone()
        if row is None:
            raise WorkbenchError("RESOURCE_VERSION_NOT_FOUND", "已批准的资料版本不存在。")
        content = store._scrub(db, row["content"])
        return {
            "resource_id": resource_id,
            "version_id": version_id,
            "content": content,
            "content_sha256": digest(content),
            "redacted_since_capture": digest(content) != row["content_sha256"],
        }


def create_human(store, display_name):
    name = _text(display_name, "Human 名称", 200).strip()
    with store._transaction() as db:
        human_id, space_id, now = _id("human"), _id("space"), _now()
        db.execute("INSERT INTO humans VALUES(?,?,?,NULL)", (human_id, name, now))
        db.execute(
            "INSERT INTO spaces VALUES(?,?,?,?,?)",
            (space_id, name + " · 私人空间", "private", human_id, now),
        )
        db.execute(
            "INSERT INTO space_memberships VALUES(?,?,?,?,NULL)", (space_id, human_id, "owner", now)
        )
        return {"human_id": human_id, "private_space_id": space_id}


def _issue_session(db, human_id):
    _active_human(db, human_id)
    token = "amh1_" + secrets.token_urlsafe(32)
    session_id = _id("human_session")
    created_at = _now()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(
        timespec="microseconds"
    )
    db.execute(
        "INSERT INTO human_sessions VALUES(?,?,?,?,?,NULL)",
        (session_id, human_id, hashlib.sha256(token.encode()).hexdigest(), created_at, expires_at),
    )
    return {"session_id": session_id, "token": token, "expires_at": expires_at}


def issue_human_session(store, human_id):
    """Internal authenticated issuance; HTTP owner route uses the narrower wrapper."""
    with store._transaction() as db:
        return _issue_session(db, human_id)


def issue_local_owner_session(store, human_id):
    """The legacy owner token must not impersonate another Human."""
    with store._transaction(readonly=True) as db:
        row = db.execute("SELECT owner_human_id FROM human_bootstrap WHERE singleton=1").fetchone()
        if row is None or (human_id is not None and human_id != row[0]):
            raise WorkbenchError(
                "HUMAN_ENROLLMENT_REQUIRED", "其他 Human 须由本人完成独立身份认领。"
            )
    return issue_human_session(store, row[0])


def create_human_invitation(store, human_id):
    """Local owner may invite, but cannot re-enroll an already claimed Human."""
    with store._transaction() as db:
        _active_human(db, human_id)
        row = db.execute(
            "SELECT claimed_at FROM human_enrollments WHERE human_id=?", (human_id,)
        ).fetchone()
        prior_session = db.execute(
            "SELECT 1 FROM human_sessions WHERE human_id=? LIMIT 1", (human_id,)
        ).fetchone()
        if (row is not None and row["claimed_at"] is not None) or prior_session is not None:
            raise WorkbenchError(
                "HUMAN_ALREADY_ENROLLED", "该 Human 已认领，不能由管理员重新签发。"
            )
        invitation = "ami1_" + secrets.token_urlsafe(32)
        expiry = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(
            timespec="microseconds"
        )
        db.execute(
            "INSERT INTO human_enrollments VALUES(?,?,?,NULL,NULL) "
            "ON CONFLICT(human_id) DO UPDATE SET "
            "invitation_sha256=excluded.invitation_sha256,expires_at=excluded.expires_at",
            (human_id, hashlib.sha256(invitation.encode()).hexdigest(), expiry),
        )
        return {"human_id": human_id, "invitation": invitation, "expires_at": expiry}


def claim_human_invitation(store, invitation):
    """Consume an invitation exactly once and hand the Human a recovery secret."""
    if (
        not isinstance(invitation, str)
        or not invitation.startswith("ami1_")
        or len(invitation) > 128
    ):
        raise WorkbenchError("UNAUTHORIZED", "邀请无效或已过期。")
    digest = hashlib.sha256(invitation.encode()).hexdigest()
    with store._transaction() as db:
        row = db.execute(
            "SELECT human_id FROM human_enrollments WHERE invitation_sha256=? "
            "AND claimed_at IS NULL AND expires_at>?",
            (digest, _now()),
        ).fetchone()
        if row is None:
            raise WorkbenchError("UNAUTHORIZED", "邀请无效或已过期。")
        human_id = row["human_id"]
        _active_human(db, human_id)
        recovery_code = "amr1_" + secrets.token_urlsafe(32)
        db.execute(
            "UPDATE human_enrollments SET claimed_at=?,recovery_sha256=? WHERE human_id=?",
            (_now(), hashlib.sha256(recovery_code.encode()).hexdigest(), human_id),
        )
        return {
            **_issue_session(db, human_id),
            "human_id": human_id,
            "recovery_code": recovery_code,
        }


def recover_human_session(store, human_id, recovery_code):
    """A one-use recovery secret rotates all sessions and itself atomically."""
    if (
        not isinstance(recovery_code, str)
        or not recovery_code.startswith("amr1_")
        or len(recovery_code) > 128
    ):
        raise WorkbenchError("UNAUTHORIZED", "恢复凭据无效。")
    digest = hashlib.sha256(recovery_code.encode()).hexdigest()
    with store._transaction() as db:
        row = db.execute(
            "SELECT 1 FROM human_enrollments WHERE human_id=? AND claimed_at IS NOT NULL "
            "AND recovery_sha256=?",
            (human_id, digest),
        ).fetchone()
        if row is None:
            raise WorkbenchError("UNAUTHORIZED", "恢复凭据无效。")
        _active_human(db, human_id)
        replacement = "amr1_" + secrets.token_urlsafe(32)
        db.execute(
            "UPDATE human_enrollments SET recovery_sha256=? WHERE human_id=?",
            (hashlib.sha256(replacement.encode()).hexdigest(), human_id),
        )
        db.execute(
            "UPDATE human_sessions SET revoked_at=? WHERE human_id=? AND revoked_at IS NULL",
            (_now(), human_id),
        )
        return {**_issue_session(db, human_id), "human_id": human_id, "recovery_code": replacement}


def setup_human_recovery(store, human_id):
    """An authenticated legacy Human records their own one-time recovery code."""
    with store._transaction() as db:
        _active_human(db, human_id)
        row = db.execute(
            "SELECT recovery_sha256 FROM human_enrollments WHERE human_id=?", (human_id,)
        ).fetchone()
        if row is not None and row["recovery_sha256"] is not None:
            raise WorkbenchError("RECOVERY_ALREADY_SET", "恢复码已设置；请用现有恢复码轮换。")
        recovery_code = "amr1_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(recovery_code.encode()).hexdigest()
        now = _now()
        if row is None:
            # No invitation is issued here; the marker cannot be claimed by an owner.
            disabled_invitation = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
            db.execute(
                "INSERT INTO human_enrollments VALUES(?,?,?,?,?)",
                (human_id, disabled_invitation, now, now, digest),
            )
        else:
            db.execute(
                "UPDATE human_enrollments SET claimed_at=?,recovery_sha256=? WHERE human_id=?",
                (now, digest, human_id),
            )
        return {"recovery_code": recovery_code}


def rotate_human_session(store, session_id, human_id):
    """An active Human renews their own bearer while invalidating the old one."""
    with store._transaction() as db:
        row = db.execute(
            "SELECT 1 FROM human_sessions WHERE id=? AND human_id=? "
            "AND revoked_at IS NULL AND expires_at>?",
            (session_id, human_id, _now()),
        ).fetchone()
        if row is None:
            raise WorkbenchError("UNAUTHORIZED", "Human 会话无效或已失效。")
        replacement = _issue_session(db, human_id)
        db.execute("UPDATE human_sessions SET revoked_at=? WHERE id=?", (_now(), session_id))
        return replacement


def authenticate_human_session(store, token):
    """Resolve one current session, including Human disable and revocation checks."""
    if not isinstance(token, str) or not token.startswith("amh1_") or len(token) > 128:
        raise WorkbenchError("UNAUTHORIZED", "Human 会话无效或已失效。")
    digest = hashlib.sha256(token.encode()).hexdigest()
    with store._transaction(readonly=True) as db:
        row = db.execute(
            "SELECT hs.id,hs.human_id,h.display_name FROM human_sessions hs "
            "JOIN humans h ON h.id=hs.human_id AND h.disabled_at IS NULL "
            "WHERE hs.token_sha256=? AND hs.revoked_at IS NULL AND hs.expires_at>?",
            (digest, _now()),
        ).fetchone()
        if row is None:
            raise WorkbenchError("UNAUTHORIZED", "Human 会话无效或已失效。")
        return dict(row)


def revoke_human_session(store, session_id):
    """Owner-only caller invalidates a session before the next scoped request."""
    with store._transaction() as db:
        result = db.execute(
            "UPDATE human_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
            (_now(), session_id),
        )
        return {"revoked": result.rowcount == 1}


def revoke_all_human_sessions(store, human_id):
    """Invalidate all sessions for a lost device or a departing Human."""
    with store._transaction() as db:
        if db.execute("SELECT 1 FROM humans WHERE id=?", (human_id,)).fetchone() is None:
            raise WorkbenchError("HUMAN_UNAVAILABLE", "Human 身份不存在。")
        result = db.execute(
            "UPDATE human_sessions SET revoked_at=? WHERE human_id=? AND revoked_at IS NULL",
            (_now(), human_id),
        )
        return {"revoked_count": result.rowcount}


def create_private_project(store, human_id, name, path):
    """Create a project in this Human's private space, not the local legacy owner's."""
    name = _text(name, "项目名称", 200).strip()
    try:
        location = Path(path).expanduser().resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise WorkbenchError("invalid_path", "项目目录不存在，请选择已有目录。") from exc
    if not location.is_dir():
        raise WorkbenchError("invalid_path", "项目路径需要是目录。")
    with store._transaction() as db:
        _active_human(db, human_id)
        space = db.execute(
            "SELECT s.id FROM spaces s JOIN space_memberships sm "
            "ON sm.space_id=s.id AND sm.human_id=? AND sm.revoked_at IS NULL "
            "WHERE s.kind='private' AND s.owner_human_id=?",
            (human_id, human_id),
        ).fetchone()
        if space is None:
            raise WorkbenchError("PRIVATE_SPACE_REQUIRED", "该 Human 没有有效的私人空间。")
        project_id, now = _id("project"), _now()
        db.execute("INSERT INTO projects VALUES(?,?,?,?)", (project_id, name, str(location), now))
        db.execute("INSERT INTO project_spaces VALUES(?,?,?)", (project_id, space[0], now))
        db.execute(
            "INSERT INTO project_human_grants VALUES(?,?,?,?,NULL)",
            (project_id, human_id, "owner", now),
        )
        return {"project_id": project_id, "space_id": space[0]}


def create_managed_private_project(store, human_id, name):
    """Create a private project in an app-owned directory, never a client path."""
    name = _text(name, "项目名称", 200).strip()
    directory = store.directory / "human-projects" / human_id / secrets.token_hex(16)
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    try:
        return create_private_project(store, human_id, name, directory)
    except BaseException:
        directory.rmdir()
        raise


def create_shared_space(store, owner_human_id, name):
    name = _text(name, "空间名称", 200).strip()
    with store._transaction() as db:
        _active_human(db, owner_human_id)
        space_id, now = _id("space"), _now()
        db.execute(
            "INSERT INTO spaces VALUES(?,?,?,?,?)", (space_id, name, "shared", owner_human_id, now)
        )
        db.execute(
            "INSERT INTO space_memberships VALUES(?,?,?,?,NULL)",
            (space_id, owner_human_id, "owner", now),
        )
        return {"space_id": space_id}


def add_space_member(store, actor_human_id, space_id, target_human_id):
    with store._transaction() as db:
        _active_human(db, actor_human_id)
        _active_human(db, target_human_id)
        space = db.execute(
            "SELECT kind,owner_human_id FROM spaces WHERE id=?", (space_id,)
        ).fetchone()
        if space is None or space["kind"] != "shared" or space["owner_human_id"] != actor_human_id:
            raise WorkbenchError("SPACE_OWNER_REQUIRED", "只有共享空间所有者可以邀请成员。")
        db.execute(
            "INSERT INTO space_memberships VALUES(?,?,?,?,NULL) "
            "ON CONFLICT(space_id,human_id) DO UPDATE SET revoked_at=NULL",
            (space_id, target_human_id, "member", _now()),
        )


def move_project_to_shared_space(store, actor_human_id, project_id, target_space_id):
    with store._transaction() as db:
        require_project_role(db, actor_human_id, project_id, ("owner",))
        space = db.execute(
            "SELECT kind,owner_human_id FROM spaces WHERE id=?", (target_space_id,)
        ).fetchone()
        if space is None or space["kind"] != "shared" or space["owner_human_id"] != actor_human_id:
            raise WorkbenchError("SPACE_OWNER_REQUIRED", "只能移入自己管理的共享空间。")
        # Grants from the old scope must not revive when a former collaborator
        # later joins the destination space. Only the owner's grant survives.
        db.execute(
            "UPDATE project_human_grants SET revoked_at=? WHERE project_id=? "
            "AND human_id!=? AND revoked_at IS NULL",
            (_now(), project_id, actor_human_id),
        )
        db.execute(
            "UPDATE project_human_reviewers SET revoked_at=? WHERE project_id=? "
            "AND revoked_at IS NULL",
            (_now(), project_id),
        )
        db.execute(
            "UPDATE project_spaces SET space_id=? WHERE project_id=?", (target_space_id, project_id)
        )
        # Existing project grants are deliberately not expanded to all space members.


def grant_project(store, actor_human_id, project_id, target_human_id, role):
    if role not in {"editor", "viewer"}:
        raise WorkbenchError("INVALID_ROLE", "请选择项目编辑者或查看者。")
    if actor_human_id == target_human_id:
        raise WorkbenchError("SOLE_OWNER_REQUIRED", "项目所有者不能降低自己的权限。")
    with store._transaction() as db:
        require_project_role(db, actor_human_id, project_id, ("owner",))
        _active_human(db, target_human_id)
        scope = _project_scope(db, project_id)
        if scope["kind"] != "shared":
            raise WorkbenchError("PRIVATE_PROJECT", "私人项目须先明确移入共享空间。")
        member = db.execute(
            "SELECT 1 FROM space_memberships WHERE space_id=? AND human_id=? "
            "AND revoked_at IS NULL",
            (scope["space_id"], target_human_id),
        ).fetchone()
        if member is None:
            raise WorkbenchError("SPACE_MEMBER_REQUIRED", "请先邀请对方加入共享空间。")
        now = _now()
        db.execute(
            "INSERT INTO project_human_grants VALUES(?,?,?,?,NULL) "
            "ON CONFLICT(project_id,human_id) DO UPDATE SET role=excluded.role,revoked_at=NULL",
            (project_id, target_human_id, role, now),
        )


def grant_review_authority(store, actor_human_id, project_id, target_human_id):
    with store._transaction() as db:
        require_project_role(db, actor_human_id, project_id, ("owner",))
        if actor_human_id == target_human_id:
            raise WorkbenchError("SOLE_OWNER_REQUIRED", "项目所有者已拥有验收权。")
        require_project_role(db, target_human_id, project_id, ("editor", "viewer"))
        db.execute(
            "INSERT INTO project_human_reviewers VALUES(?,?,?,NULL) "
            "ON CONFLICT(project_id,human_id) DO UPDATE SET revoked_at=NULL",
            (project_id, target_human_id, _now()),
        )


def revoke_review_authority(store, actor_human_id, project_id, target_human_id):
    with store._transaction() as db:
        require_project_role(db, actor_human_id, project_id, ("owner",))
        if actor_human_id == target_human_id:
            raise WorkbenchError("SOLE_OWNER_REQUIRED", "项目所有者的验收权不能在此撤销。")
        db.execute(
            "UPDATE project_human_reviewers SET revoked_at=? "
            "WHERE project_id=? AND human_id=? AND revoked_at IS NULL",
            (_now(), project_id, target_human_id),
        )


def revoke_project(store, actor_human_id, project_id, target_human_id):
    with store._transaction() as db:
        require_project_role(db, actor_human_id, project_id, ("owner",))
        if actor_human_id == target_human_id:
            raise WorkbenchError("SOLE_OWNER_REQUIRED", "项目所有者不能撤销自己。")
        db.execute(
            "UPDATE project_human_grants SET revoked_at=? "
            "WHERE project_id=? AND human_id=? AND revoked_at IS NULL",
            (_now(), project_id, target_human_id),
        )
        db.execute(
            "UPDATE project_human_reviewers SET revoked_at=? "
            "WHERE project_id=? AND human_id=? AND revoked_at IS NULL",
            (_now(), project_id, target_human_id),
        )


def revoke_space_member(store, actor_human_id, space_id, target_human_id):
    with store._transaction() as db:
        _active_human(db, actor_human_id)
        space = db.execute(
            "SELECT kind,owner_human_id FROM spaces WHERE id=?", (space_id,)
        ).fetchone()
        if space is None or space["kind"] != "shared" or space["owner_human_id"] != actor_human_id:
            raise WorkbenchError("SPACE_OWNER_REQUIRED", "只有共享空间所有者可以移除成员。")
        if actor_human_id == target_human_id:
            raise WorkbenchError("SOLE_OWNER_REQUIRED", "空间所有者不能移除自己。")
        db.execute(
            "UPDATE space_memberships SET revoked_at=? "
            "WHERE space_id=? AND human_id=? AND revoked_at IS NULL",
            (_now(), space_id, target_human_id),
        )
        # Removing a member must also erase their per-project grants. Rejoining
        # the space later cannot silently reactivate old project access.
        db.execute(
            "UPDATE project_human_grants SET revoked_at=? WHERE human_id=? "
            "AND revoked_at IS NULL AND project_id IN "
            "(SELECT project_id FROM project_spaces WHERE space_id=?)",
            (_now(), target_human_id, space_id),
        )
        db.execute(
            "UPDATE project_human_reviewers SET revoked_at=? WHERE human_id=? "
            "AND revoked_at IS NULL AND project_id IN "
            "(SELECT project_id FROM project_spaces WHERE space_id=?)",
            (_now(), target_human_id, space_id),
        )
