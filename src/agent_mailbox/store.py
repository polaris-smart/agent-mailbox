"""agent-mailbox storage: thread-safe JSON file store.

Zero external dependencies. One mail root directory::

    ~/.agent-mail/
      registry.json          agent_id -> {owner, description, created_at, pubkey?}
      inbox/<agent>/<msg_id>.json
      archive/<agent>/<msg_id>.json

Concurrency safety: every mutation takes an exclusive ``fcntl.flock`` on the
mail root lock file, so multiple MCP server processes (stdio per host app)
can share one mail root safely on macOS/Linux.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MSG_STATUSES = ("pending", "acked", "done")
RESERVED_IDS = {"boss"}


class MailboxError(ValueError):
    """Raised on invalid agent ids, unknown mailboxes, or corrupt state."""


def _now_iso() -> str:
    time.sleep(0.0005)  # keep ids sortable at ms granularity
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def _msg_id() -> str:
    return time.strftime("%Y%m%d%H%M%S", time.gmtime()) + "-" + hashlib.sha1(
        f"{time.time_ns()}".encode()
    ).hexdigest()[:8]


class MailStore:
    """The global mail root. Safe to share across processes."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self.root = Path(root or os.environ.get("AGENT_MAIL_HOME", "~/.agent-mail")).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "inbox").mkdir(exist_ok=True)
        (self.root / "archive").mkdir(exist_ok=True)
        self._registry_path = self.root / "registry.json"
        self._lock_path = self.root / ".lock"

    # ------------------------------------------------------------------ lock

    class _Lock:
        def __init__(self, path: Path) -> None:
            self._fh = open(path, "a+")  # noqa: SIM115 — lock must outlive the with-block
            fcntl.flock(self._fh, fcntl.LOCK_EX)

        def __enter__(self) -> MailStore._Lock:
            return self

        def __exit__(self, *exc: object) -> None:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()

    def _locked(self) -> MailStore._Lock:
        return MailStore._Lock(self._lock_path)

    # -------------------------------------------------------------- registry

    def _read_registry(self) -> dict[str, Any]:
        if not self._registry_path.exists():
            return {"agents": {}}
        try:
            return json.loads(self._registry_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise MailboxError(f"corrupt registry.json: {e}") from e

    def _write_registry(self, reg: dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.root, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(reg, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self._registry_path)

    # ---------------------------------------------------------------- inbox

    def _inbox_dir(self, agent_id: str) -> Path:
        self._validate_id(agent_id)
        d = self.root / "inbox" / agent_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _validate_id(agent_id: str) -> None:
        if not AGENT_ID_RE.match(agent_id or ""):
            raise MailboxError(
                f"invalid agent id {agent_id!r}: use [A-Za-z0-9_-], max 64 chars"
            )

    def _read_msg(self, path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise MailboxError(f"corrupt message {path.name}: {e}") from e

    # ================================================================ public

    def register(self, agent_id: str, owner: str = "", description: str = "") -> dict[str, Any]:
        """Register (or idempotently re-confirm) an agent. Returns its card."""
        self._validate_id(agent_id)
        with self._locked():
            reg = self._read_registry()
            exists = agent_id in reg["agents"]
            card = reg["agents"].get(agent_id, {
                "created_at": _now_iso(),
            })
            card.update({
                "owner": owner or card.get("owner", ""),
                "description": description or card.get("description", ""),
            })
            reg["agents"][agent_id] = card
            self._write_registry(reg)
        self._inbox_dir(agent_id)  # ensure inbox exists
        return {"agent_id": agent_id, "new": not exists, **card}

    def registry(self) -> dict[str, Any]:
        with self._locked():
            return self._read_registry()

    def send(
        self,
        from_id: str,
        to: str | list[str],
        subject: str,
        body: str,
        *,
        status: str = "pending",
        reply_to: str | None = None,
        priority: str = "normal",
    ) -> list[dict[str, Any]]:
        """Deliver a message to one agent, many agents, or ``"all"``."""
        if status not in MSG_STATUSES:
            raise MailboxError(f"status must be one of {MSG_STATUSES}")
        recipients = self._resolve_recipients(to)
        if not recipients:
            raise MailboxError("no recipients resolved")
        mid_base = _msg_id()
        out = []
        with self._locked():
            for rid in recipients:
                self._validate_id(rid)
                inbox = self._inbox_dir(rid)
                msg = {
                    "id": f"{mid_base}-{rid.lower()}",
                    "from": from_id,
                    "to": rid,
                    "subject": subject,
                    "body": body,
                    "priority": priority,
                    "status": status,
                    "reply_to": reply_to,
                    "created_at": _now_iso(),
                }
                (inbox / f"{msg['id']}.json").write_text(
                    json.dumps(msg, ensure_ascii=False, indent=1), encoding="utf-8"
                )
                out.append({"id": msg["id"], "to": rid})
        return out

    def _resolve_recipients(self, to: str | list[str]) -> list[str]:
        if to == "all":
            reg = self._read_registry()
            return sorted(reg["agents"].keys())
        if isinstance(to, str):
            to = [to]
        seen: list[str] = []
        for t in to:
            self._validate_id(t)
            if t not in seen:
                seen.append(t)
        return seen

    def check(self, agent_id: str, *, mark: bool = True) -> list[dict[str, Any]]:
        """Fetch pending messages; by default they become ``acked``."""
        inbox = self._inbox_dir(agent_id)
        msgs = []
        with self._locked():
            for p in sorted(inbox.glob("*.json")):
                m = self._read_msg(p)
                if m.get("status") == "pending":
                    msgs.append(m)
                    if mark:
                        m["status"] = "acked"
                        m["acked_at"] = _now_iso()
                        p.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
        msgs.sort(key=lambda m: ({"high": 0, "normal": 1, "low": 2}.get(m.get("priority", "normal"), 1), m["id"]))
        return msgs

    def list_messages(self, agent_id: str, status: str | None = None) -> list[dict[str, Any]]:
        inbox = self._inbox_dir(agent_id)
        out = []
        with self._locked():
            for p in sorted(inbox.glob("*.json")):
                m = self._read_msg(p)
                if status is None or m.get("status") == status:
                    out.append(m)
        return out

    def set_status(self, agent_id: str, msg_id: str, status: str) -> dict[str, Any]:
        if status not in MSG_STATUSES:
            raise MailboxError(f"status must be one of {MSG_STATUSES}")
        self._validate_id(agent_id)
        if "/" in msg_id or ".." in msg_id or not msg_id.endswith(".json") is False:
            pass  # msg_id is a bare id; validate below
        path = self._inbox_dir(agent_id) / f"{Path(msg_id).name}.json"
        if not path.exists():
            raise MailboxError(f"message {msg_id!r} not found in {agent_id}'s inbox")
        with self._locked():
            m = self._read_msg(path)
            m["status"] = status
            if status == "done":
                m["done_at"] = _now_iso()
            path.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
        return m

    def archive_done(self, agent_id: str) -> int:
        """Move all ``done`` messages to archive. Returns count."""
        inbox = self._inbox_dir(agent_id)
        arch = self.root / "archive" / agent_id
        arch.mkdir(parents=True, exist_ok=True)
        n = 0
        with self._locked():
            for p in sorted(inbox.glob("*.json")):
                m = self._read_msg(p)
                if m.get("status") == "done":
                    os.replace(p, arch / p.name)
                    n += 1
        return n
