"""Durable intent record for one explicitly confirmed Hermes cutover.

Each phase is persisted before its external side effect.  A crash therefore
leaves a conservative upper bound on what may have happened, never evidence
that a write or delivery succeeded.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from pathlib import Path

from .workbench_reauthorization_holds import (
    _legacy_claim_temporary_plan,
    _private_claim_temporary,
    _private_claims,
    _private_json,
)

_BINDING = re.compile(r"binding_[0-9a-f]{32}\Z")
_PLAN = re.compile(r"plan_[0-9a-f]{32}\Z")
_PHASES = {
    "stop_intent",
    "pin_intent",
    "complete",
    "restart_intent",
    "restart_verified",
    "restart_retry_intent",
    "restart_retry_verified",
}
_TRANSITIONS = {
    ("stop_intent", "pin_intent"),
    ("pin_intent", "complete"),
    ("stop_intent", "restart_intent"),
    ("restart_intent", "restart_verified"),
    ("restart_intent", "restart_retry_intent"),
    ("restart_retry_intent", "restart_retry_verified"),
}
_PIN_PHASES = {"pin_intent", "complete"}


def path(home, binding_id, plan_id):
    if (
        not isinstance(binding_id, str)
        or not _BINDING.fullmatch(binding_id)
        or not isinstance(plan_id, str)
        or not _PLAN.fullmatch(plan_id)
    ):
        raise ValueError("hermes_cutover_journal_scope_unconfirmed")
    return Path(home) / "wake" / "claims" / f"{binding_id}.{plan_id}.hermes-cutover"


def _temporary_plan(name, binding_id):
    match = re.fullmatch(
        rf"\.{re.escape(binding_id)}\.(plan_[0-9a-f]{{32}})"
        r"\.hermes-cutover\.[0-9a-f]{16}\.tmp",
        name,
    )
    return match.group(1) if match else None


def _discard_temporaries(home, binding_id, plan_id):
    """Clean only this plan's private uncommitted writes after durable advance."""
    claims = _private_claims(home)
    removed = False
    with os.scandir(claims) as entries:
        for entry in entries:
            if plan_id not in (
                _temporary_plan(entry.name, binding_id),
                _legacy_claim_temporary_plan(entry.name, binding_id, ".hermes-cutover"),
            ):
                continue
            if not _private_claim_temporary(entry):
                raise ValueError("hermes_cutover_journal_unconfirmed")
            os.unlink(entry.path)
            removed = True
    if removed:
        _fsync_parent(path(home, binding_id, plan_id))


def _payload(store, binding, plan_id, profile_id, control, runtime):
    if (
        not isinstance(binding, dict)
        or binding.get("host_kind") != "hermes"
        or type(runtime.get("pid")) is not int
        or runtime["pid"] <= 0
        or type(runtime.get("start_time")) is not int
        or not isinstance(profile_id, str)
        or not profile_id
        or control.domain not in (f"gui/{os.getuid()}", f"user/{os.getuid()}")
        or not isinstance(control.plist_digest, bytes)
        or len(control.plist_digest) != 32
        or not isinstance(control.command_digest, bytes)
        or len(control.command_digest) != 32
    ):
        raise ValueError("hermes_cutover_journal_evidence_unconfirmed")
    journal = {
        "version": 1,
        "phase": "stop_intent",
        "pin_may_have_run": False,
        "plan_id": plan_id,
        "binding_id": binding["id"],
        "employee_id": binding["employee_id"],
        "project_id": binding["project_id"],
        "profile_id": profile_id,
        "profile": str(control.profile),
        "domain": control.domain,
        "original_pid": runtime["pid"],
        "original_start_time": runtime["start_time"],
        "plist_sha256": control.plist_digest.hex(),
        "command_sha256": control.command_digest.hex(),
    }
    if path(store.root, binding["id"], plan_id).parent != _private_claims(store.root):
        raise ValueError("hermes_cutover_journal_scope_unconfirmed")
    return journal


def begin(store, binding, plan_id, profile_id, control, runtime):
    """Persist original process/service identity before writing a stop marker."""
    value = _payload(store, binding, plan_id, profile_id, control, runtime)
    target = path(store.root, binding["id"], plan_id)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise ValueError("existing_hermes_cutover_journal_requires_review") from None
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_parent(target)
    return value


def any_for_binding(home, binding_id):
    """Detect even an orphaned or malformed intent before reporting no cutover."""
    if not isinstance(binding_id, str) or not _BINDING.fullmatch(binding_id):
        raise ValueError("hermes_cutover_journal_scope_unconfirmed")
    claims = Path(home) / "wake" / "claims"
    try:
        info = claims.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("hermes_cutover_journal_directory_unsafe")
    with os.scandir(claims) as entries:
        return any(
            (
                entry.name.startswith(binding_id + ".")
                or entry.name.startswith("." + binding_id + ".")
            )
            and ".hermes-cutover" in entry.name
            for entry in entries
        )


def phases_for_binding(store, binding_id, *, employee_id, project_id, profile_id, profile):
    """Validate every prior journal before allowing another binding cutover.

    Completed records remain auditable; an unfinished, malformed, or orphaned
    entry must be refused before the Gateway receives a drain request.
    """
    if not isinstance(binding_id, str) or not _BINDING.fullmatch(binding_id):
        raise ValueError("hermes_cutover_journal_scope_unconfirmed")
    claims = Path(store.root) / "wake" / "claims"
    try:
        info = claims.lstat()
    except FileNotFoundError:
        return {}
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("hermes_cutover_journal_directory_unsafe")
    prefix = binding_id + "."
    hidden_prefix = "." + prefix
    suffix = ".hermes-cutover"
    names = []
    legacy_plans = []
    with os.scandir(claims) as entries:
        for entry in entries:
            name = entry.name
            if name.startswith(hidden_prefix) and suffix in name:
                if _temporary_plan(name, binding_id) is None or not _private_claim_temporary(entry):
                    raise ValueError("hermes_cutover_journal_unconfirmed")
            elif name.startswith(prefix) and suffix in name:
                legacy_plan = _legacy_claim_temporary_plan(name, binding_id, suffix)
                if legacy_plan is None:
                    names.append(name)
                elif _private_claim_temporary(entry):
                    legacy_plans.append(legacy_plan)
                else:
                    raise ValueError("hermes_cutover_journal_unconfirmed")
    if any(f"{binding_id}.{plan_id}{suffix}" not in names for plan_id in legacy_plans):
        raise ValueError("hermes_cutover_journal_unconfirmed")
    phases = {}
    for name in names:
        if not name.endswith(suffix):
            raise ValueError("hermes_cutover_journal_unconfirmed")
        plan_id = name[len(prefix) : -len(suffix)]
        if not _PLAN.fullmatch(plan_id):
            raise ValueError("hermes_cutover_journal_unconfirmed")
        value = read(
            store,
            binding_id,
            plan_id,
            employee_id=employee_id,
            project_id=project_id,
            profile_id=profile_id,
            profile=profile,
        )
        phases[plan_id] = value["phase"]
    return phases


def read(store, binding_id, plan_id, *, employee_id, project_id, profile_id, profile):
    target = path(store.root, binding_id, plan_id)
    value = _private_json(target)
    required = {
        "version",
        "phase",
        "pin_may_have_run",
        "plan_id",
        "binding_id",
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
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value["version"] != 1
        or value["binding_id"] != binding_id
        or value["plan_id"] != plan_id
        or value["phase"] not in _PHASES
        or type(value["pin_may_have_run"]) is not bool
        or value["pin_may_have_run"] != (value["phase"] in _PIN_PHASES)
        or not isinstance(value["employee_id"], str)
        or not isinstance(value["project_id"], str)
        or not isinstance(value["profile_id"], str)
        or not isinstance(value["profile"], str)
        or value["employee_id"] != employee_id
        or value["project_id"] != project_id
        or value["profile_id"] != profile_id
        or value["profile"] != str(profile)
        or value["domain"] not in (f"gui/{os.getuid()}", f"user/{os.getuid()}")
        or type(value["original_pid"]) is not int
        or value["original_pid"] <= 0
        or type(value["original_start_time"]) is not int
        or any(
            not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{64}", value[key])
            for key in ("plist_sha256", "command_sha256")
        )
    ):
        raise ValueError("hermes_cutover_journal_unconfirmed")
    return value


def advance(
    store, binding_id, plan_id, expected, phase, *, employee_id, project_id, profile_id, profile
):
    """Atomically put an intent on disk before entering the next external step."""
    if (expected, phase) not in _TRANSITIONS:
        raise ValueError("hermes_cutover_journal_transition_unconfirmed")
    target = path(store.root, binding_id, plan_id)
    scope = {
        "employee_id": employee_id,
        "project_id": project_id,
        "profile_id": profile_id,
        "profile": profile,
    }
    before = read(store, binding_id, plan_id, **scope)
    if before["phase"] != expected:
        raise ValueError("hermes_cutover_journal_transition_unconfirmed")
    after = {**before, "phase": phase, "pin_may_have_run": phase in _PIN_PHASES}
    raw = json.dumps(after, sort_keys=True, separators=(",", ":")).encode()
    temporary = target.with_name("." + target.name + "." + secrets.token_hex(8) + ".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        current = target.lstat()
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_uid != os.getuid()
            or current.st_nlink != 1
            or current.st_mode & 0o077
            or read(store, binding_id, plan_id, **scope) != before
        ):
            raise ValueError("hermes_cutover_journal_changed")
        os.replace(temporary, target)
        _fsync_parent(target)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
    _discard_temporaries(store.root, binding_id, plan_id)
    return after


def _fsync_parent(target):
    fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
