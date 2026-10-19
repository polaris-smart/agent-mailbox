"""Durable first-contact admission intent, independent of delivery state.

Every external stop, PM mutation and restart needs a preceding fsynced phase.
An unfinished or malformed record requires inspection; it is never a license
to replay an uncertain plugin admission or to start another Gateway.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from pathlib import Path

from .workbench_reauthorization_holds import _private_claims, _private_json

_PLAN = re.compile(r"[0-9a-f]{32}\Z")
_PHASES = {
    "stop_intent",
    "admission_intent",
    "restart_intent",
    "complete",
    "pre_admission_restore_intent",
    "pre_admission_restored",
}
_TRANSITIONS = {
    ("stop_intent", "admission_intent"),
    ("admission_intent", "restart_intent"),
    ("restart_intent", "complete"),
    ("stop_intent", "pre_admission_restore_intent"),
    ("pre_admission_restore_intent", "pre_admission_restored"),
}
_SUFFIX = ".hermes-admission"


def path(home, plan_id):
    if not isinstance(plan_id, str) or not _PLAN.fullmatch(plan_id):
        raise ValueError("hermes_admission_journal_plan_unconfirmed")
    return Path(home) / "wake" / "claims" / (plan_id + _SUFFIX)


def reserved_plan(home, plan_id):
    """A completed managed plan is history, not permission for direct replay."""
    try:
        path(home, plan_id).lstat()
    except FileNotFoundError:
        return False
    return True


def _fsync_parent(target):
    fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _scope(value, *, plan_id, employee_id, project_id, profile_id, profile):
    return (
        isinstance(value, dict)
        and set(value)
        == {
            "version",
            "phase",
            "plan_id",
            "employee_id",
            "project_id",
            "profile_id",
            "profile",
            "domain",
            "original_pid",
            "original_start_time",
            "plist_sha256",
            "command_sha256",
        }
        and type(value["version"]) is int
        and value["version"] == 1
        and isinstance(value["phase"], str)
        and value["phase"] in _PHASES
        and value["plan_id"] == plan_id
        and all(
            isinstance(value[key], str) and value[key]
            for key in ("employee_id", "project_id", "profile_id", "profile")
        )
        and value["employee_id"] == employee_id
        and value["project_id"] == project_id
        and value["profile_id"] == profile_id
        and value["profile"] == str(profile)
        and Path(value["profile"]).is_absolute()
        and value["domain"] in (f"gui/{os.getuid()}", f"user/{os.getuid()}")
        and type(value["original_pid"]) is int
        and value["original_pid"] > 0
        and type(value["original_start_time"]) is int
        and value["original_start_time"] > 0
        and all(
            isinstance(value[key], str) and re.fullmatch(r"[0-9a-f]{64}", value[key])
            for key in ("plist_sha256", "command_sha256")
        )
    )


def read(store, plan_id, *, employee_id, project_id, profile_id, profile):
    target = path(store.root, plan_id)
    value = _private_json(target)
    if not _scope(
        value,
        plan_id=plan_id,
        employee_id=employee_id,
        project_id=project_id,
        profile_id=profile_id,
        profile=profile,
    ):
        raise ValueError("hermes_admission_journal_unconfirmed")
    return value


def unfinished_exists(home):
    """Fail closed on orphaned names; complete history may remain in place."""
    wake = Path(home) / "wake"
    claims = wake / "claims"
    for directory in (wake, claims):
        try:
            info = directory.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("hermes_admission_journal_unconfirmed")
    with os.scandir(claims) as entries:
        names = [entry.name for entry in entries if _SUFFIX in entry.name]
    for name in names:
        if not name.endswith(_SUFFIX):
            raise ValueError("hermes_admission_journal_unconfirmed")
        plan_id = name[: -len(_SUFFIX)]
        if not _PLAN.fullmatch(plan_id):
            raise ValueError("hermes_admission_journal_unconfirmed")
        value = _private_json(path(home, plan_id))
        if not isinstance(value, dict) or not _scope(
            value,
            plan_id=plan_id,
            employee_id=value.get("employee_id"),
            project_id=value.get("project_id"),
            profile_id=value.get("profile_id"),
            profile=value.get("profile"),
        ):
            raise ValueError("hermes_admission_journal_unconfirmed")
        if value["phase"] not in {"complete", "pre_admission_restored"}:
            return True
    return False


def begin(store, plan_id, employee_id, project_id, profile_id, control, runtime):
    """Persist original service identity after drain proof, before stop marker."""
    claims = _private_claims(store.root)
    target = path(store.root, plan_id)
    if target.parent != claims or unfinished_exists(store.root):
        raise ValueError("hermes_admission_journal_requires_inspection")
    value = {
        "version": 1,
        "phase": "stop_intent",
        "plan_id": plan_id,
        "employee_id": employee_id,
        "project_id": project_id,
        "profile_id": profile_id,
        "profile": str(control.profile),
        "domain": control.domain,
        "original_pid": runtime.get("pid"),
        "original_start_time": runtime.get("start_time"),
        "plist_sha256": control.plist_digest.hex(),
        "command_sha256": control.command_digest.hex(),
    }
    if not _scope(
        value,
        plan_id=plan_id,
        employee_id=employee_id,
        project_id=project_id,
        profile_id=profile_id,
        profile=control.profile,
    ):
        raise ValueError("hermes_admission_journal_evidence_unconfirmed")
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise ValueError("hermes_admission_journal_requires_inspection") from None
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_parent(target)
    return value


def advance(store, plan_id, expected, phase, *, employee_id, project_id, profile_id, profile):
    """Atomically record the next intent before its external side effect."""
    if (expected, phase) not in _TRANSITIONS:
        raise ValueError("hermes_admission_journal_transition_unconfirmed")
    target = path(store.root, plan_id)
    scope = {
        "employee_id": employee_id,
        "project_id": project_id,
        "profile_id": profile_id,
        "profile": profile,
    }
    before = read(store, plan_id, **scope)
    if before["phase"] != expected:
        raise ValueError("hermes_admission_journal_transition_unconfirmed")
    after = {**before, "phase": phase}
    temporary = target.with_name("." + target.name + "." + secrets.token_hex(8) + ".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(after, sort_keys=True, separators=(",", ":")).encode())
            stream.flush()
            os.fsync(stream.fileno())
        info = target.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or read(store, plan_id, **scope) != before
        ):
            raise ValueError("hermes_admission_journal_changed")
        os.replace(temporary, target)
        _fsync_parent(target)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
    return after
