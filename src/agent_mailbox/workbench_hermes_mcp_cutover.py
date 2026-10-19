"""Owner-only migration of an existing Hermes mailbox MCP executable.

The bridge's ``gateway_stopped`` input is not evidence.  This coordinator
establishes that evidence independently before allowing a config mutation.
It does not restart Hermes or claim that the original session loaded tools.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from . import workbench_hermes_catalog as catalog
from . import workbench_hermes_process as process
from .workbench_hermes_cutover import _exclusive_cutover
from .workbench_hermes_gateway_control import HermesGatewayControl
from .workbench_host_transport import endpoint_for
from .workbench_receiver_status import _binding
from .workbench_store import WorkbenchError

_ACTIONS = {
    "preview": "mcp_command_preview",
    "apply": "mcp_command_apply",
    "status": "mcp_command_status",
    "rollback": "mcp_command_rollback",
}


def _installed_command():
    app = Path("/Applications/Agent Mailbox.app")
    binary = app / "Contents/MacOS/Agent Mailbox"
    process._file(binary, executable=True)
    try:
        check = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("installed_mailbox_signature_unconfirmed") from None
    if check.returncode != 0:
        raise ValueError("installed_mailbox_signature_unconfirmed")
    return binary


def _stopped(control, endpoint, profile):
    """Require independent service, recorded PID and receiver-socket evidence."""
    try:
        previous = control.runtime()
        pid = previous["pid"]
        return (
            previous.get("kind") == "hermes-gateway"
            and previous.get("gateway_state") == "stopped"
            and previous.get("hermes_home") == str(profile)
            and type(pid) is int
            and pid > 0
            and control.service_stopped()
            and control.original_pid_gone(pid)
            and control.receiver_socket_absent(endpoint)
        )
    except (OSError, ValueError, TypeError, KeyError, TimeoutError):
        return False


def operation(
    store,
    employee_id,
    project_id,
    profile_id,
    action,
    *,
    plan_id=None,
    confirmed=False,
    control_factory=HermesGatewayControl,
):
    """Preview/status while live; apply/rollback only with independently stopped Gateway.

    The browser supplies an opaque profile choice and an exact plan ID, never a
    command, session file, service label, or ``gateway_stopped`` assertion.
    """
    if action not in _ACTIONS:
        raise WorkbenchError("INVALID_REQUEST", "Choose a Hermes MCP migration action.")
    if action == "preview" and plan_id is not None:
        raise WorkbenchError("INVALID_REQUEST", "A new preview has no existing plan.")
    if action != "preview" and not isinstance(plan_id, str):
        raise WorkbenchError("INVALID_REQUEST", "Choose one exact Hermes MCP plan.")
    if action in {"apply", "rollback"} and confirmed is not True:
        return {
            "state": "confirmation_required",
            "runtime_verified": False,
            "session_verified": False,
            "delivery_verified": False,
        }
    python, profile = catalog.resolve(store, employee_id, project_id, profile_id)
    control = (
        control_factory(python, profile, require_stopped=True)
        if action in {"apply", "rollback"}
        else control_factory(python, profile)
    )
    binding = _binding(store.root, employee_id, project_id, "hermes")
    endpoint = endpoint_for(store.root, binding) if binding else None
    if action in {"apply", "rollback"} and not endpoint:
        return {
            "state": "configuration_changed",
            "runtime_verified": False,
            "session_verified": False,
            "delivery_verified": False,
        }
    with _exclusive_cutover(store.root):
        if action in {"apply", "rollback"} and (
            _binding(store.root, employee_id, project_id, "hermes") != binding
            or endpoint_for(store.root, binding) != endpoint
        ):
            return {
                "state": "configuration_changed",
                "runtime_verified": False,
                "session_verified": False,
                "delivery_verified": False,
            }
        if action in {"apply", "rollback"} and not _stopped(control, endpoint, profile):
            return {
                "state": "gateway_stop_unconfirmed",
                "runtime_verified": False,
                "session_verified": False,
                "delivery_verified": False,
            }
        source = process.payload()
        hashes = {
            name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in process.FILES
        }
        request = {"version": 1, "operation": _ACTIONS[action], "profile": str(profile)}
        if action == "preview":
            request["new_command"] = str(_installed_command())
        else:
            request["plan_id"] = plan_id
        if action in {"apply", "rollback"}:
            request.update({"confirmed": True, "gateway_stopped": True})
        return process.run(python, profile, request, source=source, expected_files=hashes)
