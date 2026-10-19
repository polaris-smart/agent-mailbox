"""Durable per-message hold for mail predating an explicit session reauthorization.

The owner coordinator must call ``hold_prior_pending`` before changing a Hermes
session pin. A hold never grants delivery; release requires a separate, explicit
Human decision. Unknown effects keep their existing receipt-only path.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from datetime import datetime
from pathlib import Path

from .workbench_host_bindings import load_bindings
from .workbench_mail_notifications import KIND
from .workbench_store import _now
from .workbench_wake import state_lock

IDENTIFIER = re.compile(r"message_[0-9a-f]{32}\Z")
SAFE_COMPONENT = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
PLAN = re.compile(r"plan_[0-9a-f]{32}\Z")
_EPOCH_SUFFIX = ".reauth-epoch"


def epoch_path(home, binding_id, plan_id):
    if (
        not isinstance(binding_id, str)
        or not re.fullmatch(r"binding_[0-9a-f]{32}", binding_id)
        or not isinstance(plan_id, str)
        or not PLAN.fullmatch(plan_id)
    ):
        raise ValueError("invalid_reauthorization_epoch_scope")
    return Path(home) / "wake" / "claims" / f"{binding_id}.{plan_id}{_EPOCH_SUFFIX}"


def _timestamp(value):
    if not isinstance(value, str):
        raise TypeError("reauthorization_epoch_time_unconfirmed")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("reauthorization_epoch_time_unconfirmed")
    return parsed


def _epoch_temporary_plan(name, binding_id):
    match = re.fullmatch(
        rf"\.{re.escape(binding_id)}\.(plan_[0-9a-f]{{32}})"
        rf"{re.escape(_EPOCH_SUFFIX)}\.[0-9a-f]{{16}}\.tmp",
        name,
    )
    return match.group(1) if match else None


def _legacy_claim_temporary_plan(name, binding_id, suffix):
    """Recognize only the private pre-fix rename temp shape, not state records."""
    match = re.fullmatch(
        rf"{re.escape(binding_id)}\.(plan_[0-9a-f]{{32}})"
        rf"{re.escape(suffix)}\.[0-9a-f]{{16}}\.tmp",
        name,
    )
    return match.group(1) if match else None


def _private_claim_temporary(entry):
    info = entry.stat(follow_symlinks=False)
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and info.st_nlink == 1
        and not info.st_mode & 0o077
        and info.st_size <= 4096
    )


def _epoch_records(home, binding_id):
    """Read every epoch; a malformed/partial file is a refusal, not absence."""
    if not isinstance(binding_id, str) or not SAFE_COMPONENT.fullmatch(binding_id):
        raise ValueError("invalid_reauthorization_epoch_scope")
    claims = Path(home) / "wake" / "claims"
    try:
        info = claims.lstat()
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("unsafe_claim_directory")
    records = []
    prefix = binding_id + "."
    hidden_prefix = "." + prefix
    names = []
    legacy_plans = []
    scoped = False
    with os.scandir(claims) as entries:
        for entry in entries:
            name = entry.name
            if name.startswith(hidden_prefix) and _EPOCH_SUFFIX in name:
                scoped = True
                if _epoch_temporary_plan(name, binding_id) is None or not _private_claim_temporary(
                    entry
                ):
                    raise ValueError("reauthorization_epoch_unconfirmed")
            elif name.startswith(prefix) and _EPOCH_SUFFIX in name:
                scoped = True
                legacy_plan = _legacy_claim_temporary_plan(name, binding_id, _EPOCH_SUFFIX)
                if legacy_plan is None:
                    names.append(name)
                elif _private_claim_temporary(entry):
                    legacy_plans.append(legacy_plan)
                else:
                    raise ValueError("reauthorization_epoch_unconfirmed")
    if not scoped:
        return []
    if info.st_mode & 0o077:
        raise ValueError("unsafe_claim_directory")
    if any(f"{binding_id}.{plan_id}{_EPOCH_SUFFIX}" not in names for plan_id in legacy_plans):
        raise ValueError("reauthorization_epoch_unconfirmed")
    for name in names:
        if not name.endswith(_EPOCH_SUFFIX):
            raise ValueError("reauthorization_epoch_unconfirmed")
        plan_id = name[len(prefix) : -len(_EPOCH_SUFFIX)]
        target = epoch_path(home, binding_id, plan_id)
        value = _private_json(target)
        if (
            not isinstance(value, dict)
            or set(value)
            != {"version", "binding_id", "plan_id", "phase", "previous_cutoff", "authorized_after"}
            or value["version"] != 1
            or value["binding_id"] != binding_id
            or value["plan_id"] != plan_id
            or value["phase"] not in {"pending", "complete"}
        ):
            raise ValueError("reauthorization_epoch_unconfirmed")
        _timestamp(value["previous_cutoff"])
        if value["phase"] == "pending":
            if value["authorized_after"] is not None:
                raise ValueError("reauthorization_epoch_unconfirmed")
        elif _timestamp(value["authorized_after"]) <= _timestamp(value["previous_cutoff"]):
            raise ValueError("reauthorization_epoch_not_monotonic")
        records.append(value)
    return records


def _discard_epoch_temporaries(home, binding_id, plan_id):
    """Remove only this completed plan's private orphan writes under state_lock."""
    claims = _private_claims(home)
    removed = False
    with os.scandir(claims) as entries:
        for entry in entries:
            if plan_id not in (
                _epoch_temporary_plan(entry.name, binding_id),
                _legacy_claim_temporary_plan(entry.name, binding_id, _EPOCH_SUFFIX),
            ):
                continue
            if not _private_claim_temporary(entry):
                raise ValueError("reauthorization_epoch_unconfirmed")
            os.unlink(entry.path)
            removed = True
    if removed:
        directory = os.open(claims, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _legacy_cutoff(home, binding_id):
    required = required_path(home, binding_id)
    cutoff = cutoff_path(home, binding_id)
    if not _exists(required) and not _exists(cutoff):
        return None
    if not _exists(required) or not _exists(cutoff):
        raise ValueError("reauthorization_boundary_incomplete")
    prerequisite = _private_json(required)
    marker = _private_json(cutoff)
    if (
        not isinstance(prerequisite, dict)
        or set(prerequisite) != {"version", "binding_id", "plan_id"}
        or prerequisite.get("version") != 1
        or prerequisite.get("binding_id") != binding_id
        or not isinstance(marker, dict)
        or set(marker) != {"version", "binding_id", "plan_id", "authorized_after"}
        or marker.get("version") != 1
        or marker.get("binding_id") != binding_id
        or marker.get("plan_id") != prerequisite["plan_id"]
        or not PLAN.fullmatch(prerequisite["plan_id"])
    ):
        raise ValueError("reauthorization_boundary_unconfirmed")
    _timestamp(marker["authorized_after"])
    return marker


def completed_history(home, binding_id):
    """Return durable completed plans and the latest cutoff; pending means stop."""
    legacy = _legacy_cutoff(home, binding_id)
    records = _epoch_records(home, binding_id)
    if legacy is None:
        if records:
            raise ValueError("reauthorization_epoch_without_baseline")
        return set(), None, None
    cursor = legacy["authorized_after"]
    remaining = list(records)
    completed = {legacy["plan_id"]}
    pending = None
    while remaining:
        matches = [item for item in remaining if item["previous_cutoff"] == cursor]
        if len(matches) != 1:
            raise ValueError("reauthorization_epoch_chain_unconfirmed")
        item = matches[0]
        remaining.remove(item)
        if item["phase"] == "pending":
            if remaining:
                raise ValueError("reauthorization_epoch_chain_unconfirmed")
            pending = item["plan_id"]
            break
        completed.add(item["plan_id"])
        cursor = item["authorized_after"]
    return completed, cursor, pending


def active_fence_for_plan(home, binding_id, plan_id):
    """Read-only exact fence for a plan before its pin write.

    The first cutover has a required marker and no cutoff. A later cutover
    retains the first cutoff and adds a pending epoch, so absence of cutoff
    cannot be used as a recovery predicate after the first successful pin.
    """
    if not isinstance(plan_id, str) or not PLAN.fullmatch(plan_id):
        return False
    try:
        required = _private_json(required_path(home, binding_id))
        baseline_plan = required.get("plan_id") if isinstance(required, dict) else None
        if (
            not isinstance(baseline_plan, str)
            or not PLAN.fullmatch(baseline_plan)
            or required != {"version": 1, "binding_id": binding_id, "plan_id": baseline_plan}
        ):
            return False
        if baseline_plan == plan_id:
            return not _exists(cutoff_path(home, binding_id)) and not _epoch_records(
                home, binding_id
            )
        completed, latest, pending = completed_history(home, binding_id)
        return latest is not None and baseline_plan in completed and pending == plan_id
    except (OSError, ValueError, TypeError, KeyError):
        return False


def hold_path(home, message_id):
    if not isinstance(message_id, str) or not SAFE_COMPONENT.fullmatch(message_id):
        raise ValueError("invalid_reauthorization_message_id")
    return Path(home) / "wake" / "claims" / f"{message_id}.reauth-hold"


def release_path(home, message_id, binding_id, plan_id):
    """An exact old-mail exception belongs to one completed route epoch."""
    if (
        not isinstance(message_id, str)
        or not IDENTIFIER.fullmatch(message_id)
        or not isinstance(binding_id, str)
        or not re.fullmatch(r"binding_[0-9a-f]{32}", binding_id)
        or not isinstance(plan_id, str)
        or not PLAN.fullmatch(plan_id)
    ):
        raise ValueError("invalid_reauthorization_release_scope")
    return Path(home) / "wake" / "claims" / f"{message_id}.{binding_id}.{plan_id}.reauth-release"


def held(home, message_id):
    """A malformed or symlinked marker is still a stop, never permission to send."""
    path = hold_path(home, message_id)
    return path.exists() or path.is_symlink()


def gate_path(home, binding_id):
    if not isinstance(binding_id, str) or not SAFE_COMPONENT.fullmatch(binding_id):
        raise ValueError("invalid_reauthorization_binding_id")
    return Path(home) / "wake" / "claims" / f"{binding_id}.reauth-gate"


def gated(home, binding_id):
    """An unreadable or symlinked gate remains a delivery stop."""
    path = gate_path(home, binding_id)
    if path.exists() or path.is_symlink():
        return True
    try:
        _completed, _cutoff, pending = completed_history(home, binding_id)
        return pending is not None
    except (OSError, ValueError, TypeError, KeyError):
        # An epoch intent precedes the gate write. Neither a partial intent nor
        # malformed history may permit a delivery during that interval.
        return True


def required_path(home, binding_id):
    return gate_path(home, binding_id).with_suffix(".reauth-required")


def cutoff_path(home, binding_id):
    return gate_path(home, binding_id).with_suffix(".reauth-cutoff")


def _private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or info.st_size > 4096
        ):
            raise ValueError("unsafe_reauthorization_record")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return json.loads(stream.read(4097))
    finally:
        os.close(fd)


def _exists(path):
    return path.exists() or path.is_symlink()


def latest_completed_plan(home, binding_id):
    """Return only the final durable plan in a validated cutover chain."""
    _plans, latest, pending = completed_history(home, binding_id)
    if latest is None or pending is not None:
        return None
    legacy = _legacy_cutoff(home, binding_id)
    if legacy is not None and legacy["authorized_after"] == latest:
        return legacy["plan_id"]
    matches = [
        row["plan_id"]
        for row in _epoch_records(home, binding_id)
        if row["phase"] == "complete" and row["authorized_after"] == latest
    ]
    if len(matches) != 1:
        raise ValueError("reauthorization_epoch_chain_unconfirmed")
    return matches[0]


def authorized_message(
    home, binding_id, created_at, *, message_id=None, employee_id=None, project_id=None
):
    """Only post-cutover mail may use a reauthorized route.

    The first required marker and cutoff remain as an audit baseline. Later
    cutovers add durable epochs; a pending/malformed epoch or any gate closes
    the route, while only the latest completed cutoff can authorize new mail.
    The server, not a sender, creates message timestamps.
    """
    try:
        if gated(home, binding_id):
            return False
        _plans, latest, pending = completed_history(home, binding_id)
        if pending is not None:
            return False
        if latest is None or _timestamp(created_at) > _timestamp(latest):
            return True
        if not message_id or not employee_id or not project_id or held(home, message_id):
            return False
        plan_id = latest_completed_plan(home, binding_id)
        return _private_json(release_path(home, message_id, binding_id, plan_id)) == {
            "version": 1,
            "binding_id": binding_id,
            "plan_id": plan_id,
            "employee_id": employee_id,
            "project_id": project_id,
        }
    except (OSError, ValueError, KeyError, TypeError):
        return False


def reauthorization_required(home, binding_id):
    if _exists(required_path(home, binding_id)) or _exists(cutoff_path(home, binding_id)):
        return True
    try:
        return bool(_epoch_records(home, binding_id))
    except (OSError, ValueError, TypeError, KeyError):
        return True


def _gate_matches(home, binding, plan_id):
    path = gate_path(home, binding["id"])
    try:
        value = _private_json(path)
    except (OSError, ValueError):
        return False
    return value == {
        "version": 1,
        "plan_id": plan_id,
        "binding_id": binding["id"],
        "employee_id": binding["employee_id"],
        "project_id": binding["project_id"],
    }


def _begin_epoch(home, binding_id, plan_id, previous_cutoff):
    target = epoch_path(home, binding_id, plan_id)
    expected = {
        "version": 1,
        "binding_id": binding_id,
        "plan_id": plan_id,
        "phase": "pending",
        "previous_cutoff": previous_cutoff,
        "authorized_after": None,
    }
    _write_or_verify_hold(target, expected)
    return expected


def _complete_epoch(home, binding_id, plan_id, previous_cutoff):
    target = epoch_path(home, binding_id, plan_id)
    before = _private_json(target)
    expected = {
        "version": 1,
        "binding_id": binding_id,
        "plan_id": plan_id,
        "phase": "pending",
        "previous_cutoff": previous_cutoff,
        "authorized_after": None,
    }
    if before != expected:
        if (
            not isinstance(before, dict)
            or before.get("phase") != "complete"
            or {**before, "phase": "pending", "authorized_after": None} != expected
        ):
            raise ValueError("reauthorization_epoch_changed")
        _discard_epoch_temporaries(home, binding_id, plan_id)
        return before
    now = _now()
    if _timestamp(now) <= _timestamp(previous_cutoff):
        raise ValueError("reauthorization_epoch_clock_not_monotonic")
    after = {**before, "phase": "complete", "authorized_after": now}
    # The pending target is already the durable refusal. An interrupted temp
    # write must not masquerade as another epoch and permanently block retry.
    temporary = target.with_name("." + target.name + "." + secrets.token_hex(8) + ".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(after, sort_keys=True, separators=(",", ":")).encode())
            stream.flush()
            os.fsync(stream.fileno())
        if _private_json(target) != before:
            raise ValueError("reauthorization_epoch_changed")
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
    _discard_epoch_temporaries(home, binding_id, plan_id)
    return after


def arm_binding_gate(store, binding, plan_id):
    """Block all new deliveries for one exact binding before snapshotting old mail."""
    if (
        not isinstance(binding, dict)
        or binding.get("host_kind") != "hermes"
        or not isinstance(binding.get("id"), str)
        or not re.fullmatch(r"binding_[0-9a-f]{32}", binding["id"])
        or not isinstance(plan_id, str)
        or not PLAN.fullmatch(plan_id)
    ):
        raise ValueError("invalid_reauthorization_scope")
    with state_lock(store.root):
        if binding not in load_bindings(store.root):
            raise ValueError("binding_changed_refresh_required")
        _private_claims(store.root)
        required = required_path(store.root, binding["id"])
        gate = gate_path(store.root, binding["id"])
        if _exists(cutoff_path(store.root, binding["id"])):
            completed, latest, pending = completed_history(store.root, binding["id"])
            if latest is None or plan_id in completed or pending not in (None, plan_id):
                raise ValueError("existing_reauthorization_hold_requires_review")
            if pending is None:
                _begin_epoch(store.root, binding["id"], plan_id, latest)
            elif _private_json(epoch_path(store.root, binding["id"], plan_id)) != {
                "version": 1,
                "binding_id": binding["id"],
                "plan_id": plan_id,
                "phase": "pending",
                "previous_cutoff": latest,
                "authorized_after": None,
            }:
                raise ValueError("existing_reauthorization_hold_requires_review")
        else:
            if _exists(gate) and not _exists(required):
                raise ValueError("existing_reauthorization_hold_requires_review")
            if _epoch_records(store.root, binding["id"]):
                raise ValueError("reauthorization_epoch_without_baseline")
            _write_or_verify_hold(
                required,
                {
                    "version": 1,
                    "plan_id": plan_id,
                    "binding_id": binding["id"],
                },
            )
        _write_or_verify_hold(
            gate,
            {
                "version": 1,
                "plan_id": plan_id,
                "binding_id": binding["id"],
                "employee_id": binding["employee_id"],
                "project_id": binding["project_id"],
            },
        )
    return {"state": "binding_delivery_gated", "delivery_verified": False}


def release_binding_gate(store, binding, plan_id, *, route_ready):
    """Set a durable time boundary before exposing an exact new route.

    Once a cutoff exists, a crash or a missing gate cannot authorize old mail.
    The old per-message holds are never released by this operation.
    """
    if not callable(route_ready):
        raise ValueError("verified_new_route_required")  # noqa: TRY004 - one refusal code
    with state_lock(store.root):
        if binding not in load_bindings(store.root):
            raise ValueError("binding_changed_refresh_required")
        if not _gate_matches(store.root, binding, plan_id):
            raise ValueError("reauthorization_delivery_gate_required")
        baseline = _private_json(required_path(store.root, binding["id"]))
        current_plan = baseline.get("plan_id") if isinstance(baseline, dict) else None
        if (
            not isinstance(current_plan, str)
            or not PLAN.fullmatch(current_plan)
            or baseline != {"version": 1, "plan_id": current_plan, "binding_id": binding["id"]}
        ):
            raise ValueError("reauthorization_required_marker_changed")
        if current_plan != plan_id:
            completed, latest, pending = completed_history(store.root, binding["id"])
            if (
                latest is None
                or current_plan not in completed
                or (pending != plan_id and (pending is not None or plan_id not in completed))
            ):
                raise ValueError("reauthorization_epoch_unconfirmed")
            if pending is None:
                records = [
                    item
                    for item in _epoch_records(store.root, binding["id"])
                    if item["plan_id"] == plan_id
                ]
                if len(records) != 1 or records[0]["authorized_after"] != latest:
                    raise ValueError("reauthorization_epoch_unconfirmed")
        if route_ready() is not True:
            raise ValueError("verified_new_route_required")
        if current_plan != plan_id:
            if pending == plan_id:
                _complete_epoch(store.root, binding["id"], plan_id, latest)
            gate = gate_path(store.root, binding["id"])
            gate.unlink()
            directory = os.open(gate.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return {
                "state": "new_route_released",
                "old_mail_released": False,
                "delivery_verified": False,
            }
        cutoff = cutoff_path(store.root, binding["id"])
        if _exists(cutoff):
            try:
                previous = _private_json(cutoff)
                before = datetime.fromisoformat(previous["authorized_after"])
            except (OSError, ValueError, KeyError, TypeError):
                raise ValueError("reauthorization_cutoff_requires_review") from None
            if (
                previous.get("version") != 1
                or previous.get("plan_id") != plan_id
                or previous.get("binding_id") != binding["id"]
                or before.tzinfo is None
            ):
                raise ValueError("reauthorization_cutoff_requires_review")
        else:
            _write_hold(
                cutoff,
                {
                    "version": 1,
                    "plan_id": plan_id,
                    "binding_id": binding["id"],
                    "authorized_after": _now(),
                },
            )
        gate = gate_path(store.root, binding["id"])
        gate.unlink()
        directory = os.open(gate.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return {
            "state": "new_route_released",
            "old_mail_released": False,
            "delivery_verified": False,
        }


def _private_claims(home):
    claims = Path(home) / "wake" / "claims"
    claims.mkdir(mode=0o700, exist_ok=True)
    info = claims.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("unsafe_claim_directory")
    return claims


def _write_hold(path, payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        # A previous or malformed hold must be investigated, not silently adopted.
        raise ValueError("existing_reauthorization_hold_requires_review") from None
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _write_or_verify_hold(path, payload):
    """Resume only an exact, private marker from this same cutover plan."""
    if _exists(path):
        try:
            if _private_json(path) == payload:
                return
        except (OSError, ValueError, TypeError):
            pass
        raise ValueError("existing_reauthorization_hold_requires_review")
    _write_hold(path, payload)


def hold_prior_pending(store, binding, plan_id):
    """Fence known-undelivered old letters under the same lock as wake delivery.

    This is a prerequisite to a stopped-host pin change, not a confirmation of
    the new session. Any in-flight/acknowledgement gap refuses the whole batch.
    Partial markers caused by a disk failure remain conservative holds.
    """
    if (
        not isinstance(binding, dict)
        or binding.get("host_kind") != "hermes"
        or not isinstance(plan_id, str)
        or not PLAN.fullmatch(plan_id)
    ):
        raise ValueError("invalid_reauthorization_scope")
    with state_lock(store.root):
        return _mark_prior_pending_held_locked(store, binding, plan_id)


def _mark_prior_pending_held_locked(store, binding, plan_id):
    if binding not in load_bindings(store.root):
        raise ValueError("binding_changed_refresh_required")
    if not _gate_matches(store.root, binding, plan_id):
        raise ValueError("reauthorization_delivery_gate_required")
    with store._transaction(readonly=True) as db:
        rows = db.execute(
            "SELECT n.state_hash FROM notifications n "
            "JOIN messages m ON m.id=n.state_hash AND m.recipient_id=n.target_id "
            "JOIN memberships p ON p.employee_id=n.target_id AND p.project_id=m.project_id "
            "JOIN employees e ON e.id=n.target_id AND e.lifecycle='active' "
            "WHERE n.kind=? AND n.target_id=? AND n.project_id=? "
            "AND m.project_id=? AND n.status='pending' "
            "ORDER BY n.created_at,n.id",
            (KIND, binding["employee_id"], binding["project_id"], binding["project_id"]),
        ).fetchall()
    claims = _private_claims(store.root)
    unknown = 0
    prior = []
    for row in rows:
        mid = row["state_hash"]
        if not IDENTIFIER.fullmatch(mid):
            raise ValueError("invalid_reauthorization_message_id")
        path = claims / mid
        if (claims / f"{mid}.unknown").exists():
            unknown += 1
            continue  # Existing strict receipt may still reconcile this effect.
        if path.exists() or (claims / f"{mid}.ok").exists():
            raise ValueError("inflight_delivery_requires_review")
        if held(store.root, mid):
            expected = {
                "version": 1,
                "plan_id": plan_id,
                "binding_id": binding["id"],
                "employee_id": binding["employee_id"],
                "project_id": binding["project_id"],
            }
            try:
                if _private_json(hold_path(store.root, mid)) != expected:
                    raise ValueError("existing_reauthorization_hold_requires_review")
            except (OSError, TypeError):
                raise ValueError("existing_reauthorization_hold_requires_review") from None
        prior.append(mid)
    for mid in prior:
        _write_or_verify_hold(
            hold_path(store.root, mid),
            {
                "version": 1,
                "plan_id": plan_id,
                "binding_id": binding["id"],
                "employee_id": binding["employee_id"],
                "project_id": binding["project_id"],
            },
        )
    # The file fence goes first. If the DB write fails, its partial markers
    # still block delivery; if a marker is later lost, this durable state
    # independently blocks the product's route resolver and pending query.
    with store._transaction() as db:
        for mid in prior:
            changed = db.execute(
                "UPDATE notifications SET status='reauthorization_held',updated_at=? "
                "WHERE kind=? AND target_id=? AND project_id=? AND state_hash=? "
                "AND status='pending' AND attempts=0",
                (_now(), KIND, binding["employee_id"], binding["project_id"], mid),
            ).rowcount
            if changed != 1:
                raise ValueError("pending_mail_changed_refresh_required")
    return {
        "state": "old_pending_held",
        "held_count": len(prior),
        "unknown_count": unknown,
        "delivery_verified": False,
    }


def publish_new_project_route_locked(store, binding, publish_route):
    """Fence old alerts before a new project's endpoint becomes usable.

    Caller holds state_lock. An interrupted write leaves a durable gate; it
    must be inspected instead of replaying old mail through a repaired route.
    """
    if (
        not isinstance(binding, dict)
        or binding.get("host_kind") != "hermes"
        or binding not in load_bindings(store.root)
        or not callable(publish_route)
    ):
        raise ValueError("invalid_new_project_route")
    plan_id = "plan_" + secrets.token_hex(16)
    _private_claims(store.root)
    required = required_path(store.root, binding["id"])
    gate = gate_path(store.root, binding["id"])
    cutoff = cutoff_path(store.root, binding["id"])
    if any(_exists(path) for path in (required, gate, cutoff)):
        raise ValueError("existing_reauthorization_hold_requires_review")
    _write_hold(required, {"version": 1, "plan_id": plan_id, "binding_id": binding["id"]})
    _write_hold(
        gate,
        {
            "version": 1,
            "plan_id": plan_id,
            "binding_id": binding["id"],
            "employee_id": binding["employee_id"],
            "project_id": binding["project_id"],
        },
    )
    held_result = _mark_prior_pending_held_locked(store, binding, plan_id)
    publish_route()
    _write_hold(
        cutoff,
        {"version": 1, "plan_id": plan_id, "binding_id": binding["id"], "authorized_after": _now()},
    )
    gate.unlink()
    directory = os.open(gate.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return held_result
