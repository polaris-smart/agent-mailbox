"""Discover hosts and enrol them in one step (roadmap T2).

The bar comes from outside: a competitor's onboarding is **one paste**, and the
roadmap's own judge is "a clean machine reaches its first completed task in three
minutes". This module turns the existing discovery machinery into that path:

  discover → plan → enrol (one command) → **paste one block** into the host

Design rules:
  * **discovery never enrols** — planning is free and read-only, so looking around
    cannot change the machine;
  * **enrolment is idempotent** — re-running reuses the existing employee and only
    issues a fresh session;
  * **one paste, per host** — the snippet is generated from the real session file
    and binary, in the host's own config shape (formats verified against the live
    configs on this machine: Hermes YAML, WorkBuddy JSON, Codex TOML).
"""

from __future__ import annotations

import json
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

from . import workbench_runtime
from .workbench_contract import execution_supported
from .workbench_store import WorkbenchError

HOSTS = ("generic", "hermes", "workbuddy", "codex", "claude")
SESSION_DIR = "workbench/mail-sessions"


def default_binary() -> str:
    """The CLI a host should launch: PATH first, else this interpreter's sibling."""
    found = shutil.which("agent-mailbox")
    if found:
        return found
    sibling = Path(sys.executable).parent / "agent-mailbox"
    return str(sibling) if sibling.exists() else "agent-mailbox"


def discover(*, installed_only: bool = False) -> list[dict[str, Any]]:
    """Normalised view over the runtime probe (installed first, executable first)."""
    records = workbench_runtime.discover_employees()
    out = []
    for record in records:
        kind = record.get("kind")
        connection = record.get("connection_type") or "cli"
        installed = bool(record.get("entrypoint"))
        out.append(
            {
                "kind": kind,
                "name": record.get("name") or kind,
                "connection_type": connection,
                "entrypoint": record.get("entrypoint"),
                "installed": installed,
                "auth_status": record.get("auth_status") or "unknown",
                "detail": record.get("detail") or "",
                "execution_supported": execution_supported(kind, connection),
            }
        )
    if installed_only:
        out = [row for row in out if row["installed"]]
    out.sort(
        key=lambda row: (not row["installed"], not row["execution_supported"], row["name"] or "")
    )
    return out


def enrolled(store, project_id: str) -> dict[str, list[dict[str, Any]]]:
    """Which kinds already have a member of this project (read-only)."""
    with store._transaction(readonly=True) as db:
        store._required(db, "projects", project_id)
        rows = db.execute(
            "SELECT e.id, e.name, e.kind, e.connection_type FROM employees e "
            "JOIN memberships m ON m.employee_id = e.id WHERE m.project_id = ?",
            (project_id,),
        ).fetchall()
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        item = {k: row[k] for k in row.keys()}  # noqa: SIM118
        out.setdefault(item["kind"], []).append(item)
    return out


def plan(store, project_id: str | None = None) -> dict[str, Any]:
    """What *could* be enrolled, and what is already in — **no writes at all**.

    ``project_id`` 为空 = 「只看本机能装什么」：此时不校验也不查成员
    （强校验只针对「给了项目」，否则空项目会被误伤成 invalid_field）。
    """
    if project_id:
        store.ensure_project(project_id)
    found = discover()
    have = enrolled(store, project_id) if project_id else {}
    suggestions = []
    for row in found:
        if not row["installed"] or not row["execution_supported"]:
            continue
        already = [
            item
            for item in have.get(row["kind"], [])
            if item["name"] == row["name"] and item["connection_type"] == row["connection_type"]
        ]
        suggestions.append(
            {
                **row,
                "already_enrolled": bool(already),
                "existing": [item["name"] for item in already],
                "command": (
                    f"agent-mailbox enroll {shlex.quote(row['kind'])} "
                    f"--project {shlex.quote(project_id)} --name {shlex.quote(row['name'])}"
                    if project_id
                    else None
                ),
                "needs_project": not bool(project_id),
            }
        )
    return {
        "discovered": found,
        "enrolled_kinds": {kind: [item["name"] for item in items] for kind, items in have.items()},
        "suggestions": suggestions,
        "summary": {
            "installed": len([row for row in found if row["installed"]]),
            "executable": len([row for row in found if row["execution_supported"]]),
            "already_enrolled": sum(row["already_enrolled"] for row in suggestions),
        },
    }


def mcp_snippet(host: str, *, binary: str, session_file: str) -> str:
    """One paste, in the host's own config shape (no manual editing afterwards)."""
    if host not in HOSTS:
        raise ValueError(f"未知宿主：{host}（可选：{', '.join(HOSTS)}）")
    args = ["mailbox-mcp", "--session-file", session_file]
    if host in ("generic", "workbuddy"):
        return json.dumps(
            {"mcpServers": {"agent-mailbox": {"command": binary, "args": args}}},
            ensure_ascii=False,
            indent=2,
        )
    if host == "hermes":
        lines = ["mcp_servers:", "  agent-mailbox:", f"    command: {binary}", "    args:"]
        lines += [f"      - {arg}" for arg in args]
        return "\n".join(lines)
    if host == "codex":
        lines = [
            "[mcp_servers.agent-mailbox]",
            f'command = "{binary}"',
            f"args = {json.dumps(args)}",
        ]
        return "\n".join(lines)
    return " ".join(["claude mcp add agent-mailbox --", binary, *args])


def enroll(
    store,
    project_id: str,
    kind: str,
    *,
    name: str | None = None,
    connection_type: str = "cli",
    host: str = "generic",
    binary: str | None = None,
) -> dict[str, Any]:
    """Create (or reuse) the employee, issue a session, and return the paste block."""
    if kind not in workbench_runtime.KNOWN_AGENTS:
        raise ValueError(f"未知的 agent 类型：{kind}")
    default_name = workbench_runtime.KNOWN_AGENTS[kind]
    # GUI/CLI/endpoint are separate host identities, each with one stable inbox.
    # Keep the established CLI name for existing installs; distinguish other
    # connection shapes without forcing a novice to invent a label.
    suffix = {"app": " App", "endpoint": " Endpoint"}.get(connection_type, "")
    label = name or f"{default_name}{suffix}"
    with store._transaction(readonly=True) as db:
        store._required(db, "projects", project_id)
        node_id = store.local_node_id(db)
        if name is None and suffix:
            legacy = db.execute(
                "SELECT connection_type FROM employees WHERE kind=? AND name=? AND node_id=?",
                (kind, default_name, node_id),
            ).fetchone()
            if legacy is not None and legacy["connection_type"] == connection_type:
                label = default_name  # Preserve an established App/endpoint mailbox.
        existing = db.execute(
            "SELECT * FROM employees WHERE kind=? AND name=? AND node_id=?",
            (kind, label, node_id),
        ).fetchone()
    created = existing is None
    if existing is not None and existing["connection_type"] != connection_type:
        raise WorkbenchError(
            "IDENTITY_CONNECTION_CONFLICT",
            "该身份已经使用另一连接入口，请为不同连接建立新员工身份。",
        )
    if existing is None:
        employee = store.create_employee(label, kind, project_id, connection_type=connection_type)
    else:
        store.add_project_member(project_id, existing["id"])
        employee = {k: existing[k] for k in existing.keys()}  # noqa: SIM118

    from .workbench_mail_sessions import create_session
    from .workbench_private import private_mode

    session_path = None
    manifest_created = False

    def prepare_private_file(issued):
        nonlocal session_path, manifest_created
        session_path = store.root / SESSION_DIR / f"{issued['id']}.json"
        session_path.parent.mkdir(parents=True, exist_ok=True)
        private_mode(session_path.parent, 0o700)
        session_path.touch(mode=0o600, exist_ok=False)
        manifest_created = True
        private_mode(session_path, 0o600)
        session_path.write_text(
            json.dumps(
                {
                    "home": str(store.root),
                    "session_id": issued["id"],
                    "employee_id": employee["id"],
                    "project_id": project_id,
                    "token": issued["token"],
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )

    # File preparation and same-label key rotation share one commit boundary.
    # A failed file write rolls back the revocation; another label is untouched.
    try:
        session = create_session(
            store,
            employee["id"],
            project_id,
            f"接入 · {label}",
            rotate_label=True,
            on_issued=prepare_private_file,
        )
    except BaseException:
        if manifest_created and session_path is not None:
            session_path.unlink(missing_ok=True)
        raise
    binary = binary or default_binary()
    return {
        "employee": {"id": employee["id"], "name": employee["name"], "kind": kind},
        "project_id": project_id,
        "created": created,
        "session_id": session["id"],
        "session_file": str(session_path),
        "expires_at": session.get("expires_at"),
        "binary": binary,
        "host": host,
        "snippet": mcp_snippet(host, binary=binary, session_file=str(session_path)),
    }


def onboard(
    store,
    project_id: str,
    *,
    apply: bool = False,
    kinds: list[str] | None = None,
    host: str = "generic",
    binary: str | None = None,
) -> dict[str, Any]:
    """The three-step path: discover → (optionally) enrol → paste.

    ``apply=False`` (default) is a **dry run**: it plans and prints, and writes
    nothing — looking around must never change the machine.

    入伙**必须**有项目：建员工与会话都要落到具体项目上。空项目直接拒绝，
    否则干跑会承诺一批 ``would_enroll``，而 ``apply=True`` 立刻报错（承诺与结果相反）。
    """
    if not str(project_id or "").strip():
        raise WorkbenchError(
            "invalid_field", "入伙需要明确项目：请传 --project（空项目无法建员工/会话）。"
        )
    outline = plan(store, project_id)
    targets = [
        row
        for row in outline["suggestions"]
        if (kinds is None or row["kind"] in kinds) and not row["already_enrolled"]
    ]
    results = []
    for row in targets:
        if not apply:
            results.append({"kind": row["kind"], "name": row["name"], "action": "would_enroll"})
            continue
        enrolled_row = enroll(
            store,
            project_id,
            row["kind"],
            name=row["name"],
            connection_type=row["connection_type"],
            host=host,
            binary=binary,
        )
        results.append(
            {"kind": row["kind"], "name": row["name"], "action": "enrolled", **enrolled_row}
        )
    return {
        "dry_run": not apply,
        "project_id": project_id,
        "summary": outline["summary"],
        "steps": results,
    }
