"""Owner-confirmed reuse of one Hermes original session for another project.

This changes only mailbox routing for an already authorized local employee.
The browser never supplies or receives the Hermes session key or socket path.
It does not start a host, inject mail, or infer a delivery receipt.
"""

from __future__ import annotations

import hashlib
import json

from . import workbench_hermes_catalog as catalog
from .workbench_host_bindings import (
    _replace_private,
    _set_binding_locked,
    load_bindings,
)
from .workbench_host_transport import CONFIG, endpoint_for, endpoints
from .workbench_reauthorization_holds import gated, publish_new_project_route_locked
from .workbench_store import WorkbenchError
from .workbench_wake import state_lock


def _candidate(store, employee_id, project_id):
    catalog._member(store, employee_id, project_id)
    bindings = load_bindings(store.root)
    target = next(
        (
            row
            for row in bindings
            if row["employee_id"] == employee_id and row["project_id"] == project_id
        ),
        None,
    )
    sources = [
        row
        for row in bindings
        if row["employee_id"] == employee_id
        and row["host_kind"] == "hermes"
        and row["project_id"] != project_id
    ]
    available = []
    configured = {row["binding_id"]: row for row in endpoints(store.root)}
    for source in sources:
        try:
            catalog._member(store, employee_id, source["project_id"])
        except WorkbenchError as exc:
            if exc.code != "RECEIVER_MEMBER_REQUIRED":
                raise
            continue
        row = configured.get(source["id"])
        if (
            row is not None
            and row["employee_id"] == employee_id
            and row["host_kind"] == "hermes"
            and "socket_path" in row
            and endpoint_for(store.root, source) == row["socket_path"]
        ):
            available.append((source, row))
    if not available:
        raise ValueError("source_route_required")
    scopes = {(source["host_session_id"], row["socket_path"]) for source, row in available}
    if len(scopes) != 1:
        raise ValueError("source_route_ambiguous")
    source, endpoint = min(available, key=lambda item: item[0]["id"])
    if target is not None:
        if gated(store.root, target["id"]):
            raise ValueError("target_route_fenced_requires_inspection")
        row = configured.get(target["id"])
        if (
            target["host_kind"] != "hermes"
            or target["host_session_id"] != source["host_session_id"]
            or row is not None
            and (
                row.get("socket_path") != endpoint["socket_path"]
                or row["employee_id"] != employee_id
                or row["host_kind"] != "hermes"
            )
        ):
            raise ValueError("target_route_conflict")
        if row is not None:
            return None, target, endpoint
    facts = {
        "employee_id": employee_id,
        "project_id": project_id,
        "source_binding_id": source["id"],
        "target_binding_id": target["id"] if target else None,
        "session_key": source["host_session_id"],
        "socket_path": endpoint["socket_path"],
    }
    fingerprint = hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()
    return fingerprint, target, endpoint


def operation(
    store, employee_id, project_id, action, *, expected_fingerprint=None, confirmed=False
):
    """Preview or apply one exact project link; receipt remains unverified."""
    if action not in {"preview", "apply"} or type(confirmed) is not bool:
        raise ValueError("invalid_project_link_action")
    if action == "preview":
        if confirmed or expected_fingerprint is not None:
            raise ValueError("invalid_project_link_preview")
        fingerprint, target, _endpoint = _candidate(store, employee_id, project_id)
        return (
            {"state": "already_linked", "delivery_verified": False}
            if fingerprint is None
            else {
                "state": "confirmation_required",
                "fingerprint": fingerprint,
                "repair_required": target is not None,
                "delivery_verified": False,
            }
        )
    if (
        confirmed is not True
        or not isinstance(expected_fingerprint, str)
        or len(expected_fingerprint) != 64
    ):
        raise ValueError("project_link_confirmation_required")
    with state_lock(store.root):
        try:
            fingerprint, target, source_endpoint = _candidate(store, employee_id, project_id)
        except ValueError as exc:
            raise ValueError("configuration_changed") from exc
        if fingerprint is None:
            return {"state": "already_linked", "delivery_verified": False}
        if fingerprint != expected_fingerprint:
            raise ValueError("configuration_changed")
        if target is None:
            target = _set_binding_locked(
                store,
                employee_id,
                project_id,
                host_kind="hermes",
                host_session_id=next(
                    row["host_session_id"]
                    for row in load_bindings(store.root)
                    if row["id"] == source_endpoint["binding_id"]
                ),
                expected_binding_id=None,
            )
        path = store.root / "wake" / CONFIG
        rows = endpoints(store.root)
        row = {
            "binding_id": target["id"],
            "employee_id": employee_id,
            "host_kind": "hermes",
            "socket_path": source_endpoint["socket_path"],
        }
        held = publish_new_project_route_locked(
            store,
            target,
            lambda: _replace_private(
                path, json.dumps({"version": 1, "endpoints": [*rows, row]}).encode()
            ),
        )
    event = getattr(store, "mail_wake_event", None)
    if event is not None:
        event.set()
    return {
        "state": "linked_runtime_unverified",
        "delivery_verified": False,
        "old_alerts_held": held["held_count"],
        "unknown_alerts": held["unknown_count"],
    }
