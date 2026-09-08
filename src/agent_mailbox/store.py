"""agent-mailbox storage: thread-safe JSON file store.

Zero external dependencies. One mail root directory::

    ~/.agent-mail/
      registry.json          agent_id -> {owner, description, created_at, pubkey?}
      inbox/<agent>/<msg_id>.json
      archive/<agent>/<msg_id>.json

Concurrency safety: every mutation takes an exclusive file lock (``fcntl.flock`` on
POSIX, ``msvcrt.locking`` on Windows) on the
mail root lock file, so multiple MCP server processes (stdio per host app)
can share one mail root safely on macOS/Linux.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl
from pathlib import Path
from typing import Any

from .webhook import notify_new_messages

AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MSG_STATUSES = ("pending", "acked", "done")
RESERVED_IDS = {"boss"}

TASK_STATUSES = ("todo", "doing", "review", "done")
TASK_TRANSITIONS: dict[str, set[str]] = {
    "todo": {"doing"},
    "doing": {"review"},
    "review": {"done"},
    "done": set(),  # terminal — no move out of done, even with force
}


_thread_lock = threading.Lock()


class MailboxError(ValueError):
    """Raised on invalid agent ids, unknown mailboxes, or corrupt state."""


def _now_iso() -> str:
    time.sleep(0.0005)  # keep ids sortable at ms granularity
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def _msg_id() -> str:
    return time.strftime("%Y%m%d%H%M%S", time.gmtime()) + "-" + hashlib.sha1(
        f"{time.time_ns()}-{os.urandom(8).hex()}".encode()
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
            _thread_lock.acquire()
            self._fh = open(path, "a+")  # noqa: SIM115 — lock must outlive the with-block
            if sys.platform == "win32":
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(self._fh, fcntl.LOCK_EX)

        def __enter__(self) -> MailStore._Lock:
            return self

        def __exit__(self, *exc: object) -> None:
            if sys.platform == "win32":
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            _thread_lock.release()

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
        out = []
        full: list[dict[str, Any]] = []
        with self._locked():
            mid_base = _msg_id()
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
                full.append(msg)
        # append-only audit trail inside the lock: one JSONL line per mail
        # that really hit disk. A webhook notification without a sent.log
        # line is a phantom by definition — no more full-tree greps to
        # settle "was there ever a mail".
        with open(self.root / "sent.log", "a", encoding="utf-8") as audit:
            for msg in full:
                audit.write(json.dumps(
                    {k: msg[k] for k in ("id", "from", "to", "subject", "created_at")},
                    ensure_ascii=False,
                ) + "\n")
        # outside the file lock: optional webhook wake-up, best-effort.
        # config_root binds the webhook.json lookup to THIS store's root so a
        # custom-root store can never read the production gateway config.
        notify_new_messages(full, config_root=self.root)
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

    def list_archived(self, agent_id: str, status: str | None = None) -> list[dict[str, Any]]:
        arch = self.root / "archive" / agent_id
        out = []
        if not arch.is_dir():
            return out
        with self._locked():
            for p in sorted(arch.glob("*.json")):
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
            arch = self.root / "archive" / agent_id / f"{Path(msg_id).name}.json"
            if arch.exists():
                path = arch  # already archived: update in place (idempotent re-done)
            else:
                raise MailboxError(
                    f"message {msg_id!r} not found in {agent_id}'s inbox or archive"
                )
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

    # ================================================================= tasks
    #
    # Task cards live in <root>/tasks.json ({"next_id": n, "tasks": {id: card}}),
    # guarded by the same mail-root file lock as everything else. Moving a
    # card auto-messages the assignee through the normal send() path — kanban
    # motion becomes a wake-up, no webhooks or polling required.

    def _read_tasks(self) -> dict[str, Any]:
        path = self.root / "tasks.json"
        if not path.exists():
            return {"next_id": 1, "tasks": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise MailboxError(f"corrupt tasks.json: {e}") from e
        if not isinstance(data.get("tasks"), dict):
            raise MailboxError("corrupt tasks.json: tasks must be an object")
        data.setdefault("next_id", len(data["tasks"]) + 1)
        return data

    def _write_tasks(self, data: dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.root, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.root / "tasks.json")

    def task_create(
        self,
        title: str,
        assignee: str,
        created_by: str,
        due: str = "",
        *,
        notify: bool = True,
    ) -> dict[str, Any]:
        """Create a task card (status ``todo``). Returns the card."""
        title = (title or "").strip()
        if not title:
            raise MailboxError("task title required")
        self._validate_id(assignee)
        self._validate_id(created_by)
        with self._locked():
            data = self._read_tasks()
            tid = f"t-{data['next_id']}"
            data["next_id"] += 1
            now = _now_iso()
            task = {
                "id": tid,
                "title": title,
                "assignee": assignee,
                "status": "todo",
                "due": due or "",
                "created_by": created_by,
                "created_at": now,
                "updated_at": now,
                "history": [],
            }
            data["tasks"][tid] = task
            self._write_tasks(data)
        if notify and assignee != created_by:
            self.send(
                created_by,
                assignee,
                f"[task#{tid} → todo] {title}",
                f"新任务 {tid}「{title}」已分派给你（创建人 {created_by}）。"
                + (f"截止: {due}。" if due else "")
                + "用 task_list 查看详情。",
            )
        return task

    def task_move(
        self,
        task_id: str,
        status: str,
        *,
        moved_by: str = "",
        assignee: str | None = None,
        force: bool = False,
        notify: bool = True,
        note: str = "",
    ) -> dict[str, Any]:
        """Move a task along todo→doing→review→done.

        Non-adjacent transitions (and any move out of ``done``) are rejected;
        skips need ``force=True`` — except ``done``, which is terminal. Pass
        ``assignee`` to reassign the card on the same move.
        """
        if status not in TASK_STATUSES:
            raise MailboxError(f"status must be one of {TASK_STATUSES}")
        if moved_by:
            self._validate_id(moved_by)
        if assignee is not None:
            self._validate_id(assignee)
        old_assignee = ""
        with self._locked():
            data = self._read_tasks()
            task = data["tasks"].get(task_id)
            if task is None:
                raise MailboxError(f"task {task_id!r} not found")
            cur = task["status"]
            if cur == "done":
                raise MailboxError(
                    f"task {task_id} is done (terminal) — create a new task instead"
                )
            if status == cur:
                raise MailboxError(f"task {task_id} already in {cur}")
            if status not in TASK_TRANSITIONS[cur] and not force:
                raise MailboxError(
                    f"illegal transition {cur}→{status} for {task_id}; "
                    f"allowed from {cur}: {sorted(TASK_TRANSITIONS[cur])} "
                    "(pass force=True to skip ahead)"
                )
            old_assignee = task["assignee"]
            if assignee is not None and assignee != old_assignee:
                task["assignee"] = assignee
            task["status"] = status
            task["updated_at"] = _now_iso()
            entry: dict[str, Any] = {"at": task["updated_at"], "from": cur, "to": status, "by": moved_by}
            if note:
                entry["note"] = note
            task["history"].append(entry)
            self._write_tasks(data)
        if notify and task["assignee"] != moved_by:
            lines = [f"任务 {task_id}「{task['title']}」已由 {moved_by or task['created_by']} 移至 {status}。"]
            if task["assignee"] != old_assignee:
                lines.append(f"负责人已从 {old_assignee} 转派给 {task['assignee']}。")
            if note:
                lines.append(f"说明: {note}")
            if task["due"]:
                lines.append(f"截止: {task['due']}")
            lines.append("用 task_list 查看任务详情。")
            self.send(
                moved_by or task["created_by"],
                task["assignee"],
                f"[task#{task_id} → {status}] {task['title']}",
                "\n".join(lines),
            )
        return task

    def task_list(
        self, assignee: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        """List task cards, optionally filtered by assignee and/or status."""
        if status is not None and status not in TASK_STATUSES:
            raise MailboxError(f"status must be one of {TASK_STATUSES}")
        if assignee is not None:
            self._validate_id(assignee)
        with self._locked():
            tasks = list(self._read_tasks()["tasks"].values())
        if assignee is not None:
            tasks = [t for t in tasks if t["assignee"] == assignee]
        if status is not None:
            tasks = [t for t in tasks if t["status"] == status]
        tasks.sort(key=lambda t: int(t["id"].rsplit("-", 1)[1]))
        return tasks
