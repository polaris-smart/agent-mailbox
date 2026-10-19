"""Owner-only preparation of a changed Hermes session pin; no host activation."""

import hashlib
import re
import time
from pathlib import Path

from . import workbench_hermes_catalog as catalog
from . import workbench_hermes_process as process
from .workbench_receiver_status import (
    _binding,
    _request,
    hermes_reauthorization_preview,
    reception_status,
)


def prepare(store, employee_id, project_id):
    """Create a private stopped-Gateway plan after a read-only live route check."""
    try:
        return _prepare(store, employee_id, project_id)
    except (OSError, ValueError, TypeError, KeyError):
        return {"state": "check_unconfirmed", "candidate_available": False}


def _prepare(store, employee_id, project_id):
    candidate = hermes_reauthorization_preview(store, employee_id, project_id)
    if candidate["state"] != "candidate_ready":
        return candidate
    binding = _binding(store.root, employee_id, project_id, "hermes")
    from .workbench_host_transport import endpoint_for

    endpoint = endpoint_for(store.root, binding) if binding else None
    if not endpoint or binding["id"] != candidate["binding_id"]:
        return {"state": "configuration_changed", "candidate_available": False}
    routing = _request(endpoint, {"operation": "routing_context"})
    if (
        not isinstance(routing, dict)
        or routing.get("protocol") != "agent-mailbox.hermes-routing.v1"
        or routing.get("host_kind") != "hermes"
        or not isinstance(routing.get("instance_id"), str)
        or not routing["instance_id"]
        or not isinstance(routing.get("profile_home"), str)
    ):
        return {"state": "check_unconfirmed", "candidate_available": False}
    _root, python, discovery = catalog._discover()
    matches = (
        []
        if discovery is None
        else [entry for entry in discovery["profiles"] if entry["home"] == routing["profile_home"]]
    )
    if len(matches) != 1:
        return {"state": "profile_unconfirmed", "candidate_available": False}
    source = process.payload()
    hashes = {
        name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in process.FILES
    }
    resolved = _request(
        endpoint,
        {
            "operation": "reauthorization_resolve",
            "binding": binding,
            "expected_fingerprint": candidate["candidate_fingerprint"],
        },
    )
    if (
        not isinstance(resolved, dict)
        or set(resolved)
        != {
            "protocol",
            "host_kind",
            "binding_id",
            "candidate_fingerprint",
            "candidate_session_id",
            "platform",
            "route_available",
            "session_verified",
            "delivery_verified",
        }
        or resolved["protocol"] != "agent-mailbox.host-reauthorization.v1"
        or resolved["host_kind"] != "hermes"
        or resolved["binding_id"] != binding["id"]
        or resolved["candidate_fingerprint"] != candidate["candidate_fingerprint"]
        or resolved["platform"] != candidate["platform"]
        or not isinstance(resolved["candidate_session_id"], str)
        or not resolved["candidate_session_id"]
        or resolved["route_available"] is not False
        or resolved["session_verified"] is not False
        or resolved["delivery_verified"] is not False
    ):
        return {"state": "configuration_changed", "candidate_available": False}
    plan = process.run(
        python,
        Path(matches[0]["home"]),
        {
            "version": 1,
            "operation": "pin_preview",
            "profile": matches[0]["home"],
            "options": {
                "binding_id": binding["id"],
                "session_key": binding["host_session_id"],
                "candidate_fingerprint": candidate["candidate_fingerprint"],
                "candidate_session_id": resolved["candidate_session_id"],
            },
        },
        source=source,
        expected_files=hashes,
    )
    fresh = hermes_reauthorization_preview(store, employee_id, project_id)
    after = _request(endpoint, {"operation": "routing_context"})
    stable = (
        "instance_id",
        "pid",
        "profile_home",
        "routing_home",
        "scope",
        "gateway_profile",
        "multiplex",
    )
    if (
        fresh.get("candidate_fingerprint") != candidate["candidate_fingerprint"]
        or fresh.get("binding_id") != binding["id"]
        or not isinstance(after, dict)
        or any(after.get(field) != routing.get(field) for field in stable)
    ):
        return {"state": "configuration_changed", "candidate_available": False}
    return {
        "state": "confirmation_required",
        "plan_id": plan["plan_id"],
        "profile_id": matches[0]["profile_id"],
        "pending_count": fresh["pending_count"],
        "unknown_count": fresh["unknown_count"],
        "requires_stopped_gateway": True,
        "session_verified": False,
        "delivery_verified": False,
    }


def arm_old_mail(store, employee_id, project_id, plan_id, profile_id, *, confirmed=False):
    """Fence an exact live candidate before any stopped-Gateway pin change."""
    catalog._member(store, employee_id, project_id)
    if confirmed is not True:
        return {"state": "confirmation_required", "delivery_verified": False}
    try:
        return _arm_old_mail(store, employee_id, project_id, plan_id, profile_id)
    except (OSError, ValueError, TypeError, KeyError):
        from .workbench_reauthorization_holds import reauthorization_required

        binding = _binding(store.root, employee_id, project_id, "hermes")
        if binding and reauthorization_required(store.root, binding["id"]):
            return {"state": "fence_requires_recovery", "delivery_verified": False}
        return {"state": "check_unconfirmed", "delivery_verified": False}


def _arm_old_mail(store, employee_id, project_id, plan_id, profile_id):
    from .workbench_host_transport import endpoint_for
    from .workbench_reauthorization_holds import arm_binding_gate, hold_prior_pending

    if (
        not isinstance(plan_id, str)
        or not re.fullmatch(r"plan_[0-9a-f]{32}", plan_id)
        or not isinstance(profile_id, str)
    ):
        raise ValueError("invalid_reauthorization_plan")
    binding = _binding(store.root, employee_id, project_id, "hermes")
    endpoint = endpoint_for(store.root, binding) if binding else None
    _root, python, discovery = catalog._discover()
    matches = (
        []
        if discovery is None
        else [entry for entry in discovery["profiles"] if entry["profile_id"] == profile_id]
    )
    if not endpoint or len(matches) != 1:
        return {"state": "profile_unconfirmed", "delivery_verified": False}
    profile = Path(matches[0]["home"])
    candidate = hermes_reauthorization_preview(store, employee_id, project_id)
    if candidate.get("state") != "candidate_ready" or candidate.get("binding_id") != binding["id"]:
        return {"state": "check_unconfirmed", "delivery_verified": False}
    before = _request(endpoint, {"operation": "routing_context"})
    if (
        not isinstance(before, dict)
        or before.get("protocol") != "agent-mailbox.hermes-routing.v1"
        or before.get("host_kind") != "hermes"
        or before.get("profile_home") != str(profile)
        or before.get("routing_home") != str(profile)
        or type(before.get("pid")) is not int
        or before["pid"] <= 0
        or not isinstance(before.get("instance_id"), str)
        or not before["instance_id"]
    ):
        return {"state": "check_unconfirmed", "delivery_verified": False}
    source = process.payload()
    hashes = {
        name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in process.FILES
    }
    status = process.run(
        python,
        profile,
        {
            "version": 1,
            "operation": "pin_status",
            "profile": str(profile),
            "plan_id": plan_id,
            "expected_binding_id": binding["id"],
        },
        source=source,
        expected_files=hashes,
        timeout=5,
    )
    if (
        status.get("state") != "preview"
        or status.get("candidate_fingerprint") != candidate["candidate_fingerprint"]
        or type(status.get("expires_at")) not in {int, float}
        or status["expires_at"] <= time.time() + 60
    ):
        return {"state": "check_unconfirmed", "delivery_verified": False}
    fresh = hermes_reauthorization_preview(store, employee_id, project_id)
    after = _request(endpoint, {"operation": "routing_context"})
    stable = (
        "instance_id",
        "pid",
        "profile_home",
        "routing_home",
        "scope",
        "gateway_profile",
        "multiplex",
    )
    if (
        fresh.get("state") != "candidate_ready"
        or fresh.get("binding_id") != binding["id"]
        or fresh.get("candidate_fingerprint") != candidate["candidate_fingerprint"]
        or _binding(store.root, employee_id, project_id, "hermes") != binding
        or endpoint_for(store.root, binding) != endpoint
        or not isinstance(after, dict)
        or any(after.get(field) != before.get(field) for field in stable)
    ):
        return {"state": "check_unconfirmed", "delivery_verified": False}
    arm_binding_gate(store, binding, plan_id)
    return hold_prior_pending(store, binding, plan_id)


def release_new_route(store, employee_id, project_id, plan_id, profile_id, *, confirmed=False):
    """Expose only post-cutover mail after the exact stopped-host plan is live."""
    catalog._member(store, employee_id, project_id)
    if confirmed is not True:
        return {"state": "confirmation_required", "delivery_verified": False}
    try:
        return _release_new_route(store, employee_id, project_id, plan_id, profile_id)
    except (OSError, ValueError, TypeError, KeyError):
        return {"state": "check_unconfirmed", "delivery_verified": False}


def _release_new_route(store, employee_id, project_id, plan_id, profile_id):
    from .workbench_host_transport import endpoint_for
    from .workbench_reauthorization_holds import release_binding_gate

    if (
        not isinstance(plan_id, str)
        or not re.fullmatch(r"plan_[0-9a-f]{32}", plan_id)
        or not isinstance(profile_id, str)
    ):
        raise ValueError("invalid_reauthorization_plan")
    binding = _binding(store.root, employee_id, project_id, "hermes")
    endpoint = endpoint_for(store.root, binding) if binding else None
    _root, python, discovery = catalog._discover()
    matches = (
        []
        if discovery is None
        else [entry for entry in discovery["profiles"] if entry["profile_id"] == profile_id]
    )
    if not endpoint or len(matches) != 1:
        return {"state": "profile_unconfirmed", "delivery_verified": False}
    profile = Path(matches[0]["home"])
    source = process.payload()
    hashes = {
        name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in process.FILES
    }

    def route_ready():
        if _binding(store.root, employee_id, project_id, "hermes") != binding:
            return False
        before = _request(endpoint, {"operation": "routing_context"})
        if (
            not isinstance(before, dict)
            or before.get("protocol") != "agent-mailbox.hermes-routing.v1"
            or before.get("host_kind") != "hermes"
            or before.get("profile_home") != str(profile)
            or not isinstance(before.get("instance_id"), str)
            or not before["instance_id"]
        ):
            return False
        status = process.run(
            python,
            profile,
            {
                "version": 1,
                "operation": "pin_status",
                "profile": str(profile),
                "plan_id": plan_id,
                "expected_binding_id": binding["id"],
            },
            source=source,
            expected_files=hashes,
            timeout=5,
        )
        if status.get("state") != "configured_runtime_unverified":
            return False
        health = reception_status(store, employee_id, project_id)
        after = _request(endpoint, {"operation": "routing_context"})
        stable = (
            "instance_id",
            "pid",
            "profile_home",
            "routing_home",
            "scope",
            "gateway_profile",
            "multiplex",
        )
        return (
            health.get("state") == "component_available"
            and isinstance(after, dict)
            and all(after.get(field) == before.get(field) for field in stable)
        )

    return release_binding_gate(store, binding, plan_id, route_ready=route_ready)
