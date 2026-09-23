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
import unicodedata

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl
from datetime import datetime
from pathlib import Path
from typing import Any

from .webhook import notify_new_messages

AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MSG_STATUSES = ("pending", "acked", "done")
SENT_LOG_MAX_BYTES = 10 * 1024 * 1024  # rotate sent.log one generation past this

# v0.5.0 delivery-side duplicate suppression (design A) + lifecycle windows.
# 铁1: reap_ttl must stay strictly below dedup_ttl — a reclaim window at or
# over the dedup window could resurrect a stale acked original right when its
# duplicate is finally allowed through. Defaults (24h / 1h) satisfy the rule;
# config.json overrides are validated loudly in load_window_config().
DEDUP_TTL_DEFAULT = 24 * 3600.0  # semantic-hash dedup window: 24h
REAP_TTL_DEFAULT = 3600.0        # stale-acked reclaim: 1h (缺口3: keep 1–2h, < dedup_ttl)
INTENT_TTL_DEFAULT = 1800.0      # 缺口4: a fresh (<30m) handling intent defers reclaim
DEDUP_BLOCKING = ("pending", "acked")  # non-terminal states that block a re-send
HANDLED_INTENT = "intent"        # two-phase handled_log: intent first …
HANDLED_OUTCOME = "outcome"      # … outcome last; set_status(done) only after both
HASH_LOG_CHARS = 16              # store full 64-hex; logs/display truncate to 16

# Self-echo (from == notification target): suppressed by default (R1 of the
# 2026-09-09 requirement); opt-in delivery marks the *notification* subject so
# receivers can tell echo from real mail — the stored letter keeps its subject.
ECHO_SUBJECT_PREFIX = "[echo] "
_TRUTHY = {"1", "true", "yes", "on"}


def notify_self_echo_enabled(root: Path) -> bool:
    """Whether self-addressed notifications (from == to) are delivered.

    Default ``false``: the sender is never woken by its own send echo — the
    letter itself still lands and ``mailbox_list``/``check`` are unaffected.
    Resolves env ``AGENT_MAIL_NOTIFY_SELF_ECHO`` first, then the
    ``notify_self_echo`` key of ``config.json`` in the mail root, then the
    default. Per-root binding, like webhook.json.
    """
    env = os.environ.get("AGENT_MAIL_NOTIFY_SELF_ECHO")
    if env is not None:
        return env.strip().lower() in _TRUTHY
    try:
        cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(cfg.get("notify_self_echo", False))


# ------------------------------------------------------------ semantic hash
# v0.5.0 design A: delivery-side duplicate suppression. The hash pins the
# exact 口径 agreed in the 2026-09-13 review (缺口1): fenced ``` regions are
# lifted out of the body first and hashed raw (original order, zero folding —
# differently ordered code must not collapse to one hash), everything else is
# NFKC+NFC normalized with whitespace folded.

def _split_code_regions(text: str) -> tuple[str, list[str]]:
    """Split fenced ``` regions out of ``text``.

    Returns ``(prose, regions)``. Regions keep their raw lines in original
    order (zero folding); the fence marker lines themselves are dropped. An
    unterminated fence runs to the end of the text.
    """
    prose: list[str] = []
    regions: list[str] = []
    buf: list[str] | None = None
    for line in text.splitlines():
        if buf is None:
            if line.lstrip().startswith("```"):
                buf = []
            else:
                prose.append(line)
        elif line.lstrip().startswith("```"):
            regions.append("\n".join(buf))
            buf = None
        else:
            buf.append(line)
    if buf is not None:
        regions.append("\n".join(buf))
    return "\n".join(prose), regions


def _norm_text(text: str) -> str:
    """NFKC then NFC, fold every whitespace run to a single space."""
    s = unicodedata.normalize("NFKC", text or "")
    s = unicodedata.normalize("NFC", s)
    return re.sub(r"\s+", " ", s).strip()


def semantic_hash(subject: str, body: str) -> str:
    """Content hash for delivery-side dedup (v0.5 A, 缺口1 口径).

    ``sha256(norm(subject) + "\\0" + norm(body-without-fences) + "\\0"
    + regions.join("\\0"))`` — fenced ``` regions hashed raw in original
    order (zero folding), prose NFKC/NFC normalized with whitespace folded.
    Returns the full 64-hex digest; truncate to ``HASH_LOG_CHARS`` for logs.
    """
    prose, regions = _split_code_regions(body or "")
    payload = "\x00".join([_norm_text(subject or ""), _norm_text(prose), *regions])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_window_config(root: Path) -> dict[str, float]:
    """Read and validate the lifecycle windows from ``<root>/config.json``.

    Returns ``{"dedup_ttl", "reap_ttl"}`` in seconds. Defaults: dedup 24h,
    reap 1h. 铁1: ``reap_ttl`` must be strictly below ``dedup_ttl`` —
    violations (and a corrupt config file) raise ``MailboxError`` loudly so
    a bad operator edit can never silently distort the windows.
    """
    try:
        cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        cfg = {}
    except (OSError, json.JSONDecodeError) as e:
        raise MailboxError(f"corrupt config.json: {e}") from e
    try:
        dedup_ttl = float(cfg.get("dedup_ttl", DEDUP_TTL_DEFAULT))
        reap_ttl = float(cfg.get("reap_ttl", REAP_TTL_DEFAULT))
    except (TypeError, ValueError) as e:
        raise MailboxError(f"config.json: dedup_ttl/reap_ttl must be numbers: {e}") from e
    if dedup_ttl <= 0 or reap_ttl <= 0:
        raise MailboxError("config.json: dedup_ttl and reap_ttl must be positive seconds")
    if reap_ttl >= dedup_ttl:
        raise MailboxError(
            f"config.json: reap_ttl ({reap_ttl:g}s) must be < dedup_ttl ({dedup_ttl:g}s) "
            "(铁1: a reclaim window at or over the dedup window breaks duplicate suppression)"
        )
    return {"dedup_ttl": dedup_ttl, "reap_ttl": reap_ttl}


def load_identity_binding(root: Path) -> dict[str, Any]:
    """Read the optional ``identity_binding`` block from ``<root>/config.json``.

    Schema::

        {"identity_binding": {"enabled": true, "<agent_id>": "<sha256(token) hex>"}}

    Returns ``{"enabled": bool, "<agent_id>": "<64-hex>"}``; ``{"enabled": False}``
    when the block is absent — binding off, local-trust behavior unchanged.
    A malformed block raises ``MailboxError`` loudly (same doctrine as
    ``load_window_config``): a half-written security boundary must never
    silently degrade to "disabled".
    """
    try:
        cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"enabled": False}
    except (OSError, json.JSONDecodeError) as e:
        raise MailboxError(f"corrupt config.json: {e}") from e
    binding = cfg.get("identity_binding")
    if binding is None:
        return {"enabled": False}
    if not isinstance(binding, dict):
        raise MailboxError("config.json: identity_binding must be an object")
    enabled = binding.get("enabled", False)
    if not isinstance(enabled, bool):
        raise MailboxError("config.json: identity_binding.enabled must be a boolean")
    table: dict[str, Any] = {"enabled": enabled}
    for agent_id, token_hash in binding.items():
        if agent_id == "enabled":
            continue
        if not AGENT_ID_RE.match(agent_id):
            raise MailboxError(
                f"config.json: identity_binding key {agent_id!r} is not a valid agent id"
            )
        if not isinstance(token_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", token_hash):
            raise MailboxError(
                f"config.json: identity_binding[{agent_id}] must be a sha256 hex digest"
            )
        table[agent_id] = token_hash
    return table


# replies collapse stacked "Re:" prefixes to one, like a mail client does:
# "Re: Re: X" and "rE: x" both become "Re: X"; a bare subject gains one.
_STACKED_RE_PREFIX = re.compile(r"^(?:\s*re\s*:\s*)+", re.IGNORECASE)


def _reply_subject(subject: str) -> str:
    return "Re: " + _STACKED_RE_PREFIX.sub("", subject or "").strip()


# v0.6.0 threads: a first-class thread_id on every letter. New sends mint one;
# replies inherit the original's. Legacy letters carry no thread_id (None) —
# they are back-filled heuristically by subject key (剥 Re:/Fwd: 前缀), and a
# letter that cannot be grouped stays null so the gap stays visible.
_THREAD_PREFIXES = re.compile(r"^(?:\s*(?:re|fwd?|fw)\s*:\s*)+", re.IGNORECASE)


def thread_key(subject: str) -> str:
    """Normalize a subject for thread grouping: strip stacked Re:/Fwd:/Fw:
    prefixes (any case), NFKC-fold, lowercase, collapse whitespace.

    ``"Re: Re: 发票确认"`` and ``"发票确认"`` collapse to the same key; an
    empty key (``""``) never groups with anything — callers must treat it as
    unmatchable rather than a shared bucket.
    """
    stripped = _THREAD_PREFIXES.sub("", subject or "").strip()
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", stripped)).lower()


def _thread_id() -> str:
    return "th-" + hashlib.sha1(
        f"{time.time_ns()}-{os.urandom(8).hex()}".encode()
    ).hexdigest()[:12]


def ghost_open_limit() -> int:
    """Open (non-done) letters per thread before mailbox_check warns. Reads
    env ``AGENT_MAIL_GHOST_LIMIT`` then defaults to 5 (v0.6 幽灵信防护)."""
    raw = os.environ.get("AGENT_MAIL_GHOST_LIMIT", "")
    try:
        return max(1, int(raw))
    except ValueError:
        return 5


def _handled_session(agent_id: str) -> str:
    """Return the session identifier for handled-log entries.

    ``AGENT_MAIL_SESSION`` is set per-process by the MCP host when multiple
    sessions share one agent id; when absent we fall back to the agent id so
    single-session setups keep working without extra configuration.
    """
    return os.environ.get("AGENT_MAIL_SESSION", agent_id)


def _append_handled(
    msg: dict[str, Any], agent_id: str, action: str, **fields: Any
) -> None:
    """Append an entry to the message's ``handled_log`` (in-place, append-only)."""
    log = msg.setdefault("handled_log", [])
    entry: dict[str, Any] = {"by": _handled_session(agent_id), "at": _now_iso(), "action": action}
    entry.update(fields)
    log.append(entry)
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
        # semantic-hash -> letter paths, per inbox, keyed by directory mtime
        # (缺口2: send-side dedup must not O(N)-scan the box on every send).
        self._dedup_index: dict[str, tuple[int, dict[str, list[Path]]]] = {}

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
        dedupe: bool = True,
        thread_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Deliver a message to one agent, many agents, or ``"all"``.

        With ``dedupe=True`` (default) a recipient whose inbox already holds
        a letter with the same ``semantic_hash`` in a non-terminal state
        (``pending``/``acked``) inside the dedup window gets **no** new mail:
        the per-recipient result is ``{"to": rid, "deduped": true,
        "existing_id": <id>}`` and the call has zero side effects for that
        recipient — nothing lands on disk, nothing is logged to ``sent.log``,
        no webhook fires (铁2 path ①). ``dedupe=False`` exempts the send
        (铁2 path ②). Same hash but ``done``/archived is delivered normally
        (铁2 path ③). Only the target inboxes are consulted, never the
        archive (缺口2). After the dedup window (default 24h) a same-hash
        re-send is allowed through, so the queue may then legitimately hold
        two same-hash non-terminal letters (微点2: expected, not a bug).

        Threads (v0.6): every letter carries a ``thread_id``. ``thread_id=``
        pins one explicitly; otherwise a reply (``reply_to``) inherits the
        original's thread (a legacy original without one is back-filled with
        the freshly minted id so the pair stays linked), and a fresh send
        mints a new id shared by every recipient of this call.
        """
        if status not in MSG_STATUSES:
            raise MailboxError(f"status must be one of {MSG_STATUSES}")
        recipients = self._resolve_recipients(to)
        if not recipients:
            raise MailboxError("no recipients resolved")
        if reply_to:
            subject = _reply_subject(subject)
        original_path: Path | None = None
        original_tid: str | None = None
        if reply_to:
            original_path, original_tid = self._original_thread(from_id, reply_to)
        tid = thread_id or original_tid or _thread_id()
        out = []
        full: list[dict[str, Any]] = []
        windows = load_window_config(self.root)  # 铁1 gate: loud failure on bad config
        msg_hash = semantic_hash(subject, body)
        with self._locked():
            if original_path is not None and original_tid is None:
                # legacy original without a thread: back-fill it under the
                # lock with the id the reply is carrying (re-read first —
                # another process may have back-filled it meanwhile).
                m = self._read_msg(original_path)
                if not m.get("thread_id"):
                    m["thread_id"] = tid
                    original_path.write_text(
                        json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8"
                    )
            now = time.time()
            mid_base = _msg_id()
            for rid in recipients:
                self._validate_id(rid)
                if dedupe:
                    existing = self._find_dupe(rid, msg_hash, windows["dedup_ttl"], now)
                    if existing is not None:
                        out.append({"to": rid, "deduped": True, "existing_id": existing})
                        continue
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
                    "thread_id": tid,
                    "created_at": _now_iso(),
                    "semantic_hash": msg_hash,
                }
                (inbox / f"{msg['id']}.json").write_text(
                    json.dumps(msg, ensure_ascii=False, indent=1), encoding="utf-8"
                )
                out.append({"id": msg["id"], "to": rid, "thread_id": tid})
                full.append(msg)
        if not full:
            # every recipient deduped: zero side effects — no audit line, no
            # webhook, not even sent.log rotation bookkeeping (铁2 path ①).
            return out
        # append-only audit trail inside the lock: one JSONL line per mail
        # that really hit disk. A webhook notification without a sent.log
        # line is a phantom by definition — no more full-tree greps to
        # settle "was there ever a mail".
        sent_log = self.root / "sent.log"
        echo_on = notify_self_echo_enabled(self.root)
        notify_msgs: list[dict[str, Any]] = []
        try:
            if sent_log.stat().st_size > SENT_LOG_MAX_BYTES:
                # rotate one generation: sent.log becomes sent.log.1 (any old
                # .1 is overwritten) and the fresh log starts empty.
                os.replace(sent_log, self.root / "sent.log.1")
        except FileNotFoundError:
            pass
        with open(sent_log, "a", encoding="utf-8") as audit:
            for msg in full:
                line = {k: msg[k] for k in ("id", "from", "to", "subject", "created_at")}
                if msg["from"] == msg["to"]:
                    if echo_on:
                        # opt-in: deliver the self-echo, but prefix the
                        # *notification* subject so receivers can strip it
                        # back; the stored letter keeps the original subject.
                        echo = dict(msg)
                        echo["subject"] = ECHO_SUBJECT_PREFIX + msg["subject"]
                        notify_msgs.append(echo)
                    else:
                        # default: drop the self-notification. The letter is
                        # already on disk; the audit line below carries
                        # echo_suppressed so "notification dropped" stays
                        # greppable when someone asks why no wake-up came.
                        line["echo_suppressed"] = True
                else:
                    notify_msgs.append(msg)
                audit.write(json.dumps(line, ensure_ascii=False) + "\n")
        # outside the file lock: optional webhook wake-up, best-effort.
        # config_root binds the webhook.json lookup to THIS store's root so a
        # custom-root store can never read the production gateway config.
        # unread_count (v0.6.2): per-recipient pending tally at notification
        # time, freshly landed letters included; best-effort like the POST.
        unread = {
            rid: len(self.list_messages(rid, status="pending"))
            for rid in {str(m["to"]) for m in notify_msgs}
        }
        notify_new_messages(notify_msgs, config_root=self.root, unread_counts=unread)
        return out

    def _dedup_candidates(self, agent_id: str, msg_hash: str) -> list[Path]:
        """Same-hash letter paths in the target inbox (缺口2, indexed).

        The per-inbox index is a snapshot keyed by the inbox directory's
        mtime, so letters added or removed by any process refresh it on the
        next send; in-place status flips are re-verified from the file on
        every hit. Only the inbox is ever scanned — the archive never
        participates in dedup (缺口2). Callers must re-read candidates: a
        snapshot can go stale across processes.
        """
        inbox = self._inbox_dir(agent_id)
        try:
            mtime = inbox.stat().st_mtime_ns
        except OSError:
            mtime = -1
        cached = self._dedup_index.get(agent_id)
        if cached is None or cached[0] != mtime:
            by_hash: dict[str, list[Path]] = {}
            for p in inbox.glob("*.json"):
                try:
                    m = self._read_msg(p)
                except MailboxError:
                    continue  # corrupt letters never participate in dedup
                h = m.get("semantic_hash")
                if h:
                    by_hash.setdefault(h, []).append(p)
            cached = (mtime, by_hash)
            self._dedup_index[agent_id] = cached
        return list(cached[1].get(msg_hash, ()))

    def _find_dupe(
        self, agent_id: str, msg_hash: str, dedup_ttl: float, now: float
    ) -> str | None:
        """Id of a same-hash non-terminal letter inside the window, else None."""
        for p in self._dedup_candidates(agent_id, msg_hash):
            try:
                m = self._read_msg(p)
            except MailboxError:
                continue
            if m.get("semantic_hash") != msg_hash:
                continue  # stale snapshot or foreign file
            if m.get("status") not in DEDUP_BLOCKING:
                continue  # done (or already archived away) never blocks (铁2 path ③)
            # legacy letters without a hash can't match (null hash is never
            # back-filled); an unparsable created_at counts as fresh-block.
            try:
                created = datetime.fromisoformat(
                    str(m.get("created_at", "")).replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                created = now
            if now - created >= dedup_ttl:
                continue  # TTL 放行 (微点2: two same-hash letters may then coexist)
            return str(m["id"])
        return None

    # --------------------------------------------------------------- threads
    #
    # v0.6: thread_id is a first-class field. The lookup side accepts either
    # a thread_id or any message id on the thread (resolving through the
    # letter itself), spans every agent's inbox AND archive (cross-agent
    # view), and falls back to the subject-key heuristic for legacy letters
    # that never carried a thread_id.

    def _original_thread(self, from_id: str, reply_to: str) -> tuple[Path | None, str | None]:
        """Locate the reply target and return ``(path, thread_id)``.

        ``path`` is None when the original cannot be found (the reply still
        goes out, minting its own thread); ``thread_id`` is None when the
        original is a legacy letter without one — send() back-fills it.
        """
        try:
            path, m = self._locate_msg(from_id, reply_to)
        except MailboxError:
            return None, None
        return path, m.get("thread_id")

    def _iter_all_letters(self) -> Any:
        """Yield ``(path, msg)`` for every letter under the root (inboxes and
        archives of all agents). Tolerates corrupt files (skips them)."""
        for sub in ("inbox", "archive"):
            base = self.root / sub
            if not base.is_dir():
                continue
            for box in sorted(base.iterdir()):
                if not box.is_dir():
                    continue
                for p in sorted(box.glob("*.json")):
                    try:
                        yield p, self._read_msg(p)
                    except MailboxError:
                        continue

    def thread_messages(self, thread_ref: str) -> dict[str, Any]:
        """All letters on a thread, oldest first, across every agent.

        ``thread_ref`` may be a ``thread_id`` or any message id on the thread
        (resolves through the letter). Legacy letters without a thread_id are
        matched by ``thread_key`` of the resolved thread's subjects — the
        heuristic is allowed to be imperfect; a letter that matches nothing
        simply stays out. Returns ``{"thread_id", "matched_by", "count",
        "messages"}`` where ``matched_by`` is ``thread_id`` or ``subject_key``.
        """
        resolved: str | None = None
        skey: str | None = None
        for _, m in self._iter_all_letters():
            if m.get("id") == thread_ref:
                resolved = m.get("thread_id")
                skey = thread_key(m.get("subject", ""))
                break
            if m.get("thread_id") == thread_ref:
                resolved = thread_ref
        if resolved is None:
            raise MailboxError(f"unknown thread or message id {thread_ref!r}")
        matched: list[dict[str, Any]] = []
        for p, m in self._iter_all_letters():
            if (resolved and m.get("thread_id") == resolved) or (
                skey and thread_key(m.get("subject", "")) == skey
            ):
                matched.append((p, m))
        # created_at is second-granular and several letters can share one
        # second, so file mtime (write order, kept across os.replace into the
        # archive) breaks ties before the id does.
        def _order(item: tuple[Path, dict[str, Any]]) -> tuple:
            p, m = item
            try:
                mtime = p.stat().st_mtime_ns
            except OSError:
                mtime = 0
            return (m.get("created_at", ""), mtime, m.get("id", ""))

        matched.sort(key=_order)
        messages = [m for _, m in matched]
        return {
            "thread_id": resolved,
            "matched_by": "thread_id" if not skey else "thread_id+subject_key",
            "count": len(messages),
            "messages": messages,
        }

    def backfill_threads(self) -> dict[str, Any]:
        """Heuristic back-fill for legacy letters missing ``thread_id``.

        Groups thread-less letters (inbox + archive, every agent) by their
        ``thread_key``; any group with two or more letters gets one shared
        freshly-minted thread_id written back. Singleton groups stay null so
        the gap remains visible. Returns ``{"groups": <grouped count>,
        "backfilled": <letters written>}``.
        """
        by_key: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
        for p, m in self._iter_all_letters():
            if m.get("thread_id"):
                continue
            k = thread_key(m.get("subject", ""))
            if k:
                by_key.setdefault(k, []).append((p, m))
        backfilled = 0
        grouped = 0
        with self._locked():
            for members in by_key.values():
                if len(members) < 2:
                    continue
                grouped += 1
                tid = _thread_id()
                for p, m in members:
                    m2 = self._read_msg(p)  # re-read under the lock
                    if not m2.get("thread_id"):
                        m2["thread_id"] = tid
                        p.write_text(
                            json.dumps(m2, ensure_ascii=False, indent=1), encoding="utf-8"
                        )
                        backfilled += 1
        return {"groups": grouped, "backfilled": backfilled}

    def ghost_threads(self, threshold: int | None = None) -> list[dict[str, Any]]:
        """Threads holding more than ``threshold`` open (non-done) letters.

        The v0.6 幽灵信防护: a thread that keeps accreting unread mail is the
        acked-sinking failure mode resurfacing — mailbox_check surfaces this
        list so the warning reaches the agent, not just the log.
        """
        limit = ghost_open_limit() if threshold is None else max(1, threshold)
        open_by_thread: dict[str, int] = {}
        for _, m in self._iter_all_letters():
            tid = m.get("thread_id")
            if tid and m.get("status") in ("pending", "acked"):
                open_by_thread[tid] = open_by_thread.get(tid, 0) + 1
        return [
            {"thread_id": t, "open_count": n}
            for t, n in sorted(open_by_thread.items())
            if n > limit
        ]

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
        """Fetch pending messages; by default they become ``acked``.

        Every status transition appends a ``handled_log`` entry recording
        *which session* handled the message (via ``AGENT_MAIL_SESSION`` when
        set, falling back to the agent id). This makes multi-session
        parallel handling visible: session A can see that session B already
        acked/done'd a message without re-reading the raw file.
        """
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
                        _append_handled(m, agent_id, "acked")
                        p.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
        msgs.sort(key=lambda m: ({"high": 0, "normal": 1, "low": 2}.get(m.get("priority", "normal"), 1), m["id"]))
        return msgs

    def claim(self, agent_id: str) -> list[dict[str, Any]]:
        """Atomically claim all pending mail: return it flipped to ``acked``
        in one locked pass (v0.6.2 T3 — mailbox_wait claim semantics).

        ``mailbox_wait`` used to list pending mail and then consume it via
        ``check()`` in a second unlocked step, so two waiters on one mailbox
        could both observe the same pending batch and race each other for
        it. Claim is the single-step fix: the second waiter's locked pass
        never sees letters the first one already claimed, so a batch is
        consumed exactly once. Each claimed letter records ``claimed_by``
        (the claiming agent id) plus a ``claimed`` handled_log entry.
        Fail-open: a claimed-but-never-handled letter is ordinary acked
        mail — ``reap_stale_acked`` (铁1: reap_ttl < dedup_ttl, unchanged)
        returns it to ``pending`` and drops the stale ``claimed_by``, so
        nothing is lost.
        """
        inbox = self._inbox_dir(agent_id)
        msgs = []
        with self._locked():
            for p in sorted(inbox.glob("*.json")):
                m = self._read_msg(p)
                if m.get("status") != "pending":
                    continue
                m["status"] = "acked"
                m["acked_at"] = _now_iso()
                m["claimed_by"] = agent_id
                _append_handled(m, agent_id, "claimed")
                p.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
                msgs.append(m)
        msgs.sort(key=lambda m: ({"high": 0, "normal": 1, "low": 2}.get(m.get("priority", "normal"), 1), m["id"]))
        return msgs

    def reap_stale_acked(
        self,
        agent_id: str,
        ttl_seconds: float | None = None,
        *,
        now: float | None = None,
        intent_ttl: float = INTENT_TTL_DEFAULT,
    ) -> list[str]:
        """Reclaim ``acked`` mail whose handling never completed.

        ``check()`` reserves mail: pending -> acked, handed to the caller. If
        that caller dies, is cancelled, or the check ran under a foreign
        identity, the letter is orphaned — a drain that scans only ``pending``
        never sees it again (2026-09-13 acked-state incident, task t-6). Mail
        acked longer ago than ``ttl_seconds`` goes back to ``pending``, the
        stale ``acked_at`` is dropped, and a ``reclaimed`` entry lands in
        ``handled_log`` so the round trip stays auditable. A missing
        ``acked_at`` (pre-handled_log writers) falls back to the file mtime.
        ``ttl_seconds=None`` resolves the mail root's configured ``reap_ttl``
        (default 1h; 铁1 keeps it strictly below the dedup window).

        缺口4: a letter whose newest handled_log ``intent`` is fresher than
        ``intent_ttl`` (default 30m — the assumed handling timeout) is
        skipped this round: an agent is actively on it. Pure optimization —
        the letter simply becomes reclaimable once the intent goes stale.

        This is a manual maintenance operation: nothing in the library calls
        it automatically (N1); the wake script is the only wired caller.
        Returns the reclaimed message ids, sorted by scan order.
        """
        self._validate_id(agent_id)
        if ttl_seconds is None:
            ttl_seconds = load_window_config(self.root)["reap_ttl"]
        cutoff = (time.time() if now is None else now) - ttl_seconds
        inbox = self._inbox_dir(agent_id)
        reaped: list[str] = []
        with self._locked():
            for p in sorted(inbox.glob("*.json")):
                m = self._read_msg(p)
                if m.get("status") != "acked":
                    continue
                if self._intent_fresh(m, now, intent_ttl):
                    continue  # 缺口4: actively being handled — defer
                stamp = m.get("acked_at")
                if stamp:
                    try:
                        acked = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        acked = p.stat().st_mtime
                else:
                    acked = p.stat().st_mtime
                if acked > cutoff:
                    continue
                m["status"] = "pending"
                m.pop("acked_at", None)
                m.pop("claimed_by", None)  # v0.6.2: the stale claim dies with the ack
                _append_handled(m, agent_id, "reclaimed")
                p.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
                reaped.append(m["id"])
        return reaped

    def _intent_fresh(self, m: dict[str, Any], now: float | None, intent_ttl: float) -> bool:
        """True when the newest handled_log ``intent`` is younger than ``intent_ttl``."""
        stamps = [
            e.get("at")
            for e in (m.get("handled_log") or [])
            if e.get("action") == HANDLED_INTENT
        ]
        if not stamps:
            return False
        ref = time.time() if now is None else now
        for stamp in stamps:
            try:
                t = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue  # unparsable stamp can't prove freshness — keep scanning
            if ref - t < intent_ttl:
                return True
        return False

    # --------------------------------------------------- handled_log APIs (B)
    #
    # v0.5 design B: the agent handling layer records its two-phase
    # intent/outcome through the store (唯一写入方) — never by rewriting
    # letter files by hand — so every append shares the mail-root lock and
    # the compensation table can be evaluated from handled_log alone.

    def _locate_msg(self, agent_id: str, msg_id: str) -> tuple[Path, dict[str, Any]]:
        """Find a letter in the inbox, falling back to the archive."""
        self._validate_id(agent_id)
        name = f"{Path(msg_id).name}.json"
        path = self._inbox_dir(agent_id) / name
        if not path.exists():
            arch = self.root / "archive" / agent_id / name
            if arch.exists():
                path = arch
            else:
                raise MailboxError(
                    f"message {msg_id!r} not found in {agent_id}'s inbox or archive"
                )
        return path, self._read_msg(path)

    def record_handled(
        self, agent_id: str, msg_id: str, action: str, **fields: Any
    ) -> dict[str, Any]:
        """Append a ``handled_log`` entry through the store (v0.5 B).

        The handling layer calls this with ``action="intent"`` when it starts
        working a claimed letter and ``action="outcome"`` when the work is
        finished (before ``set_status(done)``). Every append reuses the
        mail-root lock; extra keyword fields (e.g. ``note=``) ride along on
        the entry. Returns the updated message.
        """
        path, m = self._locate_msg(agent_id, msg_id)
        with self._locked():
            m = self._read_msg(path)  # re-read under the lock
            _append_handled(m, agent_id, action, **fields)
            path.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
        return m

    def resume_plan(self, agent_id: str, msg_id: str) -> dict[str, Any]:
        """Classify a half-handled letter per the v0.5 §3 compensation table.

        Reads ``handled_log`` only ("看 log 到哪一段") and returns
        ``{"id", "status", "intent", "outcome", "resume"}`` where ``resume``
        is one of:

        - ``"process"``  — pending with no records: normal handling, start
          at the intent. ``acked`` with no records at all resolves here too
          (小项1: claimed then crashed before the intent — same treatment).
        - ``"replay"``   — intent recorded, outcome missing: idempotently
          redo the handling body, then record the outcome.
        - ``"finalize"`` — intent and outcome both recorded but the letter
          is not ``done`` yet: only ``set_status(done)`` remains.
        - ``"skip"``     — already terminal (done); nothing to resume.
        """
        _, m = self._locate_msg(agent_id, msg_id)
        log = m.get("handled_log") or []
        has_intent = any(e.get("action") == HANDLED_INTENT for e in log)
        has_outcome = any(e.get("action") == HANDLED_OUTCOME for e in log)
        status = m.get("status")
        if status == "done":
            resume = "skip"
        elif has_intent and has_outcome:
            resume = "finalize"
        elif has_intent:
            resume = "replay"
        else:
            resume = "process"
        return {
            "id": m["id"],
            "status": status,
            "intent": has_intent,
            "outcome": has_outcome,
            "resume": resume,
        }

    def list_messages(
        self,
        agent_id: str,
        status: str | None = None,
        thread: str | None = None,
    ) -> list[dict[str, Any]]:
        """List inbox letters, optionally filtered by status and/or thread.

        ``thread`` matches a letter's ``thread_id`` exactly, or — for legacy
        letters without one — the ``thread_key`` of the given reference is
        compared against the letter's own subject key.
        """
        inbox = self._inbox_dir(agent_id)
        out = []
        with self._locked():
            for p in sorted(inbox.glob("*.json")):
                m = self._read_msg(p)
                if status is not None and m.get("status") != status:
                    continue
                if thread is not None and not self._thread_match(m, thread):
                    continue
                out.append(m)
        return out

    @staticmethod
    def _thread_match(m: dict[str, Any], thread: str) -> bool:
        if m.get("thread_id") == thread:
            return True
        if m.get("thread_id"):
            return False  # carries a real thread_id; the exact match above failed
        # legacy letter without a thread_id: fall back to the subject key
        want = thread_key(thread)
        return bool(want) and thread_key(m.get("subject", "")) == want

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
            _append_handled(m, agent_id, status)
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
