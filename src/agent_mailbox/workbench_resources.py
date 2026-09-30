"""Immutable, scrubbed project resource revisions and explicit human approval."""

from __future__ import annotations

import hashlib
import json
import re

from .workbench_store import WorkbenchError, _id, _now, _text

RESOURCE_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS task_resource_manifests (
        task_id TEXT PRIMARY KEY REFERENCES tasks(id), run_id TEXT NOT NULL,
        manifest TEXT NOT NULL, created_at TEXT NOT NULL
    )""",
    """CREATE TRIGGER IF NOT EXISTS immutable_task_resource_manifest_update
        BEFORE UPDATE ON task_resource_manifests BEGIN
        SELECT RAISE(ABORT,'immutable task resource manifest'); END""",
    """CREATE TRIGGER IF NOT EXISTS immutable_task_resource_manifest_delete
        BEFORE DELETE ON task_resource_manifests BEGIN
        SELECT RAISE(ABORT,'immutable task resource manifest'); END""",
    """CREATE TABLE IF NOT EXISTS resource_versions (
        id TEXT PRIMARY KEY, resource_id TEXT NOT NULL REFERENCES resources(id),
        project_id TEXT NOT NULL REFERENCES projects(id), ordinal INTEGER NOT NULL,
        content TEXT NOT NULL, content_sha256 TEXT NOT NULL, summary TEXT NOT NULL,
        source TEXT NOT NULL, employee_id TEXT REFERENCES employees(id), created_at TEXT NOT NULL,
        UNIQUE(resource_id,ordinal)
    )""",
    """CREATE TABLE IF NOT EXISTS resource_approvals (
        version_id TEXT PRIMARY KEY REFERENCES resource_versions(id), approved_at TEXT NOT NULL
    )""",
    """CREATE TRIGGER IF NOT EXISTS immutable_resource_versions_update
        BEFORE UPDATE ON resource_versions BEGIN
        SELECT RAISE(ABORT,'immutable resource revision'); END""",
    """CREATE TRIGGER IF NOT EXISTS immutable_resource_versions_delete
        BEFORE DELETE ON resource_versions BEGIN
        SELECT RAISE(ABORT,'immutable resource revision'); END""",
    """CREATE TRIGGER IF NOT EXISTS immutable_resource_approval_update
        BEFORE UPDATE ON resource_approvals BEGIN
        SELECT RAISE(ABORT,'immutable resource approval'); END""",
    """CREATE TRIGGER IF NOT EXISTS immutable_resource_approval_delete
        BEFORE DELETE ON resource_approvals BEGIN
        SELECT RAISE(ABORT,'immutable resource approval'); END""",
)


def scrub_credentials(content):
    """Remove recognizable credential material before immutable persistence.

    Opaque values without credential context cannot be classified as secrets.
    This complements store credential redaction; it never calls an LLM.
    """
    content = re.sub(
        r"-----BEGIN ([A-Z ]*PRIVATE KEY)-----.*?-----END \1-----",
        "[redacted private key]",
        content,
        flags=re.DOTALL,
    )
    content = re.sub(
        r"(?im)([\"']?(?:[A-Z][A-Z0-9_]*_)?(?:API_KEY|ACCESS_TOKEN|AUTH_TOKEN|SECRET_TOKEN|PASSWORD|CLIENT_SECRET|SECRET)[\"']?\s*[:=]\s*)"
        r"(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s,;#\n]+)",
        r"\1[redacted]",
        content,
    )
    content = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}", r"\1[redacted]", content)
    return re.sub(r"\bsk-[A-Za-z0-9_-]{20,}\b", "[redacted]", content)


def digest(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def resource(store, db, project_id, resource_id):
    store._required(db, "projects", project_id)
    row = store._required(db, "resources", resource_id)
    if row["project_id"] != project_id:
        raise WorkbenchError("permission_denied", "该资源不属于当前项目。")
    return row


def revision(store, db, project_id, resource_id, version_id):
    resource(store, db, project_id, resource_id)
    row = store._required(db, "resource_versions", version_id)
    if row["project_id"] != project_id or row["resource_id"] != resource_id:
        raise WorkbenchError("permission_denied", "该版本不属于当前资源。")
    if digest(row["content"]) != row["content_sha256"]:
        raise WorkbenchError("resource_integrity_error", "资料版本校验失败，请恢复可信备份。")
    return row


def metadata(store, db, row):
    approval = db.execute(
        "SELECT approved_at FROM resource_approvals WHERE version_id=?", (row["id"],)
    ).fetchone()
    result = {
        key: row[key]
        for key in (
            "id",
            "resource_id",
            "project_id",
            "ordinal",
            "content_sha256",
            "summary",
            "source",
            "employee_id",
            "created_at",
        )
    }
    result.update(
        status="approved" if approval else "proposed",
        created_by=row["employee_id"] or "human",
        approved_by="human" if approval else None,
        approved_at=approval["approved_at"] if approval else None,
    )
    return store._scrub(db, result)


def versions(store, project_id, resource_id):
    with store._connection() as db:
        resource(store, db, project_id, resource_id)
        rows = db.execute(
            "SELECT * FROM resource_versions WHERE resource_id=? ORDER BY ordinal DESC",
            (resource_id,),
        ).fetchall()
        for row in rows:
            revision(store, db, project_id, resource_id, row["id"])
        return {"versions": [metadata(store, db, row) for row in rows]}


def capture(store, project_id, resource_id, summary="", employee_id=None, content=None):
    summary = _text(summary, "版本说明", 2000, empty=True)
    if content is not None:
        if employee_id is None:
            raise WorkbenchError(
                "permission_denied",
                "文本提案需要已登记的项目员工身份；人类请从资料源文件捕获版本。",
            )
        content = _text(content, "提案文本", 256 * 1024, empty=True)
    with store._transaction() as db:
        row = resource(store, db, project_id, resource_id)
        if employee_id is not None:
            store._message_member(db, project_id, employee_id)
        if content is None:
            location, content = store._resource_file(row["path"])
            source = str(location)
        else:
            source = f"employee:{employee_id}:submitted_text"
        values = store._scrub(db, {"content": content, "summary": summary, "source": source})
        values["content"] = scrub_credentials(values["content"])
        values["summary"] = scrub_credentials(values["summary"])
        content_hash = digest(values["content"])
        previous = db.execute(
            "SELECT * FROM resource_versions WHERE resource_id=? ORDER BY ordinal DESC LIMIT 1",
            (resource_id,),
        ).fetchone()
        if (
            previous
            and previous["content_sha256"] == content_hash
            and previous["summary"] == values["summary"]
            and previous["employee_id"] == employee_id
            and previous["source"] == values["source"]
        ):
            revision(store, db, project_id, resource_id, previous["id"])
            return metadata(store, db, previous)
        ordinal = db.execute(
            "SELECT COALESCE(MAX(ordinal),0)+1 FROM resource_versions WHERE resource_id=?",
            (resource_id,),
        ).fetchone()[0]
        version_id, created_at = _id("revision"), _now()
        db.execute(
            "INSERT INTO resource_versions VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                version_id,
                resource_id,
                project_id,
                ordinal,
                values["content"],
                content_hash,
                values["summary"],
                values["source"],
                employee_id,
                created_at,
            ),
        )
        if employee_id is None:
            db.execute("INSERT INTO resource_approvals VALUES(?,?)", (version_id, created_at))
        event_payload = {
            "resource_id": resource_id,
            "version_id": version_id,
            "content_sha256": content_hash,
            "summary": values["summary"],
        }
        store._governance(
            db,
            "resource_version_created",
            project_id=project_id,
            employee_id=employee_id,
            actor=f"employee:{employee_id}" if employee_id else "human",
            payload=event_payload,
        )
        if employee_id is None:
            store._governance(
                db, "resource_version_approved", project_id=project_id, payload=event_payload
            )
        return metadata(store, db, store._required(db, "resource_versions", version_id))


def read(store, project_id, resource_id, version_id):
    with store._connection() as db:
        row = revision(store, db, project_id, resource_id, version_id)
        result = store._scrub(
            db,
            {
                "resource": store._entity(resource(store, db, project_id, resource_id)),
                "content": row["content"],
                "source": row["source"],
            },
        )
        returned_hash = digest(result["content"])
        result["version"] = metadata(store, db, row)
        result["provenance"] = {
            "mode": "snapshot",
            "version_id": row["id"],
            "created_at": row["created_at"],
            "content_sha256": returned_hash,
            "canonical_sha256": row["content_sha256"],
            "redacted_since_capture": returned_hash != row["content_sha256"],
        }
        return result


def approve(store, project_id, resource_id, version_id):
    with store._transaction() as db:
        row = revision(store, db, project_id, resource_id, version_id)
        inserted = db.execute(
            "INSERT OR IGNORE INTO resource_approvals VALUES(?,?)", (version_id, _now())
        )
        if inserted.rowcount:
            store._governance(
                db,
                "resource_version_approved",
                project_id=project_id,
                employee_id=row["employee_id"],
                payload={
                    "resource_id": resource_id,
                    "version_id": version_id,
                    "content_sha256": row["content_sha256"],
                    "summary": row["summary"],
                },
            )
        return metadata(store, db, row)


def manifest(store, db, project_id):
    store._required(db, "projects", project_id)
    rows = db.execute(
        "SELECT v.* FROM resource_versions v JOIN resource_approvals a "
        "ON a.version_id=v.id WHERE v.project_id=? AND v.ordinal=(SELECT MAX(v2.ordinal) "
        "FROM resource_versions v2 JOIN resource_approvals a2 ON a2.version_id=v2.id "
        "WHERE v2.resource_id=v.resource_id) ORDER BY v.resource_id",
        (project_id,),
    ).fetchall()
    items = []
    for row in rows:
        revision(store, db, project_id, row["resource_id"], row["id"])
        returned_hash = digest(store._scrub(db, row["content"]))
        items.append(
            {
                "resource_id": row["resource_id"],
                "version_id": row["id"],
                "content_sha256": returned_hash,
                "canonical_sha256": row["content_sha256"],
                "redacted_since_capture": returned_hash != row["content_sha256"],
            }
        )
    approved_ids = {item["resource_id"] for item in items}
    missing = [
        {"resource_id": row["id"], "reason": "no_approved_version"}
        for row in db.execute(
            "SELECT id FROM resources WHERE project_id=? ORDER BY id", (project_id,)
        )
        if row["id"] not in approved_ids
    ]
    body = {"resources": items, "missing_resources": missing}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return {**body, "manifest_sha256": digest(canonical)}


def freeze(store, db, task):
    result = manifest(store, db, task["project_id"])
    created_at = _now()
    db.execute(
        "INSERT INTO task_resource_manifests VALUES(?,?,?,?)",
        (task["id"], task["run_id"], json.dumps(result, ensure_ascii=False), created_at),
    )
    store._event(db, task["id"], "resources_pinned", "任务资料版本已固定。", result)


def execution_manifest(store, task_id, run_id):
    with store._connection() as db:
        task = store._required(db, "tasks", task_id)
        if task["run_id"] != run_id:
            raise WorkbenchError("permission_denied", "这次执行已过期。")
        row = db.execute(
            "SELECT * FROM task_resource_manifests WHERE task_id=? AND run_id=?", (task_id, run_id)
        ).fetchone()
        if row is None:
            raise WorkbenchError(
                "resource_manifest_missing",
                "该任务没有启动时的资料版本记录，请创建新任务以固定资料。",
            )
        try:
            result = json.loads(row["manifest"])
            expected = result["manifest_sha256"]
            body = {key: result[key] for key in ("resources", "missing_resources")}
        except (TypeError, ValueError, KeyError) as exc:
            raise WorkbenchError(
                "resource_integrity_error", "任务资料清单格式损坏，请恢复可信备份。"
            ) from exc
        if digest(json.dumps(body, sort_keys=True, separators=(",", ":"))) != expected:
            raise WorkbenchError(
                "resource_integrity_error", "任务资料清单校验失败，请恢复可信备份。"
            )
        return {
            **result,
            "task_id": task_id,
            "run_id": run_id,
            "captured_at": row["created_at"],
            "capture_mode": "at_claim",
        }


def execution_read(store, project_id, resource_id, task_id, run_id, version_id="", live=False):
    if not isinstance(live, bool):
        raise WorkbenchError("invalid_field", "实时读取选项需要是布尔值。")
    with store._connection() as db:
        task = store._required(db, "tasks", task_id)
        if task["project_id"] != project_id or task["run_id"] != run_id:
            raise WorkbenchError("permission_denied", "该任务不属于当前项目或执行已过期。")
        resource(store, db, project_id, resource_id)
    if live:
        result = store.read_resource(project_id, resource_id)
        result["provenance"]["explicit_override"] = True
        return result
    frozen = execution_manifest(store, task_id, run_id)
    pinned = next(
        (item for item in frozen["resources"] if item["resource_id"] == resource_id), None
    )
    explicit = bool(version_id)
    if not version_id:
        if pinned is None:
            raise WorkbenchError(
                "resource_unversioned",
                "任务启动时这份资料没有已批准版本，请先批准资料并创建新任务；实时读取需显式选择。",
            )
        version_id = pinned["version_id"]
    result = read(store, project_id, resource_id, version_id)
    result["provenance"].update(
        task_id=task_id,
        run_id=run_id,
        explicit_override=explicit,
        manifest_sha256=frozen["manifest_sha256"],
        matches_frozen_content=bool(
            pinned
            and pinned["version_id"] == version_id
            and pinned["content_sha256"] == result["provenance"]["content_sha256"]
        ),
    )
    return result
