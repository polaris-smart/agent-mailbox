"""Private, transactional project state for the browser workbench.

This database is deliberately independent of the legacy mailbox. Connections
are short lived, so workers, browser requests and MCP sessions can share it.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
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
SCHEMA_VERSION = 2
EMPLOYEE_STATUSES = frozenset({"installed", "auth_required", "available", "unavailable", "unknown"})
SECRET_KEYS = frozenset(
    {"token", "secret_token", "password", "authorization", "api_key", "access_token"}
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
    project_id TEXT NOT NULL REFERENCES projects(id),
    node_id TEXT NOT NULL REFERENCES devices(id), status TEXT NOT NULL,
    detail TEXT NOT NULL, secret_token TEXT NOT NULL, created_at TEXT NOT NULL
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
    result TEXT NOT NULL DEFAULT '', error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
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
    created_at TEXT NOT NULL, resolved_at TEXT,
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
        with self._connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise WorkbenchError("incompatible_version", "数据来自更新的版本，请升级程序。")
            if version < SCHEMA_VERSION:
                # Apply schema creation separately so version-1 databases can
                # receive the additive migration within the same write lock.
                db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
                try:
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
                    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
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
        for key in ("cancel_requested", "is_local"):
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
                "SELECT secret_token FROM employees UNION SELECT secret_token FROM memberships"
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
        employee["project_ids"] = [
            member[0]
            for member in db.execute(
                "SELECT project_id FROM memberships WHERE employee_id=? ORDER BY rowid",
                (row["id"],),
            )
        ]
        return employee

    def snapshot(self) -> dict:
        with self._connection() as db:
            db.execute("BEGIN")
            result = {}
            for table in ("projects", "employees", "tasks", "resources", "memories", "devices"):
                rows = db.execute(f"SELECT * FROM {table} ORDER BY created_at,rowid")
                result[table] = [
                    self._employee(db, row) if table == "employees" else self._entity(row)
                    for row in rows
                ]
            return self._scrub(db, result)

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
        project_id: str,
        node_id: str | None = None,
        status: str = "unknown",
        detail: str = "",
    ) -> dict:
        name = _text(name, "员工名称", 200).strip()
        _choice(kind, {"codex", "claude"}, "目前支持 Codex 和 Claude 员工。")
        _choice(status, EMPLOYEE_STATUSES, "请选择有效的员工连接状态。")
        detail = _text(detail, "状态说明", 4096, empty=True)
        with self._transaction() as db:
            self._required(db, "projects", project_id)
            if node_id is None:
                node_id = db.execute("SELECT id FROM devices WHERE is_local=1").fetchone()[0]
            self._required(db, "devices", node_id)
            existing = db.execute(
                "SELECT id FROM employees WHERE name=? AND kind=? AND node_id=?",
                (name, kind, node_id),
            ).fetchone()
            employee_id = existing[0] if existing else _id("employee")
            if not existing:
                db.execute(
                    "INSERT INTO employees VALUES(?,?,?,?,?,?,?,?,?)",
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
                    ),
                )
            db.execute(
                "INSERT OR IGNORE INTO memberships VALUES(?,?,?)",
                (employee_id, project_id, secrets.token_urlsafe(32)),
            )
            return self._employee(db, self._required(db, "employees", employee_id))

    def update_employee(self, employee_id: str, status: str, detail: str = "") -> dict:
        _choice(status, EMPLOYEE_STATUSES, "请选择有效的员工连接状态。")
        detail = _text(detail, "状态说明", 4096, empty=True)
        with self._transaction() as db:
            self._required(db, "employees", employee_id)
            db.execute(
                "UPDATE employees SET status=?,detail=? WHERE id=?", (status, detail, employee_id)
            )
            return self._employee(db, self._required(db, "employees", employee_id))

    def create_task(
        self,
        project_id: str,
        title: str,
        prompt: str,
        assignee_id: str,
        permission_mode: str = "read-only",
    ) -> dict:
        title = _text(title, "任务名称", 300).strip()
        prompt = _text(prompt, "任务说明")
        _choice(
            permission_mode,
            {"read-only", "workspace-write"},
            "请选择只读或允许修改项目目录的执行权限。",
        )
        with self._transaction() as db:
            self._required(db, "projects", project_id)
            employee = self._required(db, "employees", assignee_id)
            if not self._membership(db, assignee_id, project_id):
                raise WorkbenchError("permission_denied", "这位员工未加入该项目。")
            task_id, timestamp = _id("task"), _now()
            db.execute(
                "INSERT INTO tasks(id,project_id,title,prompt,assignee_id,node_id,"
                "permission_mode,run_id,session_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
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
                ),
            )
            self._event(db, task_id, "queued", "任务已进入队列。")
            return self._entity(self._required(db, "tasks", task_id))

    def claim_task(self, node_id: str) -> dict | None:
        with self._transaction() as db:
            self._required(db, "devices", node_id)
            row = db.execute(
                "SELECT t.* FROM tasks t WHERE t.node_id=? AND t.status='queued' "
                "AND NOT EXISTS (SELECT 1 FROM tasks a WHERE a.assignee_id=t.assignee_id "
                "AND a.status IN ('starting','running','waiting_approval')) "
                "ORDER BY t.created_at,t.rowid LIMIT 1",
                (node_id,),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE tasks SET status='starting',updated_at=? WHERE id=?", (_now(), row["id"])
            )
            self._event(db, row["id"], "starting", "正在连接员工执行入口。")
            return self._entity(self._required(db, "tasks", row["id"]))

    def get_task(self, task_id: str) -> dict:
        with self._connection() as db:
            return self._entity(self._required(db, "tasks", task_id))

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

    def finish_task(self, task_id: str, status: str, result: str = "", error: Any = None) -> dict:
        _choice(status, FINISH, "请选择待验收、失败、取消或中断状态。")
        result = _text(result, "任务结果", empty=True)
        serialized_error = None if error is None else _json(error)
        with self._transaction() as db:
            task = self._required(db, "tasks", task_id)
            if task["status"] not in ACTIVE:
                raise WorkbenchError("invalid_state", "这次执行已结束或尚未开始。")
            if task["cancel_requested"] and status == "review":
                status = "cancelled"
            db.execute(
                "UPDATE tasks SET status=?,result=?,error=?,updated_at=? WHERE id=?",
                (status, result, serialized_error, _now(), task_id),
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
            return self._entity(self._required(db, "tasks", task_id))

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
            db.execute(
                "INSERT INTO permissions(task_id,request_id,run_id,options,tool_call,"
                "created_at) VALUES(?,?,?,?,?,?)",
                (
                    task_id,
                    request_id,
                    task["run_id"],
                    _json(clean["options"]),
                    _json(clean["tool_call"]),
                    _now(),
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

    def resolve_permission(self, task_id: str, request_id: str, decision: str) -> dict:
        _choice(decision, {"allow_once", "deny"}, "请选择仅本次允许或拒绝。")
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
            return self._scrub(
                db, {"resource": self._entity(row), "content": content, "source": str(location)}
            )

    def project_context(self, project_id: str) -> dict:
        with self._connection() as db:
            db.execute("BEGIN")
            project = self._entity(self._required(db, "projects", project_id))
            result: dict[str, Any] = {"project": project}
            for table in ("tasks", "resources", "memories"):
                rows = db.execute(
                    f"SELECT * FROM {table} WHERE project_id=? "
                    "ORDER BY created_at DESC,rowid DESC LIMIT 100",
                    (project_id,),
                )
                items = [self._entity(row) for row in rows]
                if table == "tasks":
                    items = [
                        {k: row[k] for k in ("id", "title", "status", "assignee_id", "result")}
                        for row in items
                    ]
                    for row in items:
                        row["result"] = row["result"][:2000]
                elif table == "memories":
                    for row in items:
                        row["body"] = row["body"][:8000]
                result[table] = items
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
            self._required(db, "employees", employee_id)
            if not self._membership(db, employee_id, project_id):
                raise WorkbenchError("permission_denied", "这位员工没有该项目的访问权限。")
            token = db.execute(
                "SELECT secret_token FROM memberships WHERE employee_id=? AND project_id=?",
                (employee_id, project_id),
            ).fetchone()[0]
            return {"token": token, "employee_id": employee_id, "project_id": project_id}

    def validate_employee(self, token: str, employee_id: str, project_id: str) -> bool:
        if not all(
            isinstance(value, str) and len(value) <= 200
            for value in (token, employee_id, project_id)
        ):
            return False
        with self._connection() as db:
            employee = db.execute(
                "SELECT secret_token FROM memberships WHERE employee_id=? AND project_id=?",
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
