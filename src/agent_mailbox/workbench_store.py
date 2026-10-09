"""Private, transactional project state for the browser workbench.

This database is deliberately independent of the legacy mailbox. Connections
are short lived, so workers, browser requests and MCP sessions can share it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import backpressure, echo_guard, workbench_contacts, workbench_contract, workbench_views
from .workbench_private import private_mode


class WorkbenchError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


ACTIVE = frozenset({"starting", "running", "waiting_approval"})
FINISH = frozenset({"review", "failed", "cancelled", "interrupted"})
MAX_TEXT = 1024 * 1024
SCHEMA_VERSION = 12
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
# 迁移会补齐的关键列：_ready() 必须一并校验，否则"缺列"的库会被判成就绪，
# 补列逻辑永远够不到，之后公开 API 抛误导性 internal_error、snapshot() 甚至裸 IndexError。
# SCHEMA 声明的**全部**表名：升级路径上"缺任意一张表"都必须被判定为未就绪并幂等重建。
# （第九轮自查发现：原先只校验 6 张表的列，缺 task_links/task_deliveries 之类的表会被判"就绪"。）

REQUIRED_COLUMN_DDL: dict[tuple[str, str], str] = {
    ("devices", "last_seen"): "TEXT",
    ("memberships", "secret_token"): "TEXT",
    ("memberships", "role"): "TEXT NOT NULL DEFAULT ''",
    ("messages", "source_session_id"): "TEXT",
    ("tasks", "model"): "TEXT",
    ("tasks", "tool_token"): "TEXT",
    ("employees", "lifecycle"): "TEXT NOT NULL DEFAULT 'active'",
    ("employees", "lifecycle_reason"): "TEXT NOT NULL DEFAULT ''",
    # 能力相关列保守给空串：宁可不声称可执行，也不误判成"可执行"
    ("employees", "connection_type"): "TEXT NOT NULL DEFAULT ''",
    ("employees", "entrypoint"): "TEXT NOT NULL DEFAULT ''",
    ("permissions", "expires_at"): "TEXT",
}

REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "devices": ("last_seen",),
    "memberships": ("secret_token", "role"),
    "messages": ("source_session_id",),
    "tasks": ("model", "tool_token"),
    "employees": ("lifecycle", "lifecycle_reason", "connection_type", "entrypoint"),
    "permissions": ("expires_at",),
}


PERMISSION_TIMEOUT = 120
LIFECYCLES = frozenset({"active", "paused", "retired"})
EMPLOYEE_STATUSES = frozenset({"installed", "auth_required", "available", "unavailable", "unknown"})
SECRET_KEYS = frozenset(
    {
        "token",
        "secret_token",
        "tool_token",
        "password",
        "authorization",
        "api_key",
        "access_token",
        "token_hash",
        "membership_hash",
    }
)


def _ago_iso(hours: float) -> str:
    """N 小时前的 ISO 时间戳（与库内格式一致）——派单去重/限流的窗口判据。"""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="microseconds")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def raw_connect(path, *, timeout: float | None = None):
    """Open a raw connection with the **same busy_timeout policy** as ``_connection()``.

    仓库里还有几处直连 SQLite（迁移备份源、updates 的快照源）；它们原先用 Python 默认
    ``timeout=5.0`` 且不设 PRAGMA，于是"写路径按配置等待"这条不变量在那些路径上不成立。
    """
    db = sqlite3.connect(
        path, timeout=timeout if timeout is not None else 5.0, isolation_level=None
    )
    db.row_factory = sqlite3.Row
    try:
        db.execute(f"PRAGMA busy_timeout={WorkbenchStore._busy_timeout_ms()}")
    except sqlite3.Error:  # pragma: no cover — 只读介质等
        pass
    return db


def _classify_operational_error(exc: sqlite3.Error) -> WorkbenchError:
    """Map SQLite failures to **actionable** codes.

    ``storage_error`` 只留给真正的磁盘/文件故障；把"只读路径偷写""缺表缺列""语法错"
    "约束违约"也说成"请检查磁盘"会让人永远查不出真因（2026-10-05 第三轮复查指出）。
    """
    text = str(exc).lower()
    if "locked" in text or "busy" in text:
        return WorkbenchError(
            "busy",
            "工作数据正被另一个操作占用，请稍后重试。"
            "（若是把只读/留痕调用嵌套进了已有写事务，请改为外层事务提交后再调用。）",
        )
    if "readonly" in text:
        return WorkbenchError(
            "readonly_violation", "这条只读路径试图写库（只读连接被拒），请报告此缺陷。"
        )
    if any(
        marker in text
        for marker in (
            "no such table",
            "no such column",
            "has no column named",
            "has no column",
            "syntax error",
            "datatype mismatch",
            "columns but",
        )
    ):
        return WorkbenchError(
            "internal_error", "工作数据访问出错（结构或语句不匹配），这不是磁盘问题。"
        )
    if "constraint" in text or "not null" in text or "unique" in text or "foreign key" in text:
        return WorkbenchError("constraint_error", "数据约束不满足（重复、缺失必填或关联无效）。")
    if "out of memory" in text or "no such module" in text:
        # 能力/资源类：不是磁盘问题（报「检查磁盘」会让人查错方向）
        return WorkbenchError(
            "internal_error", "运行环境能力或资源不足（非磁盘问题），请检查 SQLite 构建或内存。"
        )
    if any(
        marker in text
        for marker in (
            "disk i/o",
            "disk is full",
            "malformed",
            "file is not a database",
            "unable to open",
            "io error",
            "corrupt",
        )
    ):
        return WorkbenchError("storage_error", "无法读写工作数据，请检查磁盘后重试。")
    return WorkbenchError("storage_error", "工作数据操作失败，请稍后重试或反馈此消息。")


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
    secret_token TEXT NOT NULL, role TEXT NOT NULL DEFAULT '',
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
    tool_token TEXT NOT NULL, execution_mode TEXT NOT NULL DEFAULT 'managed'
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
CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, channel TEXT NOT NULL,
    target_kind TEXT NOT NULL, target_id TEXT NOT NULL,
    project_id TEXT REFERENCES projects(id), task_id TEXT REFERENCES tasks(id),
    state_hash TEXT NOT NULL, external_message_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS notifications_dedupe
    ON notifications(kind, target_kind, target_id, state_hash);
CREATE TABLE IF NOT EXISTS identity_links (
    channel TEXT NOT NULL, external_id TEXT NOT NULL, employee_id TEXT REFERENCES employees(id),
    note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, PRIMARY KEY(channel, external_id)
);
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
    created_at TEXT NOT NULL, source_session_id TEXT REFERENCES mailbox_sessions(id)
);
CREATE INDEX IF NOT EXISTS messages_project ON messages(project_id,created_at);
CREATE UNIQUE INDEX IF NOT EXISTS messages_request ON messages(project_id,COALESCE(sender_id,''),request_id)
    WHERE request_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS mailbox_sessions (
    id TEXT PRIMARY KEY, employee_id TEXT NOT NULL REFERENCES employees(id),
    project_id TEXT NOT NULL REFERENCES projects(id), label TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE, membership_hash TEXT NOT NULL,
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT
);
"""


def _auxiliary_schema() -> tuple[str, ...]:
    """Idempotent DDL owned by other modules (probes/links/resources/workspaces/marks).

    这些表由各自模块按需创建；把它们纳入"开库时的结构检查与修复"，避免某张辅助表缺失时
    **在读路径才炸**（自查发现：缺 `task_links` 时 `task_detail` 报"结构或语句不匹配"）。
    FTS 索引表**不在其中**：它是派生物（可重建、可关闭），缺了不算结构损坏。
    """
    from . import workbench_onboarding, workbench_resources, workbench_views, workbench_workspaces

    statements: list[str] = []
    for module, name in (
        (workbench_onboarding, "ONBOARDING_SCHEMA"),
        (workbench_resources, "RESOURCE_SCHEMA"),
        (workbench_views, "_DDL"),
        (workbench_workspaces, "WORKSPACE_SCHEMA"),
    ):
        statements.extend(getattr(module, name, ()))
    return tuple(statements)


def _auxiliary_tables() -> frozenset[str]:
    return frozenset(
        re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", "\n".join(_auxiliary_schema()))
    )


SCHEMA_TABLES: frozenset[str] = frozenset(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA))


LEGACY_MIGRATION_MAX_VERSION = 10
"""v<=10 属 legacy 迁移：保持既有保证「迁移失败 ⇒ 用迁移前备份恢复」✗ 不加前置闸（老板 2026-10-07 裁决 a）✓。"""


def _foreign_key_violations(db: sqlite3.Connection) -> list[tuple]:
    """列出孤立外键行（`PRAGMA foreign_key_check` 原始结果）。"""
    return [tuple(row) for row in db.execute("PRAGMA foreign_key_check").fetchall()]


def _assert_no_orphans(db: sqlite3.Connection) -> None:
    """迁移前置闸：**在写任何 schema 之前**拒掉坏库（对抗评审 3 高危之一）。

    评审实测：外键检查拖在迁移**末尾** ✗ ⇒ 库有孤立行时迁移永久失败，而文案让用户
    "恢复迁移前备份" —— 那是**同一份坏库的副本** ⇒ 再失败 ⇒ **死循环** ✓。
    位置：备份之后、写 schema 之前（保住"失败可恢复"的恢复承诺 ✓，又不写入任何 schema ✓）。
    """
    violations = _foreign_key_violations(db)
    if not violations:
        return
    detail = "；".join(
        f"{table} 第 {rowid} 行 → {parent} 缺 {col}" for table, rowid, parent, col in violations[:5]
    )
    more = f"（另有 {len(violations) - 5} 处）" if len(violations) > 5 else ""
    raise WorkbenchError(
        "migration_blocked",
        f"迁移**尚未开始**：库中已有 {len(violations)} 处孤立外键 ⇒ {detail}{more}。"
        "请先修复这些行再升级；**不要**恢复迁移前备份（它可能是同一份坏库 ✗）。",
    )


def _assert_no_orphans_for_version(db: sqlite3.Connection, version: int) -> None:
    """按版本决定是否加闸 ✓（老板 2026-10-07 裁决 a）。

    v<=10 是 legacy 迁移：既有测试明确保证「corrupt v5 ⇒ migration_failed + 恰好一份备份可恢复」✓
    （tests/test_workbench_registry_messages.py:497-501 ✓），且该夹具**确实含 1 处孤立外键**
    （实测 memberships#2 → employees#1 ✓）⇒ 全面加闸会当场破坏该承诺 ✗ ⇒ 故只对 v>=11 加闸 ✓。
    """
    if version and version < LEGACY_MIGRATION_MAX_VERSION + 1:
        return
    _assert_no_orphans(db)


class WorkbenchStore:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.directory = self.root / "workbench"
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            private_mode(self.directory, 0o700)
            self.db_path = self.directory / "state.db"
            # Create privately before SQLite opens the database (no permissive window).
            fd = os.open(self.db_path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            private_mode(self.db_path, 0o600)
        except OSError as exc:
            # 只读文件/父目录不可写：给自解释错误，不抛裸 PermissionError。
            # 并且要**指对对象**：数据文件本身可读但不可写时，问题在文件权限而非目录。
            hint = ""
            fd = -1
            try:
                fd = os.open(self.db_path, os.O_RDONLY)
                hint = "（数据文件本身可读但不可写：请检查**文件**权限，而不是目录）"
            except OSError:
                pass
            finally:
                if fd >= 0:
                    os.close(fd)
            raise WorkbenchError(
                "storage_error",
                f"无法读写工作数据（{type(exc).__name__}: {exc.strerror or exc}）{hint}；"
                "请检查该路径是否存在且可写。",
            ) from exc
        self.migration_backup_path = None
        if self._ready():
            # 结构已是最新且本地节点已登记 ⇒ **不写库、不切 WAL**
            # （否则任何一次只读 CLI 命令都会走迁移写路径，把库改成 WAL）
            return
        with self._connection(switch_journal=False) as db:
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
                        # 前置闸（**仅 v>=11** ✓ 老板裁决 a）：放在**建备份之前** ⇒
                        # 坏库被拒时既不写 schema ✓ 也不留**冗余备份**（评审中危：失败迁移的备份同名堆积 ✗）
                        # v<=10 直接返回 ⇒ legacy「失败⇒用备份恢复」的成文保证不受影响 ✓
                        _assert_no_orphans_for_version(db, version)
                        destination = (
                            self.directory
                            / f"state-v{version}-before-v{SCHEMA_VERSION}-{uuid4().hex}.sqlite"
                        )
                        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                        os.close(fd)
                        private_mode(destination, 0o600)
                        source = raw_connect(self.db_path)  # 活库：走统一超时策略
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
                    if "role" not in columns:
                        db.execute(
                            "ALTER TABLE memberships ADD COLUMN role TEXT NOT NULL DEFAULT ''"
                        )
                    columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
                    if "source_session_id" not in columns:
                        db.execute(
                            "ALTER TABLE messages ADD COLUMN source_session_id TEXT REFERENCES mailbox_sessions(id)"
                        )
                    columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
                    if "model" not in columns:
                        db.execute("ALTER TABLE tasks ADD COLUMN model TEXT")
                    for column, definition in (
                        ("request_message_id", "TEXT REFERENCES messages(id)"),
                        ("source_task_id", "TEXT REFERENCES tasks(id)"),
                        ("tool_token", "TEXT"),
                        ("execution_mode", "TEXT NOT NULL DEFAULT 'managed'"),
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
                            "迁移后关联检查失败，已回滚，原库未改：请修上面的孤立外键后重试（不要恢复迁移前备份 ✗）。",
                        )
                    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
                finally:
                    db.execute("PRAGMA foreign_keys=ON")
        # 版本号一致也可能缺表/缺列（某次迁移部分写入、被手工改动、schema 被替换）：
        # 幂等补齐，且**全或无**（逐条 autocommit 的 ALTER 会在中途失败时留下半修状态）。
        # 放在迁移之后、切 WAL 之前 —— 只有"确实做了修复写入"才切 WAL。
        if not self._ready():
            with self._connection(switch_journal=False) as db:
                try:
                    db.execute("BEGIN IMMEDIATE")
                    self._ensure_schema(db)  # 缺表：逐条 CREATE TABLE IF NOT EXISTS
                    self._ensure_required_columns(db)  # 缺列：ALTER TABLE ADD COLUMN
                    db.commit()
                except sqlite3.Error as exc:
                    db.rollback()
                    text = str(exc).lower()
                    if "view" in text:
                        raise WorkbenchError(
                            "internal_error",
                            "工作数据结构与预期不符（某张表被替换成了视图），已回滚本次修复；"
                            "请从工作目录里的 state-v*-before-v*-*.sqlite 备份恢复。",
                        ) from exc
                    if "locked" in text or "busy" in text:
                        raise WorkbenchError(
                            "busy",
                            "工作数据正被另一个操作占用，结构修复已回滚，请稍后重试。",
                        ) from exc
                    raise WorkbenchError(
                        "storage_error",
                        "工作数据结构修复失败（已回滚，数据保持原样）；"
                        "请从工作目录里的 state-v*-before-v*-*.sqlite 备份恢复。",
                    ) from exc
        with self._transaction() as db:
            if not db.execute("SELECT id FROM devices WHERE is_local=1").fetchone():
                db.execute(
                    "INSERT INTO devices(id,name,is_local,created_at) VALUES(?,?,1,?)",
                    (_id("node"), socket.gethostname(), _now()),
                )
        # 迁移与必要补写都成功之后，才把库切到 WAL ——
        # 否则"迁移失败"这条失败路径本身就把库永久改成 WAL（rollback 撤不回库头）
        with self._connection(switch_journal=False) as db:
            if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                db.execute("PRAGMA journal_mode=WAL")

    @staticmethod
    def _ensure_schema(db: sqlite3.Connection) -> None:
        """Idempotently re-create **missing tables/indexes** from ``SCHEMA``.

        只在事务里逐条执行 ``CREATE … IF NOT EXISTS``（``executescript`` 会隐式提交，
        破坏"全或无"）。缺表的库此前既不修复也不报错，只是被静默切成 WAL。

        约束：按 ``;`` 拆分，因此 ``SCHEMA`` 里**不得**出现触发器或含分号的字符串字面量
        （tests/test_review8_regressions.py::test_rv8_6_schema_is_splittable 会守住这条）。
        """
        for statement in SCHEMA.split(";"):
            stripped = statement.strip()
            if not stripped:
                continue
            upper = stripped.upper()
            if upper.startswith(("CREATE TABLE IF NOT EXISTS", "CREATE INDEX IF NOT EXISTS")):
                db.execute(stripped)
        for statement in _auxiliary_schema():  # 辅助表一并幂等补齐
            clean = statement.strip()
            if clean and "IF NOT EXISTS" in clean.upper():
                db.execute(clean)

    @staticmethod
    def _ensure_required_columns(db: sqlite3.Connection) -> None:
        """Idempotent column repair — runs even when ``user_version`` is already current.

        缺列的库（某次迁移部分写入、被手工改动）此前 forever 够不到迁移块里的补列逻辑，
        之后公开 API 抛误导性 internal_error、``snapshot()`` 抛裸 IndexError。
        """
        for table, columns in REQUIRED_COLUMNS.items():
            present = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            if not present:
                continue  # 整张表都不在：交给建表/迁移路径
            for column in columns:
                if column in present:
                    continue
                ddl = REQUIRED_COLUMN_DDL.get((table, column), "TEXT")
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _ready(self) -> bool:
        """True when the schema is current **and** the local node exists (read-only probe)."""
        try:
            with self._transaction(readonly=True) as db:
                if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                    return False
                present_tables = {
                    row[0]
                    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                if not (SCHEMA_TABLES | _auxiliary_tables()) <= present_tables:
                    return False  # 缺任意一张（核心或辅助）表 ⇒ 交给幂等建表
                for table, columns in REQUIRED_COLUMNS.items():
                    present = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                    if not present or not set(columns) <= present:
                        return False  # 缺表/缺列 ⇒ 交给幂等补列路径
                return db.execute("SELECT id FROM devices WHERE is_local=1").fetchone() is not None
        except sqlite3.Error as exc:
            # 探测失败 ≠ 未就绪：库被锁/损坏时**不许**回退写路径（那会先把 delete 翻成 WAL）
            raise _classify_operational_error(exc) from exc
        except OSError as exc:
            raise WorkbenchError(
                "storage_error", f"无法读取工作数据（{type(exc).__name__}），请检查磁盘与目录权限。"
            ) from exc

    @staticmethod
    def _busy_timeout_ms() -> int:
        """Env override for tests; an invalid value falls back to the default.

        环境变量写错不该让命令吐裸 ValueError（自解释规则）。
        """
        raw = os.environ.get("AGENT_MAIL_BUSY_TIMEOUT_MS")
        if raw is None:
            return 10000
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            return 10000
        if value < 0:
            return 10000
        # 上限 10 分钟：极大值等于「锁住就永久挂起」（环境变量写错不该让命令挂死）
        return min(value, 600_000)

    @contextmanager
    def _connection(
        self, readonly: bool = False, switch_journal: bool = True
    ) -> Iterator[sqlite3.Connection]:
        try:
            db = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        except sqlite3.Error as exc:
            raise WorkbenchError("storage_error", "无法打开工作数据，请检查磁盘后重试。") from exc
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            # 只读路径最多等 2s：写者持锁时快速失败并给出可行动提示，而不是干等
            timeout = min(self._busy_timeout_ms(), 2000) if readonly else self._busy_timeout_ms()
            db.execute(f"PRAGMA busy_timeout={timeout}")
            if not readonly and switch_journal:
                # 只读连接绝不切换 journal_mode；迁移路径也不切（见 __init__），
                # 否则"迁移失败"这条失败路径本身就把库永久改成 WAL（rollback 撤不回库头）
                db.execute("PRAGMA journal_mode=WAL")
            if readonly:
                # 只读路径不许拿写锁：否则一次查询会被写者挡住，甚至误报"磁盘故障"
                db.execute("PRAGMA query_only=ON")
            # SQLite inherits the 0600 database mode for sidecar files.
            yield db
        except sqlite3.OperationalError as exc:
            raise _classify_operational_error(exc) from exc
        except sqlite3.IntegrityError as exc:
            # NOT NULL / UNIQUE / FOREIGN KEY 违约走的是 IntegrityError（不是 OperationalError）
            raise WorkbenchError(
                "constraint_error", "数据约束不满足（重复、缺失必填或关联无效）。"
            ) from exc
        except sqlite3.ProgrammingError as exc:
            raise WorkbenchError(
                "internal_error",
                "工作数据访问出错（参数或类型不匹配），这不是磁盘问题；请重试或反馈此消息。",
            ) from exc
        except sqlite3.Error as exc:
            # DatabaseError（如 "file is not a database"）也走同一分区：原先它绕过分区，
            # 使 "malformed"/"file is not a database" 两条分支永远不可达
            raise _classify_operational_error(exc) from exc
        finally:
            try:
                # Keep SQLite's sidecar handles alive through ACL verification.
                # Closing the final connection can delete them, allowing another
                # connection to recreate the same paths between ACL write/read.
                for suffix in ("", "-wal", "-shm"):
                    try:
                        private_mode(Path(str(self.db_path) + suffix), 0o600)
                    except FileNotFoundError:
                        pass
            finally:
                db.close()

    @contextmanager
    def _transaction(self, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        """``readonly=True`` ⇒ 只读连接 + 延迟 BEGIN（不拿写锁）。

        只读视图（墙/账本/简报/检索/证明查看…）必须走这条：拿写锁会让纯查询被写者挡住，
        并把锁竞争误报成"磁盘/保存"故障（2026-10-05 对抗性复查实测 10s 后失败）。
        """
        with self._connection(readonly=readonly) as db:
            db.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def ensure_project(self, project_id: str) -> None:
        """读路径也要校验项目存在：打错的 --project 不许被静默当成"全局/空"。"""
        with self._transaction(readonly=True) as db:
            self._required(db, "projects", project_id)

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
        result.pop("token_hash", None)
        result.pop("membership_hash", None)
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
        with self._transaction(readonly=True) as db:
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
        keys = set(row.keys())
        get = lambda name: row[name] if name in keys else None
        employee["execution_supported"] = workbench_contract.execution_supported(
            get("kind"), get("connection_type")
        )
        employee["execution_verified"] = bool(
            get("execution_verified") and employee["execution_supported"]
        )
        employee["project_ids"] = [
            member[0]
            for member in db.execute(
                "SELECT project_id FROM memberships WHERE employee_id=? ORDER BY rowid",
                (row["id"],),
            )
        ]
        employee["project_roles"] = {
            member["project_id"]: member["role"]
            for member in db.execute(
                "SELECT project_id,role FROM memberships WHERE employee_id=?", (row["id"],)
            )
        }
        return self._scrub(db, employee)

    def snapshot(self) -> dict:
        with self._transaction(readonly=True) as db:  # 事务已由 _transaction 开启
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
        with self._transaction(readonly=True) as db:
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
            return str(channel)
        # 读路径（updates/status 的 GET 会走这里）必须只读，不能顺手翻库
        with self._transaction(readonly=True) as db:
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
            "INSERT OR IGNORE INTO memberships(employee_id,project_id,secret_token) VALUES(?,?,?)",
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
                "SELECT 1 FROM tasks WHERE project_id=? AND assignee_id=? AND execution_mode='managed' AND status IN ('starting','running','waiting_approval')",
                (project_id, employee_id),
            ).fetchone():
                raise WorkbenchError(
                    "MEMBER_HAS_ACTIVE_TASK",
                    "这名员工在项目中仍有执行中的任务，请先停止工作再移除。",
                )
            if not self._membership(db, employee_id, project_id):
                return self._employee(db, employee)
            for task in db.execute(
                "SELECT id FROM tasks WHERE project_id=? AND assignee_id=? AND (status='queued' OR (execution_mode='mailbox' AND status='running'))",
                (project_id, employee_id),
            ).fetchall():
                db.execute(
                    "UPDATE tasks SET status='cancelled',cancel_requested=1,updated_at=? WHERE id=?",
                    (_now(), task[0]),
                )
                self._event(
                    db, task[0], "member_removed", "员工已移出项目，邮件任务和排队任务已取消。"
                )
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

    def governance_events(
        self, project_id: str | None = None, limit: int | None = 200
    ) -> list[dict]:
        """Recent ledger events (``limit=None`` = the whole ledger).

        The default stays capped for UI-style callers, but **derived views must pass
        ``limit=None``**: a view that silently reads only the newest 200 events makes
        wrong *decisions* once the ledger grows (a contacts check flips to "open",
        an observation window forgets its start, the bridge re-projects a letter).
        """
        with self._transaction(readonly=True) as db:
            clause, params = "", []
            if project_id is not None:
                self._required(db, "projects", project_id)
                clause, params = " WHERE project_id=? OR project_id IS NULL", [project_id]
            limit_clause = "" if limit is None else f" LIMIT {int(limit)}"
            rows = db.execute(
                "SELECT *, rowid AS _seq FROM governance_events"
                + clause
                + " ORDER BY created_at DESC,rowid DESC"
                + limit_clause,
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
            if status in {"paused", "retired"}:
                # Validation revokes every project capability in this transaction.
                # Retain old secrets privately so historical redaction still works.
                for task in db.execute(
                    "SELECT * FROM tasks WHERE assignee_id=? AND status IN ('queued','starting','running','waiting_approval') AND (?='retired' OR execution_mode='mailbox')",
                    (employee_id, status),
                ).fetchall():
                    db.execute(
                        "UPDATE tasks SET status=?,cancel_requested=1,updated_at=? WHERE id=?",
                        (
                            "cancelled"
                            if task["status"] == "queued" or task["execution_mode"] == "mailbox"
                            else task["status"],
                            timestamp,
                            task["id"],
                        ),
                    )
                    self._expire_permissions(db, task["id"])
                    self._event(
                        db,
                        task["id"],
                        "employee_retired" if status == "retired" else "employee_paused",
                        "员工状态已变更，邮件任务和排队任务已取消。"
                        if task["execution_mode"] == "mailbox"
                        else "员工退役，排队任务已取消；执行中的任务已请求停止。",
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

    def _dispatch_guard(self, db, project_id, assignee_id, title, prompt, actor) -> None:
        """派单是特权，不是人手一份（老板 2026-10-06：乱建任务 ⇒ 风暴）。

        三层，全部读项目策略 ``dispatch``：
        ① **默认只有人能派** —— 发起方若**解析成员工**（id 或精确名）才需要白名单，
           其它（人/CLI/系统）一律允许（用结构判据，不用字符串白名单，避免误拦内部路径）；
        ② **每小时派单上限**（默认 60，可配）—— 防刷单风暴的**主力闸**；
        ③ **同目标去重**（``dedupe_window_hours``，**默认 0 = 关**）——
           因为"同一 title+prompt 重派"是**正常重试**，硬默认会误伤（2026-10-06 实测）。
        """
        try:
            from .workbench_policy import effective_policy_in

            policy = effective_policy_in(db, project_id) or {}
        except Exception:  # noqa: BLE001 — 策略读不到时按"只有人能派 + 上限"最保守执行
            policy = {}
        dispatch = policy.get("dispatch") if isinstance(policy.get("dispatch"), dict) else {}
        # **显式豁免**：员工发起的「请求协作」（request_work）不算"派单"，
        # 它有自己的护栏 —— _check_request_chain 跳数上限（workbench_store.py:1448）
        # + 发送侧限速/线索冻结。此前守卫拿 `employee:<id>` 去比 id/name ⇒ 查不到行
        # ⇒ **静默绕过** ✗（第四轮评审 flow2 实测复现）。现在把这条路**写明并单测** ✓
        # —— 从"漏"变成"设计"。要收紧成"必须白名单"是产品决策 ✗ 见简报 round4。
        if isinstance(actor, str) and actor.startswith("employee:"):
            actor_row = None
        else:
            actor_row = db.execute(
                "SELECT name FROM employees WHERE id=? OR name=?", (actor, actor)
            ).fetchone()
        allowed = dispatch.get("employees_may_dispatch") or []
        if actor_row is not None and actor_row["name"] not in allowed:
            raise WorkbenchError(
                "dispatch_not_allowed",
                f"只有人可以在项目里派单（当前发起方是员工「{actor_row['name']}」）。"
                "需要让员工派单时，请把它的员工名加入项目策略 dispatch.employees_may_dispatch。",
            )
        per_hour = int(dispatch.get("max_tasks_per_hour", 60) or 60)
        hourly = db.execute(
            "SELECT count(*) AS c FROM tasks WHERE project_id=? AND created_at >= ?",
            (project_id, _ago_iso(1)),
        ).fetchone()["c"]
        if hourly >= per_hour:
            raise WorkbenchError(
                "dispatch_rate_limited",
                f"这个项目最近 1 小时已派 {hourly} 单（上限 {per_hour}）。"
                "请先处理在办任务，或调整项目策略 dispatch.max_tasks_per_hour。",
            )
        window = int(dispatch.get("dedupe_window_hours", 0) or 0)
        if window > 0:
            duplicate = db.execute(
                "SELECT id FROM tasks WHERE project_id=? AND assignee_id=? AND title=? AND prompt=? "
                "AND status NOT IN ('done', 'cancelled', 'failed') AND created_at >= ? LIMIT 1",
                (project_id, assignee_id, title, prompt, _ago_iso(window)),
            ).fetchone()
            if duplicate is not None:
                raise WorkbenchError(
                    "duplicate_dispatch",
                    f"同一个目标在 {window} 小时内已经派给这位员工（任务 {duplicate['id']}）。"
                    "请沿用原任务，或把目标写得更具体后再派。",
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
        self._dispatch_guard(db, project_id, assignee_id, title, prompt, actor)
        employee = self._required(db, "employees", assignee_id)
        if employee["lifecycle"] != "active":
            raise WorkbenchError(
                "employee_inactive", "这位员工已暂停或退役，请选择正常工作的员工。"
            )
        if not self._membership(db, assignee_id, project_id):
            raise WorkbenchError("permission_denied", "这位员工未加入该项目。")
        if not workbench_contract.execution_supported(
            employee["kind"], employee["connection_type"]
        ):
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
        message["sender_session_id"] = row["source_session_id"]
        message["source_run_id"] = None
        if row["source_task_id"]:
            source = self._required(db, "tasks", row["source_task_id"])
            message["sender_session_id"] = source["session_id"]
            message["source_run_id"] = source["run_id"]
        return self._scrub(db, message)

    def list_messages(self, project_id: str) -> list[dict]:
        with self._transaction(readonly=True) as db:
            self._required(db, "projects", project_id)
            return [
                self._message(db, row)
                for row in db.execute(
                    "SELECT * FROM messages WHERE project_id=? ORDER BY created_at,rowid",
                    (project_id,),
                )
            ]

    # 注意：本方法按设计会写「已读标记」（record_views_in），因此**不是**只读路径
    def employee_messages(
        self, project_id: str, employee_id: str, folder: str = "inbox", limit: int = 100
    ) -> dict:
        """Read a bound employee's folder; viewing never acknowledges or starts work."""
        if folder not in ("inbox", "sent", "group"):
            raise WorkbenchError("invalid_field", "信箱分类必须是 inbox、sent 或 group。")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise WorkbenchError("invalid_field", "信箱读取数量必须为 1–100。")
        with self._transaction() as db:
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
            result = {
                "project_id": project_id,
                "employee_id": employee_id,
                "folder": folder,
                "messages": [self._message(db, row) for row in reversed(rows[:limit])],
                "has_older": len(rows) > limit,
                "viewing_acknowledges": False,
            }
            # 水位线（见 workbench_views）：只记"看过了"，**不改任何消息/任务状态**，
            # 也只在收件箱生效；kill-switch 关闭积压折叠时它不产生任何行为影响。
            if folder == "inbox":
                newest: dict[str, str] = {}
                for message in result["messages"]:
                    sender = message.get("sender_id")
                    created = str(message.get("created_at") or "")
                    if sender and created and created > newest.get(sender, ""):
                        newest[sender] = created
                if newest:
                    workbench_views.record_views_in(db, employee_id, list(newest.items()))
            return result

    def _message_member(self, db, project_id, employee_id):
        try:
            employee = self._required(db, "employees", employee_id)
        except WorkbenchError as exc:
            if exc.code != "not_found":
                raise
            # 自解释（铁律 2）：写错/截断的 employee id 必须说清"是谁错了"，
            # 否则调用方只看到"找不到这条记录"，无从下手（实测 agent 会天天踩）。
            raise WorkbenchError(
                "not_found",
                f"收件人或发件人不存在：{employee_id!r}；"
                "请核对 employee id 是否完整（可用 project_context 取同事的准确 id）。",
            ) from exc
        if not self._membership(db, employee_id, project_id):
            raise WorkbenchError(
                "permission_denied",
                f"消息中的员工必须属于当前项目：{employee_id!r}（成员见 project_context 的 employees）。",
            )
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

    def _fold_message(
        self,
        db: sqlite3.Connection,
        project_id: str,
        sender_id: str,
        recipient_id: str,
        title: str,
        now: str,
    ) -> dict | None:
        """Fold a flooding sender's message into one digest row (rules S2/S4).

        Returns the digest message (marked ``folded``), or None when the sender is
        still under the limit and the message should be delivered normally.
        Only titles survive; bodies are dropped — the honest trade for a flood.
        """
        # 第三档（水位线第一步）：按"未看过的积压"折叠 —— 治慢滴；
        # 有 kill-switch（AGENT_MAILBOX_BACKLOG_FOLD=0），且与速率档并列、任一命中即折叠。
        backlog_trip = (
            workbench_views.backlog_folding_enabled()
            and workbench_views.backlog_count_in(db, recipient_id, sender_id)
            >= workbench_views.backlog_limit()
        )
        spec = backpressure.tiers()
        counts = []
        for window, _cap in spec:
            cutoff = backpressure.since(now, window)
            counts.append(
                db.execute(
                    "SELECT COUNT(*) AS c FROM messages WHERE project_id=? AND sender_id=? "
                    "AND recipient_id=? AND created_at>=? "
                    "AND (thread_id IS NULL OR thread_id NOT LIKE ?)",
                    (project_id, sender_id, recipient_id, cutoff, backpressure.DIGEST_MARK + "%"),
                ).fetchone()["c"]
            )
        recent = counts[0]
        if not backlog_trip and not backpressure.should_fold_tiered(counts):
            return None
        window = spec[0][0]  # 摘要窗口用**短窗**：长窗会让摘要挂 6 小时，误伤后续正常对话
        key = backpressure.digest_thread_id(sender_id, recipient_id)
        row = db.execute(
            "SELECT * FROM messages WHERE project_id=? AND sender_id=? AND recipient_id=? "
            "AND thread_id=? AND created_at>=? ORDER BY created_at DESC LIMIT 1",
            (project_id, sender_id, recipient_id, key, cutoff),
        ).fetchone()
        if row is not None:
            body = backpressure.append_folded(row["body"], title, sender_id, recipient_id)
            count = backpressure.count_folded(body)
            message_id = row["id"]
            db.execute(
                "UPDATE messages SET body=?,title=? WHERE id=?",
                (
                    self._scrub(db, body),
                    self._scrub(db, backpressure.digest_title(sender_id, recipient_id, count)),
                    message_id,
                ),
            )
        else:
            body = backpressure.new_digest_body(title, sender_id, recipient_id)
            count = backpressure.count_folded(body)
            message_id = _id("message")
            db.execute(
                "INSERT INTO messages(id,project_id,title,body,sender_id,recipient_id,reply_to,thread_id,"
                "request_work,source_task_id,request_id,request_digest,created_at,source_session_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    message_id,
                    project_id,
                    self._scrub(db, backpressure.digest_title(sender_id, recipient_id, count)),
                    self._scrub(db, body),
                    sender_id,
                    recipient_id,
                    None,
                    key,
                    0,
                    None,
                    None,
                    hashlib.sha256(_json([key, count]).encode()).hexdigest(),
                    now,
                    None,
                ),
            )
        self._governance(
            db,
            "message_folded",
            employee_id=sender_id,
            project_id=project_id,
            actor=f"employee:{sender_id}" if sender_id else "human",
            reason=(
                f"{sender_id} → {recipient_id} 在 {int(window)}s 内已发 {recent} 条，"
                f"超过上限 {backpressure.fold_limit()}，本条折叠进摘要"
            ),
            payload={
                "message_id": message_id,
                "thread_id": key,
                "folded_title": title,
                "folded_count": count,
                "recent": recent,
            },
        )
        result = self._message(db, self._required(db, "messages", message_id))
        result["folded"] = True
        result["folded_count"] = count
        return result

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
        mailbox_token: str | None = None,
    ) -> dict:
        title = _text(title, "消息标题", 300).strip()
        body = _text(body, "消息内容")
        if type(request_work) is not bool:
            raise WorkbenchError("invalid_field", "请明确是否请求员工执行工作。")
        if request_id is not None:
            request_id = _text(request_id, "请求编号", 200)
        if mailbox_token is not None:
            if not isinstance(mailbox_token, str) or not mailbox_token:
                raise WorkbenchError("UNAUTHORIZED", "项目邮箱凭据无效。")
            title = title.replace(mailbox_token, "[redacted]")
            body = body.replace(mailbox_token, "[redacted]")
        digest = hashlib.sha256(
            _json([title, body, recipient_id, reply_to, request_work, source_task_id]).encode()
        ).hexdigest()
        with self._transaction() as db:
            self._required(db, "projects", project_id)
            source_session_id = None
            if mailbox_token is not None:
                from .workbench_mail_sessions import validate_session

                session = validate_session(self, db, mailbox_token)
                if (
                    session["employee_id"] != sender_id
                    or session["project_id"] != project_id
                    or source_task_id is not None
                    or request_work
                ):
                    raise WorkbenchError("UNAUTHORIZED", "邮箱会话不能冒用其他身份或触发受管执行。")
                source_session_id = session["id"]
            if sender_id is not None:
                self._message_member(db, project_id, sender_id)
                if source_task_id is None and source_session_id is None:
                    raise WorkbenchError(
                        "MESSAGE_SOURCE_REQUIRED", "员工消息需要绑定当前受管执行。"
                    )
                source = self._required(db, "tasks", source_task_id) if source_task_id else None
                if source is not None and (
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
                # T33 通讯录白名单：收件人一旦有通讯录，只有列内发件人可达（默认无列表 ⇒ 开放，不误伤）
                verdict = workbench_contacts.may_message_in(db, project_id, sender_id, recipient_id)
                if not verdict["allowed"]:
                    raise WorkbenchError(
                        "CONTACT_REQUIRED",
                        "收件人设置了通讯录白名单，发件人不在其中；请先请对方把你加入通讯录。",
                    )
            elif sender_id is not None:
                # 员工不能靠"群发"绕过白名单：本项目只要有人设了通讯录，员工群发即拒
                enforcing = workbench_contacts.whitelisted_members_in(db, project_id)
                if enforcing:
                    raise WorkbenchError(
                        "CONTACT_REQUIRED",
                        "本项目有员工设置了通讯录白名单，员工不能群发（会绕过白名单）；请明确指定收件人。",
                    )
            if request_work and (recipient_id is None or recipient_id == sender_id):
                raise WorkbenchError("invalid_field", "请求工作需要明确选择另一名项目员工。")
            parent = self._required(db, "messages", reply_to) if reply_to is not None else None
            if parent and parent["project_id"] != project_id:
                raise WorkbenchError("permission_denied", "回复必须留在同一个项目和消息线程。")
            if (
                parent
                and sender_id
                and parent["recipient_id"] is not None
                and sender_id not in (parent["sender_id"], parent["recipient_id"])
            ):
                raise WorkbenchError("permission_denied", "不能回复无权查看的私信。")
            # S3 执行点：被判定为回声环而冻结的线程，自动发送必须被挡住
            # （检测在 echo_guard，这里只负责"不许再发"；解冻需人工留痕）
            if parent is not None and echo_guard.thread_frozen(self, parent["thread_id"]):
                raise WorkbenchError(
                    "THREAD_FROZEN",
                    "该线程因疑似回声环已被冻结，请人工确认后解除再发（解冻走 governance 事件）。",
                )
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
            timestamp = _now()
            # 背压（S2/S4）：同一发件人→同一收件人超限 ⇒ 后续消息折叠进一条摘要，
            # 收件箱不再线性增长（2026-10-04 回放：单日单对 70 封单向刷屏）。
            if (
                not request_work
                and request_id is None
                and sender_id is not None
                and recipient_id is not None
                and recipient_id != sender_id
            ):
                folded = self._fold_message(
                    db, project_id, sender_id, recipient_id, title, timestamp
                )
                if folded is not None:
                    return folded
            message_id = _id("message")
            db.execute(
                "INSERT INTO messages(id,project_id,title,body,sender_id,recipient_id,reply_to,thread_id,request_work,source_task_id,request_id,request_digest,created_at,source_session_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    source_session_id,
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

    @staticmethod
    def _claim_sql(node_id: str, project_ids: list[str] | None) -> tuple[str, list[str]]:
        """The one claim query (shared by the writer and the read-only peek)."""
        execution_clause, execution_params = workbench_contract.execution_sql("e")
        params: list[str] = [node_id]
        scope_clause = ""
        if project_ids is not None:
            placeholders = ",".join("?" for _ in project_ids)
            scope_clause = f" AND t.project_id IN ({placeholders}) "
            params.extend(project_ids)
        sql = (
            "SELECT t.* FROM tasks t WHERE t.node_id=? AND t.status='queued' AND t.execution_mode='managed' "
            + scope_clause
            + "AND EXISTS (SELECT 1 FROM employees e WHERE e.id=t.assignee_id AND e.lifecycle='active' "
            + execution_clause
            + ") "
            + "AND EXISTS (SELECT 1 FROM memberships m WHERE m.employee_id=t.assignee_id AND m.project_id=t.project_id) "
            + "AND NOT EXISTS (SELECT 1 FROM tasks a WHERE a.assignee_id=t.assignee_id "
            "AND a.execution_mode='managed' AND a.status IN ('starting','running','waiting_approval')) "
            "ORDER BY t.created_at,t.rowid LIMIT 1"
        )
        return sql, [*params, *execution_params]

    def peek_claim(self, node_id: str, project_ids: list[str] | None = None) -> dict | None:
        """Which task *would* be claimed next — **read-only** (no status change).

        ``run_now`` needs this: claiming blindly can start a different task than the one
        the human asked for, and there is no ``starting → queued`` way back.
        """
        with self._transaction(readonly=True) as db:
            self._required(db, "devices", node_id)
            if db.execute("SELECT paused FROM update_settings WHERE id=1").fetchone()[0]:
                return None
            if project_ids == []:
                return None
            sql, params = self._claim_sql(node_id, project_ids)
            row = db.execute(sql, params).fetchone()
            return self._entity(row) if row is not None else None

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
            sql, params = self._claim_sql(node_id, project_ids)
            row = db.execute(sql, params).fetchone()
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
        with self._transaction(readonly=True) as db:
            return self._scrub(db, self._entity(self._required(db, "tasks", task_id)))

    def task_detail(self, task_id: str) -> dict:
        with self._transaction(readonly=True) as db:
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
            if parent["execution_mode"] == "mailbox":
                from .workbench_mail_sessions import _bound_store
                from .workbench_mail_tasks import create_mail_task

                prompt = f"Follow up mail task {task_id}. Human requirements:\n{note}\nOriginal request:\n{parent['prompt'][:20000]}\nPrevious reported result:\n{parent['result'][:20000]}\nUse project_delivery(task_id='{task_id}') to read the delivery. Work in your existing session. Explicitly accept the new mail task before submitting. Human acceptance is still required."
                task = create_mail_task(
                    _bound_store(self, db),
                    parent["project_id"],
                    "补充 / Follow-up: " + parent["title"][:260],
                    prompt[: MAX_TEXT // 2],
                    parent["assignee_id"],
                )
            else:
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
            status = (
                "cancelled"
                if task["status"] == "queued" or task["execution_mode"] == "mailbox"
                else task["status"]
            )
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
                    "已请求取消，等待执行器停止。"
                    if status != "cancelled"
                    else "邮件任务或排队任务已取消。",
                )
            return self._entity(self._required(db, "tasks", task_id))

    def recover_runs(self, node_id: str) -> list[dict]:
        with self._transaction() as db:
            self._required(db, "devices", node_id)
            rows = db.execute(
                "SELECT id FROM tasks WHERE node_id=? "
                "AND execution_mode='managed' AND status IN ('starting','running','waiting_approval')",
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
        with self._transaction(readonly=True) as db:
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
        _choice(
            decision,
            {"allow_once", "allow_run", "deny"},
            "请选择仅本次允许、本任务内允许或拒绝。",
        )
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

    # 配额/限流信号：命中即视为"这个 agent 没额度了"，要上报给人（老板 2026-10-06 要求）
    QUOTA_PATTERNS = (
        "429",
        "too many requests",
        "rate limit",
        "rate_limit",
        "insufficient_quota",
        "quota exceeded",
        "quota_exhausted",
        "exceeded your current quota",
        "额度不足",
        "配额已用尽",
        "配额不足",
        "超出配额",
        "账户余额不足",
    )

    HEARTBEAT_TYPE = "agent_heartbeat"
    QUOTA_TYPE = "quota_exhausted"
    QUOTA_RESTORED_TYPE = "quota_restored"

    @classmethod
    def classify_run_output(cls, text: str) -> str | None:
        """把一段运行输出归类：命中配额/限流信号返回 ``quota_exhausted``，否则 ``None``。

        为什么单独分类：429/配额耗尽**不是普通失败** ✓ —— 普通失败要人判断技术原因 ✓，
        而"没额度了" 要人**去续费/换套餐** ✓（同一句话，动作完全不同 ✓）。
        """
        if not text:
            return None
        lowered = text.lower()
        return cls.QUOTA_TYPE if any(pat.lower() in lowered for pat in cls.QUOTA_PATTERNS) else None

    def record_quota_restored(
        self, employee_id: str, project_id: str | None = None, detail: str = ""
    ) -> None:
        """显式声明"额度已恢复"（续费/换套餐/成功跑通一次之后）。

        **心跳不能代替它** ✗ —— 第四轮评审 eng-verify 实测：写一条心跳就把 quota_exhausted
        洗成 False ⇒ 教室视图假绿（心跳只证"活着"，不证"额度恢复" ✓）。
        """
        with self._transaction() as db:
            self._required(db, "employees", employee_id)
            self._governance(
                db,
                self.QUOTA_RESTORED_TYPE,
                employee_id=employee_id,
                project_id=project_id,
                task_id=None,
                actor="human",
                reason=detail,
            )

    def record_heartbeat(
        self, employee_id: str, project_id: str | None = None, detail: str = ""
    ) -> None:
        """记一次心跳（落 governance 事件，无需改表结构）。"""
        with self._transaction() as db:
            self._required(db, "employees", employee_id)
            self._governance(
                db,
                self.HEARTBEAT_TYPE,
                employee_id=employee_id,
                project_id=project_id,
                task_id=None,
                actor=employee_id,
                reason=detail,
            )

    def agent_health(self, project_id: str | None = None, stale_seconds: int = 120) -> list[dict]:
        """每个 agent 的健康：心跳新旧 · 是否没额度 · 是否在办（只读）。

        * **心跳**：来自 ``agent_heartbeat`` 事件；**没有心跳来源就显式说"不可用"** ✗
          （不拿 `devices.last_seen` 那种"没有写入方的列"凑数 —— 第二轮评审的教训 ✓）
        * **额度**：最近一条 `quota_exhausted` 事件晚于最近一条心跳 ⇒ 判定"没额度" ✓
        """
        with self._transaction(readonly=True) as db:
            scope, params = "", ()
            if project_id:
                scope, params = " AND project_id = ?", (project_id,)
            out = []
            for row in db.execute("SELECT id, name, kind FROM employees ORDER BY name").fetchall():
                eid = row["id"]
                hb = db.execute(
                    "SELECT created_at FROM governance_events WHERE type=? AND employee_id=?"
                    f"{scope} ORDER BY created_at DESC LIMIT 1",
                    (self.HEARTBEAT_TYPE, eid, *params),
                ).fetchone()
                quota = db.execute(
                    "SELECT created_at FROM governance_events WHERE type=? AND employee_id=?"
                    f"{scope} ORDER BY created_at DESC LIMIT 1",
                    (self.QUOTA_TYPE, eid, *params),
                ).fetchone()
                restored = db.execute(
                    "SELECT created_at FROM governance_events WHERE type=? AND employee_id=?"
                    f"{scope} ORDER BY created_at DESC LIMIT 1",
                    (self.QUOTA_RESTORED_TYPE, eid, *params),
                ).fetchone()
                hb_at, quota_at = (
                    (hb["created_at"] if hb else None),
                    (quota["created_at"] if quota else None),
                )
                age = (
                    db.execute(
                        "SELECT CAST((julianday('now') - julianday(?)) * 86400 AS INTEGER)",
                        (hb_at,),
                    ).fetchone()[0]
                    if hb_at
                    else None
                )
                out.append(
                    {
                        "id": eid,
                        "name": row["name"],
                        "kind": row["kind"],
                        "heartbeat_at": hb_at,
                        "heartbeat_age_seconds": age,
                        "heartbeat_available": hb is not None,  # 没来源就说不可用 ✓
                        "stale": bool(hb_at) and age is not None and age > stale_seconds,
                        "quota_exhausted": (
                            bool(quota_at)
                            and (restored is None or quota_at > restored["created_at"])
                        ),
                        "quota_at": quota_at,
                        "quota_restored_at": restored["created_at"] if restored else None,
                    }
                )
            return out

    def activity_snapshot(self, project_id: str | None = None) -> dict:
        """教室视图的数据底座：每个员工一行 + 每台设备一行（只读）。

        设计依据（第二轮评审）：
        * **砍掉"多久没动"** ✗ —— `devices.last_seen` 没有写入方（只在配对时写一次），
          拿它排序等于**展示假数字**，会制造不信任；
        * **加"最近一件产出/验收"** ✓ —— 四问全绿仍可能是空转（天天在跑、0 交付）；
        * 时间一律用 `datetime()` 归一化比较（`expires_at` 是 ISO-T+tz，裸比字符串会侥幸成立 ✗）。
        """
        now_sql = "datetime('now')"
        with self._transaction(readonly=True) as db:
            scope_e, params_e = "", ()
            if project_id:
                scope_e = " AND m.project_id = ?"
                params_e = (project_id,)
            employees = []
            rows = db.execute(
                "SELECT e.id, e.name, e.kind, e.lifecycle, e.connection_type "
                "FROM employees e LEFT JOIN memberships m ON m.employee_id = e.id "
                f"WHERE 1=1{scope_e} GROUP BY e.id ORDER BY e.name",
                params_e,
            ).fetchall()
            for row in rows:
                eid = row["id"]
                live_sessions = db.execute(
                    "SELECT count(*) AS c FROM mailbox_sessions WHERE employee_id=? "
                    f"AND revoked_at IS NULL AND ({now_sql} < datetime(expires_at) OR expires_at IS NULL)",
                    (eid,),
                ).fetchone()["c"]
                active = db.execute(
                    "SELECT count(*) AS c FROM tasks WHERE assignee_id=? "
                    "AND status IN ('queued','starting','running','waiting_approval')",
                    (eid,),
                ).fetchone()["c"]
                last = db.execute(
                    "SELECT type, created_at FROM governance_events WHERE employee_id=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (eid,),
                ).fetchone()
                produced = db.execute(
                    "SELECT id, title, updated_at FROM tasks WHERE assignee_id=? AND status IN ('review','done') "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (eid,),
                ).fetchone()
                employees.append(
                    {
                        "id": eid,
                        "name": row["name"],
                        "kind": row["kind"],
                        "lifecycle": row["lifecycle"],
                        "connection_type": row["connection_type"],
                        "live_sessions": live_sessions,
                        "active_tasks": active,
                        # 接线状态：有活跃会话 = 已接；否则登记未接（不再拿 execution_verified 硬凑）
                        "wired": live_sessions > 0,
                        "last_action": last["created_at"] if last else None,
                        "last_action_type": last["type"] if last else None,
                        "last_outcome": (
                            {
                                "task_id": produced["id"],
                                "title": produced["title"],
                                "at": produced["updated_at"],
                            }
                            if produced
                            else None
                        ),
                    }
                )
            devices = []
            for row in db.execute("SELECT id, name, is_local, status FROM devices").fetchall():
                running = db.execute(
                    "SELECT count(*) AS c FROM tasks WHERE status IN ('starting','running','waiting_approval')",
                ).fetchone()["c"]
                devices.append(
                    {
                        "id": row["id"],
                        "name": row["name"],
                        "is_local": bool(row["is_local"]),
                        "status": row["status"],
                        "running_tasks": running,
                        # 明确不提供 last_seen：没有写入方，避免展示假数字 ✗
                        "heartbeat_available": False,
                    }
                )
            return {"employees": employees, "devices": devices}

    def proof_index(self, project_id: str | None = None) -> dict[str, str]:
        """任务 → 交付证明结论（只含有证明的任务）。

        数据源是 ``governance_events`` 的 ``delivery_proof``（没有独立的证明表）。
        列表行要能直接看出"这一单有没有据"——证据前移到决策点（第 10 轮 + 两路评审共识）。
        """
        with self._transaction(readonly=True) as db:
            sql = "SELECT task_id, payload, created_at FROM governance_events WHERE type = 'delivery_proof'"
            params: tuple = ()
            if project_id:
                sql += " AND project_id = ?"
                params = (project_id,)
            index: dict[str, str] = {}
            for row in db.execute(sql + " ORDER BY created_at", params):
                payload = row["payload"]
                try:
                    data = json.loads(payload) if isinstance(payload, str) else (payload or {})
                except ValueError:
                    data = {}
                index[row["task_id"]] = str(data.get("verdict") or data.get("status") or "recorded")
            return index

    def pending_permissions(self, project_id: str | None = None) -> list[dict]:
        """未决的权限请求（只读）。

        没有它，人侧只能看到"有人在等"，看不出**等的是什么** —— 一屏三问就答不全
        （第 10 轮/flow2 报告 FL-a）。
        """
        with self._transaction(readonly=True) as db:
            sql = (
                "SELECT p.request_id, p.task_id, p.run_id, p.created_at, p.expires_at, "
                "t.project_id, t.title FROM permissions p JOIN tasks t ON t.id = p.task_id "
                "WHERE p.status = 'pending'"
            )
            params: tuple = ()
            if project_id:
                sql += " AND t.project_id = ?"
                params = (project_id,)
            return [dict(row) for row in db.execute(sql + " ORDER BY p.created_at", params)]

    def run_has_allow_run(self, task_id: str, run_id: str) -> bool:
        """这个 run 是否已被授权「本任务内允许」（只读）。

        人批过一次后，同一 run 的后续权限请求不再打断人（引擎据此自动放行）。
        """
        with self._transaction(readonly=True) as db:
            row = db.execute(
                "SELECT * FROM permissions WHERE task_id=? AND run_id=? "
                "AND decision='allow_run' ORDER BY created_at DESC LIMIT 1",
                (task_id, run_id),
            ).fetchone()
            if row is None:
                return False
            expires = row["expires_at"]
            if expires:
                # 存活校验：过期请求不再构成"本任务内允许"（已批准记录是 resolved，故只看时间）
                try:
                    if datetime.fromisoformat(str(expires)) <= datetime.now(timezone.utc):
                        return False
                except ValueError:
                    return False
            return True

    def _resolve_permission(self, db, task, request_id, decision):
        task_id = task["id"]
        if decision in {"allow_once", "allow_run"} and not any(
            isinstance(option, dict) and option.get("kind") == decision
            for option in self._permission(db, task_id, request_id)["options"]
        ):
            raise WorkbenchError("invalid_field", "员工没有提供该批准选项，请拒绝这次操作。")
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
        with self._transaction(readonly=True) as db:
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
        with self._transaction(readonly=True) as db:
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

        with self._transaction(readonly=True) as db:
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

        with self._transaction(readonly=True) as db:
            project = self._required(db, "projects", project_id)
            path = project["path"]
        result = knowledge_status(path)
        result["provider_version"] = result.get("version")
        result["reason"] = result.get("error_code") or result.get("freshness_reason")
        with self._transaction(readonly=True) as db:
            return self._scrub(db, result)

    def merge_employee_memberships(
        self, old_employee_id: str, new_employee_id: str, *, dry_run: bool = True
    ) -> dict:
        """合并两行的 **memberships**（合并员工行前**必须先做** ✓ **默认 dry-run** ✗）。

        为什么必须单独做（评审预言 + 我实测双证 ✗）：`memberships` 主键是 `(employee_id, project_id)`
        ⇒ 直接 `UPDATE memberships SET employee_id=新行`，在"两人同属一个项目"时**必然撞约束** ✗
        （实测到 `IntegrityError` → `constraint_error` ✓）。

        规则（按项目分情况 ✓）：
        * 两人**同属**该项目 ⇒ 删旧行 ✓ + **撤销旧行的邮箱会话**（token 随之失效 ✓）
        * **只有旧行**属于该项目 ⇒ 改指向新行 ✓（无冲突 ✓）
        返回 {moved_projects, dropped_projects, revoked_sessions} ⇒ 迁移前后**数字对账**用 ✓。
        """
        if old_employee_id == new_employee_id:
            raise WorkbenchError("INVALID_MERGE", "合并目标不能是同一行。")
        with self._transaction() as db:
            self._required(db, "employees", old_employee_id)
            self._required(db, "employees", new_employee_id)
            old_projects = [
                row["project_id"]
                for row in db.execute(
                    "SELECT project_id FROM memberships WHERE employee_id=?", (old_employee_id,)
                )
            ]
            new_projects = {
                row["project_id"]
                for row in db.execute(
                    "SELECT project_id FROM memberships WHERE employee_id=?", (new_employee_id,)
                )
            }
            moved = [pid for pid in old_projects if pid not in new_projects]
            dropped = [pid for pid in old_projects if pid in new_projects]
            session_ids = [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM mailbox_sessions WHERE employee_id=? AND revoked_at IS NULL",
                    (old_employee_id,),
                )
            ]
            if not dry_run:
                for project_id in dropped:
                    db.execute(
                        "DELETE FROM memberships WHERE employee_id=? AND project_id=?",
                        (old_employee_id, project_id),
                    )
                for project_id in moved:
                    db.execute(
                        "UPDATE memberships SET employee_id=? WHERE employee_id=? AND project_id=?",
                        (new_employee_id, old_employee_id, project_id),
                    )
                for session_id in session_ids:
                    db.execute(
                        "UPDATE mailbox_sessions SET revoked_at=? WHERE id=?", (_now(), session_id)
                    )
            return {
                "old": old_employee_id,
                "new": new_employee_id,
                "dry_run": dry_run,
                "moved_projects": moved,
                "dropped_projects": dropped,
                "revoked_sessions": session_ids,
                "moved": len(moved),
                "dropped": len(dropped),
                "sessions": len(session_ids),
            }

    def repoint_employee_references(
        self, old_employee_id: str, new_employee_id: str, *, dry_run: bool = True
    ) -> dict:
        """把 `old` 的引用重指向 `new`（**默认 dry-run** ✗ 显式 False 才写 ✓）。

        **故意排除 `memberships`** ✗：主键 `(employee_id, project_id)` ⇒ 盲 UPDATE 必撞约束 ✗
        （实测 `constraint_error` ✓ 评审亦预言 ✓）⇒ 必须**先**调 `merge_employee_memberships()` ✓。

        覆盖两类（只做外键会漏第二类 ✗）：
        ① 动态发现的 id 列（employee_id/sender_id/recipient_id/assignee_id/owner_id ✓ 除 memberships）
        ② `governance_events.actor` 的 `'employee:<id>'` **字符串**（无外键 ⇒ foreign_key_check 看不见 ✗）
        返回每处 `表.列` 受影响行数 + `memberships_skipped` ⇒ 迁移前后**数字对账**用 ✓。
        """
        if old_employee_id == new_employee_id:
            raise WorkbenchError("INVALID_MERGE", "合并目标不能是同一行。")
        with self._transaction() as db:
            self._required(db, "employees", old_employee_id)
            self._required(db, "employees", new_employee_id)
            id_columns = ("employee_id", "sender_id", "recipient_id", "assignee_id", "owner_id")
            targets: list[tuple[str, str]] = []
            for (table_name,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ):
                if table_name == "memberships":
                    continue  # ✗ 交给 merge_employee_memberships（先去重 ✓）
                for column in db.execute(f"PRAGMA table_info({table_name})"):
                    if column["name"] in id_columns:
                        targets.append((table_name, column["name"]))
            counts: dict[str, int] = {}
            for table, column in targets:
                affected = db.execute(
                    f"SELECT count(*) FROM {table} WHERE {column}=?", (old_employee_id,)
                ).fetchone()[0]
                if affected:
                    counts[f"{table}.{column}"] = affected
                    if not dry_run:
                        db.execute(
                            f"UPDATE {table} SET {column}=? WHERE {column}=?",
                            (new_employee_id, old_employee_id),
                        )
            actor_prefix = f"employee:{old_employee_id}"
            actor_affected = db.execute(
                "SELECT count(*) FROM governance_events WHERE actor=?", (actor_prefix,)
            ).fetchone()[0]
            if actor_affected:
                counts["governance_events.actor(字符串)"] = actor_affected
                if not dry_run:
                    db.execute(
                        "UPDATE governance_events SET actor=? WHERE actor=?",
                        (f"employee:{new_employee_id}", actor_prefix),
                    )
            return {
                "old": old_employee_id,
                "new": new_employee_id,
                "dry_run": dry_run,
                "affected": counts,
                "total": sum(counts.values()),
                "memberships_skipped": db.execute(
                    "SELECT count(*) FROM memberships WHERE employee_id=?", (old_employee_id,)
                ).fetchone()[0],
            }

    # ── ③ 外发通知（outbox）：**本地是权威 ✓ 飞书只是通道** ✗ ────────────────────
    def record_notification(
        self,
        kind: str,
        *,
        channel: str,
        target_kind: str,
        target_id: str,
        state_hash: str,
        project_id: str | None = None,
        task_id: str | None = None,
    ) -> dict:
        """记一条外发通知；**同一 (kind, 目标, 状态哈希) 只可能有一行** ✓。

        幂等由**数据库唯一索引**保证 ✓（不是靠调用方自觉 ✗）⇒ "**没变就闭嘴**" 成为结构性事实 ✓
        （`notifications_dedupe` ✓）。重复调用返回既有行 ✓ 而不是报错 ✗。
        """
        if not kind or not channel or not target_kind or not target_id or not state_hash:
            raise WorkbenchError(
                "INVALID_NOTIFICATION", "通知的 kind/channel/目标/状态哈希都不能为空。"
            )
        now = _now()
        with self._transaction() as db:
            existing = db.execute(
                "SELECT * FROM notifications WHERE kind=? AND target_kind=? AND target_id=? AND state_hash=?",
                (kind, target_kind, target_id, state_hash),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            notification_id = _id("note")
            db.execute(
                "INSERT INTO notifications(id,kind,channel,target_kind,target_id,project_id,task_id,"
                "state_hash,external_message_id,status,attempts,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,NULL,'pending',0,?,?)",
                (
                    notification_id,
                    kind,
                    channel,
                    target_kind,
                    target_id,
                    project_id,
                    task_id,
                    state_hash,
                    now,
                    now,
                ),
            )
            return dict(
                db.execute("SELECT * FROM notifications WHERE id=?", (notification_id,)).fetchone()
            )

    def mark_notification(
        self,
        notification_id: str,
        *,
        status: str,
        external_message_id: str | None = None,
        last_error: str | None = None,
    ) -> dict:
        """记录发送结果（成功 ⇒ 存 `external_message_id` 供**原地更新** ✓）。"""
        with self._transaction() as db:
            self._required(db, "notifications", notification_id)
            db.execute(
                "UPDATE notifications SET status=?, external_message_id=COALESCE(?, external_message_id),"
                " last_error=?, attempts=attempts+1, updated_at=? WHERE id=?",
                (status, external_message_id, last_error, _now(), notification_id),
            )
            return dict(
                db.execute("SELECT * FROM notifications WHERE id=?", (notification_id,)).fetchone()
            )

    def latest_notification(self, kind: str, *, target_kind: str, target_id: str) -> dict | None:
        """该目标最近一次外发的状态哈希 ⇒ 与当前状态比对即可"变了才发" ✓（只读 ✓）。"""
        with self._transaction(readonly=True) as db:
            row = db.execute(
                "SELECT * FROM notifications WHERE kind=? AND target_kind=? AND target_id=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (kind, target_kind, target_id),
            ).fetchone()
            return dict(row) if row else None

    def link_identity(
        self, channel: str, external_id: str, employee_id: str, note: str = ""
    ) -> dict:
        """外部身份 → 员工（评审指出此前**缺这张表** ✗）；重复绑定覆盖 ✓。"""
        if not channel or not external_id:
            raise WorkbenchError("INVALID_IDENTITY", "channel 与 external_id 都不能为空。")
        with self._transaction() as db:
            self._required(db, "employees", employee_id)
            db.execute(
                "INSERT INTO identity_links(channel,external_id,employee_id,note,created_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(channel,external_id) DO UPDATE SET employee_id=excluded.employee_id, note=excluded.note",
                (channel, external_id, employee_id, note, _now()),
            )
            return dict(
                db.execute(
                    "SELECT * FROM identity_links WHERE channel=? AND external_id=?",
                    (channel, external_id),
                ).fetchone()
            )

    def identity_employee(self, channel: str, external_id: str) -> str | None:
        """反查：外部身份对应的员工 id（未绑定 ⇒ None ✓ 不猜 ✗）。"""
        with self._transaction(readonly=True) as db:
            row = db.execute(
                "SELECT employee_id FROM identity_links WHERE channel=? AND external_id=?",
                (channel, external_id),
            ).fetchone()
            return row["employee_id"] if row else None

    def task_counts_by_status(self, project_id: str | None = None) -> dict:
        """只读：任务**按状态计数**（投影卡"一屏三问"的数据源 ✓ 真实列值 ✓ 不猜 ✗）。

        真实状态分布（2026-10-07 实测 ✓）：`queued` / `review` / `failed` / `done`。
        另即时算出 `done_today`（按 `updated_at` 日期 ✓）。
        """
        with self._transaction(readonly=True) as db:
            rows = db.execute(
                "SELECT status, count(*) AS c FROM tasks "
                + ("WHERE project_id=? " if project_id else "")
                + "GROUP BY status",
                (project_id,) if project_id else (),
            )
            counts = {row["status"]: row["c"] for row in rows}
            today = _now()[:10]
            done_today = db.execute(
                "SELECT count(*) FROM tasks WHERE status='done' AND substr(updated_at,1,10)=? "
                + ("AND project_id=? " if project_id else ""),
                (today, project_id) if project_id else (today,),
            ).fetchone()[0]
            today_items = [
                row["title"]
                for row in db.execute(
                    "SELECT title FROM tasks WHERE status='done' AND substr(updated_at,1,10)=? "
                    + ("AND project_id=? " if project_id else "")
                    + "ORDER BY updated_at DESC LIMIT 5",
                    (today, project_id) if project_id else (today,),
                )
            ]
        return {"counts": counts, "done_today": done_today, "today_items": today_items}

    def duplicate_employee_report(self) -> dict:
        """只读：列出**疑似重复的员工身份**及其引用计数（合并迁移的数字对账基准 ✓）。

        HS 审计：同一人因 connection_type 不同建了多行（DSH / DSH App ✗ …）。
        本报告**只读** ✓：分组 · 每行引用计数 · 建议保留行 · 需重指向的行数 ✓
        引用统计**动态找列**并**单独统计 `governance_events.actor` 字符串**（无外键 ⇒
        `foreign_key_check` 看不见 ✗ 评审教训 ✓）。
        """
        with self._transaction(readonly=True) as db:
            # 显式循环（推导里重名 row 会把列名当表名 ✗ —— 已踩）
            id_columns = ("employee_id", "sender_id", "recipient_id", "assignee_id", "owner_id")
            columns: list[tuple[str, str]] = []
            for (table_name,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ):
                for column in db.execute(f"PRAGMA table_info({table_name})"):
                    if column["name"] in id_columns:
                        columns.append((table_name, column["name"]))
            people = [
                dict(row)
                for row in db.execute(
                    "SELECT id, name, kind, connection_type, lifecycle, node_id, created_at "
                    "FROM employees ORDER BY name, created_at"
                )
            ]
            refs: dict[str, int] = {person["id"]: 0 for person in people}
            detail: list[dict] = []
            for table, column in columns:
                for row in db.execute(
                    f"SELECT {column} AS eid, count(*) AS c FROM {table} "
                    f"WHERE {column} IS NOT NULL GROUP BY {column}"
                ):
                    if row["eid"] in refs:
                        refs[row["eid"]] += row["c"]
                        detail.append(
                            {
                                "table": table,
                                "column": column,
                                "employee_id": row["eid"],
                                "count": row["c"],
                            }
                        )
            for row in db.execute(
                "SELECT actor, count(*) AS c FROM governance_events "
                "WHERE actor LIKE 'employee:%' GROUP BY actor"
            ):
                eid = row["actor"][len("employee:") :]
                if eid in refs:
                    refs[eid] += row["c"]
                    detail.append(
                        {
                            "table": "governance_events",
                            "column": "actor(字符串)",
                            "employee_id": eid,
                            "count": row["c"],
                        }
                    )
            groups: dict[tuple, list[dict]] = {}
            for person in people:
                groups.setdefault((person["name"].split()[0].lower(), person["kind"]), []).append(
                    person
                )
        duplicates = []
        for (person, kind), rows in sorted(groups.items()):
            if len(rows) < 2:
                continue
            for row in rows:
                row["references"] = refs.get(row["id"], 0)
            keep = max(rows, key=lambda r: (r["references"], r["created_at"]))
            duplicates.append(
                {
                    "person": person,
                    "kind": kind,
                    "forms": len(rows),
                    "keeper_suggestion": keep["id"],
                    "rows": rows,
                    "references_to_move": sum(
                        r["references"] for r in rows if r["id"] != keep["id"]
                    ),
                }
            )
        return {
            "employees": len(people),
            "duplicate_groups": duplicates,
            "total_duplicate_rows": sum(len(g["rows"]) for g in duplicates),
            "reference_breakdown": detail,
            "ok": not duplicates,
        }

    def orphan_report(self) -> dict:
        """只读体检：列出**孤立引用**（迁移前必查 ✓）。

        两类都要查（对抗评审实测 ✗：只查 `PRAGMA foreign_key_check` 会漏掉第二类 ✓）：
        ① 有外键的孤立行 —— `PRAGMA foreign_key_check` ✓
        ② **无外键但存 id 的引用**：`mail_view_marks.employee_id`（建表未写 REFERENCES ✗）
           与 `governance_events.actor` 里 `'employee:<id>'` 字符串（会被反解成 id ✓）
        """

        def _has_table(db: sqlite3.Connection, name: str) -> bool:
            return (
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
                ).fetchone()
                is not None
            )

        with self._transaction(readonly=True) as db:
            violations = [list(row) for row in _foreign_key_violations(db)]
            ghost_marks = 0
            if _has_table(db, "mail_view_marks"):  # 视图层懒建 ⇒ 缺表时计 0（不报假警 ✗）
                ghost_marks = db.execute(
                    "SELECT count(*) FROM mail_view_marks WHERE employee_id NOT IN (SELECT id FROM employees)"
                ).fetchone()[0]
            ghost_actors = db.execute(
                "SELECT count(*) FROM governance_events WHERE actor LIKE 'employee:%' "
                "AND substr(actor, 10) NOT IN (SELECT id FROM employees)"
            ).fetchone()[0]
        return {
            "foreign_key_violations": violations,
            "ghost_view_marks": ghost_marks,
            "ghost_actors": ghost_actors,
            "ok": not violations and ghost_marks == 0 and ghost_actors == 0,
        }

    def project_path_for(self, project_id: str) -> str:
        """按项目解析 checkout 路径（**只读诊断的唯一入口** ✓）。

        agent **不传路径** ✗ —— 服务端按项目解析后再交给 graft/aoci 包装 ✓（HS 2026-10-07 裁定 ✓）。
        """
        with self._transaction(readonly=True) as db:
            row = self._required(db, "projects", project_id)
        return str(row["path"])

    def query_knowledge(self, project_id: str, query: str) -> dict:
        from .workbench_knowledge import knowledge_query

        with self._transaction(readonly=True) as db:
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
        with self._transaction(readonly=True) as db:
            return self._scrub(db, result)

    def project_context(self, project_id: str, employee_id: str | None = None) -> dict:
        with self._transaction(readonly=True) as db:  # 事务已由 _transaction 开启
            project = self._entity(self._required(db, "projects", project_id))
            team = db.execute(
                "SELECT e.*,m.role AS project_role FROM employees e JOIN memberships m ON m.employee_id=e.id WHERE m.project_id=? ORDER BY e.name,e.id",
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
                            "project_role",
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
        with self._transaction(readonly=True) as db:
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
        with self._transaction(readonly=True) as db:
            task = self._required(db, "tasks", task_id)
            employee = self._required(db, "employees", task["assignee_id"])
            if (
                task["execution_mode"] != "managed"
                or task["status"] not in ACTIVE
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
        with self._transaction(readonly=True) as db:
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
                and task["execution_mode"] == "managed"
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
        with self._transaction(readonly=True) as db:
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
            private_mode(destination, 0o600)
            with self._connection() as db:
                target = sqlite3.connect(destination)
                try:
                    db.backup(target)
                finally:
                    target.close()
            private_mode(destination, 0o600)
            return destination
        except FileExistsError as exc:
            raise WorkbenchError("invalid_path", "备份文件已存在，请选择新的文件名。") from exc
        except OSError as exc:
            raise WorkbenchError(
                "storage_error", "无法创建备份，请检查目录权限和磁盘空间。"
            ) from exc
