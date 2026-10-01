"""Export private, revocable session credentials without putting tokens in UI config."""

from __future__ import annotations

import json
import os
import sys

from .workbench_private import private_mode


def export_session(store, employee_id, project_id, label):
    from .workbench_mail_sessions import create_session, revoke_session

    session = create_session(store, employee_id, project_id, label)
    directory = store.directory / "mail-sessions"
    path = directory / (session["id"] + ".json")
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
    args = [] if getattr(sys, "frozen", False) else ["-m", "agent_mailbox"]
    config = {
        "mcpServers": {
            "agent-mailbox-project": {
                "command": sys.executable,
                "args": [*args, "mailbox-mcp", "--session-file", str(path)],
            }
        }
    }
    return {**{k: v for k, v in session.items() if k != "token"}, "config": config}
