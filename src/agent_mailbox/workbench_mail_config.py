"""Export private, revocable session credentials without putting tokens in UI config."""

from __future__ import annotations

import json
import os
import sys

from .workbench_private import private_access, private_mode
from .workbench_store import WorkbenchError


def _configuration(path):
    args = [] if getattr(sys, "frozen", False) else ["-E", "-m", "agent_mailbox"]
    return {
        "mcpServers": {
            "agent-mailbox-project": {
                "command": sys.executable,
                "args": [*args, "mailbox-mcp", "--session-file", str(path)],
            }
        }
    }


def export_session(store, employee_id, project_id, label):
    from .workbench_mail_sessions import create_session, revoke_session, validate_session

    session = create_session(store, employee_id, project_id, label, reuse_label=True)
    directory = store.directory / "mail-sessions"
    path = directory / (session["id"] + ".json")
    if session.get("reused"):
        # An existing named connection may be shown again without issuing a
        # second key. Never return a path until its private manifest still
        # matches this active session. A session created by another host has
        # no UI export manifest and must use a different label or be revoked.
        try:
            if path.is_symlink() or not path.is_file() or not private_access(path, 0o600):
                raise ValueError("missing or non-private manifest")
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                # Reject malformed persisted data through the session error path.
                raise ValueError("invalid manifest shape")  # noqa: TRY004
            if any(
                payload.get(key) != value
                for key, value in (
                    ("home", str(store.root)),
                    ("session_id", session["id"]),
                    ("employee_id", employee_id),
                    ("project_id", project_id),
                )
            ):
                raise ValueError("manifest identity mismatch")
            with store._transaction(readonly=True) as db:
                if validate_session(store, db, payload.get("token"))["id"] != session["id"]:
                    raise ValueError("manifest token mismatch")
        except (OSError, ValueError, KeyError, TypeError, WorkbenchError) as exc:
            raise WorkbenchError(
                "SESSION_MANIFEST_UNAVAILABLE",
                "这个会话名称已有有效钥匙，但原接入文件不可用。请撤销该钥匙，或为另一会话换个名称。",
            ) from exc
        return {**session, "config": _configuration(path)}
    try:
        directory.mkdir(exist_ok=True, mode=0o700)
        private_mode(directory, 0o700)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            private_mode(path, 0o600)
            json.dump(
                {
                    "home": str(store.root),
                    "session_id": session["id"],
                    "employee_id": employee_id,
                    "project_id": project_id,
                    "token": session["token"],
                },
                file,
            )
    except BaseException:
        revoke_session(store, session["id"])
        path.unlink(missing_ok=True)
        raise
    return {
        **{k: v for k, v in session.items() if k != "token"},
        "config": _configuration(path),
        "reused": False,
    }
