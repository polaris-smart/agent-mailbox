"""Private, transactional project state for the browser workbench.

This database is deliberately independent of the legacy mailbox. Connections
are short lived, so workers, browser requests and MCP sessions can share it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import socket
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


class WorkbenchError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


ACTIVE = frozenset({"starting", "running", "waiting_approval"})
FINISH = frozenset({"review", "failed", "cancelled", "interrupted"})
MAX_TEXT = 1024 * 1024
SCHEMA_VERSION = 9
EMPLOYEE_KINDS = frozenset(
    {
        "codex",
        "claude",
        "gemini",
        "opencode",
        "zcode",
        "hermes",
        "workbuddy",
        "deepseek",
        "qwen",
        "doubao",
        "coze",
        "ima",
        "cursor",
        "windsurf",
        "trae",
        "aider",
        "qoder",
        "kiro",
        "ollama",
    }
)
CONNECTION_TYPES = frozenset({"cli", "app", "endpoint"})
AUTH_STATUSES = frozenset({"authenticated", "auth_required", "unknown", "not_checked"})
MAX_REQUEST_CHAIN = 4
PERMISSION_TIMEOUT = 120
LIFECYCLES = frozenset({"active", "paused", "retired"})
EMPLOYEE_STATUSES = frozenset({"installed", "auth_required", "available", "unavailable", "unknown"})
SECRET_KEYS = frozenset(
    {"token", "secret_token", "tool_token", "password", "authorization", "api_key", "access_token"}
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _id(kind: str) -> str:
    return f"{kind}_{uuid4().hex}"


def _text(value: Any, field: str, limit: int = MAX_TEXT, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise WorkbenchError("invalid_field", f"请填写有效的{field}。")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise WorkbenchError("invalid_field", f"{field}包含无效字符。") from exc
    if "\x00" in value or size > limit:
        raise WorkbenchError("invalid_field", f"{field}过长或包含无效字符。")
    return value


def _choice(value: Any, allowed: set | frozenset, message: str) -> None:
    if not isinstance(value, str) or value not in allowed:
        raise WorkbenchError("invalid_field", message)


def _json(value: Any) -> str:
    try:
        result = json.dumps(value, ensure_ascii=False, allow_nan=False)
        size = len(result.encode("utf-8"))
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeEncodeError) as exc:
        raise WorkbenchError("invalid_field", "内容需要是有效的 JSON 数据。") from exc
    if size > MAX_TEXT:
        raise WorkbenchError("payload_too_large", "内容超过 1 MiB，请缩小后重试。")
    return result


SCHEMA = """
CREATE TABLE IF NOT EXISTS update_settings (
    id INTEGER PRIMARY KEY CHECK(id=1), channel TEXT NOT NULL DEFAULT 'stable',
    paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1))
);
INSERT OR IGNORE INTO update_settings(id) VALUES(1);
CREATE TABLE IF NOT EXISTS update_claims(id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, is_local INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'online', created_at TEXT NOT NULL, last_seen TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_local_device ON devices(is_local) WHERE is_local = 1;
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS employees (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
    project_id TEXT REFERENCES projects(id),
    node_id TEXT NOT NULL REFERENCES devices(id), status TEXT NOT NULL,
    detail TEXT NOT NULL, secret_token TEXT NOT NULL, created_at TEXT NOT NULL,
    lifecycle TEXT NOT NULL DEFAULT 'active', lifecycle_reason TEXT NOT NULL DEFAULT '',
    lifecycle_changed_at TEXT, connection_type TEXT NOT NULL DEFAULT 'cli',
    entrypoint TEXT NOT NULL DEFAULT '', execution_verified INTEGER NOT NULL DEFAULT 0,
    auth_status TEXT NOT NULL DEFAULT 'unknown'
);
CREATE TABLE IF NOT EXISTS memberships (
    employee_id TEXT NOT NULL REFERENCES employees(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    secret_token TEXT NOT NULL,
    PRIMARY KEY (employee_id, project_id)
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL, prompt TEXT NOT NULL,
    assignee_id TEXT NOT NULL REFERENCES employees(id),
    node_id TEXT NOT NULL REFERENCES devices(id), permission_mode TEXT NOT NULL,
    run_id TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'queued', cancel_requested INTEGER NOT NULL DEFAULT 0,
    result TEXT NOT NULL DEFAULT '', error TEXT, model TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    request_message_id TEXT REFERENCES messages(id), source_task_id TEXT REFERENCES tasks(id),
    tool_token TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_queue ON tasks(node_id, status, created_at);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
    type TEXT NOT NULL, message TEXT NOT NULL, payload TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id, created_at);
CREATE TABLE IF NOT EXISTS permissions (
    task_id TEXT NOT NULL REFERENCES tasks(id), request_id TEXT NOT NULL,
    run_id TEXT NOT NULL, options TEXT NOT NULL, tool_call TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', decision TEXT,
    created_at TEXT NOT NULL, resolved_at TEXT, expires_at TEXT,
    PRIMARY KEY(task_id, request_id)
);
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL, body TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resources (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL, kind TEXT NOT NULL, path TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS governance_events (
    id TEXT PRIMARY KEY, employee_id TEXT REFERENCES employees(id),
    project_id TEXT REFERENCES projects(id), task_id TEXT REFERENCES tasks(id),
    type TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
    payload TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS governance_project ON governance_events(project_id,created_at);
CREATE TABLE IF NOT EXISTS remote_receipts (
    task_id TEXT NOT NULL REFERENCES tasks(id), run_id TEXT NOT NULL,
    digest TEXT NOT NULL, PRIMARY KEY(task_id, run_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL, body TEXT NOT NULL,
    sender_id TEXT REFERENCES employees(id), recipient_id TEXT REFERENCES employees(id),
    reply_to TEXT REFERENCES messages(id), thread_id TEXT NOT NULL,
    request_work INTEGER NOT NULL DEFAULT 0, task_id TEXT REFERENCES tasks(id),
    source_task_id TEXT REFERENCES tasks(id), request_id TEXT, request_digest TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_project ON messages(project_id,created_at);
CREATE UNIQUE INDEX IF NOT EXISTS messages_request ON messages(project_id,COALESCE(sender_id,''),request_id)
    WHERE request_id IS NOT NULL;
"""


class WorkbenchStore:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.directory = self.root / "workbench"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.db_path = self.directory / "state.db"
        # Create privately before SQLite opens the database (no permissive window).
        fd = os.open(self.db_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.db_path.chmod(0o600)
        self.migration_backup_path = None
        with self._connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise WorkbenchError("incompatible_version", "数据来自更新的版本，请升级程序。")
            if version < SCHEMA_VERSION:
                # Rebuilding the legacy NOT NULL employee origin must not
                # rewrite child foreign keys or discard project credentials.
                db.execute("PRAGMA foreign_keys=OFF")
                db.execute("BEGIN IMMEDIATE")
                try:
                    version = db.execute("PRAGMA user_version").fetchone()[0]
                    if version and version < SCHEMA_VERSION:
                        destination = (
                            self.directory
                            / f"state-v{version}-before-v{SCHEMA_VERSION}-{uuid4().hex}.sqlite"
                        )
                        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                        os.close(fd)
                        source = sqlite3.connect(self.db_path)
                        target = sqlite3.connect(destination)
                        try:
                            source.backup(target)
                        finally:
                            source.close()
                            target.close()
                        self.migration_backup_path = destination
                    for statement in SCHEMA.split(";"):
                        if statement.strip():
                            db.execute(statement)
                    from .workbench_resources import RESOURCE_SCHEMA

                    for statement in RESOURCE_SCHEMA:
                        db.execute(statement)
                    from .workbench_onboarding import ONBOARDING_SCHEMA
                    from .workbench_workspaces import WORKSPACE_SCHEMA

                    for statement in (*ONBOARDING_SCHEMA, *WORKSPACE_SCHEMA):
                        db.execute(statement)
                    columns = {row[1] for row in db.execute("PRAGMA table_info(devices)")}
                    if "last_seen" not in columns:
                        db.execute("ALTER TABLE devices ADD COLUMN last_seen TEXT")
                    columns = {row[1] for row in db.execute("PRAGMA table_info(memberships)")}
                    if "secret_token" not in columns:
                        db.execute("ALTER TABLE memberships ADD COLUMN secret_token TEXT")
                        for row in db.execute(
                            "SELECT m.employee_id,m.project_id,e.secret_token FROM memberships m "
                            "JOIN employees e ON e.id=m.employee_id"
                        ).fetchall():
                            db.execute(
                                "UPDATE memberships SET secret_token=? "
                                "WHERE employee_id=? AND project_id=?",
                                (row[2], row[0], row[1]),
                            )
                    columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
                    if "model" not in columns:
                        db.execute("ALTER TABLE tasks ADD COLUMN model TEXT")
                    for column, definition in (
                        ("request_message_id", "TEXT REFERENCES messages(id)"),
                        ("source_task_id", "TEXT REFERENCES tasks(id)"),
                        ("tool_token", "TEXT"),
                    ):
                        if column not in columns:
                            db.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
                    for row in db.execute(
                        "SELECT id FROM tasks WHERE tool_token IS NULL"
                    ).fetchall():
                        db.execute(
                            "UPDATE tasks SET tool_token=? WHERE id=?",
                            (secrets.token_urlsafe(32), row[0]),
                        )
                    columns = {row[1] for row in db.execute("PRAGMA table_info(employees)")}
                    if "lifecycle" not in columns:
                        db.execute(
                            "ALTER TABLE employees ADD COLUMN lifecycle TEXT NOT NULL DEFAULT 'active'"
                        )
                        db.execute(
                            "ALTER TABLE employees ADD COLUMN lifecycle_reason TEXT NOT NULL DEFAULT ''"
                        )
                        db.execute("ALTER TABLE employees ADD COLUMN lifecycle_changed_at TEXT")
                    for column, definition in (
                        ("connection_type", "TEXT NOT NULL DEFAULT 'cli'"),
                        ("entrypoint", "TEXT NOT NULL DEFAULT ''"),
                        ("execution_verified", "INTEGER NOT NULL DEFAULT 0"),
                        ("auth_status", "TEXT NOT NULL DEFAULT 'unknown'"),
                    ):
                        if column not in columns:
                            db.execute(f"ALTER TABLE employees ADD COLUMN {column} {definition}")
                    employee_columns = db.execute("PRAGMA table_info(employees)").fetchall()
                    if next(row for row in employee_columns if row[1] == "project_id")[3]:
                        employee_schema = next(
                            statement
                            for statement in SCHEMA.split(";")
                            if "CREATE TABLE IF NOT EXISTS employees (" in statement
                        )
                        db.execute(
                            employee_schema.replace("IF NOT EXISTS employees", "employees_v6")
                        )
                        names = ",".join(row[1] for row in employee_columns)
                        db.execute(
                            f"INSERT INTO employees_v6({names}) SELECT {names} FROM employees"
                        )
                        db.execute("DROP TABLE employees")
                        db.execute("ALTER TABLE employees_v6 RENAME TO employees")
                    columns = {row[1] for row in db.execute("PRAGMA table_info(permissions)")}
                    if "expires_at" not in columns:
                        db.execute("ALTER TABLE permissions ADD COLUMN expires_at TEXT")
                    for row in db.execute(
                        "SELECT task_id,request_id,created_at FROM permissions WHERE expires_at IS NULL"
                    ).fetchall():
                        deadline = datetime.fromisoformat(row["created_at"]) + timedelta(
                            seconds=PERMISSION_TIMEOUT
                        )
                        db.execute(
                            "UPDATE permissions SET expires_at=? WHERE task_id=? AND request_id=?",
                            (deadline.isoformat(), row["task_id"], row["request_id"]),
                        )
                    if db.execute("PRAGMA foreign_key_check").fetchone():
                        raise WorkbenchError(
                            "migration_failed",
                            "数据关联检查失败，原数据库已保留，请恢复迁移前备份。",
                        )
                    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
                finally:
                    db.execute("PRAGMA foreign_keys=ON")
        with self._transaction() as db:
            if not db.execute("SELECT id FROM devices WHERE is_local=1").fetchone():
                db.execute(
                    "INSERT INTO devices(id,name,is_local,created_at) VALUES(?,?,1,?)",
                    (_id("node"), socket.gethostname(), _now()),
                )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        try:
            db = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        except sqlite3.Error as exc:
            raise WorkbenchError("storage_error", "无法打开工作数据，请检查磁盘后重试。") from exc
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("PRAGMA journal_mode=WAL")
            # SQLite inherits the 0600 database mode for sidecar files.
            yield db
        except sqlite3.Error as exc:
            raise WorkbenchError("storage_error", "无法保存工作数据，请检查磁盘后重试。") from exc
        finally:
            db.close()
            for suffix in ("", "-wal", "-shm"):
                try:
                    Path(str(self.db_path) + suffix).chmod(0o600)
                except FileNotFoundError:
                    pass

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _required(db: sqlite3.Connection, table: str, entity_id: str) -> sqlite3.Row:
        _text(entity_id, "记录编号", 128)
        # Table names are exclusively internal constants, never user input.
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (entity_id,)).fetchone()
        if row is None:
            raise WorkbenchError("not_found", "找不到这条记录，请刷新页面。")
        return row

    @staticmethod
    def _entity(row: sqlite3.Row) -> dict:
        result = dict(row)
        result.pop("secret_token", None)
        result.pop("tool_token", None)
        result.pop("request_digest", None)
        for key in ("cancel_requested", "is_local", "execution_verified", "request_work"):
            if key in result:
                result[key] = bool(result[key])
        for key in ("payload", "options", "tool_call", "error"):
            if key in result and result[key] is not None:
                result[key] = json.loads(result[key])
        return result

    @staticmethod
    def _scrub(db: sqlite3.Connection, value: Any) -> Any:
        tokens = [
            row[0]
            for row in db.execute(
                "SELECT secret_token FROM employees UNION SELECT secret_token FROM memberships UNION SELECT tool_token FROM tasks"
            )
            if row[0]
        ]

        def clean(item: Any) -> Any:
            if isinstance(item, dict):
                return {
                    k: "[redacted]" if str(k).lower() in SECRET_KEYS else clean(v)
                    for k, v in item.items()
                }
            if isinstance(item, (list, tuple)):
                return [clean(v) for v in item]
            if isinstance(item, str):
                for token in tokens:
                    item = item.replace(token, "[redacted]")
            return item

        return clean(value)

    def local_node(self) -> dict:
        with self._connection() as db:
            row = db.execute("SELECT id,name FROM devices WHERE is_local=1").fetchone()
            return dict(row)

    def upsert_device(
        self, device_id: str, name: str, status: str, last_seen: str | None = None
    ) -> dict:
        device_id = _text(device_id, "设备编号", 128)
        name = _text(name, "设备名称", 200).strip()
        _choice(status, {"online", "offline", "unknown"}, "设备状态需要是在线、离线或未知。")
        if last_seen is not None:
            _text(last_seen, "设备在线时间", 100)
        with self._transaction() as db:
            db.execute(
                "INSERT INTO devices(id,name,status,created_at,last_seen) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name,status=excluded.status,"
                "last_seen=excluded.last_seen",
                (device_id, name, status, _now(), last_seen),
            )
            return self._entity(self._required(db, "devices", device_id))

    def _employee(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict:
        employee = self._entity(row)
        employee["execution_supported"] = (
            row["kind"] in {"codex", "claude"} and row["connection_type"] == "cli"
        )
        employee["execution_verified"] = bool(
            row["execution_verified"] and employee["execution_supported"]
        )
        employee["project_ids"] = [
            member[0]
            for member in db.execute(
                "SELECT project_id FROM memberships WHERE employee_id=? ORDER BY rowid",
                (row["id"],),
            )
        ]
        return self._scrub(db, employee)

    def snapshot(self) -> dict:
        with self._connection() as db:
            db.execute("BEGIN")
            result = {}
            for table in (
                "projects",
                "employees",
                "tasks",
                "resources",
                "memories",
                "devices",
                "messages",
            ):
                rows = db.execute(f"SELECT * FROM {table} ORDER BY created_at,rowid")
                result[table] = [
                    self._employee(db, row)
                    if table == "employees"
                    else self._message(db, row)
                    if table == "messages"
                    else self._entity(row)
                    for row in rows
                ]
            return self._scrub(db, result)

    def update_maintenance(self) -> dict:
        with self._connection() as db:
            db.execute("BEGIN")
            paused = bool(db.execute("SELECT paused FROM update_settings WHERE id=1").fetchone()[0])
            active = [
                dict(row)
                for row in db.execute(
                    "SELECT id,title,status FROM tasks WHERE status IN ('starting','running','waiting_approval') ORDER BY created_at"
                )
            ]
            queued = db.execute("SELECT count(*) FROM tasks WHERE status='queued'").fetchone()[0]
            pending = [
                dict(row)
                for row in db.execute("SELECT id,created_at FROM update_claims ORDER BY created_at")
            ]
            return self._scrub(
                db,
                {
                    "paused": paused,
                    "active_tasks": active,
                    "queued_count": queued,
                    "pending_claims": pending,
                },
            )

    def begin_update_claim(self, claim_id: str) -> bool:
        _text(claim_id, "领取编号", 128)
        with self._transaction() as db:
            if db.execute("SELECT paused FROM update_settings WHERE id=1").fetchone()[0]:
                return False
            db.execute("INSERT INTO update_claims(id,created_at) VALUES(?,?)", (claim_id, _now()))
            return True

    def end_update_claim(self, claim_id: str) -> None:
        _text(claim_id, "领取编号", 128)
        with self._transaction() as db:
            db.execute("DELETE FROM update_claims WHERE id=?", (claim_id,))

    def pause_updates(self, paused: bool) -> dict:
        if not isinstance(paused, bool):
            raise WorkbenchError("invalid_field", "更新暂停状态需要为布尔值。")
        with self._transaction() as db:
            db.execute("UPDATE update_settings SET paused=? WHERE id=1", (int(paused),))
        return self.update_maintenance()

    def update_channel(self, channel=None) -> str:
        if channel is not None:
            _choice(channel, {"stable", "beta"}, "请选择稳定版或 Beta 更新渠道。")
            with self._transaction() as db:
                db.execute("UPDATE update_settings SET channel=? WHERE id=1", (channel,))
        with self._connection() as db:
            return db.execute("SELECT channel FROM update_settings WHERE id=1").fetchone()[0]

    def create_project(self, name: str, path: str | Path) -> dict:
        name = _text(name, "项目名称", 200).strip()
        try:
            location = Path(path).expanduser().resolve(strict=True)
        except (OSError, TypeError, ValueError) as exc:
            raise WorkbenchError("invalid_path", "项目目录不存在，请选择已有目录。") from exc
        if not location.is_dir():
            raise WorkbenchError("invalid_path", "项目路径需要是目录。")
        with self._transaction() as db:
            project_id = _id("project")
            db.execute(
                "INSERT INTO projects VALUES(?,?,?,?)", (project_id, name, str(location), _now())
            )
            return self._entity(self._required(db, "projects", project_id))

    def create_employee(
        self,
        name: str,
        kind: str,
        project_id: str | None = None,
        node_id: str | None = None,
        status: str = "unknown",
        detail: str = "",
        connection_type: str = "cli",
        entrypoint: str = "",
        auth_status: str = "unknown",
    ) -> dict:
        name = _text(name, "员工名称", 200).strip()
        _choice(kind, EMPLOYEE_KINDS, "请选择已支持登记的 AI 工具类型。")
        _choice(connection_type, CONNECTION_TYPES, "请选择命令行、应用或服务连接。")
        _choice(auth_status, AUTH_STATUSES, "请选择有效的登录确认状态。")
        entrypoint = _text(entrypoint, "连接入口", 4096, empty=True).strip()
        _choice(status, EMPLOYEE_STATUSES, "请选择有效的员工连接状态。")
        detail = _text(detail, "状态说明", 4096, empty=True)
        with self._transaction() as db:
            if project_id is not None:
                self._required(db, "projects", project_id)
            if node_id is None:
                node_id = db.execute("SELECT id FROM devices WHERE is_local=1").fetchone()[0]
            self._required(db, "devices", node_id)
            existing = db.execute(
                "SELECT id,lifecycle,connection_type,entrypoint FROM employees WHERE name=? AND kind=? AND node_id=?",
                (name, kind, node_id),
            ).fetchone()
            employee_id = existing[0] if existing else _id("employee")
            if existing and existing["lifecycle"] == "retired":
                raise WorkbenchError(
                    "employee_retired", "这位员工已退役。请使用新名称建立新的员工身份。"
                )
            if existing and (
                existing["connection_type"] != connection_type
                or (existing["entrypoint"] and entrypoint and existing["entrypoint"] != entrypoint)
            ):
                raise WorkbenchError(
                    "IDENTITY_CONNECTION_CONFLICT",
                    "该身份已经使用另一连接入口，请为不同连接建立新员工身份。",
                )
            if not existing:
                db.execute(
                    "INSERT INTO employees(id,name,kind,project_id,node_id,status,detail,secret_token,created_at,connection_type,entrypoint,auth_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        employee_id,
                        name,
                        kind,
                        project_id,
                        node_id,
                        status,
                        detail,
                        secrets.token_urlsafe(32),
                        _now(),
                        connection_type,
                        entrypoint,
                        auth_status,
                    ),
                )
                self._governance(
                    db,
                    "employee_registered",
                    employee_id=employee_id,
                    actor="human" if node_id == self.local_node_id(db) else f"device:{node_id}",
                    reason="员工身份已登记。",
                )
            else:
                db.execute(
                    "UPDATE employees SET auth_status=? WHERE id=?", (auth_status, employee_id)
                )
                if entrypoint and not existing["entrypoint"]:
                    db.execute(
                        "UPDATE employees SET entrypoint=? WHERE id=?", (entrypoint, employee_id)
                    )
            if project_id is not None:
                self._add_project_member(db, project_id, employee_id)
            return self._employee(db, self._required(db, "employees", employee_id))

    def _add_project_member(self, db, project_id, employee_id):
        self._required(db, "projects", project_id)
        employee = self._required(db, "employees", employee_id)
        if employee["lifecycle"] == "retired":
            raise WorkbenchError("employee_retired", "退役员工不能加入项目，请建立新身份。")
        added = db.execute(
            "INSERT OR IGNORE INTO memberships VALUES(?,?,?)",
            (employee_id, project_id, secrets.token_urlsafe(32)),
        )
        if added.rowcount:
            self._governance(
                db,
                "employee_joined",
                employee_id=employee_id,
                project_id=project_id,
                actor="human"
                if employee["node_id"] == self.local_node_id(db)
                else f"device:{employee['node_id']}",
                reason="员工已加入项目。",
            )
        return self._employee(db, employee)

    def add_project_member(self, project_id: str, employee_id: str) -> dict:
        with self._transaction() as db:
            return self._add_project_member(db, project_id, employee_id)

    def remove_project_member(self, project_id: str, employee_id: str) -> dict:
        with self._transaction() as db:
            self._required(db, "projects", project_id)
            employee = self._required(db, "employees", employee_id)
            if db.execute(
                "SELECT 1 FROM tasks WHERE project_id=? AND assignee_id=? AND status IN ('starting','running','waiting_approval')",
                (project_id, employee_id),
            ).fetchone():
                raise WorkbenchError(
                    "MEMBER_HAS_ACTIVE_TASK",
                    "这名员工在项目中仍有执行中的任务，请先停止工作再移除。",
                )
            if not self._membership(db, employee_id, project_id):
                return self._employee(db, employee)
            for task in db.execute(
                "SELECT id FROM tasks WHERE project_id=? AND assignee_id=? AND status='queued'",
                (project_id, employee_id),
            ).fetchall():
                db.execute(
                    "UPDATE tasks SET status='cancelled',cancel_requested=1,updated_at=? WHERE id=?",
                    (_now(), task[0]),
                )
                self._event(db, task[0], "member_removed", "员工已移出项目，排队任务已取消。")
            db.execute(
                "DELETE FROM memberships WHERE project_id=? AND employee_id=?",
                (project_id, employee_id),
            )
            db.execute(
                "UPDATE employees SET project_id=NULL WHERE id=? AND project_id=?",
                (employee_id, project_id),
            )
            self._governance(
                db,
                "employee_left",
                employee_id=employee_id,
                project_id=project_id,
                reason="员工已移出项目，项目授权已撤销。",
            )
            return self._employee(db, self._required(db, "employees", employee_id))

    @staticmethod
    def local_node_id(db: sqlite3.Connection) -> str:
        return db.execute("SELECT id FROM devices WHERE is_local=1").fetchone()[0]

    def _governance(
        self,
        db,
        event_type,
        *,
        employee_id=None,
        project_id=None,
        task_id=None,
        actor="human",
        reason="",
        payload=None,
    ):
        clean = self._scrub(db, payload)
        reason = self._scrub(db, reason)
        event_id = _id("governance")
        db.execute(
            "INSERT INTO governance_events VALUES(?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                employee_id,
                project_id,
                task_id,
                event_type,
                actor,
                reason,
                _json(clean),
                _now(),
            ),
        )
        return self._entity(self._required(db, "governance_events", event_id))

    def governance_events(self, project_id: str | None = None) -> list[dict]:
        with self._connection() as db:
            clause, params = "", []
            if project_id is not None:
                self._required(db, "projects", project_id)
                clause, params = " WHERE project_id=? OR project_id IS NULL", [project_id]
            rows = db.execute(
                "SELECT * FROM governance_events"
                + clause
                + " ORDER BY created_at DESC,rowid DESC LIMIT 200",
                params,
            )
            return self._scrub(db, [self._entity(row) for row in rows])

    def set_employee_lifecycle(self, employee_id: str, status: str, reason: str = "") -> dict:
        _choice(status, LIFECYCLES, "请选择正常、暂停或退役。")
        reason = _text(reason, "变更原因", 4096, empty=status != "retired").strip()
        with self._transaction() as db:
            employee = self._required(db, "employees", employee_id)
            reason = self._scrub(db, reason)
            previous = employee["lifecycle"]
            if previous == "retired" and status != "retired":
                raise WorkbenchError("employee_retired", "退役身份不能恢复，请建立新的员工身份。")
            if previous == status:
                return {"employee": self._employee(db, employee), "tasks": []}
            timestamp = _now()
            db.execute(
                "UPDATE employees SET lifecycle=?,lifecycle_reason=?,lifecycle_changed_at=? WHERE id=?",
                (status, reason, timestamp, employee_id),
            )
            tasks = []
            if status == "retired":
                # Validation revokes every project capability in this transaction.
                # Retain old secrets privately so historical redaction still works.
                for task in db.execute(
                    "SELECT * FROM tasks WHERE assignee_id=? AND status IN ('queued','starting','running','waiting_approval')",
                    (employee_id,),
                ).fetchall():
                    db.execute(
                        "UPDATE tasks SET status=?,cancel_requested=1,updated_at=? WHERE id=?",
                        (
                            "cancelled" if task["status"] == "queued" else task["status"],
                            timestamp,
                            task["id"],
                        ),
                    )
                    self._expire_permissions(db, task["id"])
                    self._event(
                        db,
                        task["id"],
                        "employee_retired",
                        "员工退役，排队任务已取消；执行中的任务已请求停止。",
                    )
                    tasks.append(self._entity(self._required(db, "tasks", task["id"])))
            self._governance(
                db,
                "employee_lifecycle",
                employee_id=employee_id,
                reason=reason,
                payload={"previous": previous, "status": status},
            )
            return {
                "employee": self._employee(db, self._required(db, "employees", employee_id)),
                "tasks": tasks,
            }

    def update_employee(
        self, employee_id: str, status: str, detail: str = "", auth_status: str | None = None
    ) -> dict:
        _choice(status, EMPLOYEE_STATUSES, "请选择有效的员工连接状态。")
        detail = _text(detail, "状态说明", 4096, empty=True)
        if auth_status is not None:
            _choice(auth_status, AUTH_STATUSES, "请选择有效的登录确认状态。")
        with self._transaction() as db:
            self._required(db, "employees", employee_id)
            db.execute(
                "UPDATE employees SET status=?,detail=? WHERE id=?", (status, detail, employee_id)
            )
            if auth_status is not None:
                db.execute(
                    "UPDATE employees SET auth_status=? WHERE id=?", (auth_status, employee_id)
                )
            return self._employee(db, self._required(db, "employees", employee_id))

    def create_task(
        self,
        project_id: str,
        title: str,
        prompt: str,
        assignee_id: str,
        permission_mode: str = "read-only",
        model: str | None = None,
        actor: str = "human",
    ) -> dict:
        if model is not None:
            model = _text(model, "模型", 200).strip()
        title = _text(title, "任务名称", 300).strip()
        prompt = _text(prompt, "任务说明")
        _choice(
            permission_mode,
            {"read-only", "workspace-write"},
            "请选择只读或允许修改项目目录的执行权限。",
        )
        with self._transaction() as db:
            return self._create_task(
                db, project_id, title, prompt, assignee_id, permission_mode, model, actor
            )

    def _create_task(
        self,
        db,
        project_id,
        title,
        prompt,
        assignee_id,
        permission_mode="read-only",
        model=None,
        actor="human",
        request_message_id=None,
        source_task_id=None,
    ):
        self._required(db, "projects", project_id)
        employee = self._required(db, "employees", assignee_id)
        if employee["lifecycle"] != "active":
            raise WorkbenchError(
                "employee_inactive", "这位员工已暂停或退役，请选择正常工作的员工。"
            )
        if not self._membership(db, assignee_id, project_id):
            raise WorkbenchError("permission_denied", "这位员工未加入该项目。")
        if employee["kind"] not in {"codex", "claude"} or employee["connection_type"] != "cli":
            raise WorkbenchError(
                "ADAPTER_UNSUPPORTED", "已登记此员工连接，但当前还没有可执行任务的适配器。"
            )
        if permission_mode == "workspace-write" and employee["node_id"] != self.local_node()["id"]:
            raise WorkbenchError(
                "WORKSPACE_REMOTE_UNSUPPORTED",
                "远端修改任务尚未支持独立工作区；请使用只读任务或在本机执行修改。",
            )
        title, prompt = self._scrub(db, title), self._scrub(db, prompt)
        task_id, timestamp = _id("task"), _now()
        db.execute(
            "INSERT INTO tasks(id,project_id,title,prompt,assignee_id,node_id,"
            "permission_mode,run_id,session_id,created_at,updated_at,model,request_message_id,source_task_id,tool_token) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id,
                project_id,
                title,
                prompt,
                assignee_id,
                employee["node_id"],
                permission_mode,
                _id("run"),
                _id("session"),
                timestamp,
                timestamp,
                model,
                request_message_id,
                source_task_id,
                secrets.token_urlsafe(32),
            ),
        )
        self._event(db, task_id, "queued", "任务已进入队列。")
        self._governance(
            db,
            "task_dispatched",
            employee_id=assignee_id,
            project_id=project_id,
            task_id=task_id,
            actor=actor,
            payload={"permission_mode": permission_mode},
        )
        return self._entity(self._required(db, "tasks", task_id))

    def _message(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict:
        message = self._entity(row)
        for field in ("sender", "recipient"):
            employee_id = row[f"{field}_id"]
            if employee_id:
                employee = self._employee(db, self._required(db, "employees", employee_id))
                message[field] = {
                    key: employee[key]
                    for key in (
                        "id",
                        "name",
                        "kind",
                        "node_id",
                        "connection_type",
                        "execution_supported",
                        "lifecycle",
                    )
                }
            else:
                message[field] = None
        message["attribution"] = {
            "scope": "employee_session" if row["sender_id"] else "human",
            "internal_actor_verified": False,
        }
        message["attribution_scope"] = message["attribution"]["scope"]
        message["internal_actor_verified"] = False
        message["sender_session_id"] = None
        message["source_run_id"] = None
        if row["source_task_id"]:
            source = self._required(db, "tasks", row["source_task_id"])
            message["sender_session_id"] = source["session_id"]
            message["source_run_id"] = source["run_id"]
        return self._scrub(db, message)

    def list_messages(self, project_id: str) -> list[dict]:
        with self._connection() as db:
            self._required(db, "projects", project_id)
            return [
                self._message(db, row)
                for row in db.execute(
                    "SELECT * FROM messages WHERE project_id=? ORDER BY created_at,rowid",
                    (project_id,),
                )
            ]

    def employee_messages(
        self, project_id: str, employee_id: str, folder: str = "inbox", limit: int = 100
    ) -> dict:
        """Read a bound employee's folder; viewing never acknowledges or starts work."""
        if folder not in ("inbox", "sent", "group"):
            raise WorkbenchError("invalid_field", "信箱分类必须是 inbox、sent 或 group。")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise WorkbenchError("invalid_field", "信箱读取数量必须为 1–100。")
        with self._connection() as db:
            self._required(db, "projects", project_id)
            self._message_member(db, project_id, employee_id)
            condition = {
                "inbox": "recipient_id=?",
                "sent": "sender_id=?",
                "group": "recipient_id IS NULL AND (sender_id IS NULL OR sender_id!=?)",
            }[folder]
            rows = db.execute(
                "SELECT * FROM messages WHERE project_id=? AND "
                + condition
                + " ORDER BY created_at DESC,rowid DESC LIMIT ?",
                (project_id, employee_id, limit + 1),
            ).fetchall()
            return {
                "project_id": project_id,
                "employee_id": employee_id,
                "folder": folder,
                "messages": [self._message(db, row) for row in reversed(rows[:limit])],
                "has_older": len(rows) > limit,
                "viewing_acknowledges": False,
            }

    def _message_member(self, db, project_id, employee_id):
        employee = self._required(db, "employees", employee_id)
        if not self._membership(db, employee_id, project_id):
            raise WorkbenchError("permission_denied", "消息中的员工必须属于当前项目。")
        if employee["lifecycle"] != "active":
            raise WorkbenchError("employee_inactive", "暂停或退役员工不能发送或接收新的项目消息。")
        return employee

    def _check_request_chain(self, db, source_task_id, recipient_id):
        depth, seen_tasks, employees = 1, set(), set()
        current_id = source_task_id
        while current_id:
            if current_id in seen_tasks:
                raise WorkbenchError("MESSAGE_REQUEST_CYCLE", "工作请求出现循环，已停止继续触发。")
            seen_tasks.add(current_id)
            task = self._required(db, "tasks", current_id)
            employees.add(task["assignee_id"])
            if task["request_message_id"]:
                origin = self._required(db, "messages", task["request_message_id"])
                if origin["sender_id"]:
                    depth += 1
                    employees.add(origin["sender_id"])
            if depth > MAX_REQUEST_CHAIN:
                raise WorkbenchError(
                    "MESSAGE_REQUEST_LIMIT", "员工连续请求工作最多四次，请由 human 确认后重新安排。"
                )
            current_id = task["source_task_id"]
        if recipient_id in employees:
            raise WorkbenchError(
                "MESSAGE_REQUEST_CYCLE", "不能请求上游员工再次执行，以免形成循环唤醒。"
            )

    def send_message(
        self,
        project_id: str,
        title: str,
        body: str,
        recipient_id: str | None = None,
        sender_id: str | None = None,
        reply_to: str | None = None,
        request_work: bool = False,
        source_task_id: str | None = None,
        request_id: str | None = None,
    ) -> dict:
        title = _text(title, "消息标题", 300).strip()
        body = _text(body, "消息内容")
        if type(request_work) is not bool:
            raise WorkbenchError("invalid_field", "请明确是否请求员工执行工作。")
        if request_id is not None:
            request_id = _text(request_id, "请求编号", 200)
        digest = hashlib.sha256(
            _json([title, body, recipient_id, reply_to, request_work, source_task_id]).encode()
        ).hexdigest()
        with self._transaction() as db:
            self._required(db, "projects", project_id)
            if sender_id is not None:
                self._message_member(db, project_id, sender_id)
                if source_task_id is None:
                    raise WorkbenchError(
                        "MESSAGE_SOURCE_REQUIRED", "员工消息需要绑定当前受管执行。"
                    )
                source = self._required(db, "tasks", source_task_id)
                if (
                    source["project_id"] != project_id
                    or source["assignee_id"] != sender_id
                    or source["status"] not in ACTIVE
                    or source["cancel_requested"]
                ):
                    raise WorkbenchError(
                        "permission_denied", "消息来源必须是该员工在当前项目的有效执行。"
                    )
            elif source_task_id is not None:
                raise WorkbenchError("permission_denied", "人工消息不能冒用员工执行来源。")
            if recipient_id is not None:
                self._message_member(db, project_id, recipient_id)
            if request_work and (recipient_id is None or recipient_id == sender_id):
                raise WorkbenchError("invalid_field", "请求工作需要明确选择另一名项目员工。")
            parent = self._required(db, "messages", reply_to) if reply_to is not None else None
            if parent and parent["project_id"] != project_id:
                raise WorkbenchError("permission_denied", "回复必须留在同一个项目和消息线程。")
            if request_id is not None:
                previous = db.execute(
                    "SELECT * FROM messages WHERE project_id=? AND sender_id IS ? AND request_id=?",
                    (project_id, sender_id, request_id),
                ).fetchone()
                if previous:
                    if not hmac.compare_digest(previous["request_digest"], digest):
                        raise WorkbenchError(
                            "MESSAGE_REQUEST_CONFLICT", "同一个请求编号不能用于不同的消息。"
                        )
                    return self._message(db, previous)
            if request_work and source_task_id:
                self._check_request_chain(db, source_task_id, recipient_id)
            message_id, timestamp = _id("message"), _now()
            db.execute(
                "INSERT INTO messages(id,project_id,title,body,sender_id,recipient_id,reply_to,thread_id,request_work,source_task_id,request_id,request_digest,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    message_id,
                    project_id,
                    self._scrub(db, title),
                    self._scrub(db, body),
                    sender_id,
                    recipient_id,
                    reply_to,
                    parent["thread_id"] if parent else message_id,
                    int(request_work),
                    source_task_id,
                    request_id,
                    digest,
                    timestamp,
                ),
            )
            if request_work:
                task = self._create_task(
                    db,
                    project_id,
                    title,
                    body,
                    recipient_id,
                    actor=f"employee:{sender_id}" if sender_id else "human",
                    request_message_id=message_id,
                    source_task_id=source_task_id,
                )
                db.execute("UPDATE messages SET task_id=? WHERE id=?", (task["id"], message_id))
                self._event(
                    db,
                    task["id"],
                    "team_message",
                    "收到项目同事的工作请求。" if sender_id else "收到人工工作请求。",
                    {
                        "from_id": sender_id,
                        "message_id": message_id,
                        "source_task_id": source_task_id,
                    },
                )
            self._governance(
                db,
                "message_sent",
                employee_id=sender_id,
                project_id=project_id,
                actor=f"employee:{sender_id}" if sender_id else "human",
                payload={
                    "message_id": message_id,
                    "recipient_id": recipient_id,
                    "request_work": request_work,
                    "source_task_id": source_task_id,
                },
            )
            return self._message(db, self._required(db, "messages", message_id))

    def claim_task(self, node_id: str, project_ids: list[str] | None = None) -> dict | None:
        if project_ids is not None and (
            not isinstance(project_ids, list)
            or len(project_ids) > 100
            or not all(isinstance(value, str) and 0 < len(value) <= 128 for value in project_ids)
        ):
            raise WorkbenchError("invalid_field", "请提供有效的授权项目列表。")
        with self._transaction() as db:
            self._required(db, "devices", node_id)
            if db.execute("SELECT paused FROM update_settings WHERE id=1").fetchone()[0]:
                return None
            if project_ids == []:
                return None
            scope_clause = ""
            params: list[str] = [node_id]
            if project_ids is not None:
                placeholders = ",".join("?" for _ in project_ids)
                scope_clause = f" AND t.project_id IN ({placeholders}) "
                params.extend(project_ids)
            row = db.execute(
                "SELECT t.* FROM tasks t WHERE t.node_id=? AND t.status='queued' "
                + scope_clause
                + "AND EXISTS (SELECT 1 FROM employees e WHERE e.id=t.assignee_id AND e.lifecycle='active' AND e.kind IN ('codex','claude') AND e.connection_type='cli') "
                + "AND EXISTS (SELECT 1 FROM memberships m WHERE m.employee_id=t.assignee_id AND m.project_id=t.project_id) "
                + "AND NOT EXISTS (SELECT 1 FROM tasks a WHERE a.assignee_id=t.assignee_id "
                "AND a.status IN ('starting','running','waiting_approval')) "
                "ORDER BY t.created_at,t.rowid LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                return None
            if row["permission_mode"] == "workspace-write" and node_id != self.local_node()["id"]:
                error = {
                    "code": "WORKSPACE_REMOTE_UNSUPPORTED",
                    "message": "远端修改任务尚未支持独立工作区，请改为只读或本机修改任务。",
                }
                db.execute(
                    "UPDATE tasks SET status='failed',error=?,updated_at=? WHERE id=?",
                    (_json(error), _now(), row["id"]),
                )
                self._event(db, row["id"], "failed", error["message"], {"error": error})
                return None
            db.execute(
                "UPDATE tasks SET status='starting',updated_at=? WHERE id=?", (_now(), row["id"])
            )
            from .workbench_resources import freeze

            freeze(self, db, row)
            self._event(db, row["id"], "starting", "正在连接员工执行入口。")
            return self._entity(self._required(db, "tasks", row["id"]))

    def get_task(self, task_id: str) -> dict:
        with self._connection() as db:
            return self._scrub(db, self._entity(self._required(db, "tasks", task_id)))

    def task_detail(self, task_id: str) -> dict:
        with self._connection() as db:
            db.execute("BEGIN")
            task = self._entity(self._required(db, "tasks", task_id))
            events = db.execute(
                "SELECT * FROM events WHERE task_id=? ORDER BY created_at,rowid", (task_id,)
            )
            permissions = db.execute(
                "SELECT * FROM permissions WHERE task_id=? ORDER BY created_at,rowid",
                (task_id,),
            )
            return self._scrub(
                db,
                {
                    "task": task,
                    "events": [self._entity(r) for r in events],
                    "permissions": [self._entity(r) for r in permissions],
                    "links": [
                        dict(r)
                        for r in db.execute(
                            "SELECT t.id AS task_id,t.title,t.status,l.relation,'parent' AS direction FROM task_links l JOIN tasks t ON t.id=l.parent_task_id WHERE l.task_id=? "
                            "UNION ALL SELECT t.id AS task_id,t.title,t.status,l.relation,'child' AS direction FROM task_links l JOIN tasks t ON t.id=l.task_id WHERE l.parent_task_id=?",
                            (task_id, task_id),
                        )
                    ],
                },
            )

    def set_status(self, task_id: str, status: str) -> dict:
        _text(status, "任务状态", 64)
        transitions = {
            "starting": {"running"},
            "running": {"waiting_approval"},
            "waiting_approval": {"running"},
        }
        with self._transaction() as db:
            task = self._required(db, "tasks", task_id)
            if status == task["status"] and status in ACTIVE:
                return self._entity(task)
            if status not in transitions.get(task["status"], set()):
                raise WorkbenchError("invalid_state", "任务当前状态不允许此操作。")
            if task["cancel_requested"]:
                raise WorkbenchError("invalid_state", "任务正在停止，请等待执行器确认。")
            if (
                status == "running"
                and db.execute(
                    "SELECT 1 FROM permissions WHERE task_id=? AND status='pending'",
                    (task_id,),
                ).fetchone()
            ):
                raise WorkbenchError("invalid_state", "任务还有待处理的权限请求。")
            db.execute(
                "UPDATE tasks SET status=?,updated_at=? WHERE id=?", (status, _now(), task_id)
            )
            self._event(db, task_id, status, "任务状态已更新。")
            return self._entity(self._required(db, "tasks", task_id))

    def _event(
        self,
        db: sqlite3.Connection,
        task_id: str,
        event_type: str,
        message: str,
        payload: Any = None,
    ) -> dict:
        event_type = _text(event_type, "事件类型", 100)
        message = _text(message, "事件说明", empty=True)
        clean = self._scrub(db, {"type": event_type, "message": message, "payload": payload})
        event_id = _id("event")
        db.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?)",
            (
                event_id,
                task_id,
                clean["type"],
                clean["message"],
                _json(clean["payload"]),
                _now(),
            ),
        )
        return self._entity(self._required(db, "events", event_id))

    def add_event(self, task_id: str, type: str, message: str, payload: Any = None) -> dict:
        _json(payload)
        with self._transaction() as db:
            self._required(db, "tasks", task_id)
            return self._event(db, task_id, type, message, payload)

    @staticmethod
    def _expire_permissions(db: sqlite3.Connection, task_id: str) -> None:
        db.execute(
            "UPDATE permissions SET status='expired',resolved_at=? "
            "WHERE task_id=? AND status='pending'",
            (_now(), task_id),
        )

    def _finish_task(self, db, task_id, status, result, error):
        task = self._required(db, "tasks", task_id)
        if task["status"] not in ACTIVE:
            raise WorkbenchError("invalid_state", "这次执行已结束或尚未开始。")
        if task["cancel_requested"] and status in {"review", "failed"}:
            status = "cancelled"
        probe = db.execute("SELECT * FROM employee_probes WHERE task_id=?", (task_id,)).fetchone()
        if probe is not None and status == "review":
            employee = self._required(db, "employees", task["assignee_id"])
            if not (
                probe["context_seen"]
                and probe["note_seen"]
                and employee["lifecycle"] == "active"
                and self._membership(db, employee["id"], task["project_id"])
            ):
                status = "failed"
                error = {
                    "code": "PROBE_INCOMPLETE",
                    "message": "模型已结束，但绑定项目的上下文读取与验证回执未全部完成。",
                }
        if status == "review" and task["status"] == "running" and error is None:
            db.execute(
                "UPDATE employees SET execution_verified=1 WHERE id=?", (task["assignee_id"],)
            )
        result, error = self._scrub(db, result), self._scrub(db, error)
        db.execute(
            "UPDATE tasks SET status=?,result=?,error=?,updated_at=? WHERE id=?",
            (status, result, None if error is None else _json(error), _now(), task_id),
        )
        self._expire_permissions(db, task_id)
        self._event(
            db,
            task_id,
            status,
            "执行已结束，等待验收。" if status == "review" else "执行已结束。",
            {"error": error},
        )
        return self._entity(self._required(db, "tasks", task_id))

    def finish_task(self, task_id: str, status: str, result: str = "", error: Any = None) -> dict:
        _choice(status, FINISH, "请选择待验收、失败、取消或中断状态。")
        result = _text(result, "任务结果", empty=True)
        with self._transaction() as db:
            return self._finish_task(db, task_id, status, result, error)

    def finish_remote_task(self, task_id, run_id, status, result="", error=None):
        """Persist the final receipt and its replay proof in the same transaction.

        Only an identical receipt for the same execution can be acknowledged
        again. A replay never changes a human's acceptance or cancellation.
        """
        _choice(status, FINISH, "请选择待验收、失败、取消或中断状态。")
        result = _text(result, "任务结果", empty=True)
        digest = hashlib.sha256(
            json.dumps(
                json.loads(_json({"status": status, "result": result, "error": error})),
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        with self._transaction() as db:
            task = self._required(db, "tasks", task_id)
            if task["run_id"] != run_id:
                raise WorkbenchError("permission_denied", "这次执行已过期。")
            previous = db.execute(
                "SELECT digest FROM remote_receipts WHERE task_id=? AND run_id=?",
                (task_id, run_id),
            ).fetchone()
            if previous:
                if not hmac.compare_digest(previous["digest"], digest):
                    raise WorkbenchError("invalid_state", "这次执行已收到不同的回执。")
                return self._entity(task)
            if status == "review" and task["status"] != "running":
                raise WorkbenchError("invalid_state", "尚未确认员工开始执行，不能提交完成回执。")
            result_task = self._finish_task(db, task_id, status, result, error)
            db.execute(
                "INSERT INTO remote_receipts(task_id,run_id,digest) VALUES(?,?,?)",
                (task_id, run_id, digest),
            )
            return result_task

    def review_task(self, task_id: str, decision: str, note: str = "") -> dict:
        _choice(decision, {"accept", "reject"}, "请选择通过或退回验收。")
        note = _text(note, "验收意见", empty=True)
        with self._transaction() as db:
            task = self._required(db, "tasks", task_id)
            if task["status"] != "review":
                raise WorkbenchError("invalid_state", "只有待验收的任务可以提交验收决定。")
            status = "done" if decision == "accept" else "failed"
            error = (
                None
                if decision == "accept"
                else _json({"code": "REVIEW_REJECTED", "message": note or "验收未通过。"})
            )
            db.execute(
                "UPDATE tasks SET status=?,error=?,updated_at=? WHERE id=?",
                (status, error, _now(), task_id),
            )
            self._event(db, task_id, "reviewed", note or "验收决定已保存。", {"decision": decision})
            self._governance(
                db,
                "task_reviewed",
                employee_id=task["assignee_id"],
                project_id=task["project_id"],
                task_id=task_id,
                reason=note,
                payload={"decision": decision, "run_id": task["run_id"]},
            )
            return self._entity(self._required(db, "tasks", task_id))

    def follow_up_task(self, task_id: str, note: str) -> dict:
        note = _text(note, "补充要求", 10000).strip()
        with self._transaction() as db:
            parent = self._required(db, "tasks", task_id)
            if parent["status"] != "review":
                raise WorkbenchError("invalid_state", "只有待验收任务可以退回并创建后续任务。")
            if db.execute("SELECT paused FROM update_settings WHERE id=1").fetchone()[0]:
                raise WorkbenchError(
                    "UPDATE_PAUSED", "更新准备期间暂停创建补充任务，请先恢复接单。"
                )
            prompt = f"Follow up task {task_id}. Human follow-up requirements:\n{note}\nOriginal request excerpt:\n{parent['prompt'][:20000]}\nPrevious reported result excerpt:\n{parent['result'][:20000]}\nRead the full fixed delivery with project_delivery(task_id='{task_id}'). Use the inherited isolated workspace for edits when provided. Delivery still requires human acceptance."
            task = self._create_task(
                db,
                parent["project_id"],
                "补充 / Follow-up: " + parent["title"][:260],
                prompt[: MAX_TEXT // 2],
                parent["assignee_id"],
                parent["permission_mode"],
                parent["model"],
            )
            db.execute(
                "INSERT INTO task_links(task_id,parent_task_id,relation) VALUES(?,?,'follow_up')",
                (task["id"], task_id),
            )
            db.execute(
                "UPDATE tasks SET status='failed',error=?,updated_at=? WHERE id=?",
                (_json({"code": "REVIEW_REJECTED", "message": note}), _now(), task_id),
            )
            self._event(
                db,
                task_id,
                "reviewed",
                note,
                {"decision": "reject", "follow_up_task_id": task["id"]},
            )
            self._governance(
                db,
                "task_reviewed",
                project_id=parent["project_id"],
                task_id=task_id,
                employee_id=parent["assignee_id"],
                reason=note,
                payload={"decision": "reject", "follow_up_task_id": task["id"]},
            )
            return {"task": task, "parent_task": self._entity(self._required(db, "tasks", task_id))}

    def cancel_task(self, task_id: str) -> dict:
        with self._transaction() as db:
            task = self._required(db, "tasks", task_id)
            if task["status"] not in ACTIVE | {"queued"}:
                raise WorkbenchError("invalid_state", "任务已结束，无需取消。")
            status = "cancelled" if task["status"] == "queued" else task["status"]
            db.execute(
                "UPDATE tasks SET status=?,cancel_requested=1,updated_at=? WHERE id=?",
                (status, _now(), task_id),
            )
            self._expire_permissions(db, task_id)
            if not task["cancel_requested"]:
                self._event(
                    db,
                    task_id,
                    "cancel_requested",
                    "已请求取消，等待执行器停止。" if status != "cancelled" else "排队任务已取消。",
                )
            return self._entity(self._required(db, "tasks", task_id))

    def recover_runs(self, node_id: str) -> list[dict]:
        with self._transaction() as db:
            self._required(db, "devices", node_id)
            rows = db.execute(
                "SELECT id FROM tasks WHERE node_id=? "
                "AND status IN ('starting','running','waiting_approval')",
                (node_id,),
            ).fetchall()
            recovered = []
            for row in rows:
                task_id = row["id"]
                db.execute(
                    "UPDATE tasks SET status='interrupted',error=?,updated_at=? WHERE id=?",
                    (
                        _json({"code": "RUN_INTERRUPTED", "message": "服务重启，执行已中断。"}),
                        _now(),
                        task_id,
                    ),
                )
                self._expire_permissions(db, task_id)
                self._event(db, task_id, "interrupted", "服务重启，任务已中断；不会自动重新执行。")
                recovered.append(self._entity(self._required(db, "tasks", task_id)))
            return recovered

    def request_permission(
        self, task_id: str, request_id: str, options: Any, tool_call: Any
    ) -> dict:
        request_id = _text(request_id, "权限请求编号", 200)
        if not isinstance(options, list) or not options or len(options) > 32:
            raise WorkbenchError("invalid_field", "权限请求需要提供有效的选项列表。")
        if not isinstance(tool_call, dict):
            raise WorkbenchError("invalid_field", "权限请求需要提供工具调用说明。")
        _json(options)
        _json(tool_call)
        with self._transaction() as db:
            task = self._required(db, "tasks", task_id)
            if task["status"] not in {"running", "waiting_approval"} or task["cancel_requested"]:
                raise WorkbenchError("invalid_state", "这次执行已停止，不能再请求权限。")
            existing = db.execute(
                "SELECT * FROM permissions WHERE task_id=? AND request_id=?", (task_id, request_id)
            ).fetchone()
            clean = self._scrub(db, {"options": options, "tool_call": tool_call})
            if existing:
                if existing["run_id"] != task["run_id"] or existing["status"] != "pending":
                    raise WorkbenchError("invalid_state", "这个权限请求已处理或过期。")
                if (
                    json.loads(existing["options"]) != clean["options"]
                    or json.loads(existing["tool_call"]) != clean["tool_call"]
                ):
                    raise WorkbenchError("invalid_state", "权限请求内容已改变，请发起新的请求。")
                return self._entity(existing)
            timestamp = _now()
            deadline = (
                datetime.fromisoformat(timestamp) + timedelta(seconds=PERMISSION_TIMEOUT)
            ).isoformat()
            db.execute(
                "INSERT INTO permissions(task_id,request_id,run_id,options,tool_call,"
                "created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                (
                    task_id,
                    request_id,
                    task["run_id"],
                    _json(clean["options"]),
                    _json(clean["tool_call"]),
                    timestamp,
                    deadline,
                ),
            )
            db.execute(
                "UPDATE tasks SET status='waiting_approval',updated_at=? WHERE id=?",
                (_now(), task_id),
            )
            self._event(
                db,
                task_id,
                "permission_requested",
                "员工正在等待你的权限决定。",
                {"request_id": request_id, "tool_call": clean["tool_call"]},
            )
            return self._permission(db, task_id, request_id)

    def _permission(self, db: sqlite3.Connection, task_id: str, request_id: str) -> dict:
        _text(request_id, "权限请求编号", 200)
        row = db.execute(
            "SELECT * FROM permissions WHERE task_id=? AND request_id=?", (task_id, request_id)
        ).fetchone()
        if row is None:
            raise WorkbenchError("not_found", "找不到这个权限请求。")
        return self._entity(row)

    def get_permission(self, task_id: str, request_id: str) -> dict:
        with self._connection() as db:
            self._required(db, "tasks", task_id)
            return self._permission(db, task_id, request_id)

    def _permission_live(self, db, task, permission) -> bool:
        return (
            task["status"] in {"running", "waiting_approval"}
            and not task["cancel_requested"]
            and permission["run_id"] == task["run_id"]
            and self._required(db, "employees", task["assignee_id"])["lifecycle"] != "retired"
            and permission["expires_at"] is not None
            and datetime.fromisoformat(permission["expires_at"]) > datetime.now(timezone.utc)
        )

    def _expire_one_permission(self, db, task, request_id, *, invalidate_resolved=False):
        allowed = "('pending','resolved')" if invalidate_resolved else "('pending')"
        changed = db.execute(
            "UPDATE permissions SET status='expired',decision='deny',resolved_at=? "
            f"WHERE task_id=? AND request_id=? AND status IN {allowed}",
            (_now(), task["id"], request_id),
        )
        if not changed.rowcount:
            return
        if (
            task["status"] == "waiting_approval"
            and not task["cancel_requested"]
            and not db.execute(
                "SELECT 1 FROM permissions WHERE task_id=? AND status='pending'", (task["id"],)
            ).fetchone()
        ):
            db.execute(
                "UPDATE tasks SET status='running',updated_at=? WHERE id=?", (_now(), task["id"])
            )
        self._event(
            db,
            task["id"],
            "permission_expired",
            "权限请求已过期，默认拒绝。",
            {"request_id": request_id},
        )
        self._governance(
            db,
            "permission_expired",
            employee_id=task["assignee_id"],
            project_id=task["project_id"],
            task_id=task["id"],
            actor="runtime",
            payload={"request_id": request_id, "run_id": task["run_id"]},
        )

    def permission_decision(self, task_id: str, request_id: str, run_id: str) -> dict:
        with self._transaction() as db:
            task = self._required(db, "tasks", task_id)
            permission = self._permission(db, task_id, request_id)
            if run_id != task["run_id"] or run_id != permission["run_id"]:
                raise WorkbenchError("permission_denied", "这次执行与权限请求不匹配。")
            if not self._permission_live(db, task, permission):
                if permission["status"] == "pending":
                    self._expire_one_permission(db, task, request_id)
                return {
                    **self._permission(db, task_id, request_id),
                    "decision": "deny",
                    "status": "expired",
                }
            return permission

    def expire_permission(self, task_id: str, request_id: str, run_id: str) -> dict:
        with self._transaction() as db:
            task = self._required(db, "tasks", task_id)
            permission = self._permission(db, task_id, request_id)
            if run_id != task["run_id"] or run_id != permission["run_id"]:
                raise WorkbenchError("permission_denied", "这次执行与权限请求不匹配。")
            if permission["status"] in {"pending", "resolved"}:
                self._expire_one_permission(db, task, request_id, invalidate_resolved=True)
            return self._permission(db, task_id, request_id)

    def resolve_permission(self, task_id: str, request_id: str, decision: str) -> dict:
        _choice(decision, {"allow_once", "deny"}, "请选择仅本次允许或拒绝。")
        expired = False
        with self._transaction() as db:
            task = self._required(db, "tasks", task_id)
            permission = self._permission(db, task_id, request_id)
            if (
                task["status"] != "waiting_approval"
                or task["cancel_requested"]
                or permission["run_id"] != task["run_id"]
                or permission["status"] != "pending"
            ):
                raise WorkbenchError("invalid_state", "这次权限请求已处理或执行已过期。")
            if not self._permission_live(db, task, permission):
                self._expire_one_permission(db, task, request_id)
                expired = True
            else:
                return self._resolve_permission(db, task, request_id, decision)
        if expired:
            raise WorkbenchError(
                "permission_expired", "权限请求已过期并默认拒绝，请让员工重新请求。"
            )

    def _resolve_permission(self, db, task, request_id, decision):
        task_id = task["id"]
        if decision == "allow_once" and not any(
            isinstance(option, dict) and option.get("kind") == "allow_once"
            for option in self._permission(db, task_id, request_id)["options"]
        ):
            raise WorkbenchError("invalid_field", "员工没有提供仅本次允许的选项，请拒绝这次操作。")
        cursor = db.execute(
            "UPDATE permissions SET status='resolved',decision=?,resolved_at=? "
            "WHERE task_id=? AND request_id=? AND status='pending'",
            (
                decision,
                _now(),
                task_id,
                request_id,
            ),
        )
        if cursor.rowcount != 1:
            raise WorkbenchError("invalid_state", "这次权限请求已被其他操作处理。")
        pending = db.execute(
            "SELECT 1 FROM permissions WHERE task_id=? AND status='pending'", (task_id,)
        ).fetchone()
        if not pending:
            db.execute(
                "UPDATE tasks SET status='running',updated_at=? WHERE id=?", (_now(), task_id)
            )
        self._event(
            db,
            task_id,
            "permission_resolved",
            "权限决定已保存。",
            {"request_id": request_id, "decision": decision},
        )
        self._governance(
            db,
            "permission_decided",
            employee_id=task["assignee_id"],
            project_id=task["project_id"],
            task_id=task_id,
            payload={"request_id": request_id, "run_id": task["run_id"], "decision": decision},
        )
        return self._permission(db, task_id, request_id)

    def add_memory(self, project_id: str, title: str, body: str, source: str = "human") -> dict:
        title = _text(title, "记忆标题", 300).strip()
        body = _text(body, "记忆内容")
        source = _text(source, "记忆来源", 100)
        with self._transaction() as db:
            self._required(db, "projects", project_id)
            memory_id = _id("memory")
            db.execute(
                "INSERT INTO memories VALUES(?,?,?,?,?,?)",
                (memory_id, project_id, title, body, source, _now()),
            )
            return self._entity(self._required(db, "memories", memory_id))

    def delete_memory(self, memory_id: str) -> None:
        with self._transaction() as db:
            self._required(db, "memories", memory_id)
            db.execute("DELETE FROM memories WHERE id=?", (memory_id,))

    def search_memory(self, project_id: str, q: str) -> list[dict]:
        q = _text(q, "搜索内容", 1000, empty=True)
        # Escaping LIKE wildcards makes the user's search literal, including % and _.
        needle = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self._connection() as db:
            self._required(db, "projects", project_id)
            rows = db.execute(
                "SELECT * FROM memories WHERE project_id=? "
                "AND (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\') "
                "ORDER BY created_at,rowid LIMIT 100",
                (project_id, needle, needle),
            )
            return self._scrub(db, [self._entity(row) for row in rows])

    @staticmethod
    def _resource_file(path: str | Path) -> tuple[Path, str]:
        try:
            raw = Path(path).expanduser()
            location = raw.resolve(strict=True)
            parts = {part.lower() for part in raw.parts + location.parts}
            if any(
                part.startswith(".env")
                or part
                in {
                    ".ssh",
                    ".aws",
                    ".gnupg",
                    "auth",
                    "auth.json",
                    "credentials",
                    "credentials.json",
                    "credentials.toml",
                    "authorized_keys",
                    "id_rsa",
                    "id_ed25519",
                    "secrets",
                    "secrets.json",
                    "secrets.toml",
                }
                for part in parts
            ):
                raise WorkbenchError("permission_denied", "该文件可能包含凭据，请选择项目资料。")
            if not location.is_file():
                raise WorkbenchError("invalid_path", "资源需要是已有的文本文件。")
            with location.open("rb") as stream:
                data = stream.read(MAX_TEXT + 1)
            if len(data) > MAX_TEXT:
                raise WorkbenchError("payload_too_large", "资源超过 1 MiB，请选择较小的文本文件。")
            if b"\x00" in data:
                raise WorkbenchError("invalid_field", "资源需要是 UTF-8 文本文件。")
            return location, data.decode("utf-8-sig")
        except WorkbenchError:
            raise
        except UnicodeDecodeError as exc:
            raise WorkbenchError("invalid_field", "资源需要是 UTF-8 文本文件。") from exc
        except (OSError, TypeError, ValueError) as exc:
            raise WorkbenchError(
                "invalid_path", "无法读取资源文件，请检查路径与访问权限。"
            ) from exc

    def add_resource(self, project_id: str, name: str, kind: str, path: str | Path) -> dict:
        name = _text(name, "资源名称", 300).strip()
        kind = _text(kind, "资源类型", 100)
        location, _ = self._resource_file(path)
        with self._transaction() as db:
            self._required(db, "projects", project_id)
            resource_id = _id("resource")
            db.execute(
                "INSERT INTO resources VALUES(?,?,?,?,?,?)",
                (resource_id, project_id, name, kind, str(location), _now()),
            )
            return self._entity(self._required(db, "resources", resource_id))

    def read_resource(self, project_id: str, resource_id: str) -> dict:
        with self._connection() as db:
            self._required(db, "projects", project_id)
            row = self._required(db, "resources", resource_id)
            if row["project_id"] != project_id:
                raise WorkbenchError("permission_denied", "该资源不属于当前项目。")
            location, content = self._resource_file(row["path"])
            result = self._scrub(
                db, {"resource": self._entity(row), "content": content, "source": str(location)}
            )
            result["provenance"] = {
                "mode": "live",
                "read_at": _now(),
                "content_sha256": hashlib.sha256(result["content"].encode("utf-8")).hexdigest(),
            }
            return result

    def resource_versions(self, project_id: str, resource_id: str) -> dict:
        from .workbench_resources import versions

        return versions(self, project_id, resource_id)

    def capture_resource_version(
        self,
        project_id: str,
        resource_id: str,
        summary: str = "",
        employee_id: str | None = None,
        content: str | None = None,
    ) -> dict:
        from .workbench_resources import capture

        return capture(self, project_id, resource_id, summary, employee_id, content)

    def read_resource_version(self, project_id: str, resource_id: str, version_id: str) -> dict:
        from .workbench_resources import read

        return read(self, project_id, resource_id, version_id)

    def approve_resource_version(self, project_id: str, resource_id: str, version_id: str) -> dict:
        from .workbench_resources import approve

        return approve(self, project_id, resource_id, version_id)

    def resource_manifest(self, project_id: str) -> dict:
        from .workbench_resources import manifest

        with self._connection() as db:
            db.execute("BEGIN")
            return manifest(self, db, project_id)

    def execution_resource_manifest(self, task_id: str, run_id: str) -> dict:
        from .workbench_resources import execution_manifest

        return execution_manifest(self, task_id, run_id)

    def read_execution_resource(
        self,
        project_id: str,
        resource_id: str,
        task_id: str,
        run_id: str,
        version_id: str = "",
        live: bool = False,
    ) -> dict:
        from .workbench_resources import execution_read

        return execution_read(self, project_id, resource_id, task_id, run_id, version_id, live)

    def knowledge_status(self, project_id: str) -> dict:
        from .workbench_knowledge import knowledge_status

        with self._connection() as db:
            project = self._required(db, "projects", project_id)
            path = project["path"]
        result = knowledge_status(path)
        result["provider_version"] = result.get("version")
        result["reason"] = result.get("error_code") or result.get("freshness_reason")
        with self._connection() as db:
            return self._scrub(db, result)

    def query_knowledge(self, project_id: str, query: str) -> dict:
        from .workbench_knowledge import knowledge_query

        with self._connection() as db:
            project = self._required(db, "projects", project_id)
            path = project["path"]
        result = knowledge_query(path, query)
        result["provider_version"] = result.get("version")
        result["provenance"] = {
            key: result.get(key)
            for key in (
                "provider",
                "version",
                "mode",
                "revision",
                "working_tree_dirty",
                "freshness",
                "freshness_reason",
            )
        }
        result["content"] = json.dumps(result.get("symbols", []), ensure_ascii=False, indent=2)
        with self._connection() as db:
            return self._scrub(db, result)

    def project_context(self, project_id: str, employee_id: str | None = None) -> dict:
        with self._connection() as db:
            db.execute("BEGIN")
            project = self._entity(self._required(db, "projects", project_id))
            team = db.execute(
                "SELECT e.* FROM employees e JOIN memberships m ON m.employee_id=e.id WHERE m.project_id=? ORDER BY e.name,e.id",
                (project_id,),
            ).fetchall()
            result: dict[str, Any] = {
                "project": project,
                "employees": [
                    {
                        k: employee[k]
                        for k in (
                            "id",
                            "name",
                            "kind",
                            "node_id",
                            "status",
                            "lifecycle",
                            "connection_type",
                            "entrypoint",
                            "execution_supported",
                            "execution_verified",
                            "auth_status",
                        )
                    }
                    for employee in (self._employee(db, row) for row in team)
                ],
            }
            for table in ("tasks", "resources", "memories"):
                rows = db.execute(
                    f"SELECT * FROM {table} WHERE project_id=? "
                    "ORDER BY created_at DESC,rowid DESC LIMIT 100",
                    (project_id,),
                )
                items = [self._entity(row) for row in rows]
                if table == "tasks":
                    items = [
                        {
                            k: row[k]
                            for k in (
                                "id",
                                "title",
                                "status",
                                "assignee_id",
                                "result",
                                "request_message_id",
                                "source_task_id",
                            )
                        }
                        for row in items
                    ]
                    for row in items:
                        row["result"] = row["result"][:2000]
                elif table == "memories":
                    for row in items:
                        row["body"] = row["body"][:8000]
                result[table] = items
            from .workbench_resources import manifest

            result["resource_manifest"] = manifest(self, db, project_id)
            if employee_id:
                self._message_member(db, project_id, employee_id)
            message_filter = (
                " AND (recipient_id IS NULL OR recipient_id=? OR sender_id=?)"
                if employee_id
                else ""
            )
            message_args = (project_id, employee_id, employee_id) if employee_id else (project_id,)
            messages = db.execute(
                "SELECT * FROM messages WHERE project_id=?"
                + message_filter
                + " ORDER BY created_at DESC,rowid DESC LIMIT 20",
                message_args,
            ).fetchall()
            result["messages"] = [self._message(db, row) for row in messages]
            for message in result["messages"]:
                message["body"] = message["body"][:4000]
            return self._scrub(db, result)

    @staticmethod
    def _membership(db: sqlite3.Connection, employee_id: str, project_id: str) -> bool:
        return (
            db.execute(
                "SELECT 1 FROM memberships WHERE employee_id=? AND project_id=?",
                (employee_id, project_id),
            ).fetchone()
            is not None
        )

    def employee_credentials(self, employee_id: str, project_id: str) -> dict:
        with self._connection() as db:
            employee = self._required(db, "employees", employee_id)
            if employee["lifecycle"] == "retired":
                raise WorkbenchError("employee_retired", "这位员工已退役，项目授权已撤销。")
            if not self._membership(db, employee_id, project_id):
                raise WorkbenchError("permission_denied", "这位员工没有该项目的访问权限。")
            token = db.execute(
                "SELECT m.secret_token FROM memberships m JOIN employees e ON e.id=m.employee_id WHERE m.employee_id=? AND m.project_id=? AND e.lifecycle!='retired'",
                (employee_id, project_id),
            ).fetchone()[0]
            return {"token": token, "employee_id": employee_id, "project_id": project_id}

    def execution_credentials(self, task_id: str) -> dict:
        """Private capability for one owned execution, never a UI response."""
        with self._connection() as db:
            task = self._required(db, "tasks", task_id)
            employee = self._required(db, "employees", task["assignee_id"])
            if (
                task["status"] not in ACTIVE
                or task["cancel_requested"]
                or employee["lifecycle"] == "retired"
                or not self._membership(db, employee["id"], task["project_id"])
            ):
                raise WorkbenchError("permission_denied", "这次执行当前没有项目工具授权。")
            return {
                "token": task["tool_token"],
                "employee_id": task["assignee_id"],
                "project_id": task["project_id"],
                "task_id": task["id"],
                "run_id": task["run_id"],
            }

    def validate_execution(
        self, token: str, employee_id: str, project_id: str, task_id: str, run_id: str
    ) -> bool:
        if not all(
            isinstance(value, str) and 0 < len(value) <= 200
            for value in (token, employee_id, project_id, task_id, run_id)
        ):
            return False
        with self._connection() as db:
            task = db.execute(
                "SELECT t.* FROM tasks t JOIN employees e ON e.id=t.assignee_id WHERE t.id=? AND t.assignee_id=? AND t.project_id=? AND t.run_id=? AND e.lifecycle!='retired'",
                (task_id, employee_id, project_id, run_id),
            ).fetchone()
            expected = task["tool_token"] if task else "0" * 43
            try:
                matched = hmac.compare_digest(expected.encode("utf-8"), token.encode("utf-8"))
            except UnicodeEncodeError:
                return False
            return bool(
                task
                and matched
                and task["status"] in ACTIVE
                and not task["cancel_requested"]
                and self._membership(db, employee_id, project_id)
            )

    def validate_employee(self, token: str, employee_id: str, project_id: str) -> bool:
        if not all(
            isinstance(value, str) and len(value) <= 200
            for value in (token, employee_id, project_id)
        ):
            return False
        with self._connection() as db:
            employee = db.execute(
                "SELECT m.secret_token FROM memberships m JOIN employees e ON e.id=m.employee_id WHERE m.employee_id=? AND m.project_id=? AND e.lifecycle!='retired'",
                (employee_id, project_id),
            ).fetchone()
            expected = employee[0] if employee else "0" * 43
            try:
                matched = hmac.compare_digest(expected.encode("utf-8"), token.encode("utf-8"))
            except UnicodeEncodeError:
                return False
            return bool(matched and employee and self._membership(db, employee_id, project_id))

    def backup(self, path: Path) -> Path:
        try:
            destination = Path(path).expanduser().resolve()
        except (OSError, TypeError, ValueError) as exc:
            raise WorkbenchError("invalid_path", "请选择有效的备份文件路径。") from exc
        if destination == self.db_path:
            raise WorkbenchError("invalid_path", "备份文件不能覆盖正在使用的数据文件。")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
            with self._connection() as db:
                target = sqlite3.connect(destination)
                try:
                    db.backup(target)
                finally:
                    target.close()
            destination.chmod(0o600)
            return destination
        except FileExistsError as exc:
            raise WorkbenchError("invalid_path", "备份文件已存在，请选择新的文件名。") from exc
        except OSError as exc:
            raise WorkbenchError(
                "storage_error", "无法创建备份，请检查目录权限和磁盘空间。"
            ) from exc
