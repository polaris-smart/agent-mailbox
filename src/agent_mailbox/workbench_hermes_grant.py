"""Owner-only permission repair for one previously bound Hermes conversation.

No browser path or session key is accepted. This does not start/reload a host.
"""

from __future__ import annotations

import hashlib
import re
from contextlib import nullcontext

from . import workbench_hermes_catalog as catalog
from . import workbench_hermes_process as process
from .workbench_hermes_cutover import _exclusive_cutover
from .workbench_receiver_status import _binding
from .workbench_store import WorkbenchError


def operation(
    store,
    employee_id,
    project_id,
    action,
    *,
    profile_id,
    expected_binding_id=None,
    expected_config_sha256=None,
    confirmed=False,
):
    if (
        not isinstance(action, str)
        or action not in {"preview", "apply"}
        or type(confirmed) is not bool
    ):
        raise WorkbenchError("INVALID_REQUEST", "Choose a receiver permission action.")
    if action == "preview" and (
        confirmed or expected_binding_id is not None or expected_config_sha256 is not None
    ):
        raise WorkbenchError("INVALID_REQUEST", "Refresh the permission preview.")
    if action == "apply" and (
        confirmed is not True
        or not isinstance(expected_binding_id, str)
        or not isinstance(expected_config_sha256, str)
        or re.fullmatch(r"[a-f0-9]{64}", expected_config_sha256) is None
    ):
        raise WorkbenchError("INVALID_REQUEST", "Confirm the current permission preview.")
    python, profile = catalog.resolve(store, employee_id, project_id, profile_id)
    try:
        with _exclusive_cutover(store.root) if action == "apply" else nullcontext():
            binding = _binding(store.root, employee_id, project_id, "hermes")
            if not binding or (action == "apply" and binding["id"] != expected_binding_id):
                raise ValueError("existing_binding_required")
            source = process.payload()
            pins = {
                name: hashlib.sha256((source / name).read_bytes()).hexdigest()
                for name in process.FILES
            }
            options = {"mailbox_home": str(store.root), "session_key": binding["host_session_id"]}
            if action == "apply":
                options.update(expected_config_sha256=expected_config_sha256, confirmed=True)
            result = process.run(
                python,
                profile,
                {
                    "version": 1,
                    "operation": "grant_" + action,
                    "profile": str(profile),
                    "options": options,
                },
                source=source,
                expected_files=pins,
            )
            catalog._member(store, employee_id, project_id)
            if _binding(store.root, employee_id, project_id, "hermes") != binding:
                raise ValueError("binding_changed")
            if action == "apply" and result["enabled"] is True:
                # The host reads this permission live. Recheck existing pending
                # intents now; the worker still enforces readiness and receipts.
                event = getattr(store, "mail_wake_event", None)
                if event is not None:
                    event.set()
            return {**result, "profile_id": profile_id, "binding_id": binding["id"]}
    except (OSError, ValueError, TypeError, KeyError):
        raise WorkbenchError(
            "RECEIVER_GRANT_UNCONFIRMED",
            "Receiver permission is unconfirmed. Refresh its state before retrying; host reception is not verified.",
        ) from None
