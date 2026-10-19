"""Owner-scoped preparation of an unprivileged Hermes diagnostic component.

Browser inputs are opaque identities only. This stage never enables the host,
grants injection, installs mailbox MCP credentials, or claims a live session.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import uuid
from pathlib import Path

from . import workbench_hermes_catalog as catalog
from . import workbench_hermes_process as process
from .workbench_store import WorkbenchError

OPAQUE = re.compile(r"[a-f0-9]{32}\Z")
SOCKET_ROOT = Path("/private/tmp") if Path("/private/tmp").is_dir() else Path("/tmp").resolve()


def _private(path, *, create=False):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("private_installer_path_required")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("private_installer_directory_required")
    return path


def _record(directory, public_id):
    if not isinstance(public_id, str) or OPAQUE.fullmatch(public_id) is None:
        raise ValueError("hermes_plan_required")
    fd = os.open(directory / (public_id + ".json"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or info.st_size > 65536
        ):
            raise ValueError("hermes_plan_private_record_required")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            value = json.loads(stream.read(65537))
    finally:
        os.close(fd)
    if not isinstance(value, dict) or value.get("id") != public_id:
        raise ValueError("hermes_plan_invalid_record")
    return value


def _source_pins():
    source = process.payload()
    return source, {
        name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in process.FILES
    }


def _projection(record, result):
    return {
        "plan_id": record["id"],
        "profile_id": record["profile_id"],
        "state": result["state"],
        "requires_confirmation": result.get("requires_confirmation", False),
        "will_activate": False,
        "will_write_injection_grant": False,
        "session_verified": False,
        "delivery_verified": False,
        "activation_requested": False,
    }


def _unprepared(profile_id):
    return {
        "state": "not_prepared_here",
        "profile_id": profile_id,
        "will_activate": False,
        "will_write_injection_grant": False,
        "session_verified": False,
        "delivery_verified": False,
        "activation_requested": False,
    }


def installation(
    store, employee_id, project_id, action, *, profile_id=None, plan_id=None, confirmed=False
):
    if os.name == "nt":
        raise WorkbenchError(
            "RECEIVER_PLATFORM_UNSUPPORTED", "Hermes diagnostic setup requires a local Unix host."
        )
    if action not in {"preview", "lookup", "apply", "status", "remove"}:
        raise WorkbenchError("INVALID_REQUEST", "Choose a diagnostic preparation action.")
    catalog._member(store, employee_id, project_id)
    try:
        if action == "lookup":
            if plan_id is not None or confirmed is not False:
                raise ValueError("unexpected_hermes_plan_arguments")
            catalog.resolve(store, employee_id, project_id, profile_id)
            records = store.directory / "hermes-discovery-plans"
            if not records.exists():
                return _unprepared(profile_id)
            records = _private(records)
            names = list(records.iterdir())
            if len(names) > 16:
                raise ValueError("too_many_hermes_plans")
            matches = []
            for path in names:
                if not path.name.endswith(".json"):
                    continue
                record = _record(records, path.stem)
                if (
                    record.get("version") == 1
                    and record.get("employee_id") == employee_id
                    and record.get("project_id") == project_id
                    and record.get("profile_id") == profile_id
                ):
                    matches.append(record)
            if not matches:
                return _unprepared(profile_id)
            source, _ = _source_pins()
            preview = None
            removed = None
            for record in sorted(matches, key=lambda item: item["created_at"], reverse=True):
                result = process.run(
                    Path(record["python"]),
                    Path(record["profile"]),
                    {
                        "version": 1,
                        "operation": "status",
                        "profile": record["profile"],
                        "plan_id": record["native_plan_id"],
                    },
                    source=source,
                    expected_files=record["source_sha256"],
                )
                projection = _projection(record, result)
                if result["state"] in {
                    "prepared_not_activated",
                    "configuration_changed",
                    "preparation_incomplete",
                }:
                    return projection
                if (
                    result["state"] == "preview"
                    and preview is None
                    and record["created_at"] + 600 > time.time()
                ):
                    preview = projection
                if result["state"] == "preparation_removed" and removed is None:
                    removed = projection
            return preview or removed or _unprepared(profile_id)
        if action == "preview":
            if plan_id is not None or confirmed is not False:
                raise ValueError("unexpected_hermes_plan_arguments")
            python, profile = catalog.resolve(store, employee_id, project_id, profile_id)
            # macOS Unix socket paths have a short byte limit. A private,
            # profile-specific directory under the canonical local temp root
            # avoids failure for long home/store paths.
            key = hashlib.sha256((str(store.root) + "\0" + str(profile)).encode()).hexdigest()[:20]
            ipc = _private(SOCKET_ROOT / ("ambh-" + key), create=True)
            socket_path = ipc / "hermes-diag.sock"
            source, pins = _source_pins()
            result = process.run(
                python,
                profile,
                {
                    "version": 1,
                    "operation": "preview_discovery",
                    "profile": str(profile),
                    "options": {"socket_path": str(socket_path)},
                },
                source=source,
                expected_files=pins,
            )
            if (
                result.get("state") != "preview"
                or result.get("requires_confirmation") is not True
                or not isinstance(result.get("plan_id"), str)
                or re.fullmatch(r"plan_[a-f0-9]{32}", result["plan_id"]) is None
                or result.get("will_activate") is not False
                or result.get("will_write_injection_grant") is not False
                or result.get("session_verified") is not False
                or result.get("delivery_verified") is not False
            ):
                raise ValueError("diagnostic_plan_permissions_unconfirmed")
            records = _private(store.directory / "hermes-discovery-plans", create=True)
            public_id = uuid.uuid4().hex
            record = {
                "version": 1,
                "id": public_id,
                "employee_id": employee_id,
                "project_id": project_id,
                "profile_id": profile_id,
                "profile": str(profile),
                "python": str(python),
                "native_plan_id": result["plan_id"],
                "source_sha256": pins,
                "created_at": time.time(),
            }
            raw = json.dumps(record).encode()
            fd = os.open(
                records / (public_id + ".json"),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            return _projection(record, result)
        if profile_id is not None:
            raise ValueError("unexpected_hermes_profile_argument")
        records = _private(store.directory / "hermes-discovery-plans")
        record = _record(records, plan_id)
        if (
            record.get("version") != 1
            or record.get("employee_id") != employee_id
            or record.get("project_id") != project_id
        ):
            raise ValueError("hermes_plan_scope_mismatch")
        # Existing owner record may be inspected even when profile enumeration
        # later fails. Child preparation validates profile and source afresh.
        source, _ = _source_pins()
        request = {
            "version": 1,
            "operation": action,
            "profile": record["profile"],
            "plan_id": record["native_plan_id"],
        }
        if action in {"apply", "remove"}:
            if confirmed is not True:
                raise ValueError("explicit_confirmation_required")
            request["confirmed"] = True
        elif confirmed is not False:
            raise ValueError("unexpected_hermes_confirmation")
        result = process.run(
            Path(record["python"]),
            Path(record["profile"]),
            request,
            source=source,
            expected_files=record["source_sha256"],
        )
        return _projection(record, result)
    except (OSError, ValueError, KeyError, TypeError):
        raise WorkbenchError(
            "RECEIVER_PREPARATION_UNCONFIRMED",
            "Hermes diagnostic preparation needs review; no receiver activation is claimed.",
        ) from None
