"""Owner-confirmed workspace installation of an already issued mail session.

The browser supplies identities, never paths, commands, configuration or secrets.
No credential issuance, project trust, MCP reload, host restart or mail delivery.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import sys
import time
import uuid
from pathlib import Path

from . import workbench_codex_config as config
from . import workbench_codex_connection as receiver
from .workbench_mail_sessions import validate_session
from .workbench_store import WorkbenchError
from .workbench_wake import state_lock

TTL = 600


def _read_private(directory, name, limit=1024 * 1024):
    receiver._private(directory)
    fd = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or info.st_mode & 0o7077
            or info.st_size > limit
        ):
            raise ValueError("installer_private_record_required")
        value = json.loads(stream.read(limit + 1))
    if not isinstance(value, dict):
        raise ValueError("installer_invalid_record")  # noqa: TRY004 - invalid persisted data
    return value


def _write(directory, name, value, *, new=False):
    receiver._private(directory)
    encoded = json.dumps(value, ensure_ascii=False).encode()
    if new:
        fd = os.open(directory / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        _read_private(directory, name)
        receiver._atomic(directory / name, encoded)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _credential(store, employee_id, project_id, session_id):
    if (
        not isinstance(session_id, str)
        or not session_id.startswith("mail_session_")
        or len(session_id) > 80
        or not session_id.replace("_", "").isalnum()
    ):
        raise ValueError("installer_session_required")
    directory = store.directory / "mail-sessions"
    record = _read_private(directory, session_id + ".json", 65536)
    if (
        record.get("session_id") != session_id
        or record.get("employee_id") != employee_id
        or record.get("project_id") != project_id
        or record.get("home") != str(store.root)
    ):
        raise ValueError("installer_session_scope_mismatch")
    with store._transaction(readonly=True) as db:
        session = validate_session(store, db, record.get("token"))
        if (
            session["id"] != session_id
            or session["employee_id"] != employee_id
            or session["project_id"] != project_id
        ):
            raise ValueError("installer_session_scope_mismatch")
    args = [] if getattr(sys, "frozen", False) else ["-E", "-m", "agent_mailbox"]
    return sys.executable, [
        *args,
        "mailbox-mcp",
        "--session-file",
        str(directory / (session_id + ".json")),
    ]


def _inspect(store, thread_id, workspace):
    root, ipc = receiver._paths(store.root)
    if not receiver._installed(root):
        raise ValueError("receiver_install_required")
    endpoint = (
        receiver._codex_launcher_endpoint(store.root, ipc, thread_id) if ipc.exists() else None
    )
    return receiver._configuration(endpoint, thread_id, workspace)


def _verify(store, record, thread_id, command, args):
    root, ipc = receiver._paths(store.root)
    if not receiver._installed(root):
        raise ValueError("receiver_install_required")
    endpoint = (
        receiver._codex_launcher_endpoint(store.root, ipc, thread_id) if ipc.exists() else None
    )
    digest = hashlib.sha256(
        json.dumps(
            {"command": command, "args": args},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    result = receiver._control(endpoint, "identity", thread_id, entry_digest=digest)
    expected = {
        "protocol": "agent-mailbox.identity.v1",
        **{key: record[key] for key in ("employee_id", "project_id", "session_id")},
    }
    cwd = result.get("cwd")
    if (
        result.get("thread_id") != thread_id
        or not isinstance(cwd, str)
        or not 1 <= len(cwd) < 4096
        or not Path(cwd).is_absolute()
        or Path(cwd).resolve() != Path(record["workspace"]).resolve()
        or result.get("identity") != expected
    ):
        raise ValueError("receiver_identity_scope_mismatch")
    return expected


def _empty_mailbox_scope(inspection):
    # Disabled/inherited entries and still-loaded servers are conflicts too.
    rows = [*inspection["effective_servers"], *inspection["runtime_servers"]]
    for layer in inspection["layers"]:
        rows.extend(layer["servers"])
        if layer["workspace_layer"] and layer["disabled"]:
            raise ValueError("installer_project_config_disabled")
    if any(row["mailbox_like"] or row["name"] == config.SERVER for row in rows):
        raise ValueError("installer_existing_mailbox_requires_review")
    online = {row["name"] for row in inspection["runtime_servers"] if row["connected"]}
    if any(not row["connected"] for row in inspection["runtime_servers"]) or any(
        row["enabled"] and row["name"] not in online for row in inspection["effective_servers"]
    ):
        # An unavailable wrapper may be a mailbox; metadata cannot rule it out.
        raise ValueError("installer_host_inventory_incomplete")


def _replacement_scope(inspection, owned_block):
    """Check configuration ownership without calling an expired credential."""
    entry = config._parse(owned_block)["mcp_servers"][config.SERVER]
    digest = hashlib.sha256(
        json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    own = [row for row in inspection["effective_servers"] if row["name"] == config.SERVER]
    if len(own) != 1 or not own[0]["enabled"] or own[0].get("entry_digest") != digest:
        raise ValueError("installer_replacement_ownership_unconfirmed")
    owners = 0
    for layer in inspection["layers"]:
        if layer["workspace_layer"] and layer["disabled"]:
            raise ValueError("installer_project_config_disabled")
        for row in layer["servers"]:
            if row["mailbox_like"] or row["name"] == config.SERVER:
                if (
                    not layer["workspace_layer"]
                    or layer["disabled"]
                    or row["name"] != config.SERVER
                    or not row["enabled"]
                    or row.get("entry_digest") != digest
                ):
                    raise ValueError("installer_replacement_ownership_unconfirmed")
                owners += 1
    if owners != 1:
        raise ValueError("installer_replacement_ownership_unconfirmed")
    # The known owned server may be offline because its credential expired.
    # Unknown unavailable servers and other mailbox aliases still fail closed.
    effective = [row for row in inspection["effective_servers"] if row["name"] != config.SERVER]
    runtime = [row for row in inspection["runtime_servers"] if row["name"] != config.SERVER]
    _empty_mailbox_scope({"effective_servers": effective, "runtime_servers": runtime, "layers": []})
    if any(not row["tools_available"] for row in runtime):
        raise ValueError("installer_host_inventory_incomplete")


def _load_record(directory, plan_id, employee_id, project_id, workspace):
    if (
        not isinstance(plan_id, str)
        or len(plan_id) != 32
        or any(c not in "0123456789abcdef" for c in plan_id)
    ):
        raise ValueError("installer_plan_required")
    record = _read_private(directory, plan_id + ".json")
    if (
        record.get("id") != plan_id
        or record.get("employee_id") != employee_id
        or record.get("project_id") != project_id
        or record.get("workspace") != workspace
    ):
        raise ValueError("installer_plan_scope_mismatch")
    return record


def _new_plan(record, command, args):
    plan = _plan(record)
    if record.get("replaces"):
        return config.plan_replace(
            plan.before,
            owned_block=base64.b64decode(record["replaced_block"], validate=True),
            command=command,
            args=args,
        )
    return config.plan_install(plan.before, command=command, args=args)


def _plan(record):
    before = record["before"]
    return config.ConfigPlan(
        base64.b64decode(before, validate=True) if before is not None else None,
        base64.b64decode(record["after"], validate=True),
        base64.b64decode(record["owned_block"], validate=True),
    )


def _projection(record, state):
    return {
        "plan_id": record["id"],
        "state": state,
        "employee_id": record["employee_id"],
        "project_id": record["project_id"],
        "session_id": record["session_id"],
        "thread_id": record["thread_id"],
        "workspace": record["workspace"],
        "scope": "workspace_codex_sessions",
        "expires_at": record["expires_at"],
        "connection_verified": False,
        "replaces": record.get("replaces"),
        "recovery_observation": record.get("recovery_observation"),
    }


def _status(record, workspace, directory=None):
    current = config.read_config(workspace)
    if record.get("replacement_id"):
        child = _load_record(
            directory,
            record["replacement_id"],
            record["employee_id"],
            record["project_id"],
            workspace,
        )
        if child.get("replaces") != record["id"]:
            raise ValueError("installer_replacement_record_conflict")
        return (
            "replaced"
            if (
                child["state"] in {"written_waiting_for_host", "removing", "removed"}
                or current == _plan(child).after
            )
            else "outcome_requires_review"
        )
    if record["state"] == "closed_at_original":
        return "closed_at_original" if current == _plan(record).before else "configuration_changed"
    if current == _plan(record).after:
        return "written_waiting_for_host"
    if record.get("state") in {"removing", "removed"}:
        return (
            "removed"
            if current is not None and config._sha(current) == record.get("removed_sha256")
            else "configuration_changed"
        )
    if record["state"] == "prepared":
        if current != _plan(record).before:
            return "configuration_changed"
        return "confirmation_required" if record["expires_at"] >= time.time() else "preview_expired"
    if record["state"] == "written_waiting_for_host" or current != _plan(record).before:
        return "configuration_changed"
    return "outcome_requires_review"


def _recover(directory, record, workspace):
    """Reconcile private records only; never write config or retry execution.

    A matching preimage proves current content, not that no earlier write or
    host load ever happened. A fresh preview/confirmation is always required.
    """
    if record.get("replacement_id"):
        return _projection(record, _status(record, workspace, directory))
    current = config.read_config(workspace)
    plan = _plan(record)
    parent = None
    if record["state"] in {"applying", "closed_at_original"}:
        if record["state"] == "applying" and current == plan.after:
            record.update(
                state="written_waiting_for_host", recovery_observation="current_matches_after"
            )
        elif current == plan.before:
            if record.get("replaces"):
                parent = _load_record(
                    directory,
                    record["replaces"],
                    record["employee_id"],
                    record["project_id"],
                    workspace,
                )
                if (
                    parent.get("replacement_id") not in {None, record["id"]}
                    or parent["owned_block"] != record["replaced_block"]
                ):
                    raise ValueError("installer_replacement_record_conflict")
            record.update(state="closed_at_original", recovery_observation="current_matches_before")
        else:
            return _projection(record, "configuration_changed")
    elif record["state"] == "removing":
        if current is not None and config._sha(current) == record.get("removed_sha256"):
            record.update(state="removed", recovery_observation="current_matches_removed")
        elif record.get("removal_before") is not None and current == base64.b64decode(
            record["removal_before"], validate=True
        ):
            record.update(
                state="written_waiting_for_host",
                recovery_observation="current_matches_removal_before",
            )
        else:
            return _projection(record, "configuration_changed")
    else:
        return _projection(record, _status(record, workspace, directory))
    if config.read_config(workspace) != current:
        raise ValueError("installer_configuration_changed_during_check")
    # End the child first. If clearing the parent fails, a repeat recovery can
    # finish that step, while old configuration actions remain conservatively blocked.
    _write(directory, record["id"] + ".json", record)
    if parent and parent.get("replacement_id") == record["id"]:
        del parent["replacement_id"]
        _write(directory, parent["id"] + ".json", parent)
    return _projection(record, _status(record, workspace, directory))


def installation(
    store,
    employee_id,
    project_id,
    action,
    *,
    thread_id=None,
    session_id=None,
    plan_id=None,
    confirmed=False,
):
    """Owner HTTP only. Writes are scoped, confirmed and never blindly retried."""
    if action not in {
        "preview",
        "preview_replace",
        "replaceable",
        "apply",
        "status",
        "remove",
        "recover",
        "history",
        "verify",
    }:
        raise WorkbenchError("INVALID_REQUEST", "Unknown mailbox installation action.")
    if sys.platform != "darwin":
        raise WorkbenchError("RECEIVER_UNSUPPORTED", "This installer currently requires macOS.")
    try:
        with state_lock(store.root):
            receiver._member(store, employee_id, project_id)
            if (
                action not in {"status", "history", "replaceable"}
                and store.update_maintenance()["paused"]
            ):
                raise WorkbenchError(
                    "UPDATE_PAUSED", "Resume the workbench before changing configuration."
                )
            with store._transaction(readonly=True) as db:
                workspace = str(Path(store._required(db, "projects", project_id)["path"]).resolve())
            directory = store.directory / "codex-installs"
            if action in {"history", "replaceable"}:
                if not directory.exists():
                    return {"plans": []}
                receiver._private(directory)
                plans = []
                for number, entry in enumerate(directory.iterdir()):
                    if number >= 1000:
                        raise ValueError("installer_history_requires_review")
                    if entry.suffix != ".json":
                        continue
                    item = _read_private(directory, entry.name)
                    if (
                        item.get("id") != entry.stem
                        or item.get("employee_id") != employee_id
                        or item.get("project_id") != project_id
                        or (action == "history" and item.get("session_id") != session_id)
                        or item.get("workspace") != workspace
                    ):
                        continue
                    if action == "replaceable":
                        if (
                            item.get("session_id") == session_id
                            or item.get("replacement_id")
                            or item.get("state") not in {"applying", "written_waiting_for_host"}
                        ):
                            continue
                        try:
                            config.plan_remove(
                                config.read_config(workspace), owned_block=_plan(item).owned_block
                            )
                        except (ValueError, TypeError):
                            continue
                    plans.append(_projection(item, _status(item, workspace, directory)))
                return {"plans": sorted(plans, key=lambda p: p["expires_at"], reverse=True)[:50]}
            if action in {"preview", "preview_replace"}:
                command, args = _credential(store, employee_id, project_id, session_id)
                old = None
                if action == "preview_replace":
                    old = _load_record(directory, plan_id, employee_id, project_id, workspace)
                    if (
                        old.get("replacement_id")
                        or old.get("session_id") == session_id
                        or old["state"] not in {"applying", "written_waiting_for_host"}
                    ):
                        raise ValueError("installer_replacement_source_required")
                    plan = config.plan_replace(
                        config.read_config(workspace),
                        owned_block=_plan(old).owned_block,
                        command=command,
                        args=args,
                    )
                inspection = _inspect(store, thread_id, workspace)
                if old:
                    _replacement_scope(inspection, _plan(old).owned_block)
                else:
                    _empty_mailbox_scope(inspection)
                    plan = config.plan_install(
                        config.read_config(workspace), command=command, args=args
                    )
                receiver._private(directory, create=True)
                plan_id = uuid.uuid4().hex
                record = {
                    "id": plan_id,
                    "employee_id": employee_id,
                    "project_id": project_id,
                    "session_id": session_id,
                    "thread_id": thread_id,
                    "workspace": workspace,
                    "expires_at": time.time() + TTL,
                    "state": "prepared",
                    "config_digest": inspection["config_digest"],
                    "before": base64.b64encode(plan.before).decode()
                    if plan.before is not None
                    else None,
                    "after": base64.b64encode(plan.after).decode(),
                    "owned_block": base64.b64encode(plan.owned_block).decode(),
                }
                if old:
                    record.update(replaces=old["id"], replaced_block=old["owned_block"])
                _write(directory, plan_id + ".json", record, new=True)
                return {**_projection(record, "confirmation_required"), "preview": plan.preview()}
            record = _load_record(directory, plan_id, employee_id, project_id, workspace)
            name = plan_id + ".json"
            plan = _plan(record)
            if action == "status":
                return _projection(record, _status(record, workspace, directory))
            if action == "verify":
                if (
                    record.get("session_id") != session_id
                    or record["state"] not in {"applying", "written_waiting_for_host"}
                    or _status(record, workspace, directory) != "written_waiting_for_host"
                ):
                    raise ValueError("installer_written_configuration_required")
                command, args = _credential(store, employee_id, project_id, record["session_id"])
                if _new_plan(record, command, args) != plan:
                    raise ValueError("installer_command_changed")
                _verify(store, record, thread_id, command, args)
                # Current observation only; never persist a permanent green status.
                _credential(store, employee_id, project_id, record["session_id"])
                if _status(record, workspace, directory) != "written_waiting_for_host":
                    raise ValueError("installer_configuration_changed_during_check")
                return {
                    **_projection(record, "written_waiting_for_host"),
                    "connection_verified": True,
                    "verified_thread_id": thread_id,
                    "checked_at": time.time(),
                    "verification_scope": "mailbox_identity_only",
                }
            if confirmed is not True:
                raise ValueError("installer_explicit_confirmation_required")
            if action == "recover":
                return _recover(directory, record, workspace)
            if record.get("replacement_id"):
                return _projection(record, _status(record, workspace, directory))
            if action == "remove":
                if record["state"] not in {"applying", "written_waiting_for_host"}:
                    return _projection(record, _status(record, workspace, directory))
                removal = config.plan_remove(
                    config.read_config(workspace), owned_block=plan.owned_block
                )
                record.update(
                    state="removing",
                    removed_sha256=hashlib.sha256(removal.after).hexdigest(),
                    removal_before=base64.b64encode(removal.before).decode(),
                )
                _write(directory, name, record)
                config.apply_plan(workspace, removal, journal_directory=directory)
                record["state"] = "removed"
                _write(directory, name, record)
                return _projection(record, "removed")
            if record["state"] != "prepared":
                return _projection(record, _status(record, workspace, directory))
            if record["expires_at"] < time.time():
                raise ValueError("installer_preview_expired")
            command, args = _credential(store, employee_id, project_id, record["session_id"])
            # A changed binary path or session file must get a fresh preview.
            if _new_plan(record, command, args) != plan:
                raise ValueError("installer_command_changed")
            inspection = _inspect(store, record["thread_id"], workspace)
            old = None
            if record.get("replaces"):
                old = _load_record(
                    directory, record["replaces"], employee_id, project_id, workspace
                )
                if (
                    old.get("replacement_id")
                    or old["state"] not in {"applying", "written_waiting_for_host"}
                    or old["owned_block"] != record["replaced_block"]
                ):
                    raise ValueError("installer_replacement_source_required")
                _replacement_scope(inspection, _plan(old).owned_block)
            else:
                _empty_mailbox_scope(inspection)
            if inspection["config_digest"] != record["config_digest"]:
                raise ValueError("installer_host_config_changed")
            receiver._member(store, employee_id, project_id)
            if _credential(store, employee_id, project_id, record["session_id"]) != (command, args):
                raise ValueError("installer_command_changed")
            record["state"] = "applying"
            _write(directory, name, record)
            if old:
                # Reserve the old record before the one disk write. Unknown outcomes
                # block further replacement/removal until independently reconciled.
                old["replacement_id"] = record["id"]
                _write(directory, old["id"] + ".json", old)
            receipt = config.apply_plan(workspace, plan, journal_directory=directory)
            record.update(
                state="written_waiting_for_host", transaction_id=receipt["transaction_id"]
            )
            _write(directory, name, record)
            return _projection(record, "written_waiting_for_host")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Never expose TOML, credential JSON, command arguments or OS exception text.
        reason = (
            str(exc)
            if isinstance(exc, ValueError)
            and str(exc).startswith(
                ("installer_", "codex_", "receiver_", "private_receiver_", "invalid_receiver_")
            )
            else "installer_operation_unconfirmed"
        )
        raise WorkbenchError("MAILBOX_INSTALL_UNAVAILABLE", reason) from None
