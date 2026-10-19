"""Read-only evidence gate before a Hermes Gateway session-pin change.

This module never stops a Gateway.  A caller must use the official external
drain control, obtain a fresh routing_context from the *same* live instance,
and independently prove the PID's start-time identity.  Missing evidence is
not an idle Gateway.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def drained_idle(runtime, routing, *, pid_identity_confirmed=False, now=None):
    """Return True only for a fresh, matching, drained and idle live Gateway."""
    if not isinstance(runtime, dict) or not isinstance(routing, dict):
        return False
    if pid_identity_confirmed is not True:
        return False
    if (
        runtime.get("kind") != "hermes-gateway"
        or runtime.get("gateway_state") != "draining"
        or routing.get("protocol") != "agent-mailbox.hermes-routing.v1"
        or routing.get("host_kind") != "hermes"
    ):
        return False
    pid = runtime.get("pid")
    if (
        type(pid) is not int
        or pid <= 0
        or type(routing.get("pid")) is not int
        or pid != routing["pid"]
    ):
        return False
    if (
        type(runtime.get("start_time")) is not int
        or runtime["start_time"] <= 0
        or not isinstance(routing.get("instance_id"), str)
        or not routing["instance_id"]
    ):
        return False
    home = runtime.get("hermes_home")
    route_home = routing.get("routing_home")
    if (
        not isinstance(home, str)
        or not isinstance(route_home, str)
        or not Path(home).is_absolute()
        or home != route_home
    ):
        return False
    if runtime.get("active_agents") != 0 or type(runtime.get("active_agents")) is not int:
        return False
    if runtime.get("active_work") not in (None, []):
        return False
    stamp = runtime.get("updated_at")
    if not isinstance(stamp, str):
        return False
    try:
        updated = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        current = now or datetime.now(timezone.utc)
        if updated.tzinfo is None or current.tzinfo is None:
            return False
        age = (current - updated).total_seconds()
    except (ValueError, TypeError, OverflowError):
        return False
    return 0 <= age <= 120


def first_contact_drained_idle(runtime, live_status, *, pid_identity_confirmed=False, now=None):
    """Gate a plugin-free first contact with a live official status response.

    The diagnostic socket cannot be required before its plugin is admitted.
    This substitutes the Gateway's own control-socket answer, never a retained
    JSON snapshot or an unverified caller-supplied ``gateway_stopped`` flag.
    """
    if (
        not isinstance(runtime, dict)
        or not isinstance(live_status, dict)
        or pid_identity_confirmed is not True
    ):
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return False
    pid, start, home = (runtime.get(key) for key in ("pid", "start_time", "hermes_home"))
    if (
        runtime.get("kind") != "hermes-gateway"
        or runtime.get("gateway_state") != "draining"
        or type(pid) is not int
        or pid <= 0
        or type(start) is not int
        or start <= 0
        or not isinstance(home, str)
        or not Path(home).is_absolute()
        or live_status.get("protocol") != 1
        or live_status.get("kind") != "hermes-gateway"
        or live_status.get("gateway_state") != "draining"
        or live_status.get("hermes_home") != home
        or live_status.get("pid") != pid
        or live_status.get("start_time") != start
        or live_status.get("answering_pid") != pid
        or type(live_status.get("active_agents")) is not int
        or live_status["active_agents"] != 0
        or live_status.get("active_work") not in (None, [])
        or type(runtime.get("active_agents")) is not int
        or runtime["active_agents"] != 0
        or runtime.get("active_work") not in (None, [])
    ):
        return False
    answered_at = live_status.get("answered_at")
    if type(answered_at) not in (int, float):
        return False
    try:
        updated = datetime.fromisoformat(live_status["updated_at"].replace("Z", "+00:00"))
        age = (current - updated).total_seconds()
        response_age = current.timestamp() - answered_at
    except (KeyError, AttributeError, ValueError, TypeError, OverflowError):
        return False
    return updated.tzinfo is not None and 0 <= age <= 120 and 0 <= response_age <= 5


def stopped_after_drain(
    before,
    routing,
    after,
    *,
    pid_identity_confirmed=False,
    original_pid_gone=False,
    service_stopped=False,
    receiver_socket_absent=False,
    now=None,
):
    """Join one fresh drained instance to independent post-stop observations.

    The caller must obtain OS process identity, official service state and socket
    evidence independently; a retained JSON file alone never proves shutdown.
    """
    if (
        not drained_idle(before, routing, pid_identity_confirmed=pid_identity_confirmed, now=now)
        or original_pid_gone is not True
        or service_stopped is not True
        or receiver_socket_absent is not True
        or not isinstance(after, dict)
    ):
        return False
    if (
        after.get("kind") != "hermes-gateway"
        or after.get("gateway_state") != "stopped"
        or after.get("pid") != before["pid"]
        or after.get("start_time") != before["start_time"]
        or after.get("hermes_home") != before["hermes_home"]
        or type(after.get("active_agents")) is not int
        or after["active_agents"] != 0
        or after.get("active_work") not in (None, [])
        or after.get("restart_requested") is not False
    ):
        return False
    try:
        earlier = datetime.fromisoformat(before["updated_at"].replace("Z", "+00:00"))
        later = datetime.fromisoformat(after["updated_at"].replace("Z", "+00:00"))
        current = now or datetime.now(timezone.utc)
        if earlier.tzinfo is None or later.tzinfo is None or current.tzinfo is None:
            return False
        return earlier < later and 0 <= (current - later).total_seconds() <= 120
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return False


def stopped_after_first_contact(
    before,
    live_status,
    after,
    *,
    observed_at,
    pid_identity_confirmed=False,
    original_pid_gone=False,
    service_stopped=False,
    receiver_socket_absent=False,
    now=None,
):
    """Prove first-contact stop without requiring a pre-admission plugin socket.

    ``observed_at`` is when the caller obtained the official control-socket
    response. Rechecking its five-second freshness after launchd has stopped
    would incorrectly reject an otherwise proven shutdown. The post-stop
    evidence must still be fresh, match the original PID and come after that
    live answer. This predicate does not stop or start a host.
    """
    current = now or datetime.now(timezone.utc)
    if (
        not isinstance(observed_at, datetime)
        or observed_at.tzinfo is None
        or current.tzinfo is None
    ):
        return False
    try:
        elapsed = (current - observed_at).total_seconds()
    except (TypeError, OverflowError):
        return False
    if (
        not 0 <= elapsed <= 120
        or not first_contact_drained_idle(
            before, live_status, pid_identity_confirmed=pid_identity_confirmed, now=observed_at
        )
        or original_pid_gone is not True
        or service_stopped is not True
        or receiver_socket_absent is not True
        or not isinstance(after, dict)
    ):
        return False
    if (
        after.get("kind") != "hermes-gateway"
        or after.get("gateway_state") != "stopped"
        or after.get("pid") != before["pid"]
        or after.get("start_time") != before["start_time"]
        or after.get("hermes_home") != before["hermes_home"]
        or type(after.get("active_agents")) is not int
        or after["active_agents"] != 0
        or after.get("active_work") not in (None, [])
        or after.get("restart_requested") is not False
    ):
        return False
    try:
        earlier = datetime.fromisoformat(before["updated_at"].replace("Z", "+00:00"))
        later = datetime.fromisoformat(after["updated_at"].replace("Z", "+00:00"))
        if earlier.tzinfo is None or later.tzinfo is None:
            return False
        return (
            earlier < later
            and observed_at <= later
            and 0 <= (current - later).total_seconds() <= 120
        )
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return False
