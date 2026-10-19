"""Owner-only, read-only visibility into an interrupted Hermes cutover.

This view never clears a Gateway marker, releases old mail, restarts a host or
authorizes a pin write.  It distinguishes our durable fence from a stop marker
that may belong to an unrelated Hermes operator.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import time

from . import workbench_hermes_catalog as catalog
from . import workbench_hermes_journal as journal
from . import workbench_hermes_process as process
from .workbench_hermes_cutover import _exclusive_cutover
from .workbench_hermes_gateway_control import HermesGatewayControl
from .workbench_host_transport import endpoint_for
from .workbench_reauthorization_holds import (
    _private_json,
    active_fence_for_plan,
    completed_history,
    cutoff_path,
    gate_path,
    required_path,
)
from .workbench_receiver_status import _binding


def inspect(store, employee_id, project_id, profile_id):
    """Return a conservative state, never a recovery instruction or secret."""
    _python, profile = catalog.resolve(store, employee_id, project_id, profile_id)
    binding = _binding(store.root, employee_id, project_id, "hermes")
    if not binding:
        return {"state": "binding_required", "delivery_verified": False}
    binding_id = binding.get("id")
    if not isinstance(binding_id, str) or not re.fullmatch(r"binding_[0-9a-f]{32}", binding_id):
        return {"state": "check_unconfirmed", "delivery_verified": False}
    gate = gate_path(store.root, binding_id)
    marker = profile / ".gateway-planned-stop.json"
    try:
        try:
            marker_info = marker.lstat()
        except FileNotFoundError:
            marker_present = False
        else:
            if not stat.S_ISREG(marker_info.st_mode) or marker_info.st_uid != os.getuid():
                return {"state": "check_unconfirmed", "delivery_verified": False}
            marker_present = True

        if not gate.exists() and not gate.is_symlink():
            required = required_path(store.root, binding_id)
            cutoff_file = cutoff_path(store.root, binding_id)
            if not (
                required.exists()
                or required.is_symlink()
                or cutoff_file.exists()
                or cutoff_file.is_symlink()
            ):
                if journal.any_for_binding(store.root, binding_id):
                    return {"state": "check_unconfirmed", "delivery_verified": False}
                state = "external_stop_marker_present" if marker_present else "no_cutover_recovery"
                return {"state": state, "delivery_verified": False}
            if marker_present:
                return {"state": "check_unconfirmed", "delivery_verified": False}
            completed, latest, pending = completed_history(store.root, binding_id)
            if latest is None or pending is not None:
                return {"state": "check_unconfirmed", "delivery_verified": False}
            phases = journal.phases_for_binding(
                store,
                binding_id,
                employee_id=employee_id,
                project_id=project_id,
                profile_id=profile_id,
                profile=profile,
            )
            if set(phases) != completed:
                return {"state": "check_unconfirmed", "delivery_verified": False}
            state = (
                "authorization_cutoff_present"
                if all(phase == "complete" for phase in phases.values())
                else "route_released_journal_requires_review"
            )
            return {"state": state, "delivery_verified": False}

        record = _private_json(gate)
        plan_id = record.get("plan_id") if isinstance(record, dict) else None
        expected = {
            "version": 1,
            "plan_id": plan_id,
            "binding_id": binding_id,
            "employee_id": employee_id,
            "project_id": project_id,
        }
        if (
            not isinstance(plan_id, str)
            or not re.fullmatch(r"plan_[0-9a-f]{32}", plan_id)
            or record != expected
            or not active_fence_for_plan(store.root, binding_id, plan_id)
        ):
            return {"state": "check_unconfirmed", "delivery_verified": False}
        journal_file = journal.path(store.root, binding_id, plan_id)
        if journal_file.exists() or journal_file.is_symlink():
            evidence = journal.read(
                store,
                binding_id,
                plan_id,
                employee_id=employee_id,
                project_id=project_id,
                profile_id=profile_id,
                profile=profile,
            )
            if evidence["phase"] == "complete":
                return {"state": "check_unconfirmed", "delivery_verified": False}
            if evidence["phase"] == "restart_intent":
                return {
                    "state": "gateway_restart_outcome_requires_inspection",
                    "delivery_verified": False,
                }
            if evidence["phase"] == "restart_retry_intent":
                return {
                    "state": "gateway_restart_retry_outcome_requires_inspection",
                    "delivery_verified": False,
                }
            if evidence["phase"] in {"restart_verified", "restart_retry_verified"}:
                return {
                    "state": "old_gateway_restored_recorded_mail_fenced",
                    "current_host_verified": False,
                    "delivery_verified": False,
                }
            if marker_present:
                return {
                    "state": "stop_marker_requires_recovery",
                    "pin_may_have_run": evidence["pin_may_have_run"],
                    "delivery_verified": False,
                }
            if evidence["pin_may_have_run"]:
                return {"state": "pin_outcome_requires_inspection", "delivery_verified": False}
        state = "stop_marker_requires_recovery" if marker_present else "fence_requires_review"
        return {"state": state, "delivery_verified": False}
    except (OSError, ValueError, TypeError, KeyError):
        return {"state": "check_unconfirmed", "delivery_verified": False}


def restore_pre_pin(
    store,
    employee_id,
    project_id,
    plan_id,
    profile_id,
    *,
    confirmed=False,
    control_factory=HermesGatewayControl,
    route_seconds=20,
):
    """Once only, restore the original stopped Gateway before a pin write.

    The old-mail fence is never released here. An uncertain restart remains a
    durable inspection state; a second request cannot start another process.
    """
    catalog._member(store, employee_id, project_id)
    if confirmed is not True:
        return {"state": "confirmation_required", "delivery_verified": False}
    if not isinstance(plan_id, str) or not re.fullmatch(r"plan_[0-9a-f]{32}", plan_id):
        return {"state": "plan_unconfirmed", "delivery_verified": False}
    try:
        with _exclusive_cutover(store.root):
            return _restore_pre_pin(
                store,
                employee_id,
                project_id,
                plan_id,
                profile_id,
                control_factory=control_factory,
                route_seconds=route_seconds,
            )
    except BlockingIOError:
        return {"state": "cutover_in_progress", "delivery_verified": False}
    except (OSError, ValueError, TypeError, KeyError, TimeoutError):
        return {"state": "check_unconfirmed", "delivery_verified": False}


def _restore_pre_pin(
    store, employee_id, project_id, plan_id, profile_id, *, control_factory, route_seconds
):
    python, profile = catalog.resolve(store, employee_id, project_id, profile_id)
    binding = _binding(store.root, employee_id, project_id, "hermes")
    if not binding:
        return {"state": "binding_required", "delivery_verified": False}
    binding_id = binding.get("id")
    if not isinstance(binding_id, str) or not re.fullmatch(r"binding_[0-9a-f]{32}", binding_id):
        return {"state": "check_unconfirmed", "delivery_verified": False}
    endpoint = endpoint_for(store.root, binding)
    if not endpoint:
        return {"state": "check_unconfirmed", "delivery_verified": False}
    expected_gate = {
        "version": 1,
        "plan_id": plan_id,
        "binding_id": binding_id,
        "employee_id": employee_id,
        "project_id": project_id,
    }
    if _private_json(
        gate_path(store.root, binding_id)
    ) != expected_gate or not active_fence_for_plan(store.root, binding_id, plan_id):
        return {"state": "check_unconfirmed", "delivery_verified": False}
    scope = {
        "employee_id": employee_id,
        "project_id": project_id,
        "profile_id": profile_id,
        "profile": profile,
    }
    evidence = journal.read(store, binding_id, plan_id, **scope)
    if evidence["phase"] in {"restart_verified", "restart_retry_verified"}:
        return {
            "state": "old_gateway_restored_recorded_mail_fenced",
            "current_host_verified": False,
            "delivery_verified": False,
        }
    if evidence["phase"] in {"restart_intent", "restart_retry_intent"}:
        # Reconcile an interrupted verification without ever replaying start.
        try:
            live = control_factory(python, profile, require_stopped=False)
            ready = (
                live.domain == evidence["domain"]
                and live.plist_digest.hex() == evidence["plist_sha256"]
                and live.command_digest.hex() == evidence["command_sha256"]
                and live.restored_original_route(
                    evidence["domain"],
                    endpoint,
                    evidence["original_pid"],
                    evidence["original_start_time"],
                )
                and _pin_preview(python, profile, plan_id, binding_id)
                and _binding(store.root, employee_id, project_id, "hermes") == binding
                and endpoint_for(store.root, binding) == endpoint
                and _private_json(gate_path(store.root, binding_id)) == expected_gate
                and active_fence_for_plan(store.root, binding_id, plan_id)
            )
            if ready:
                next_phase = (
                    "restart_verified"
                    if evidence["phase"] == "restart_intent"
                    else "restart_retry_verified"
                )
                journal.advance(store, binding_id, plan_id, evidence["phase"], next_phase, **scope)
                return {"state": "old_gateway_restored_mail_fenced", "delivery_verified": False}
        except (OSError, ValueError, TypeError, KeyError, TimeoutError):
            pass
        state = (
            "gateway_restart_outcome_requires_inspection"
            if evidence["phase"] == "restart_intent"
            else "gateway_restart_retry_outcome_requires_inspection"
        )
        return {"state": state, "delivery_verified": False}
    if evidence["phase"] != "stop_intent":
        return {"state": "pin_outcome_requires_inspection", "delivery_verified": False}
    control = control_factory(python, profile, require_stopped=True)
    stopped = control.runtime()
    if (
        stopped.get("kind") != "hermes-gateway"
        or stopped.get("gateway_state") != "stopped"
        or stopped.get("hermes_home") != str(profile)
        or stopped.get("pid") != evidence["original_pid"]
        or stopped.get("start_time") != evidence["original_start_time"]
        or type(stopped.get("active_agents")) is not int
        or stopped["active_agents"] != 0
        or stopped.get("active_work") not in (None, [])
        or stopped.get("restart_requested") is not False
        or control.plist_digest.hex() != evidence["plist_sha256"]
        or control.command_digest.hex() != evidence["command_sha256"]
        or not control.original_pid_gone(evidence["original_pid"])
        or not control.service_stopped()
        or not control.receiver_socket_absent(endpoint)
        or not control.planned_stop_marker_absent()
        or not control.strict_gateway_absent()
    ):
        return {"state": "gateway_stop_unconfirmed", "delivery_verified": False}
    _marker, drain = control._drain_marker(plan_id)
    if drain is not None and drain.get("principal") != f"agent-mailbox-cutover:{plan_id}":
        return {"state": "other_drain_in_progress", "delivery_verified": False}
    if (
        not _pin_preview(python, profile, plan_id, binding_id)
        or _binding(store.root, employee_id, project_id, "hermes") != binding
        or endpoint_for(store.root, binding) != endpoint
        or _private_json(gate_path(store.root, binding_id)) != expected_gate
        or not active_fence_for_plan(store.root, binding_id, plan_id)
        or not control.service_stopped()
        or not control.strict_gateway_absent()
    ):
        return {"state": "gateway_restore_scope_changed", "delivery_verified": False}
    # An external clear/start may now happen. Persist this first; on crash,
    # recovery must inspect the outcome instead of issuing a second bootstrap.
    journal.advance(store, binding_id, plan_id, "stop_intent", "restart_intent", **scope)
    try:
        control.clear_drain(plan_id)
        control.start_stopped_original(
            evidence["domain"],
            plist_sha256=evidence["plist_sha256"],
            command_sha256=evidence["command_sha256"],
        )
        deadline = time.monotonic() + max(0, route_seconds)
        while True:
            if control.restored_original_route(
                evidence["domain"],
                endpoint,
                evidence["original_pid"],
                evidence["original_start_time"],
            ):
                journal.advance(
                    store, binding_id, plan_id, "restart_intent", "restart_verified", **scope
                )
                return {"state": "old_gateway_restored_mail_fenced", "delivery_verified": False}
            if time.monotonic() >= deadline:
                break
            time.sleep(0.2)
    except (OSError, ValueError, TypeError, KeyError, TimeoutError):
        pass
    return {"state": "gateway_restart_outcome_requires_inspection", "delivery_verified": False}


def retry_restore_pre_pin(
    store,
    employee_id,
    project_id,
    plan_id,
    profile_id,
    *,
    confirmed=False,
    control_factory=HermesGatewayControl,
    route_seconds=20,
):
    """One explicit owner retry after an uncertain first restart, never an automatic replay."""
    catalog._member(store, employee_id, project_id)
    if confirmed is not True:
        return {"state": "confirmation_required", "delivery_verified": False}
    if not isinstance(plan_id, str) or not re.fullmatch(r"plan_[0-9a-f]{32}", plan_id):
        return {"state": "plan_unconfirmed", "delivery_verified": False}
    try:
        with _exclusive_cutover(store.root):
            return _retry_restore_pre_pin(
                store,
                employee_id,
                project_id,
                plan_id,
                profile_id,
                control_factory=control_factory,
                route_seconds=route_seconds,
            )
    except BlockingIOError:
        return {"state": "cutover_in_progress", "delivery_verified": False}
    except (OSError, ValueError, TypeError, KeyError, TimeoutError):
        return {"state": "check_unconfirmed", "delivery_verified": False}


def _retry_restore_pre_pin(
    store, employee_id, project_id, plan_id, profile_id, *, control_factory, route_seconds
):
    python, profile = catalog.resolve(store, employee_id, project_id, profile_id)
    binding = _binding(store.root, employee_id, project_id, "hermes")
    if not binding:
        return {"state": "binding_required", "delivery_verified": False}
    binding_id = binding.get("id")
    if not isinstance(binding_id, str) or not re.fullmatch(r"binding_[0-9a-f]{32}", binding_id):
        return {"state": "check_unconfirmed", "delivery_verified": False}
    endpoint = endpoint_for(store.root, binding)
    if not endpoint:
        return {"state": "check_unconfirmed", "delivery_verified": False}
    expected_gate = {
        "version": 1,
        "plan_id": plan_id,
        "binding_id": binding_id,
        "employee_id": employee_id,
        "project_id": project_id,
    }
    if _private_json(
        gate_path(store.root, binding_id)
    ) != expected_gate or not active_fence_for_plan(store.root, binding_id, plan_id):
        return {"state": "check_unconfirmed", "delivery_verified": False}
    scope = {
        "employee_id": employee_id,
        "project_id": project_id,
        "profile_id": profile_id,
        "profile": profile,
    }
    evidence = journal.read(store, binding_id, plan_id, **scope)
    if evidence["phase"] == "restart_retry_verified":
        return {
            "state": "old_gateway_restored_recorded_mail_fenced",
            "current_host_verified": False,
            "delivery_verified": False,
        }
    if evidence["phase"] == "restart_retry_intent":
        return {
            "state": "gateway_restart_retry_outcome_requires_inspection",
            "delivery_verified": False,
        }
    if evidence["phase"] != "restart_intent":
        return {"state": "retry_plan_not_available", "delivery_verified": False}
    control = control_factory(python, profile, require_stopped=True)
    stopped = control.runtime()
    if (
        stopped.get("kind") != "hermes-gateway"
        or stopped.get("gateway_state") != "stopped"
        or stopped.get("hermes_home") != str(profile)
        or stopped.get("pid") != evidence["original_pid"]
        or stopped.get("start_time") != evidence["original_start_time"]
        or type(stopped.get("active_agents")) is not int
        or stopped["active_agents"] != 0
        or stopped.get("active_work") not in (None, [])
        or stopped.get("restart_requested") is not False
        or control.plist_digest.hex() != evidence["plist_sha256"]
        or control.command_digest.hex() != evidence["command_sha256"]
        or not control.original_pid_gone(evidence["original_pid"])
        or not control.service_stopped()
        or not control.receiver_socket_absent(endpoint)
        or not control.planned_stop_marker_absent()
        or not control.strict_gateway_absent()
    ):
        return {"state": "gateway_stop_unconfirmed", "delivery_verified": False}
    _marker, drain = control._drain_marker(plan_id)
    if drain is not None and drain.get("principal") != f"agent-mailbox-cutover:{plan_id}":
        return {"state": "other_drain_in_progress", "delivery_verified": False}
    if (
        not _pin_preview(python, profile, plan_id, binding_id)
        or _binding(store.root, employee_id, project_id, "hermes") != binding
        or endpoint_for(store.root, binding) != endpoint
        or _private_json(gate_path(store.root, binding_id)) != expected_gate
        or not active_fence_for_plan(store.root, binding_id, plan_id)
        or not control.service_stopped()
        or not control.strict_gateway_absent()
    ):
        return {"state": "gateway_restore_scope_changed", "delivery_verified": False}
    # Persist a distinct second-attempt intent before clear/start. A failure
    # here can be inspected or reconciled, but never replayed automatically.
    journal.advance(store, binding_id, plan_id, "restart_intent", "restart_retry_intent", **scope)
    try:
        control.clear_drain(plan_id)
        control.start_stopped_original(
            evidence["domain"],
            plist_sha256=evidence["plist_sha256"],
            command_sha256=evidence["command_sha256"],
        )
        deadline = time.monotonic() + max(0, route_seconds)
        while True:
            if (
                control.restored_original_route(
                    evidence["domain"],
                    endpoint,
                    evidence["original_pid"],
                    evidence["original_start_time"],
                )
                and _pin_preview(python, profile, plan_id, binding_id)
                and _binding(store.root, employee_id, project_id, "hermes") == binding
                and endpoint_for(store.root, binding) == endpoint
                and _private_json(gate_path(store.root, binding_id)) == expected_gate
                and active_fence_for_plan(store.root, binding_id, plan_id)
            ):
                journal.advance(
                    store,
                    binding_id,
                    plan_id,
                    "restart_retry_intent",
                    "restart_retry_verified",
                    **scope,
                )
                return {"state": "old_gateway_restored_mail_fenced", "delivery_verified": False}
            if time.monotonic() >= deadline:
                break
            time.sleep(0.2)
    except (OSError, ValueError, TypeError, KeyError, TimeoutError):
        pass
    return {
        "state": "gateway_restart_retry_outcome_requires_inspection",
        "delivery_verified": False,
    }


def _pin_preview(python, profile, plan_id, binding_id):
    source = process.payload()
    hashes = {
        name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in process.FILES
    }
    result = process.run(
        python,
        profile,
        {
            "version": 1,
            "operation": "pin_status",
            "profile": str(profile),
            "plan_id": plan_id,
            "expected_binding_id": binding_id,
        },
        source=source,
        expected_files=hashes,
        timeout=5,
    )
    return isinstance(result, dict) and result.get("state") == "preview"
