"""Hermes Gateway adapter. Scheduling is not a receipt; observe the actual turn.

Only the standard library is required. No model calls, credentials, chat history,
or Human-channel sends belong in this plugin.
"""

# Hermes loads standalone plugin directories by manifest, not Python package name.
# ruff: noqa: N999
from __future__ import annotations

import errno
import fcntl
import hashlib
import importlib.util
import json
import logging
import os
import re
import secrets
import socket
import socketserver
import sqlite3
import stat
import tempfile
import threading
from pathlib import Path

PROTOCOL = "agent-mailbox.host-receipt.v1"
REAUTH_PROTOCOL = "agent-mailbox.host-reauthorization.v1"
FIELDS = ("id", "employee_id", "project_id", "host_kind", "host_session_id")
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,512}\Z")
MARKER = re.compile(r"\[agent-mailbox-delivery:([a-f0-9]{64})\]")


def pinned_factory(ctx, **kwargs):
    # Standalone Hermes plugin directories are not guaranteed importable packages.
    # Only the adjacent bundled component is eligible; no public unpinned fallback.
    spec = importlib.util.spec_from_file_location(
        "mailbox_hermes_pinned", Path(__file__).with_name("pinned.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PinnedGateway(ctx, **kwargs)


def private_pinned_factory(ctx, **kwargs):
    spec = importlib.util.spec_from_file_location(
        "mailbox_hermes_private_pinned", Path(__file__).with_name("pinned.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PinnedGateway(ctx, private_mode=True, **kwargs)


def require_private_host():
    spec = importlib.util.spec_from_file_location(
        "mailbox_hermes_private_host_preflight", Path(__file__).with_name("pinned.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.compatible_sdk(private_mode=True)


def live_factory(ctx):
    spec = importlib.util.spec_from_file_location(
        "mailbox_hermes_live", Path(__file__).with_name("live.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.LiveRouting(ctx)


def register_private_platform(ctx, authorize_event):
    spec = importlib.util.spec_from_file_location(
        "mailbox_hermes_private_platform", Path(__file__).with_name("private_platform.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.register_private_platform(ctx, authorize_event)


class Diagnostics:
    """First-contact mode has no journal, binding, injection or turn hook."""
    def __init__(self, ctx):
        self.closed = False
        self.routing = live_factory(ctx)

    def routing_context(self):
        if self.closed:
            raise ValueError("diagnostics_closed")
        result = self.routing.snapshot()
        if self.closed:
            raise ValueError("diagnostics_closed")
        return result

    def available(self):
        try:
            self.routing_context()
            return True
        except Exception:  # noqa: BLE001 — unsupported/unpublished Gateway stays unavailable
            return False

    def probe(self, binding=None):
        result = {"protocol": "agent-mailbox.host-health.v1", "host_kind": "hermes",
                "gateway_injection_available": False, "pinned_sessions": 0,
                "unconfirmed_deliveries": 0, "recorded_turn_receipts": 0}
        if binding is not None:
            result["route_available"] = False
        return result

    def receive(self, _envelope):
        raise ValueError("diagnostics_cannot_receive")

    def close(self):
        self.closed = True


def private_directory(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("absolute_private_directory_required")
    # Reject redirected ancestors as well as a redirected leaf.
    if path.resolve() != path.absolute():
        raise ValueError("symlink_directory_refused")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("owner_private_directory_required")
    return path


class Receiver:
    def __init__(self, ctx, home, state_dir, sessions, *, pin_factory=pinned_factory,
                 private_mode=False):
        self.ctx = ctx
        self.routing = None
        self.pin_factory = pin_factory
        self.private_mode = private_mode is True
        self.private_adapter_type = None
        self.closed = False
        self.home = Path(home)
        if not self.home.is_absolute():
            raise ValueError("absolute_mailbox_home_required")
        # Gateway key -> pinned session identity + platform, never inferred from title.
        if not isinstance(sessions, dict) or not sessions:
            raise ValueError("explicit_existing_sessions_required")
        for key, value in sessions.items():
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 512
                or any(ord(c) < 32 for c in key)
                or not isinstance(value, dict)
                or any(
                    not isinstance(value.get(k), str) or not value[k]
                    for k in ("session_id", "platform")
                )
            ):
                raise ValueError("invalid_session_pin")
        self.sessions = json.loads(json.dumps(sessions))
        self.condition = threading.Condition(threading.RLock())
        directory = private_directory(state_dir)
        db_path = directory / "deliveries.sqlite3"
        if db_path.is_symlink():
            raise ValueError("symlink_journal_refused")
        fd = os.open(db_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        os.close(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("owner_private_journal_required")
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS deliveries "
            "(id TEXT PRIMARY KEY, fingerprint TEXT, nonce TEXT UNIQUE, "
            "receipt TEXT, session_id TEXT, platform TEXT, status TEXT)"
        )
        self.db.commit()

    def available(self):
        # Public injection prefers CLI/TUI before Gateway. Fail closed rather than
        # accidentally notify a different surface. These read-only compatibility
        # guards must be revalidated when the Hermes plugin routing API changes.
        manager = getattr(self.ctx, "_manager", None)
        return (
            not self.closed
            and manager is not None
            and getattr(manager, "_cli_ref", None) is None
            and not getattr(manager, "has_tui_message_injector", True)
            and getattr(manager, "has_gateway_message_injector", False)
            and self.ctx._gateway_injection_allowed()
        )

    def current_binding(self, binding):
        data = json.loads((self.home / "wake" / "host-bindings.json").read_text())
        if data.get("version") != 1 or not isinstance(data.get("bindings"), list):
            return False
        matches = [
            b
            for b in data["bindings"]
            if isinstance(b, dict)
            and b.get("employee_id") == binding["employee_id"]
            and b.get("project_id") == binding["project_id"]
        ]
        return len(matches) == 1 and all(matches[0].get(k) == binding[k] for k in FIELDS)

    @staticmethod
    def private_origin(binding):
        return {
            "platform": "agent_mailbox_private",
            "chat_id": f"{binding['project_id']}_{binding['employee_id']}",
            "chat_type": "dm",
            "user_id": binding["employee_id"],
        }

    def private_route_current(self, binding):
        """Use only the adapter this plugin registered in its own live profile."""
        if not self.private_mode or self.private_adapter_type is None:
            return None, None
        registration = getattr(self.ctx._manager, "_gateway_message_injector", None)
        if not isinstance(registration, tuple) or len(registration) != 2:
            return None, None
        owner = registration[0]
        try:
            profile = owner._served_profile_for_home(self.ctx._manager.home_path)
            if profile is None or not owner._plugin_injection_accepting():
                return None, None
            matches = [
                adapter for adapter in owner._adapters_for_profile(profile).values()
                if type(adapter) is self.private_adapter_type
                and getattr(adapter.platform, "value", None) == "agent_mailbox_private"
                and adapter.plugin_id == self.ctx.plugin_id
            ]
            if len(matches) != 1:
                return None, None
            adapter = matches[0]
            origin = self.private_origin(binding)
            source = adapter.build_source(
                origin["chat_id"], chat_type="dm", user_id=origin["user_id"],
            )
            if owner._private_mailbox_tools_ready(source) is not True:
                return None, None
            return owner, adapter
        except Exception:  # noqa: BLE001 — any host route drift fails closed
            return None, None

    def private_event_authorized(self, event):
        """Recheck original binding and durable nonce at the final adapter intake."""
        with self.condition:
            if self.closed or not self.private_mode:
                return False
            nonces = MARKER.findall(event.text or "")
            if len(nonces) != 1:
                return False
            row = self.db.execute(
                "SELECT receipt,session_id,status FROM deliveries "
                "WHERE nonce=? AND platform='agent_mailbox_private'", (nonces[0],)
            ).fetchone()
            if row is None or row[2] != "dispatching":
                return False
            receipt = json.loads(row[0])
            binding = {"id": receipt["binding_id"], **{k: receipt[k] for k in FIELDS[1:]}}
            owner, adapter = self.private_route_current(binding)
            if owner is None or adapter is None or not self.current_binding(binding):
                return False
            pin = self.sessions.get(binding["host_session_id"])
            if (pin is None or pin["session_id"] != row[1]
                    or owner.session_store.peek_session_id(binding["host_session_id"]) != row[1]):
                return False
            source = event.source
            authorized = (
                getattr(source, "chat_id", None) == self.private_origin(binding)["chat_id"]
                and getattr(source, "user_id", None) == binding["employee_id"]
            )
            if not authorized:
                return False
            # Admission is a durable one-time claim. A second in-process caller
            # with the same event body cannot enqueue another turn before the
            # first one reaches pre_llm_call. A crash remains unknown, not retryable.
            claimed = self.db.execute(
                "UPDATE deliveries SET status='admitted' WHERE nonce=? AND status='dispatching'",
                (nonces[0],),
            ).rowcount
            self.db.commit()
            return claimed == 1

    def probe(self, binding=None):
        """Local diagnostics only: liveness is never a delivery receipt or queue claim."""
        with self.condition:
            counts = dict(self.db.execute("SELECT status,COUNT(*) FROM deliveries GROUP BY status"))
            route_available = None
            if binding is not None:
                route_available = False
                if isinstance(binding, dict) and self.available():
                    key = binding.get("host_session_id")
                    pin = self.sessions.get(key)
                    if pin and self.current_binding(binding):
                        try:
                            request = self.pin_factory(
                                self.ctx, session_key=key,
                                session_id=pin["session_id"], platform=pin["platform"],
                                still_authorized=lambda: not self.closed and self.current_binding(binding),
                            )
                            route_available = request.current() is True and (
                                not self.private_mode or self.private_route_current(binding)[0] is not None
                            )
                        except Exception:  # noqa: BLE001 — stale/unsupported pins fail closed
                            route_available = False
        result = {
            "protocol": "agent-mailbox.host-health.v1",
            "host_kind": "hermes",
            "gateway_injection_available": self.available(),
            "pinned_sessions": len(self.sessions),
            "unconfirmed_deliveries": sum(n for state, n in counts.items() if state != "received"),
            "recorded_turn_receipts": counts.get("received", 0),
        }
        if binding is not None:
            result["route_available"] = route_available
        return result

    def reauthorization_candidate(self, binding, *, expected_fingerprint=None):
        """Inspect one changed Gateway session; resolve only an exact prior preview.

        This private IPC is not an authorization or a browser API. The raw ID
        is for a subsequent owner-confirmed private config transaction only.
        Neither form schedules a turn, mutates the pin or touches the journal.
        """
        with self.condition:
            if (not isinstance(binding, dict) or set(binding) != set(FIELDS)
                    or any(not isinstance(binding.get(k), str) or not binding[k]
                           for k in FIELDS)
                    or binding["host_kind"] != "hermes"
                    or not self.available() or not self.current_binding(binding)):
                raise ValueError("reauthorization_route_unavailable")
            key = binding["host_session_id"]
            pin = self.sessions.get(key)
            registration = getattr(self.ctx._manager, "_gateway_message_injector", None)
            if not pin or not isinstance(registration, tuple) or len(registration) != 2:
                raise ValueError("reauthorization_route_unavailable")
            owner = registration[0]
            current_id = owner.session_store.peek_session_id(key)
            if not isinstance(current_id, str) or not current_id or current_id == pin["session_id"]:
                raise ValueError("reauthorization_candidate_unavailable")
            candidate = self.pin_factory(
                self.ctx, session_key=key, session_id=current_id,
                platform=pin["platform"],
                still_authorized=lambda: not self.closed and self.current_binding(binding),
            )
            if candidate.current() is not True:
                raise ValueError("reauthorization_candidate_unavailable")
            fingerprint = hashlib.sha256(
                json.dumps([binding["id"], key, pin["session_id"],
                            current_id, pin["platform"]], separators=(",", ":")).encode()
            ).hexdigest()
            if (expected_fingerprint is not None
                    and (not isinstance(expected_fingerprint, str)
                         or expected_fingerprint != fingerprint)):
                raise ValueError("reauthorization_candidate_changed")
            result = {"protocol": REAUTH_PROTOCOL, "host_kind": "hermes",
                      "binding_id": binding["id"], "candidate_fingerprint": fingerprint,
                      "platform": pin["platform"], "route_available": False,
                      "session_verified": False, "delivery_verified": False}
            if expected_fingerprint is not None:
                # Private owner IPC only. The workbench must never serialize this
                # value to a browser, project message or diagnostic log.
                result["candidate_session_id"] = current_id
            return result

    def routing_context(self):
        with self.condition:
            if self.closed:
                raise ValueError("receiver_closed")
            if self.routing is None:
                self.routing = live_factory(self.ctx)
            return self.routing.snapshot()

    def validate(self, envelope):
        binding = envelope.get("binding")
        fresh = envelope.get("fresh")
        if (
            envelope.get("receipt_protocol") != PROTOCOL
            or not isinstance(binding, dict)
            or any(not isinstance(binding.get(k), str) for k in FIELDS)
            or any(not IDENTIFIER.fullmatch(binding[k]) for k in FIELDS[:-1])
            or binding["host_kind"] != "hermes"
            or binding["host_session_id"] not in self.sessions
            or envelope.get("employee_id") != binding["employee_id"]
            or not isinstance(envelope.get("delivery_id"), str)
            or not envelope["delivery_id"].startswith("wake-")
            or not IDENTIFIER.fullmatch(envelope["delivery_id"])
            or not isinstance(fresh, list)
            or not 1 <= len(fresh) <= 200
            or any(not isinstance(m, str) or not IDENTIFIER.fullmatch(m) for m in fresh)
            or len(set(fresh)) != len(fresh)
        ):
            raise ValueError("invalid_delivery")
        return binding, {
            "protocol": PROTOCOL,
            "delivery_id": envelope["delivery_id"],
            "employee_id": binding["employee_id"],
            "message_ids": fresh,
            "binding_id": binding["id"],
            "project_id": binding["project_id"],
            "host_kind": "hermes",
            "host_session_id": binding["host_session_id"],
        }

    def delivery_status(self, envelope):
        """Query one existing delivery by exact identity; never schedule a turn."""
        binding, receipt = self.validate(envelope)
        fingerprint = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
        with self.condition:
            if self.closed:
                return {**receipt, "status": "unknown"}
            row = self.db.execute(
                "SELECT fingerprint,status FROM deliveries WHERE id=?", (receipt["delivery_id"],)
            ).fetchone()
            if row and row[0] != fingerprint:
                raise ValueError("delivery_identity_conflict")
            status = (
                "received" if row and row[1] == "received" and self.current_binding(binding)
                else "unknown"
            )
            return {**receipt, "status": status}

    def receive(self, envelope, wait_seconds=6):
        binding, receipt = self.validate(envelope)
        fingerprint = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
        delivery = receipt["delivery_id"]
        with self.condition:
            if self.closed:
                return {**receipt, "status": "not_received"}
            row = self.db.execute(
                "SELECT fingerprint,status FROM deliveries WHERE id=?", (delivery,)
            ).fetchone()
            if row and row[0] != fingerprint:
                raise ValueError("delivery_identity_conflict")
            if not self.current_binding(binding):
                return {**receipt, "status": "unknown" if row else "not_received"}
            if row and row[1] == "received":
                return {**receipt, "status": "received"}
            if row is None:
                if not self.available():
                    return {**receipt, "status": "not_received"}
                nonce = secrets.token_hex(32)
                pin = self.sessions[binding["host_session_id"]]
                try:
                    request = self.pin_factory(
                        self.ctx,
                        session_key=binding["host_session_id"],
                        session_id=pin["session_id"],
                        platform=pin["platform"],
                        still_authorized=lambda: not self.closed and self.current_binding(binding),
                    )
                    if not request.current():
                        return {**receipt, "status": "not_received"}
                    if self.private_mode and self.private_route_current(binding)[0] is None:
                        return {**receipt, "status": "not_received"}
                except Exception:  # noqa: BLE001 — unsupported host must not use public fallback
                    return {**receipt, "status": "not_received"}
                self.db.execute(
                    "INSERT INTO deliveries VALUES (?,?,?,?,?,?,?)",
                    (
                        delivery,
                        fingerprint,
                        nonce,
                        json.dumps(receipt),
                        pin["session_id"],
                        "agent_mailbox_private" if self.private_mode else pin["platform"],
                        "dispatching",
                    ),
                )
                # Persist BEFORE injection. Crash/ambiguous scheduling never causes
                # automatic reinjection; the same delivery can still gain a late receipt.
                self.db.commit()
                content = (
                    "Agent-mailbox notification, not a new Human instruction. "
                    "Use your own agent-mailbox tools: project_context first, then verify project and recipient. "
                    "If project_message_open is available, explicitly open each indicated message with it; "
                    "a successful open records first-open evidence, not task acceptance. "
                    "If that tool is unavailable, use project_messages without claiming a read receipt was recorded. "
                    "Never bypass denied access or send an extra ACK for opening. Ordinary mail does not "
                    "authorize work. Accept only an already authorized task, read its pinned "
                    "resources, and reply or deliver through agent-mailbox. Do not report "
                    "Human acceptance. "
                    + json.dumps(
                        {
                            "project_id": binding["project_id"],
                            "employee_id": binding["employee_id"],
                            "message_ids": receipt["message_ids"],
                        }
                    )
                    + f"\n[agent-mailbox-delivery:{nonce}]"
                )
                try:
                    # True is only scheduling. Even False/exception after this call
                    # is conservatively unknown until independent turn evidence.
                    if self.private_mode:
                        self.ctx.inject_message(
                            content, origin=self.private_origin(binding),
                            reply_policy="mailbox_only",
                        )
                    else:
                        request.schedule(content)
                except Exception:  # noqa: BLE001 — third-party scheduling boundary
                    logging.getLogger(__name__).warning(
                        "Gateway scheduling unconfirmed; awaiting turn evidence"
                    )

            def observed():
                return (
                    not self.closed
                    and self.db.execute(
                        "SELECT status FROM deliveries WHERE id=?", (delivery,)
                    ).fetchone()[0]
                    == "received"
                )

            self.condition.wait_for(lambda: self.closed or observed(), timeout=max(0, min(wait_seconds, 6)))
            status = "received" if observed() and self.current_binding(binding) else "unknown"
            return {**receipt, "status": status}

    def observe_turn(self, *, session_id, user_message, platform="", **_ignored):
        # Do not inspect conversation_history or retain model/provider metadata.
        # This is transport admission evidence, NOT message-read/task acceptance.
        if not isinstance(user_message, str):
            return
        with self.condition:
            if self.closed:
                return
            nonces = MARKER.findall(user_message)
            if self.private_mode and (platform != "agent_mailbox_private" or len(nonces) != 1):
                return
            for nonce in nonces:
                if self.private_mode:
                    row = self.db.execute(
                        "SELECT id,receipt,session_id,status FROM deliveries "
                        "WHERE nonce=? AND platform='agent_mailbox_private'", (nonce,)
                    ).fetchone()
                else:
                    row = self.db.execute(
                        "SELECT id,receipt FROM deliveries WHERE nonce=? "
                        "AND session_id=? AND platform=?",
                        (nonce, session_id, platform),
                    ).fetchone()
                if row is None:
                    continue
                receipt = json.loads(row[1])
                binding = {"id": receipt["binding_id"], **{k: receipt[k] for k in FIELDS[1:]}}
                if not self.current_binding(binding):
                    continue
                if self.private_mode:
                    owner, adapter = self.private_route_current(binding)
                    pin = self.sessions.get(binding["host_session_id"])
                    if owner is None or adapter is None or pin is None or row[3] != "admitted":
                        continue
                    source = adapter.build_source(
                        self.private_origin(binding)["chat_id"], chat_type="dm",
                        user_id=binding["employee_id"],
                    )
                    key = adapter._source_session_key(source)
                    if (pin["session_id"] != row[2]
                            or owner.session_store.peek_session_id(binding["host_session_id"]) != row[2]
                            or owner.session_store.peek_session_id(key) != session_id):
                        continue
                # Busy turns can start after the transport timeout. Persist new
                # independent evidence for recovery, without reinjection.
                # The DB is authoritative after a crash; the file is a mirror.
                try:
                    self.db.execute("UPDATE deliveries SET status='received' WHERE id=?", (row[0],))
                    self.db.commit()
                except Exception:
                    self.db.rollback()
                    raise
                try:
                    self.write_receipt({**receipt, "status": "received"})
                except OSError:
                    logging.getLogger(__name__).warning(
                        "Private receipt file unavailable; durable DB receipt retained"
                        if self.private_mode else
                        "Hermes receipt mirror failed; durable journal retained"
                    )
            self.condition.notify_all()

    def write_receipt(self, receipt):
        directory = private_directory(self.home / "wake" / "receipts")
        fd, temporary = tempfile.mkstemp(prefix=".hermes-receipt-", dir=directory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(receipt, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / receipt["delivery_id"])
        finally:
            Path(temporary).unlink(missing_ok=True)

    def close(self):
        with self.condition:
            if not self.closed:
                self.closed = True
                self.condition.notify_all()
                self.db.close()


class LocalServer(socketserver.UnixStreamServer):
    """One bounded request at a time; background turn hooks do not use this socket."""

    def __init__(self, path, receiver):
        path = Path(path)
        private_directory(path.parent)
        if len(os.fsencode(path)) > 100:
            raise ValueError("socket_path_unavailable")
        self.receiver = receiver
        self.owned_inode = None
        self.lock_fd = None
        self.server_address = str(path)
        try:
            # Keep the lock file: unlinking it lets another process lock a new inode
            # while this one still owns the old inode. Kernel release handles crashes.
            lock_path = Path(str(path) + ".lock")
            self.lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            info = os.fstat(self.lock_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise ValueError("owner_private_socket_lock_required")
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if lock_path.lstat().st_ino != info.st_ino:
                raise ValueError("socket_lock_changed")
            self._recover_stale_socket(path)
            # Delay bind so partial initialization always closes under our lock.
            super().__init__(str(path), _Handler, bind_and_activate=False)
            self.server_bind()
            self.owned_inode = path.lstat().st_ino
            os.chmod(path, 0o600)
            self.server_activate()
        except BaseException:
            self.server_close()
            raise

    def _recover_stale_socket(self, path):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("socket_path_unavailable")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.25)
            try:
                probe.connect(str(path))
            except OSError as exc:
                # Timeout, permissions and every ambiguous error fail closed.
                if exc.errno != errno.ECONNREFUSED:
                    raise ValueError("socket_liveness_unknown") from exc
            else:
                raise ValueError("socket_already_listening")
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("socket_path_changed")
        path.unlink()

    def server_close(self):
        if hasattr(self, "socket"):
            super().server_close()
        path = Path(self.server_address)
        try:
            if (
                self.owned_inode is not None
                and path.lstat().st_ino == self.owned_inode
                and stat.S_ISSOCK(path.lstat().st_mode)
            ):
                path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self.owned_inode = None
            if self.lock_fd is not None:
                os.close(self.lock_fd)
                self.lock_fd = None


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(8)
        try:
            raw = self.rfile.readline(65537)
            if len(raw) > 65536 or not raw.endswith(b"\n"):
                raise ValueError("invalid_frame")
            payload = json.loads(raw)
            if isinstance(payload, dict) and payload.get("operation") == "probe" and set(payload) <= {"operation", "binding"}:
                result = self.server.receiver.probe(payload.get("binding"))
            elif (isinstance(payload, dict) and payload.get("operation") == "reauthorization_candidate"
                  and set(payload) == {"operation", "binding"}):
                result = self.server.receiver.reauthorization_candidate(payload["binding"])
            elif (isinstance(payload, dict) and payload.get("operation") == "reauthorization_resolve"
                  and set(payload) == {"operation", "binding", "expected_fingerprint"}):
                result = self.server.receiver.reauthorization_candidate(
                    payload["binding"], expected_fingerprint=payload["expected_fingerprint"])
            elif payload == {"operation": "routing_context"}:
                result = self.server.receiver.routing_context()
            elif (isinstance(payload, dict) and set(payload) == {"operation", "envelope"}
                  and payload["operation"] == "delivery_status"):
                result = self.server.receiver.delivery_status(payload["envelope"])
            elif isinstance(payload, dict) and "operation" not in payload:
                result = self.server.receiver.receive(payload)
            else:
                raise ValueError("invalid_operation")
            self.wfile.write(json.dumps(result).encode() + b"\n")
        except Exception:  # noqa: BLE001 — private socket protocol boundary
            # Never leak session identifiers, tracebacks or config to socket callers.
            self.wfile.write(b'{"error":"delivery_unconfirmed"}\n')


def register(ctx):
    diagnostics = ctx.get_config("discovery_only") is True
    private_mode = not diagnostics and ctx.get_config("private_work_session") is True
    if private_mode:
        require_private_host()  # Fail before opening a journal or registering a platform.
    receiver = Diagnostics(ctx) if diagnostics else Receiver(
        ctx, ctx.get_config("home"), ctx.get_config("state_dir"), ctx.get_config("sessions"),
        pin_factory=private_pinned_factory if private_mode else pinned_factory,
        private_mode=private_mode,
    )
    socket_path = ctx.get_config("socket_path")
    if not isinstance(socket_path, str) or not Path(socket_path).is_absolute():
        receiver.close()
        raise ValueError("absolute_socket_path_required")
    stop = threading.Event()
    server_holder = []
    if not diagnostics:
        try:
            if receiver.private_mode:
                receiver.private_adapter_type = register_private_platform(
                    ctx, receiver.private_event_authorized,
                )
            ctx.register_hook("pre_llm_call", receiver.observe_turn)
        except BaseException:
            receiver.close()
            raise

    def serve():
        # Plugin loading precedes Gateway publication. This waits for host startup,
        # never polls mail or schedules Agent turns. CLI-only processes open no socket.
        while not stop.is_set():
            if receiver.available():
                try:
                    server = LocalServer(socket_path, receiver)
                    server.timeout = 0.25
                    server_holder.append(server)
                    while not stop.is_set():
                        server.handle_request()
                    return
                finally:
                    if server_holder:
                        server_holder[0].server_close()
            stop.wait(0.25)

    thread = threading.Thread(target=serve, name="agent-mailbox-gateway", daemon=True)

    def cleanup():
        stop.set()
        if thread.ident is not None:
            thread.join(timeout=16)
        if not thread.is_alive():
            receiver.close()

    ctx.on_unload(cleanup)
    thread.start()
