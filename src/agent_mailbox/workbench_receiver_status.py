"""Owner-only component diagnostics, never admission, binding or installation."""

import errno
import json
import os
import re
import socket
import stat
import time
from pathlib import Path

from .workbench_host_bindings import load_bindings
from .workbench_host_transport import configured_for_binding, endpoint_for, receiver_online
from .workbench_mail_notifications import legacy_untracked_pending_count
from .workbench_store import WorkbenchError


def _member(store, employee_id, project_id):
    with store._transaction(readonly=True) as db:
        employee = store._required(db, "employees", employee_id)
        store._required(db, "projects", project_id)
        if (
            employee["lifecycle"] != "active"
            or not db.execute(
                "SELECT 1 FROM memberships WHERE employee_id=? AND project_id=?",
                (employee_id, project_id),
            ).fetchone()
        ):
            raise WorkbenchError("RECEIVER_MEMBER_REQUIRED", "Choose an active project member.")
        return employee["kind"], employee["node_id"] == store.local_node_id(db)


def _binding(home, employee_id, project_id, kind):
    return next(
        (
            b
            for b in load_bindings(home)
            if b["employee_id"] == employee_id
            and b["project_id"] == project_id
            and b["host_kind"] == kind
        ),
        None,
    )


def _request(endpoint, request, *, timeout=2):
    path = Path(endpoint)
    parent = path.parent
    info = parent.lstat()
    if (
        parent.resolve() != parent
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("unsafe_receiver_directory")
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("unsafe_receiver_socket")
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(timeout)
        channel.connect(str(path))
        channel.settimeout(max(0.001, deadline - time.monotonic()))
        channel.sendall(json.dumps(request).encode() + b"\n")
        buffer = b""
        while b"\n" not in buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("receiver_probe_timeout")
            channel.settimeout(remaining)
            chunk = channel.recv(min(4096, 8193 - len(buffer)))
            if not chunk:
                raise ValueError("incomplete_receiver_probe")
            buffer += chunk
            if len(buffer) > 8192:
                raise ValueError("receiver_probe_too_large")
    return json.loads(buffer.split(b"\n", 1)[0])


def _probe(endpoint, kind, *, timeout=2, binding=None, details=False):
    request = {"operation": "probe"}
    if kind == "hermes" and binding is not None:
        request["binding"] = binding
    value = _request(endpoint, request, timeout=timeout)
    protocol = (
        "agent-mailbox.host-health.v1" if kind == "hermes" else "agent-mailbox.host-receipt.v1"
    )
    flag = (
        "route_available"
        if kind == "hermes" and binding is not None
        else "gateway_injection_available"
        if kind == "hermes"
        else "queue_supported"
    )
    if (
        not isinstance(value, dict)
        or value.get("protocol") != protocol
        or value.get("host_kind") != kind
        or type(value.get(flag)) is not bool
    ):
        raise ValueError("unexpected_receiver_probe")
    if details:
        if (
            kind != "hermes"
            or binding is None
            or type(value.get("gateway_injection_available")) is not bool
        ):
            raise ValueError("unexpected_receiver_probe")
        return {
            "route_available": value["route_available"],
            "gateway_available": value["gateway_injection_available"],
        }
    return value[flag]


def _reauthorization_candidate(endpoint, binding):
    """Return only a valid changed-session candidate, never a generic host error."""
    value = _request(endpoint, {"operation": "reauthorization_candidate", "binding": binding})
    if value == {"error": "delivery_unconfirmed"}:
        return None
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "protocol",
            "host_kind",
            "binding_id",
            "candidate_fingerprint",
            "platform",
            "route_available",
            "session_verified",
            "delivery_verified",
        }
        or value["protocol"] != "agent-mailbox.host-reauthorization.v1"
        or value["host_kind"] != "hermes"
        or value["binding_id"] != binding["id"]
        or not isinstance(value["platform"], str)
        or not value["platform"]
        or not re.fullmatch(r"[a-f0-9]{64}", value["candidate_fingerprint"])
        or value["route_available"] is not False
        or value["session_verified"] is not False
        or value["delivery_verified"] is not False
    ):
        raise ValueError("unexpected_reauthorization_candidate")
    return value


def reception_status(store, employee_id, project_id):
    kind, local = _member(store, employee_id, project_id)
    with store._transaction(readonly=True) as db:
        pending = db.execute(
            "SELECT state_hash,created_at FROM notifications "
            "WHERE kind='mailbox_wake' AND target_id=? AND project_id=? AND status='pending'",
            (employee_id, project_id),
        ).fetchall()
        held_count = db.execute(
            "SELECT count(*) FROM notifications "
            "WHERE kind='mailbox_wake' AND target_id=? AND project_id=? "
            "AND status='reauthorization_held'",
            (employee_id, project_id),
        ).fetchone()[0]
        legacy_untracked = legacy_untracked_pending_count(db, store.root, employee_id, project_id)
    claims = store.root / "wake" / "claims"
    unknown_count = sum((claims / f"{row['state_hash']}.unknown").is_file() for row in pending)
    result = {
        "host_kind": kind,
        "component_available": False,
        "binding_configured": False,
        "route_configured": False,
        "session_verified": False,
        "delivery_verified": False,
        "pending_alert_count": len(pending),
        "legacy_untracked_alert_count": legacy_untracked,
        "held_alert_count": held_count,
        "unknown_delivery_alert_count": unknown_count,
        "oldest_pending_alert_at": min((row["created_at"] for row in pending), default=None),
    }
    if kind == "hermes":
        # First contact is a home-wide operation, even when this member has
        # no binding of their own. Unknown must not look like an empty home.
        result["hermes_route_present"] = None
    if not local:
        return {**result, "state": "remote_device"}
    if os.name == "nt" or kind not in {"hermes", "deepseek", "codex"}:
        return {**result, "state": "unsupported"}
    try:
        bindings = load_bindings(store.root)
        if kind == "hermes":
            result["hermes_route_present"] = any(item["host_kind"] == "hermes" for item in bindings)
        binding = next(
            (
                item
                for item in bindings
                if item["employee_id"] == employee_id
                and item["project_id"] == project_id
                and item["host_kind"] == kind
            ),
            None,
        )
        if not binding:
            return {**result, "state": "binding_required"}
        result["binding_configured"] = True
        if not configured_for_binding(store.root, binding):
            return {**result, "state": "route_required"}
        result["route_configured"] = True
        endpoint = endpoint_for(store.root, binding)
        if not endpoint:
            return {**result, "state": "receiver_offline"}
        if kind == "codex":
            health = receiver_online(store.root, binding)
        else:
            health = (
                _probe(endpoint, kind, binding=binding, details=True)
                if kind == "hermes"
                else _probe(endpoint, kind)
            )
        if (
            _member(store, employee_id, project_id) != (kind, True)
            or _binding(store.root, employee_id, project_id, kind) != binding
            or endpoint_for(store.root, binding) != endpoint
        ):
            return {**result, "state": "configuration_changed"}
        if kind == "hermes":
            available = health["route_available"]
            state = "component_available" if available else "host_unavailable"
            if not available and health["gateway_available"]:
                candidate = _reauthorization_candidate(endpoint, binding)
                if (
                    _member(store, employee_id, project_id) != (kind, True)
                    or _binding(store.root, employee_id, project_id, kind) != binding
                    or endpoint_for(store.root, binding) != endpoint
                ):
                    return {**result, "state": "configuration_changed"}
                if candidate is not None:
                    state = "session_reconnect_required"
        else:
            available = health
            state = "component_available" if available else "host_unavailable"
        return {**result, "state": state, "component_available": available}
    except OSError as exc:
        state = (
            "receiver_offline"
            if exc.errno in {errno.ENOENT, errno.ECONNREFUSED}
            else "check_unconfirmed"
        )
        return {**result, "state": state}
    except (ValueError, TypeError, KeyError):
        return {**result, "state": "check_unconfirmed"}


def hermes_reauthorization_preview(store, employee_id, project_id):
    """Owner-only read of a changed exact session; no authorization or delivery."""
    kind, local = _member(store, employee_id, project_id)
    if kind != "hermes" or not local:
        raise WorkbenchError("RECEIVER_MEMBER_REQUIRED", "Choose an active local Hermes member.")
    status = reception_status(store, employee_id, project_id)
    if status["state"] != "session_reconnect_required":
        return {"state": status["state"], "candidate_available": False}
    try:
        binding = _binding(store.root, employee_id, project_id, kind)
        endpoint = endpoint_for(store.root, binding) if binding else None
        if not endpoint:
            return {"state": "configuration_changed", "candidate_available": False}
        value = _reauthorization_candidate(endpoint, binding)
        if value is None:
            return {"state": "host_unavailable", "candidate_available": False}
        if (
            _member(store, employee_id, project_id) != (kind, True)
            or _binding(store.root, employee_id, project_id, kind) != binding
            or endpoint_for(store.root, binding) != endpoint
        ):
            return {"state": "configuration_changed", "candidate_available": False}
        from .workbench_mail_notifications import pending_ids

        pending = pending_ids(store, employee_id)
        claims = store.root / "wake" / "claims"
        unknown_count = sum((claims / f"{mid}.unknown").is_file() for mid in pending)
        return {
            "state": "candidate_ready",
            "candidate_available": True,
            "binding_id": binding["id"],
            "candidate_fingerprint": value["candidate_fingerprint"],
            "platform": value["platform"],
            "pending_count": len(pending),
            "unknown_count": unknown_count,
            "session_verified": False,
            "delivery_verified": False,
        }
    except (OSError, ValueError, TypeError, KeyError):
        return {"state": "check_unconfirmed", "candidate_available": False}
