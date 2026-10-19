"""Owner-scoped diagnostic plugin admission behind an independent stop gate.

Preparation stays disabled. This second, explicit transaction uses Hermes PM to
make its discovery-only plugin eligible at the next Gateway start. It neither
starts that Gateway nor grants injection or claims a delivered message.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

from . import workbench_hermes_admission_journal as admission_journal
from . import workbench_hermes_catalog as catalog
from . import workbench_hermes_installer as installer
from . import workbench_hermes_process as process
from .workbench_hermes_cutover import _exclusive_cutover
from .workbench_hermes_gateway_control import HermesGatewayControl
from .workbench_hermes_mcp_cutover import _stopped
from .workbench_store import WorkbenchError

_OPERATIONS = {
    "preview": "diagnostic_admission_preview",
    "apply": "diagnostic_admission_apply",
    "status": "diagnostic_admission_status",
    "rollback": "diagnostic_admission_rollback",
}


def _socket(store, profile):
    key = hashlib.sha256((str(store.root) + "\0" + str(profile)).encode()).hexdigest()[:20]
    return installer.SOCKET_ROOT / ("ambh-" + key) / "hermes-diag.sock"


def _project(record, result):
    return {
        "plan_id": record["id"],
        "profile_id": record["profile_id"],
        "state": result["state"],
        "requires_confirmation": True,
        "requires_stopped_gateway": True,
        "runtime_verified": False,
        "session_verified": False,
        "delivery_verified": False,
    }


def _absent(profile_id):
    return {
        "profile_id": profile_id,
        "state": "not_admitted_here",
        "runtime_verified": False,
        "session_verified": False,
        "delivery_verified": False,
    }


def _pm_generation_pair(old, current):
    """Accept only two committed-generation-shaped paths in one Hermes install."""
    install = Path.home() / ".hermes/hermes-agent"
    key = hashlib.sha256(str(install).encode()).hexdigest()[:16]
    base = Path.home() / ".hermes/installs" / key / "environments"

    def generation(value):
        if not isinstance(value, (str, os.PathLike)):
            return None
        path = Path(value)
        if (
            path.name != "python"
            or path.parent.name != "bin"
            or path.parent.parent.name != "venv"
            or path.parent.parent.parent.parent != base
            or re.fullmatch(r"[0-9a-f]{32}", path.parent.parent.parent.name) is None
        ):
            return None
        return path.parent.parent.parent.name

    return generation(old) is not None and generation(current) is not None


def _admission_intent(record, profile):
    native = record.get("native_plan_id")
    if not isinstance(native, str) or re.fullmatch(r"plan_[0-9a-f]{32}", native) is None:
        return False
    try:
        folder = installer._private(profile / "mailbox-diagnostic-admissions" / native)
        process._file(folder / "intent.json")
        return True
    except (OSError, ValueError, TypeError):
        return False


def _record_for(store, employee_id, project_id, public_id, *, preparation, allow_pm_reselect=False):
    directory = installer._private(
        store.directory / ("hermes-discovery-plans" if preparation else "hermes-admission-plans")
    )
    record = installer._record(directory, public_id)
    if (
        record.get("version") != 1
        or record.get("employee_id") != employee_id
        or record.get("project_id") != project_id
    ):
        raise ValueError("hermes_admission_scope_mismatch")
    python, profile = catalog.resolve(store, employee_id, project_id, record.get("profile_id"))
    if record.get("profile") != str(profile):
        raise ValueError("hermes_admission_runtime_changed")
    if record.get("python") != str(python) and not (
        allow_pm_reselect
        and _pm_generation_pair(record.get("python"), python)
        and (preparation or _admission_intent(record, profile))
    ):
        raise ValueError("hermes_admission_runtime_changed")
    source, pins = installer._source_pins()
    if pins != record.get("source_sha256"):
        raise ValueError("hermes_admission_component_changed")
    return record, python, profile, source, pins


def _save(store, record):
    directory = installer._private(store.directory / "hermes-admission-plans", create=True)
    path = directory / (record["id"] + ".json")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(json.dumps(record, sort_keys=True).encode())
        stream.flush()
        os.fsync(stream.fileno())


def admission(
    store,
    employee_id,
    project_id,
    action,
    *,
    profile_id=None,
    preparation_id=None,
    plan_id=None,
    confirmed=False,
    control_factory=HermesGatewayControl,
):
    """Preview/status are safe while live; mutations require independent stop proof."""
    if action not in {*_OPERATIONS, "lookup", "verify"}:
        raise WorkbenchError("INVALID_REQUEST", "Choose a diagnostic admission action.")
    catalog._member(store, employee_id, project_id)
    if action == "lookup":
        if plan_id is not None or preparation_id is not None or confirmed is not False:
            raise WorkbenchError("INVALID_REQUEST", "Lookup needs one listed profile.")
    elif action == "preview":
        if profile_id is not None or plan_id is not None or confirmed is not False:
            raise WorkbenchError("INVALID_REQUEST", "Preview needs one preparation plan.")
    elif profile_id is not None or preparation_id is not None or not isinstance(plan_id, str):
        raise WorkbenchError("INVALID_REQUEST", "Choose one diagnostic admission plan.")
    if action in {"apply", "rollback"} and confirmed is not True:
        return {
            "state": "confirmation_required",
            "runtime_verified": False,
            "session_verified": False,
            "delivery_verified": False,
        }
    if action in {"status", "verify"} and confirmed is not False:
        raise WorkbenchError("INVALID_REQUEST", "Inspection needs no confirmation.")
    try:
        if action == "lookup":
            catalog.resolve(store, employee_id, project_id, profile_id)
            directory = store.directory / "hermes-admission-plans"
            if not directory.exists():
                return _absent(profile_id)
            directory = installer._private(directory)
            names = list(directory.iterdir())
            if len(names) > 16:
                raise ValueError("too_many_hermes_admission_plans")
            matches = []
            for path in names:
                if not path.name.endswith(".json"):
                    continue
                record = installer._record(directory, path.stem)
                if (
                    record.get("employee_id") == employee_id
                    and record.get("project_id") == project_id
                    and record.get("profile_id") == profile_id
                ):
                    matches.append(record)
            if not matches:
                return _absent(profile_id)
            # A newer unused preview must not hide an older enabled/unknown
            # transaction. Inspect exact plans before choosing what Owner sees.
            projections = []
            for record in sorted(matches, key=lambda item: item["created_at"], reverse=True):
                checked, python, profile, source, pins = _record_for(
                    store,
                    employee_id,
                    project_id,
                    record["id"],
                    preparation=False,
                    allow_pm_reselect=True,
                )
                result = process.run(
                    python,
                    profile,
                    {
                        "version": 1,
                        "operation": _OPERATIONS["status"],
                        "profile": str(profile),
                        "plan_id": checked["native_plan_id"],
                    },
                    source=source,
                    expected_files=pins,
                )
                projections.append(_project(checked, result))
            # An uncertain older write outranks a newer apparently usable
            # plan: hiding it would invite a second mutation before review.
            for state in (
                "admission_unknown",
                "rollback_unknown",
                "inspection_required",
                "diagnostic_enabled_restart_required",
            ):
                for item in projections:
                    if item["state"] == state:
                        return item
            return projections[0]
        if action == "preview":
            prepared, python, profile, source, pins = _record_for(
                store, employee_id, project_id, preparation_id, preparation=True
            )
            prior = admission(
                store, employee_id, project_id, "lookup", profile_id=prepared["profile_id"]
            )
            if prior["state"] in {"admission_unknown", "rollback_unknown", "inspection_required"}:
                raise ValueError("previous_admission_requires_inspection")
            result = process.run(
                python,
                profile,
                {
                    "version": 1,
                    "operation": _OPERATIONS[action],
                    "profile": str(profile),
                    "preparation_id": prepared["native_plan_id"],
                },
                source=source,
                expected_files=pins,
            )
            if result["state"] != "preview":
                raise ValueError("hermes_admission_preview_unconfirmed")
            record = {
                "version": 1,
                "id": uuid.uuid4().hex,
                "employee_id": employee_id,
                "project_id": project_id,
                "profile_id": prepared["profile_id"],
                "preparation_id": preparation_id,
                "profile": str(profile),
                "python": str(python),
                "source_sha256": pins,
                "native_plan_id": result["plan_id"],
                "created_at": time.time(),
            }
            _save(store, record)
            return _project(record, result)
        record, python, profile, source, pins = _record_for(
            store, employee_id, project_id, plan_id, preparation=False, allow_pm_reselect=True
        )
        prepared, *_ = _record_for(
            store,
            employee_id,
            project_id,
            record.get("preparation_id"),
            preparation=True,
            allow_pm_reselect=_admission_intent(record, profile),
        )
        if prepared["profile"] != record["profile"]:
            raise ValueError("hermes_admission_preparation_changed")
        if action == "verify":
            inspection = {
                "version": 1,
                "operation": _OPERATIONS["status"],
                "profile": str(profile),
                "plan_id": record["native_plan_id"],
            }
            first = process.run(python, profile, inspection, source=source, expected_files=pins)
            if first["state"] != "diagnostic_enabled_restart_required":
                return _project(record, first)
            try:
                control = control_factory(python, profile)
                observed = control.observe_diagnostic(_socket(store, profile))
            except (OSError, ValueError, TypeError, KeyError, TimeoutError):
                return {
                    **_project(record, first),
                    "state": "diagnostic_enabled_runtime_unconfirmed",
                    "requires_confirmation": False,
                    "requires_stopped_gateway": False,
                }
            if observed != {
                "state": "diagnostic_runtime_observed",
                "runtime_verified": True,
                "session_verified": False,
                "delivery_verified": False,
            }:
                raise ValueError("hermes_diagnostic_observation_invalid")
            last = process.run(python, profile, inspection, source=source, expected_files=pins)
            if last["state"] != first["state"]:
                return _project(record, last)
            return {
                **_project(record, last),
                **observed,
                "requires_confirmation": False,
                "requires_stopped_gateway": False,
            }
        request = {
            "version": 1,
            "operation": _OPERATIONS[action],
            "profile": str(profile),
            "plan_id": record["native_plan_id"],
        }
        if action in {"apply", "rollback"}:
            # A browser boolean is never accepted as proof. The exact profile,
            # previous PID, launchd domains and receiver socket are checked
            # under the mailbox cutover lock immediately before the child call.
            installer._private(store.root / "wake", create=True)
            with _exclusive_cutover(store.root):
                # A managed stop/PM transaction may have died after its
                # external intent. The older direct endpoint must not bypass
                # that durable unknown and replay the same mutation.
                if admission_journal.unfinished_exists(
                    store.root
                ) or admission_journal.reserved_plan(store.root, plan_id):
                    raise ValueError("hermes_admission_journal_requires_inspection")
                prior = admission(
                    store, employee_id, project_id, "lookup", profile_id=record["profile_id"]
                )
                if prior["state"] in {
                    "admission_unknown",
                    "rollback_unknown",
                    "inspection_required",
                }:
                    raise ValueError("previous_admission_requires_inspection")
                control = control_factory(python, profile, require_stopped=True)
                if not _stopped(control, _socket(store, profile), profile):
                    return {
                        "state": "gateway_stop_unconfirmed",
                        "runtime_verified": False,
                        "session_verified": False,
                        "delivery_verified": False,
                    }
                request.update({"confirmed": True, "gateway_stopped": True})
                result = process.run(
                    python, profile, request, source=source, expected_files=pins, timeout=120
                )
        else:
            result = process.run(python, profile, request, source=source, expected_files=pins)
        return _project(record, result)
    except (OSError, ValueError, KeyError, TypeError):
        raise WorkbenchError(
            "RECEIVER_ADMISSION_UNCONFIRMED",
            "Hermes diagnostic admission needs inspection; no live delivery is claimed.",
        ) from None
