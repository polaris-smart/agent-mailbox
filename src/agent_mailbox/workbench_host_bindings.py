"""Owner-configured project/employee host routes; no credentials or executable code."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path

from .workbench_private import private_mode

FILENAME = "host-bindings.json"


def load_bindings(home):
    path = Path(home) / "wake" / FILENAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("Invalid host binding configuration")
    bindings = value.get("bindings")
    if type(bindings) is not list:
        raise ValueError("Invalid host binding list")
    pairs, identities, host_owners = set(), set(), {}
    for item in bindings:
        fields = ("id", "employee_id", "project_id", "host_kind", "host_session_id")
        if not isinstance(item, dict) or any(
            not isinstance(item.get(k), str) or not item[k].strip() for k in fields
        ):
            raise ValueError("Invalid host binding fields")
        surface = item.get("receiver_surface")
        if surface is not None and (
            item["host_kind"] != "codex"
            or not isinstance(surface, str)
            or surface not in {"desktop", "cli"}
        ):
            raise ValueError("Invalid receiver surface")
        pair = (item["employee_id"], item["project_id"])
        if pair in pairs or item["id"] in identities:
            raise ValueError("Ambiguous host binding")
        host = (item["host_kind"], item["host_session_id"])
        owner = host_owners.setdefault(host, item["employee_id"])
        if owner != item["employee_id"]:
            raise ValueError("host_session_already_owned")
        pairs.add(pair)
        identities.add(item["id"])
    return bindings


def set_binding(store, employee_id, project_id, *, host_kind=None, host_session_id=None):
    """Called by the local owner CLI. Omitting both host fields revokes the route."""
    from .workbench_wake import state_lock

    with state_lock(store.root):
        endpoint_path = Path(store.root) / "wake" / "host-endpoints.json"
        original = None
        if host_kind is None and host_session_id is None:
            from .workbench_host_transport import endpoints

            prior = next(
                (
                    row
                    for row in load_bindings(store.root)
                    if (row["employee_id"], row["project_id"]) == (employee_id, project_id)
                ),
                None,
            )
            if prior and endpoint_path.exists():
                rows = endpoints(store.root)
                if any(row["binding_id"] == prior["id"] for row in rows):
                    original = endpoint_path.read_bytes()
                    document = json.loads(original)
                    if document.get("endpoints") != rows:
                        raise ValueError("endpoint_config_changed")
                    document["endpoints"] = [
                        row for row in rows if row["binding_id"] != prior["id"]
                    ]
                    replacement = json.dumps(document).encode()
                    _replace_private(endpoint_path, replacement)
        try:
            return _set_binding_locked(
                store, employee_id, project_id, host_kind=host_kind, host_session_id=host_session_id
            )
        except Exception:
            if original is not None:
                _replace_private(endpoint_path, original)
            raise


def _replace_private(path: Path, payload: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".host-endpoints-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        private_mode(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _set_binding_locked(
    store,
    employee_id,
    project_id,
    *,
    host_kind=None,
    host_session_id=None,
    expected_binding_id=...,
    receiver_surface=None,
):
    """Caller holds the wake lock; owner UI also supplies a compare-and-swap identity."""
    remove = host_kind is None and host_session_id is None
    if not remove and any(
        not isinstance(v, str) or not v.strip() or len(v) > 512 or any(ord(c) < 32 for c in v)
        for v in (host_kind, host_session_id)
    ):
        raise ValueError("Host and existing session must be nonempty identifiers")
    if receiver_surface is not None and (
        remove
        or host_kind != "codex"
        or not isinstance(receiver_surface, str)
        or receiver_surface not in {"desktop", "cli"}
    ):
        raise ValueError("Invalid receiver surface")
    # Serialize route changes and membership validation with the write transaction.
    with store._transaction() as db:
        employee = store._required(db, "employees", employee_id)
        store._required(db, "projects", project_id)
        if not remove and (
            employee["lifecycle"] != "active"
            or employee["node_id"] != store.local_node_id(db)
            or not db.execute(
                "SELECT 1 FROM memberships WHERE employee_id=? AND project_id=?",
                (employee_id, project_id),
            ).fetchone()
        ):
            raise ValueError("Only an active local project member may bind a host")
        current = load_bindings(store.root)
        prior = next(
            (
                b
                for b in current
                if (b["employee_id"], b["project_id"]) == (employee_id, project_id)
            ),
            None,
        )
        if (
            expected_binding_id is not ...
            and (prior["id"] if prior else None) != expected_binding_id
        ):
            raise ValueError("receiver_binding_changed_refresh_required")
        remaining = [
            b for b in current if (b["employee_id"], b["project_id"]) != (employee_id, project_id)
        ]
        binding = None
        if not remove:
            if any(
                b["host_kind"] == host_kind
                and b["host_session_id"] == host_session_id
                and b["employee_id"] != employee_id
                for b in remaining
            ):
                raise ValueError("host_session_already_owned")
            binding = {
                "id": "binding_" + uuid.uuid4().hex,
                "employee_id": employee_id,
                "project_id": project_id,
                "host_kind": host_kind,
                "host_session_id": host_session_id,
            }
            if receiver_surface is not None:
                binding["receiver_surface"] = receiver_surface
            remaining.append(binding)
        base = Path(store.root) / "wake"
        private_mode(base, 0o700)
        fd, temporary = tempfile.mkstemp(prefix=".host-bindings-", dir=base)
        try:
            os.close(fd)
            private_mode(temporary, 0o600)
            with open(temporary, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "bindings": remaining}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, base / FILENAME)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return binding


def resolve_routes(store, employee_id, message_ids):
    """Resolve only current authorized messages; missing routes remain pending."""
    from .workbench_reauthorization_holds import authorized_message

    bindings = {
        b["project_id"]: b for b in load_bindings(store.root) if b["employee_id"] == employee_id
    }
    if not bindings:
        return [], list(message_ids)
    groups = {}
    missing = []
    with store._transaction(readonly=True) as db:
        for mid in message_ids:
            row = db.execute(
                "SELECT m.project_id,m.created_at FROM messages m "
                "JOIN memberships p ON p.project_id=m.project_id AND p.employee_id=m.recipient_id "
                "JOIN employees e ON e.id=m.recipient_id AND e.lifecycle='active' "
                "WHERE m.id=? AND m.recipient_id=? AND e.node_id=? "
                "AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.kind='mailbox_wake' "
                "AND n.target_id=m.recipient_id AND n.state_hash=m.id "
                "AND n.status='reauthorization_held')",
                (mid, employee_id, store.local_node_id(db)),
            ).fetchone()
            binding = bindings.get(row[0]) if row else None
            old_unknown = (Path(store.root) / "wake" / "claims" / f"{mid}.unknown").is_file()
            if binding is None or (
                not old_unknown
                and not authorized_message(
                    store.root,
                    binding["id"],
                    row["created_at"],
                    message_id=mid,
                    employee_id=employee_id,
                    project_id=row["project_id"],
                )
            ):
                missing.append(mid)
            else:
                entry = groups.setdefault(binding["id"], {"binding": binding, "messages": []})
                entry["messages"].append(mid)
    return list(groups.values()), missing
