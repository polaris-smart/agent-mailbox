"""First-contact diagnostic admission with a stopped-Gateway PM boundary.

This coordinator is deliberately not exposed by the Owner HTTP/UI yet. It
supports only a profile with no Hermes host binding: an existing receiver
requires the separate old-mail fence and route-cutover protocol. An uncertain
stop or PM call leaves its durable intent for explicit recovery, never retry.
"""

from __future__ import annotations

import subprocess
import time
from datetime import datetime, timezone

from . import workbench_hermes_admission as admission
from . import workbench_hermes_admission_journal as journal
from . import workbench_hermes_catalog as catalog
from . import workbench_hermes_installer as installer
from . import workbench_hermes_process as process
from .workbench_hermes_cutover import _exclusive_cutover
from .workbench_hermes_gateway_control import (
    HermesGatewayControl,
    HermesStopAfterMarkerUnconfirmed,
)
from .workbench_hermes_gateway_gate import (
    first_contact_drained_idle,
    stopped_after_first_contact,
)
from .workbench_host_bindings import load_bindings
from .workbench_store import WorkbenchError


def _result(state, *, runtime_verified=False):
    return {
        "state": state,
        "runtime_verified": runtime_verified,
        "session_verified": False,
        "delivery_verified": False,
    }


def _other_hermes_route(home):
    return any(item["host_kind"] == "hermes" for item in load_bindings(home))


def _running_verified(
    store, employee_id, project_id, record, profile, endpoint, evidence, control_factory
):
    """Reconcile a restart from independent current evidence, never start it."""
    selected, selected_profile = catalog.resolve(
        store, employee_id, project_id, record["profile_id"]
    )
    if selected_profile != profile or _other_hermes_route(store.root):
        return False
    control = control_factory(selected, profile)
    current = control.runtime()
    if (
        control.domain != evidence["domain"]
        or control.plist_digest.hex() != evidence["plist_sha256"]
        or control.command_digest.hex() != evidence["command_sha256"]
        or (current.get("pid"), current.get("start_time"))
        == (evidence["original_pid"], evidence["original_start_time"])
        or not control.pid_identity(current)
        or control.observe_diagnostic(endpoint)
        != {
            "state": "diagnostic_runtime_observed",
            "runtime_verified": True,
            "session_verified": False,
            "delivery_verified": False,
        }
    ):
        return False
    verified = admission.admission(
        store,
        employee_id,
        project_id,
        "verify",
        plan_id=record["id"],
        control_factory=control_factory,
    )
    return (
        verified["state"] == "diagnostic_runtime_observed"
        and verified["runtime_verified"] is True
        and not _other_hermes_route(store.root)
    )


def _original_gateway_verified(
    store, employee_id, project_id, record, profile, endpoint, evidence, control_factory
):
    """Prove the original service was restored without admitting the plugin."""
    python, selected_profile = catalog.resolve(store, employee_id, project_id, record["profile_id"])
    if (
        selected_profile != profile
        or str(python) != record["python"]
        or _other_hermes_route(store.root)
    ):
        return False
    control = control_factory(python, profile)
    runtime, live = control.live_status()
    pid, start = runtime.get("pid"), runtime.get("start_time")
    stamp = live.get("answered_at")
    now = datetime.now(timezone.utc)
    if (
        control.domain != evidence["domain"]
        or control.plist_digest.hex() != evidence["plist_sha256"]
        or control.command_digest.hex() != evidence["command_sha256"]
        or runtime.get("kind") != "hermes-gateway"
        or runtime.get("gateway_state") != "running"
        or runtime.get("hermes_home") != str(profile)
        or type(pid) is not int
        or pid <= 0
        or type(start) is not int
        or start <= 0
        or (pid, start) == (evidence["original_pid"], evidence["original_start_time"])
        or not control.pid_identity(runtime)
        or live.get("protocol") != 1
        or live.get("gateway_state") != "running"
        or live.get("answering_pid") != pid
        or live.get("pid") != pid
        or live.get("start_time") != start
        or live.get("hermes_home") != str(profile)
        or type(stamp) not in {int, float}
        or not 0 <= now.timestamp() - stamp <= 5
        or not control.receiver_socket_absent(endpoint)
    ):
        return False
    status = admission.admission(store, employee_id, project_id, "status", plan_id=record["id"])
    return status["state"] == "preview" and not _other_hermes_route(store.root)


def _stopped_identity_verified(control, profile, endpoint, evidence):
    stopped = control.runtime()
    return (
        stopped.get("kind") == "hermes-gateway"
        and stopped.get("gateway_state") == "stopped"
        and stopped.get("hermes_home") == str(profile)
        and stopped.get("pid") == evidence["original_pid"]
        and stopped.get("start_time") == evidence["original_start_time"]
        and type(stopped.get("active_agents")) is int
        and stopped["active_agents"] == 0
        and stopped.get("active_work") in (None, [])
        and stopped.get("restart_requested") is False
        and control.plist_digest.hex() == evidence["plist_sha256"]
        and control.command_digest.hex() == evidence["command_sha256"]
        and control.original_pid_gone(evidence["original_pid"])
        and control.service_stopped()
        and control.receiver_socket_absent(endpoint)
        and control.planned_stop_marker_absent()
        and control.strict_gateway_absent()
    )


def inspect_first_contact(store, employee_id, project_id, plan_id):
    """Read one durable first-contact phase; never infer current host health.

    A recorded phase identifies the last intent, not whether its external
    operation committed. In particular, inspecting an unknown PM or restart
    does not grant permission to repeat it.
    """
    try:
        catalog._member(store, employee_id, project_id)
        directory = installer._private(store.directory / "hermes-admission-plans")
        record = installer._record(directory, plan_id)
        if (
            record.get("version") != 1
            or record.get("employee_id") != employee_id
            or record.get("project_id") != project_id
        ):
            return _result("admission_scope_unconfirmed")
        _python, profile = catalog.resolve(store, employee_id, project_id, record.get("profile_id"))
        if record.get("profile") != str(profile):
            return _result("admission_scope_unconfirmed")
        scope = {
            "employee_id": employee_id,
            "project_id": project_id,
            "profile_id": record["profile_id"],
            "profile": profile,
        }
        other_unfinished = journal.unfinished_exists(store.root)
        try:
            evidence = journal.read(store, plan_id, **scope)
        except FileNotFoundError:
            return _result(
                "admission_journal_requires_inspection"
                if other_unfinished
                else "first_contact_not_started"
            )
        states = {
            "stop_intent": "stop_outcome_requires_inspection",
            "admission_intent": "plugin_outcome_requires_inspection",
            "restart_intent": "restart_outcome_requires_inspection",
            "complete": "recorded_complete_runtime_unconfirmed",
            "pre_admission_restore_intent": "pre_admission_restore_requires_inspection",
            "pre_admission_restored": "pre_admission_restored_recorded",
        }
        return {
            **_result(states[evidence["phase"]]),
            "phase": evidence["phase"],
            "plan_id": plan_id,
        }
    except (OSError, ValueError, TypeError, KeyError, WorkbenchError):
        return _result("admission_scope_unconfirmed")


def reconcile_first_contact(
    store, employee_id, project_id, plan_id, *, control_factory=HermesGatewayControl
):
    """Finish a verified restart intent without a second stop, PM, or start."""
    try:
        catalog._member(store, employee_id, project_id)
        installer._private(store.root / "wake", create=True)
        with _exclusive_cutover(store.root):
            record, _python, profile, _source, _pins = admission._record_for(
                store, employee_id, project_id, plan_id, preparation=False, allow_pm_reselect=True
            )
            scope = {
                "employee_id": employee_id,
                "project_id": project_id,
                "profile_id": record["profile_id"],
                "profile": profile,
            }
            other_unfinished = journal.unfinished_exists(store.root)
            try:
                evidence = journal.read(store, plan_id, **scope)
            except FileNotFoundError:
                return _result(
                    "admission_journal_requires_inspection"
                    if other_unfinished
                    else "first_contact_not_started"
                )
            if evidence["phase"] != "restart_intent":
                return _result("restart_intent_required")
            endpoint = admission._socket(store, profile)
            if not _running_verified(
                store, employee_id, project_id, record, profile, endpoint, evidence, control_factory
            ):
                return _result("restart_outcome_requires_inspection")
            journal.advance(store, plan_id, "restart_intent", "complete", **scope)
            return _result("diagnostic_runtime_observed", runtime_verified=True)
    except BlockingIOError:
        return _result("cutover_in_progress")
    except (OSError, ValueError, TypeError, KeyError, WorkbenchError, subprocess.TimeoutExpired):
        return _result("restart_outcome_requires_inspection")


def restore_before_admission(
    store,
    employee_id,
    project_id,
    plan_id,
    *,
    confirmed=False,
    control_factory=HermesGatewayControl,
    route_seconds=20,
):
    """Explicitly restore a stopped original Gateway before any PM mutation.

    This is not wired to Owner HTTP/UI. A second call can only reconcile an
    uncertain restart; it never starts the service twice or runs PM apply.
    """
    if confirmed is not True:
        return _result("confirmation_required")
    if type(route_seconds) not in {int, float} or not 0 <= route_seconds <= 120:
        return _result("cutover_bounds_unconfirmed")
    try:
        catalog._member(store, employee_id, project_id)
        installer._private(store.root / "wake", create=True)
        with _exclusive_cutover(store.root):
            record, python, profile, _source, _pins = admission._record_for(
                store, employee_id, project_id, plan_id, preparation=False
            )
            scope = {
                "employee_id": employee_id,
                "project_id": project_id,
                "profile_id": record["profile_id"],
                "profile": profile,
            }
            evidence = journal.read(store, plan_id, **scope)
            endpoint = admission._socket(store, profile)
            if evidence["phase"] == "pre_admission_restored":
                return _result("pre_admission_restored_recorded")
            if evidence["phase"] == "pre_admission_restore_intent":
                if _original_gateway_verified(
                    store,
                    employee_id,
                    project_id,
                    record,
                    profile,
                    endpoint,
                    evidence,
                    control_factory,
                ):
                    journal.advance(
                        store,
                        plan_id,
                        "pre_admission_restore_intent",
                        "pre_admission_restored",
                        **scope,
                    )
                    return {**_result("pre_admission_restored"), "gateway_running_verified": True}
                return _result("pre_admission_restore_requires_inspection")
            if evidence["phase"] != "stop_intent":
                return _result("stop_intent_required")
            if (
                _other_hermes_route(store.root)
                or admission.admission(store, employee_id, project_id, "status", plan_id=plan_id)[
                    "state"
                ]
                != "preview"
            ):
                return _result("pre_admission_restore_scope_changed")
            control = control_factory(python, profile, require_stopped=True)
            if not _stopped_identity_verified(control, profile, endpoint, evidence):
                return _result("gateway_stop_requires_inspection")
            # The external clear/start may have happened even if its response
            # is lost. Commit intent first and never issue a second start.
            journal.advance(store, plan_id, "stop_intent", "pre_admission_restore_intent", **scope)
            control.clear_drain(record["native_plan_id"])
            control_factory.restore_after_pm(
                profile,
                evidence["domain"],
                original_plist_sha256=evidence["plist_sha256"],
                original_command_sha256=evidence["command_sha256"],
            )
            deadline = time.monotonic() + route_seconds
            while True:
                try:
                    if _original_gateway_verified(
                        store,
                        employee_id,
                        project_id,
                        record,
                        profile,
                        endpoint,
                        evidence,
                        control_factory,
                    ):
                        journal.advance(
                            store,
                            plan_id,
                            "pre_admission_restore_intent",
                            "pre_admission_restored",
                            **scope,
                        )
                        return {
                            **_result("pre_admission_restored"),
                            "gateway_running_verified": True,
                        }
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    KeyError,
                    WorkbenchError,
                    subprocess.TimeoutExpired,
                ):
                    pass
                if time.monotonic() >= deadline:
                    return _result("pre_admission_restore_requires_inspection")
                time.sleep(0.2)
    except BlockingIOError:
        return _result("cutover_in_progress")
    except (OSError, ValueError, TypeError, KeyError, WorkbenchError, subprocess.TimeoutExpired):
        return _result("pre_admission_restore_requires_inspection")


def resume_committed_admission(
    store,
    employee_id,
    project_id,
    plan_id,
    *,
    confirmed=False,
    control_factory=HermesGatewayControl,
    route_seconds=20,
):
    """Restart once only when PM independently reports a committed admission.

    No PM write is repeated. An unknown/preview/changed PM result keeps the
    stopped host under inspection rather than guessing how to restore it.
    This branch is not exposed to Owner HTTP/UI yet.
    """
    if confirmed is not True:
        return _result("confirmation_required")
    if type(route_seconds) not in {int, float} or not 0 <= route_seconds <= 120:
        return _result("cutover_bounds_unconfirmed")
    restart_intent_written = False
    try:
        catalog._member(store, employee_id, project_id)
        installer._private(store.root / "wake", create=True)
        with _exclusive_cutover(store.root):
            record, python, profile, _source, _pins = admission._record_for(
                store, employee_id, project_id, plan_id, preparation=False, allow_pm_reselect=True
            )
            scope = {
                "employee_id": employee_id,
                "project_id": project_id,
                "profile_id": record["profile_id"],
                "profile": profile,
            }
            evidence = journal.read(store, plan_id, **scope)
            if evidence["phase"] != "admission_intent":
                return _result("admission_intent_required")
            endpoint = admission._socket(store, profile)
            if (
                _other_hermes_route(store.root)
                or admission.admission(store, employee_id, project_id, "status", plan_id=plan_id)[
                    "state"
                ]
                != "diagnostic_enabled_restart_required"
            ):
                return _result("plugin_outcome_requires_inspection")
            control = control_factory(python, profile, require_stopped=True)
            if not _stopped_identity_verified(control, profile, endpoint, evidence):
                return _result("gateway_stop_requires_inspection")
            # The original cutover cleared its own drain before PM apply.
            # PM has already committed. Record the restart attempt before
            # starting the original launchd job; do not replay either write.
            journal.advance(store, plan_id, "admission_intent", "restart_intent", **scope)
            restart_intent_written = True
            control_factory.restore_after_pm(
                profile,
                evidence["domain"],
                original_plist_sha256=evidence["plist_sha256"],
                original_command_sha256=evidence["command_sha256"],
            )
            deadline = time.monotonic() + route_seconds
            while True:
                try:
                    if _running_verified(
                        store,
                        employee_id,
                        project_id,
                        record,
                        profile,
                        endpoint,
                        evidence,
                        control_factory,
                    ):
                        journal.advance(store, plan_id, "restart_intent", "complete", **scope)
                        return _result("diagnostic_runtime_observed", runtime_verified=True)
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    KeyError,
                    WorkbenchError,
                    subprocess.TimeoutExpired,
                ):
                    pass
                if time.monotonic() >= deadline:
                    return _result("restart_outcome_requires_inspection")
                time.sleep(0.2)
    except BlockingIOError:
        return _result("cutover_in_progress")
    except (OSError, ValueError, TypeError, KeyError, WorkbenchError, subprocess.TimeoutExpired):
        return _result(
            "restart_outcome_requires_inspection"
            if restart_intent_written
            else "plugin_outcome_requires_inspection"
        )


def execute_first_contact(
    store,
    employee_id,
    project_id,
    plan_id,
    *,
    confirmed=False,
    control_factory=HermesGatewayControl,
    drain_seconds=60,
    stop_seconds=15,
    route_seconds=20,
):
    """Run one exact confirmed plan; no existing binding or mail is cut over."""
    if confirmed is not True:
        return _result("confirmation_required")
    if any(
        type(value) not in {int, float} or not 0 <= value <= 120
        for value in (drain_seconds, stop_seconds, route_seconds)
    ):
        return _result("cutover_bounds_unconfirmed")
    try:
        catalog._member(store, employee_id, project_id)
        installer._private(store.root / "wake", create=True)
        with _exclusive_cutover(store.root):
            return _execute_locked(
                store,
                employee_id,
                project_id,
                plan_id,
                control_factory=control_factory,
                drain_seconds=drain_seconds,
                stop_seconds=stop_seconds,
                route_seconds=route_seconds,
            )
    except BlockingIOError:
        return _result("cutover_in_progress")
    except (OSError, ValueError, TypeError, KeyError, WorkbenchError):
        return _result("admission_scope_unconfirmed")


def _execute_locked(
    store,
    employee_id,
    project_id,
    plan_id,
    *,
    control_factory,
    drain_seconds,
    stop_seconds,
    route_seconds,
):
    record, python, profile, source, pins = admission._record_for(
        store, employee_id, project_id, plan_id, preparation=False
    )
    prepared, *_ = admission._record_for(
        store, employee_id, project_id, record["preparation_id"], preparation=True
    )
    if prepared["profile"] != record["profile"]:
        return _result("admission_preparation_changed")
    if journal.unfinished_exists(store.root) or journal.reserved_plan(store.root, plan_id):
        return _result("admission_journal_requires_inspection")
    # A first-contact diagnostic has no legacy route to fence. Never silence
    # another project/member's Hermes binding while changing this Gateway.
    if _other_hermes_route(store.root):
        return _result("existing_hermes_route_requires_cutover")
    prior = admission.admission(
        store, employee_id, project_id, "lookup", profile_id=record["profile_id"]
    )
    if prior["state"] != "preview" or prior.get("plan_id") != plan_id:
        return _result("admission_plan_changed")
    endpoint = admission._socket(store, profile)
    control = control_factory(python, profile)
    if not control.receiver_socket_absent(endpoint):
        return _result("diagnostic_route_preexisting")

    requested = False
    intent_written = False
    stop_attempted = False
    stopped = False
    drain_cleared = False
    before = live = observed_at = None
    try:
        requested = True
        control.request_drain(record["native_plan_id"])
        deadline = time.monotonic() + drain_seconds
        while True:
            before, live = control.live_status()
            observed_at = datetime.now(timezone.utc)
            if first_contact_drained_idle(
                before, live, pid_identity_confirmed=control.pid_identity(before), now=observed_at
            ):
                break
            if time.monotonic() >= deadline:
                return _result("gateway_drain_not_idle")
            time.sleep(0.25)
        # Re-read immediately before committing the stop intent. A drain
        # state can age or change while prior checks and journal scans run.
        before, live = control.live_status()
        observed_at = datetime.now(timezone.utc)
        if not first_contact_drained_idle(
            before, live, pid_identity_confirmed=control.pid_identity(before), now=observed_at
        ):
            return _result("gateway_drain_changed")
        if _other_hermes_route(store.root):
            return _result("existing_hermes_route_requires_cutover")
        journal.begin(
            store, plan_id, employee_id, project_id, record["profile_id"], control, before
        )
        intent_written = True
        stop_attempted = True
        try:
            control.stop()
        except (HermesStopAfterMarkerUnconfirmed, subprocess.TimeoutExpired):
            return _result("gateway_stop_marker_requires_recovery")
        deadline = time.monotonic() + stop_seconds
        while True:
            after = control.runtime()
            stopped = stopped_after_first_contact(
                before,
                live,
                after,
                observed_at=observed_at,
                pid_identity_confirmed=True,
                original_pid_gone=control.original_pid_gone(before["pid"]),
                service_stopped=control.service_stopped(),
                receiver_socket_absent=control.receiver_socket_absent(endpoint),
            )
            if stopped or time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        if not stopped or not control.planned_stop_marker_absent():
            return _result("gateway_stop_requires_inspection")
        control.clear_drain(record["native_plan_id"])
        drain_cleared = True
        if (
            not control.service_stopped()
            or not control.original_pid_gone(before["pid"])
            or not control.receiver_socket_absent(endpoint)
            or not control.strict_gateway_absent()
        ):
            return _result("gateway_stop_requires_inspection")
        if _other_hermes_route(store.root):
            return _result("configuration_changed_after_stop")

        scope = {
            "employee_id": employee_id,
            "project_id": project_id,
            "profile_id": record["profile_id"],
            "profile": profile,
        }
        journal.advance(store, plan_id, "stop_intent", "admission_intent", **scope)
        applied = process.run(
            python,
            profile,
            {
                "version": 1,
                "operation": "diagnostic_admission_apply",
                "profile": str(profile),
                "plan_id": record["native_plan_id"],
                "confirmed": True,
                "gateway_stopped": True,
            },
            source=source,
            expected_files=pins,
            timeout=120,
        )
        if applied.get("state") != "diagnostic_enabled_restart_required":
            return _result("admission_requires_inspection")
        # PM may have committed a different Python. Never use the pre-PM
        # interpreter to bootstrap the original launchd service.
        journal.advance(store, plan_id, "admission_intent", "restart_intent", **scope)
        control_factory.restore_after_pm(
            profile,
            control.domain,
            original_plist_sha256=control.plist_digest.hex(),
            original_command_sha256=control.command_digest.hex(),
        )
        evidence = journal.read(store, plan_id, **scope)
        deadline = time.monotonic() + route_seconds
        while True:
            try:
                if _running_verified(
                    store,
                    employee_id,
                    project_id,
                    record,
                    profile,
                    endpoint,
                    evidence,
                    control_factory,
                ):
                    journal.advance(store, plan_id, "restart_intent", "complete", **scope)
                    return _result("diagnostic_runtime_observed", runtime_verified=True)
            except (OSError, ValueError, TypeError, KeyError, WorkbenchError):
                pass
            if time.monotonic() >= deadline:
                return _result("diagnostic_runtime_unconfirmed")
            time.sleep(0.2)
    except (OSError, ValueError, TypeError, KeyError, WorkbenchError, subprocess.TimeoutExpired):
        return _result(
            "admission_requires_inspection" if intent_written else "admission_scope_unconfirmed"
        )
    finally:
        # Before a stop attempt, only our own drain marker may be removed.
        # Once a stop marker may exist, automatic cleanup could race Hermes's
        # watcher. A durable unfinished intent instead demands recovery.
        if requested and not stop_attempted and not drain_cleared:
            try:
                control.clear_drain(record["native_plan_id"])
            except (OSError, ValueError, TypeError):
                pass
