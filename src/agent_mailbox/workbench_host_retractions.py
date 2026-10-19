"""Dismiss an already queued DSH mail alert after explicit recipient opens.

The DB notification is a durable intent. The host queue is external state:
retraction never rewrites the original delivery receipt or task acceptance.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
from hashlib import sha256
from pathlib import Path

from .workbench_store import _id, _now

KIND = "mailbox_queue_retract"


def status_counts(store, *, employee_id=None, project_id=None):
    """Aggregate owner diagnostics without exposing message or session IDs."""
    with store._transaction(readonly=True) as db:
        return _status_counts(db, employee_id=employee_id, project_id=project_id)


def _status_counts(db, *, employee_id=None, project_id=None):
    clauses = ["kind=?"]
    parameters = [KIND]
    if employee_id is not None:
        clauses.append("target_id=?")
        parameters.append(employee_id)
    if project_id is not None:
        clauses.append("project_id=?")
        parameters.append(project_id)
    return {
        row["status"]: row["count"]
        for row in db.execute(
            "SELECT status,COUNT(*) AS count FROM notifications "
            f"WHERE {' AND '.join(clauses)} GROUP BY status",
            parameters,
        ).fetchall()
    }


def status_counts_file(home):
    """Read an existing CLI store without creating or migrating a database.

    With no WAL, an immutable read needs no SQLite sidecars. With an active WAL,
    copy the stable DB/WAL bytes to a private temporary directory: SQLite's
    read-only WAL connection can still modify the source SHM read marks.
    Ambiguous or changing states are unconfirmed, not zero.
    """
    try:
        directory = Path(home).expanduser().resolve(strict=True) / "workbench"
        if not _entry_exists(directory):
            return {}
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return None
        database = directory / "state.db"
        if not _entry_exists(database):
            return {}
        if not _private_regular(database):
            return None
        before = database.stat()
        wal, shm = Path(f"{database}-wal"), Path(f"{database}-shm")
        has_wal, has_shm = _entry_exists(wal), _entry_exists(shm)
        if has_wal != has_shm or (has_wal and not all(_private_regular(p) for p in (wal, shm))):
            return None
        if has_wal:
            with tempfile.TemporaryDirectory(prefix="agent-mailbox-status-") as temporary:
                snapshot = Path(temporary)
                db_copy = snapshot / database.name
                fingerprints = {
                    database: _private_fingerprint(database, copy_to=db_copy),
                    wal: _private_fingerprint(wal, copy_to=snapshot / wal.name),
                }
                with sqlite3.connect(db_copy.as_uri() + "?mode=ro", uri=True, timeout=1) as db:
                    db.row_factory = sqlite3.Row
                    counts = _status_counts(db)
                if any(
                    _private_fingerprint(path) != original
                    for path, original in fingerprints.items()
                ):
                    return None
                if not _private_regular(shm):
                    return None
        else:
            with sqlite3.connect(
                database.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1
            ) as db:
                db.row_factory = sqlite3.Row
                counts = _status_counts(db)
        after = database.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            return None
        if not has_wal and (_entry_exists(wal) or _entry_exists(shm)):
            return None
        return counts
    except (OSError, sqlite3.Error, ValueError):
        return None


def _entry_exists(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _private_regular(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0


def _private_fingerprint(path, *, copy_to=None):
    """Fingerprint one private file through a no-follow descriptor, optionally copying it."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_mode & 0o077
        ):
            raise ValueError("unsafe_retraction_database")
        signature = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        digest = sha256()
        total = 0
        if copy_to is None:
            output = None
        else:
            output = copy_to.open("xb")
            copy_to.chmod(0o600)
        try:
            while chunk := os.read(fd, 1024 * 1024):
                digest.update(chunk)
                total += len(chunk)
                if output is not None:
                    output.write(chunk)
        finally:
            if output is not None:
                output.close()
        after = os.fstat(fd)
        if (
            total != before.st_size
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            != signature
        ):
            raise ValueError("retraction_database_changed")
        return signature, digest.hexdigest()
    finally:
        os.close(fd)


def _read_private(path, limit=65536):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > limit
        ):
            raise ValueError("unsafe_retraction_record")
        value = os.read(fd, limit + 1)
        if len(value) > limit:
            raise ValueError("retraction_record_too_large")
        return value.decode("utf-8")
    finally:
        os.close(fd)


def enqueue_in(db, message):
    """Call in the same transaction as the recipient's first-open event."""
    db.execute(
        "INSERT OR IGNORE INTO notifications "
        "(id,kind,channel,target_kind,target_id,project_id,state_hash,created_at,updated_at) "
        "VALUES(?,?,'host','employee',?,?,?,?,?)",
        (
            _id("note"),
            KIND,
            message["recipient_id"],
            message["project_id"],
            message["id"],
            _now(),
            _now(),
        ),
    )


def _settle(store, message_ids, employee_id, status):
    with store._transaction() as db:
        db.executemany(
            "UPDATE notifications SET status=?,updated_at=? "
            "WHERE kind=? AND target_id=? AND state_hash=? AND status='pending'",
            [(status, _now(), KIND, employee_id, mid) for mid in message_ids],
        )


def reconcile_opened_queue(store, home):
    """Run under the wake directory lock, after ordinary delivery completes.

    No claim means no host admission can follow an already-opened letter:
    pending_ids excludes it, and the lock excludes a concurrent delivery pass.
    An uncertain external response is terminal unknown, never false success.
    """
    from .workbench_host_bindings import load_bindings
    from .workbench_host_transport import endpoint_for, retract_dsh_queue
    from .workbench_mail_notifications import _recipient_opened

    with store._transaction(readonly=True) as db:
        pending = db.execute(
            "SELECT state_hash,target_id,project_id FROM notifications "
            "WHERE kind=? AND status='pending' ORDER BY created_at,id",
            (KIND,),
        ).fetchall()
    if not pending:
        return []
    if os.name == "nt":
        # The current DSH receiver uses an owner-private Unix socket. A future
        # Windows transport must establish its own equivalent caller identity.
        for row in pending:
            _settle(store, [row["state_hash"]], row["target_id"], "unsupported_host")
        return [(row["state_hash"], "unsupported_host") for row in pending]
    base = Path(home) / "wake"
    claims = base / "claims"
    seen = set()
    results = []
    for row in pending:
        mid, employee_id = row["state_hash"], row["target_id"]
        if mid in seen:
            continue
        group = [mid]
        claim = claims / mid
        confirmed = _private_regular(Path(f"{claim}.ok"))
        uncertain = _private_regular(Path(f"{claim}.unknown"))
        if not confirmed and not uncertain:
            if claim.exists():
                continue  # In-flight or corrupt claim: never infer non-delivery.
            with store._transaction(readonly=True) as db:
                original = db.execute(
                    "SELECT status,attempts FROM notifications "
                    "WHERE kind='mailbox_wake' AND target_id=? AND state_hash=?",
                    (employee_id, mid),
                ).fetchone()
            status = (
                "not_needed"
                if original is not None
                and original["status"] == "recipient_opened"
                and original["attempts"] == 0
                else "unknown"
            )
            _settle(store, [mid], employee_id, status)
            results.append((mid, status))
            continue
        try:
            marker = claim if confirmed else Path(f"{claim}.unknown")
            delivery_id = _read_private(marker, 512).strip()
            if not delivery_id.startswith("wake-") or Path(delivery_id).name != delivery_id:
                raise ValueError("invalid_retraction_claim")
            payload = json.loads(_read_private(base / "wake-outbox" / f"{delivery_id}.json"))
            binding = payload["binding"]
            fresh = payload["fresh"]
            if (
                payload.get("delivery_id") != delivery_id
                or payload.get("receipt_protocol") != "agent-mailbox.host-receipt.v1"
                or payload.get("employee_id") != employee_id
                or not isinstance(binding, dict)
                or binding.get("employee_id") != employee_id
                or binding.get("project_id") != row["project_id"]
                or not isinstance(fresh, list)
                or mid not in fresh
                or not fresh
                or len(fresh) > 200
                or len(set(fresh)) != len(fresh)
                or any(not isinstance(item, str) or Path(item).name != item for item in fresh)
            ):
                raise ValueError("retraction_outbox_identity_mismatch")
            if confirmed:
                receipt = json.loads(_read_private(base / "receipts" / delivery_id))
                expected = {
                    "protocol": "agent-mailbox.host-receipt.v1",
                    "delivery_id": delivery_id,
                    "employee_id": employee_id,
                    "message_ids": fresh,
                    "binding_id": binding.get("id"),
                    "project_id": binding.get("project_id"),
                    "host_kind": binding.get("host_kind"),
                    "host_session_id": binding.get("host_session_id"),
                    "status": "received",
                }
                if not isinstance(receipt, dict) or any(
                    receipt.get(key) != value for key, value in expected.items()
                ):
                    raise ValueError("retraction_original_receipt_mismatch")
            group = fresh
            seen.update(fresh)
            with store._transaction(readonly=True) as db:
                all_opened = all(
                    _recipient_opened(db, row["project_id"], item, employee_id) for item in fresh
                )
            if not all_opened:
                continue  # One queued prompt may contain other unread letters.
            if binding.get("host_kind") != "deepseek":
                _settle(store, fresh, employee_id, "unsupported_host")
                results.append((delivery_id, "unsupported_host"))
                continue
            current = [b for b in load_bindings(home) if b["id"] == binding.get("id")]
            if len(current) != 1 or any(
                current[0].get(key) != binding.get(key)
                for key in ("id", "employee_id", "project_id", "host_kind", "host_session_id")
            ):
                _settle(store, fresh, employee_id, "binding_changed")
                results.append((delivery_id, "binding_changed"))
                continue
            endpoint = endpoint_for(home, binding)
            if endpoint is None:
                continue  # Receiver offline: durable intent remains pending.
            status = retract_dsh_queue(
                endpoint,
                {
                    **payload,
                    "operation": "retract_opened",
                    "receipt_protocol": "agent-mailbox.host-retraction.v1",
                },
            )
            if status is None:
                continue  # Definitively offline before request submission.
            _settle(store, fresh, employee_id, status)
            results.append((delivery_id, status))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            _settle(store, group, employee_id, "unknown")
            results.append((delivery_id if group != [mid] else mid, "unknown"))
    return results
