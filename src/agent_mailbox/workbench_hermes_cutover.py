"""Single-plan Hermes pin cutover with a durable old-mail fence.

The caller must be an authenticated local owner.  No live operation starts on
import or by preparing a plan.  A failed/uncertain step leaves old mail fenced;
it never retries an uncertain pin write or replays an unknown delivery.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows has no Unix Gateway control channel.
    fcntl = None

from . import workbench_hermes_catalog as catalog
from . import workbench_hermes_journal as journal
from . import workbench_hermes_process as process
from .workbench_hermes_gateway_control import (
    HermesGatewayControl,
    HermesStopAfterMarkerUnconfirmed,
)
from .workbench_hermes_gateway_gate import drained_idle, stopped_after_drain
from .workbench_hermes_reauthorization import arm_old_mail, release_new_route
from .workbench_host_transport import endpoint_for
from .workbench_reauthorization_holds import (
    _private_claims,
    completed_history,
    gated,
)
from .workbench_receiver_status import _binding


class _CutoverAbort(Exception):
    def __init__(self, state):
        self.state = state
        super().__init__(state)


@contextmanager
def _exclusive_cutover(home):
    """Serialize owner cutovers across App processes without blocking wake."""
    if fcntl is None or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("hermes_unix_cutover_required")
    path = _private_claims(home) / "hermes-cutover.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("hermes_cutover_lock_unsafe")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def execute(
    store,
    employee_id,
    project_id,
    plan_id,
    profile_id,
    *,
    confirmed=False,
    control_factory=HermesGatewayControl,
    drain_seconds=60,
    stop_seconds=10,
    route_seconds=20,
):
    catalog._member(store, employee_id, project_id)
    if confirmed is not True:
        return {"state": "confirmation_required", "delivery_verified": False}
    if not isinstance(plan_id, str) or not re.fullmatch(r"plan_[0-9a-f]{32}", plan_id):
        return {"state": "plan_unconfirmed", "delivery_verified": False}
    try:
        with _exclusive_cutover(store.root):
            return _execute(
                store,
                employee_id,
                project_id,
                plan_id,
                profile_id,
                confirmed=True,
                control_factory=control_factory,
                drain_seconds=drain_seconds,
                stop_seconds=stop_seconds,
                route_seconds=route_seconds,
            )
    except BlockingIOError:
        return {"state": "cutover_in_progress", "delivery_verified": False}
    except (OSError, ValueError, TypeError, KeyError):
        return {"state": "check_unconfirmed", "delivery_verified": False}


def _execute(
    store,
    employee_id,
    project_id,
    plan_id,
    profile_id,
    *,
    confirmed=False,
    control_factory=HermesGatewayControl,
    drain_seconds=60,
    stop_seconds=10,
    route_seconds=20,
):
    """Cut over one exact default-profile binding; never mark delivery verified."""
    catalog._member(store, employee_id, project_id)
    if confirmed is not True:
        return {"state": "confirmation_required", "delivery_verified": False}
    _root, python, discovery = catalog._discover()
    matches = (
        []
        if discovery is None
        else [entry for entry in discovery["profiles"] if entry["profile_id"] == profile_id]
    )
    if len(matches) != 1:
        return {"state": "profile_unconfirmed", "delivery_verified": False}
    profile = Path(matches[0]["home"])
    binding = _binding(store.root, employee_id, project_id, "hermes")
    endpoint = endpoint_for(store.root, binding) if binding else None
    if not endpoint:
        return {"state": "configuration_changed", "delivery_verified": False}
    # The Gateway and delivery fence belong to the binding, not the plan.
    # Completed journals are audit history, not a permanent prohibition.
    # Every prior completed journal must have a matching durable cutoff epoch;
    # journals are deliberately retained. A future retirement mechanism must
    # preserve a verifiable tombstone before this equality can change.
    # any unfinished/orphaned record refuses before constructing or draining.
    # The owner cutover lock covers this read and begin().
    try:
        phases = journal.phases_for_binding(
            store,
            binding["id"],
            employee_id=employee_id,
            project_id=project_id,
            profile_id=profile_id,
            profile=profile,
        )
        if any(phase != "complete" for phase in phases.values()):
            return {"state": "cutover_journal_requires_inspection", "delivery_verified": False}
        if gated(store.root, binding["id"]):
            return {"state": "fence_requires_recovery", "delivery_verified": False}
        completed, _cutoff, pending = completed_history(store.root, binding["id"])
        if pending is not None or set(phases) != completed:
            return {"state": "fence_requires_recovery", "delivery_verified": False}
    except (OSError, ValueError):
        return {"state": "cutover_journal_requires_inspection", "delivery_verified": False}
    control = control_factory(python, profile)

    drain_requested = False
    gate_armed = False
    stop_attempted = False
    stop_marker_uncertain = False
    stopped = False
    applied = False
    failure = "check_unconfirmed"
    try:
        # A bounded host-lifecycle wait, not a mailbox polling worker.  The
        # official Hermes watcher changes state only after receiving the drain.
        drain_requested = True
        control.request_drain(plan_id)
        deadline = time.monotonic() + drain_seconds
        while True:
            before = control.runtime()
            route = control.routing(endpoint)
            if drained_idle(before, route, pid_identity_confirmed=control.pid_identity(before)):
                break
            if time.monotonic() >= deadline:
                failure = "gateway_drain_not_idle"
                break
            time.sleep(0.25)
        if failure == "gateway_drain_not_idle":
            raise _CutoverAbort(failure)

        armed = arm_old_mail(store, employee_id, project_id, plan_id, profile_id, confirmed=True)
        if armed.get("state") != "old_pending_held":
            # The arm step may have written its first durable marker before
            # returning a refusal.  Treat every non-success as an uncertain
            # fence and inspect the private record before another attempt.
            failure = "fence_requires_recovery"
            gate_armed = True
            raise _CutoverAbort(failure)
        gate_armed = True
        if (
            _binding(store.root, employee_id, project_id, "hermes") != binding
            or endpoint_for(store.root, binding) != endpoint
        ):
            failure = "fence_requires_recovery"
            raise _CutoverAbort(failure)
        before = control.runtime()
        route = control.routing(endpoint)
        if not drained_idle(before, route, pid_identity_confirmed=control.pid_identity(before)):
            failure = "fence_requires_recovery"
            raise _CutoverAbort(failure)

        # This fsynced intent predates the official planned-stop marker. A
        # process death after that marker can be attributed to the exact old
        # Gateway identity without treating marker absence as proof of stop.
        journal.begin(store, binding, plan_id, profile_id, control, before)
        stop_command_ok = True
        stop_attempted = True
        try:
            control.stop()
            stop_marker_uncertain = True
        except HermesStopAfterMarkerUnconfirmed:
            # The running Gateway's watcher can act on this marker even when
            # bootout failed.  Do not clear drain or start a second instance.
            stop_marker_uncertain = True
            stop_command_ok = False
        except (OSError, ValueError, TypeError, TimeoutError):
            # The command may have timed out after a successful shutdown.
            # Gather independent evidence so finally can restore a proven
            # stopped host, but never apply a pin after an uncertain command.
            stop_command_ok = False
        # Hermes stop is asynchronous at the OS boundary.  Its CLI may return
        # before launchd/PID/socket and retained state converge; a single
        # immediate probe would strand a safely fenced but stopped Gateway.
        stop_deadline = time.monotonic() + stop_seconds
        while True:
            try:
                after = control.runtime()
                pid_gone = control.original_pid_gone(before["pid"])
                service_down = control.service_stopped()
                if pid_gone and service_down and not control.receiver_socket_absent(endpoint):
                    control.remove_stale_receiver_socket(
                        endpoint, original_pid_gone=True, service_stopped=True
                    )
                stopped = stopped_after_drain(
                    before,
                    route,
                    after,
                    pid_identity_confirmed=True,
                    original_pid_gone=pid_gone,
                    service_stopped=service_down,
                    receiver_socket_absent=control.receiver_socket_absent(endpoint),
                )
            except (OSError, ValueError, TypeError, KeyError, TimeoutError):
                stopped = False
            if stopped or time.monotonic() >= stop_deadline:
                break
            time.sleep(0.2)
        if not stopped or _binding(store.root, employee_id, project_id, "hermes") != binding:
            failure = "fence_requires_recovery"
            raise _CutoverAbort(failure)
        if not stop_command_ok:
            raise _CutoverAbort("stop_command_requires_inspection")
        if not control.planned_stop_marker_absent():
            raise _CutoverAbort("gateway_stop_marker_requires_recovery")
        stop_marker_uncertain = False

        source = process.payload()
        hashes = {
            name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in process.FILES
        }
        # If the process dies during the external pin writer, recovery must
        # assume it may have run and must never blindly replay it.
        journal.advance(
            store,
            binding["id"],
            plan_id,
            "stop_intent",
            "pin_intent",
            employee_id=employee_id,
            project_id=project_id,
            profile_id=profile_id,
            profile=profile,
        )
        result = process.run(
            python,
            profile,
            {
                "version": 1,
                "operation": "pin_apply",
                "profile": str(profile),
                "plan_id": plan_id,
                "expected_binding_id": binding["id"],
                "confirmed": True,
                "gateway_stopped": True,
            },
            source=source,
            expected_files=hashes,
        )
        applied = result.get("state") == "configured_runtime_unverified"
        if not applied:
            failure = "pin_apply_requires_inspection"
    except _CutoverAbort as exc:
        failure = exc.state
    except (OSError, ValueError, TypeError, KeyError, TimeoutError):
        failure = "fence_requires_recovery" if gate_armed else "check_unconfirmed"
    finally:
        clear_ok = True
        if drain_requested and not stop_marker_uncertain:
            try:
                control.clear_drain(plan_id)
            except (OSError, ValueError, TypeError):
                clear_ok = False
        restore_stopped = stopped
        if stop_attempted and not stopped and not stop_marker_uncertain:
            # A successful stop may have made the retained state unreadable.
            # This evidence is sufficient only to restore service, never to
            # authorize pin_apply or release the old-mail fence.
            try:
                pid_gone = control.original_pid_gone(before["pid"])
                service_down = control.service_stopped()
                if pid_gone and service_down and not control.receiver_socket_absent(endpoint):
                    control.remove_stale_receiver_socket(
                        endpoint, original_pid_gone=True, service_stopped=True
                    )
                restore_stopped = (
                    pid_gone and service_down and control.receiver_socket_absent(endpoint)
                )
            except (OSError, ValueError, TypeError, KeyError, TimeoutError):
                restore_stopped = False
        start_ok = False
        if restore_stopped and clear_ok and not stop_marker_uncertain:
            try:
                control.start()
                start_ok = True
            except (OSError, ValueError, TypeError, TimeoutError):
                pass

    if stop_marker_uncertain:
        return {"state": "gateway_stop_marker_requires_recovery", "delivery_verified": False}
    if (
        not clear_ok
        or (restore_stopped and not start_ok)
        or (stop_attempted and not restore_stopped)
    ):
        return {"state": "gateway_recovery_required", "delivery_verified": False}
    if not applied:
        return {"state": failure, "delivery_verified": False}
    # The official start command returns before the receiver socket has
    # necessarily registered its exact session route.  Keep the fence closed
    # until that route is observed, rather than reporting a false failure.
    route_deadline = time.monotonic() + route_seconds
    while True:
        released = release_new_route(
            store, employee_id, project_id, plan_id, profile_id, confirmed=True
        )
        if released.get("state") == "new_route_released":
            try:
                journal.advance(
                    store,
                    binding["id"],
                    plan_id,
                    "pin_intent",
                    "complete",
                    employee_id=employee_id,
                    project_id=project_id,
                    profile_id=profile_id,
                    profile=profile,
                )
            except (OSError, ValueError, TypeError):
                return {
                    "state": "route_released_journal_requires_review",
                    "old_mail_released": False,
                    "delivery_verified": False,
                }
            return {
                "state": "new_route_released",
                "old_mail_released": False,
                "delivery_verified": False,
            }
        if time.monotonic() >= route_deadline:
            return {"state": "fence_requires_recovery", "delivery_verified": False}
        time.sleep(0.25)
