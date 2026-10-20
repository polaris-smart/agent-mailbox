"""First-install preparation, run with the selected Hermes profile's interpreter.

Internal adapter, not a browser API. The workbench must authorize the employee,
project, exported credential and selected profile before calling it. Preparation
does not enable/reload a plugin, bind a route, send mail or assert tool readiness.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import stat
import time
import uuid
from pathlib import Path
from typing import ClassVar

PLUGIN = "agent-mailbox-host"
PLUGIN_KEY = "plugins.entries." + PLUGIN
MCP_KEY = "mcp_servers.agent-mailbox"
FILES = ("__init__.py", "pinned.py", "live.py", "private_platform.py", "plugin.yaml", "README.md")
TTL = 600


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def directory(path, *, private=True, create=False):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("canonical_absolute_directory_required")
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid not in ({os.getuid()} if private else {os.getuid(), 0})
        or info.st_mode & (0o077 if private else 0o022)
    ):
        raise ValueError("unsafe_directory")
    return path


def read_file(path, *, private=True, limit=1024 * 1024):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid not in ({os.getuid()} if private else {os.getuid(), 0})
            or info.st_size > limit
            or info.st_mode & (0o077 if private else 0o022)
        ):
            raise ValueError("unsafe_file")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("file_too_large")
        return data


def write_new(path, raw):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def item(value, dotted):
    for key in dotted.split("."):
        if not isinstance(value, dict):
            raise ValueError("configuration_shape_conflict")  # noqa: TRY004 - invalid persisted config
        value = value.get(key, {})
    return value


def contains(value, dotted):
    for key in dotted.split("."):
        if not isinstance(value, dict):
            raise ValueError("configuration_shape_conflict")  # noqa: TRY004 - invalid persisted config
        if key not in value:
            return False
        value = value[key]
    return True


def without_entries(value, keys=(PLUGIN_KEY, MCP_KEY)):
    value = copy.deepcopy(value)
    for dotted in keys:
        keys = dotted.split(".")
        current = value
        chain = []
        for key in keys[:-1]:
            child = current.get(key)
            if not isinstance(child, dict):
                break
            chain.append((current, key))
            current = child
        else:
            current.pop(keys[-1], None)
            for parent, key in reversed(chain):
                if parent[key]:
                    break
                del parent[key]
    return value


class Preparation:
    def __init__(self, profile, *, source=None, api=None):
        self.profile = directory(profile)
        if api is None:
            from types import SimpleNamespace

            from hermes_cli import config, managed_scope

            api = SimpleNamespace(config=config, managed=managed_scope)
        self.api = api
        self.config = self.profile / "config.yaml"
        if Path(api.config.get_config_path()).resolve() != self.config:
            raise ValueError("host_profile_mismatch")
        self.source = Path(source or Path(__file__).resolve().parent)
        self.records = self.profile / "mailbox-install-plans"
        self.plugins = self.profile / "plugins"
        self.target = self.plugins / PLUGIN

    def snapshot(self):
        before = read_file(self.config)
        value = self.api.config.require_readable_config_before_write(self.config)
        if not isinstance(value, dict) or read_file(self.config) != before:
            raise ValueError("configuration_changed_during_read")
        return before, value

    def writable(self, entries):
        if self.api.config.is_managed():
            raise ValueError("host_configuration_managed")

        def leaves(prefix, value):
            yield prefix
            if isinstance(value, dict):
                for key, child in value.items():
                    yield from leaves(prefix + "." + key, child)

        for key, value in entries.items():
            if any(self.api.managed.is_key_managed(p) for p in leaves(key, value)):
                raise ValueError("host_configuration_managed")
            if any(
                p == key or p.startswith(key + ".") or key.startswith(p + ".")
                for p in self.api.managed.managed_config_keys()
            ):
                raise ValueError("host_configuration_managed")

    def source_files(self):
        directory(self.source, private=False)
        return {name: read_file(self.source / name, private=False) for name in FILES}

    def new_socket(self, socket_path):
        socket_path = Path(socket_path)
        directory(socket_path.parent)
        if (
            socket_path.resolve() != socket_path
            or len(str(socket_path).encode()) > 100
            or socket_path.exists()
            or socket_path.is_symlink()
        ):
            raise ValueError("new_private_socket_path_required")
        return str(socket_path)

    def preview_discovery(self, *, socket_path):
        """Prepare metadata-only first contact, without credentials or grants.

        The plugin is still disabled. Admission/activation is a distinct owner
        operation; this method cannot reload a Gateway or enable other plugins.
        """
        return self._preview({PLUGIN_KEY: {
            "allow_gateway_injection": False,
            "settings": {"discovery_only": True, "socket_path": self.new_socket(socket_path)},
        }}, {})

    def preview(self, *, mailbox_home, state_dir, socket_path, sessions, command, session_file):
        home = directory(mailbox_home)
        state = directory(state_dir)
        socket_path = self.new_socket(socket_path)
        command = Path(command)
        session_file = Path(session_file)
        if (
            not command.is_absolute()
            or command.resolve() != command
            or not os.access(command, os.X_OK)
        ):
            raise ValueError("fixed_executable_required")
        read_file(command, private=False, limit=256 * 1024 * 1024)
        directory(session_file.parent)
        read_file(session_file)
        if session_file.resolve() != session_file:
            raise ValueError("canonical_session_file_required")
        if not isinstance(sessions, dict) or not 1 <= len(sessions) <= 32:
            raise ValueError("explicit_existing_sessions_required")
        for key, value in sessions.items():
            if (
                not isinstance(key, str)
                or not 1 <= len(key) <= 512
                or any(ord(c) < 32 for c in key)
                or not isinstance(value, dict)
                or set(value) != {"session_id", "platform"}
                or any(
                    not isinstance(v, str) or not 1 <= len(v) <= 512 or any(ord(c) < 32 for c in v)
                    for v in value.values()
                )
                or value["platform"].lower() in {"cli", "tui", "desktop"}
            ):
                raise ValueError("invalid_gateway_session_pin")
        entries = {
            PLUGIN_KEY: {
                "allow_gateway_injection": True,
                "settings": {
                    "home": str(home),
                    "state_dir": str(state),
                    "socket_path": str(socket_path),
                    "sessions": sessions,
                },
            },
            MCP_KEY: {
                "command": str(command),
                "args": ["mailbox-mcp", "--session-file", str(session_file)],
            },
        }
        return self._preview(entries, {
            "session_file_sha256": digest(read_file(session_file)),
            "command_sha256": digest(read_file(command, private=False, limit=256 * 1024 * 1024)),
        })

    def _preview(self, entries, runtime):
        self.writable(entries)
        raw, config = self.snapshot()
        if self.target.exists() or self.target.is_symlink():
            raise ValueError("existing_plugin_requires_review")
        plugin_entries = item(config, "plugins.entries")
        servers = item(config, "mcp_servers")
        enabled = item(config, "plugins").get("enabled", [])
        if (
            not isinstance(plugin_entries, dict)
            or not isinstance(servers, dict)
            or not isinstance(enabled, list)
            or any(not isinstance(v, str) for v in enabled)
        ):
            raise ValueError("configuration_shape_conflict")
        if PLUGIN in plugin_entries or "agent-mailbox" in servers:
            raise ValueError("existing_configuration_requires_review")
        if PLUGIN in enabled:
            raise ValueError("stale_enabled_plugin_requires_review")
        for row in servers.values():
            if not isinstance(row, dict):
                raise ValueError("configuration_shape_conflict")  # noqa: TRY004 - persisted config error
            args = row.get("args", [])
            command_value = row.get("command", "")
            if (
                not isinstance(args, list)
                or any(not isinstance(v, str) for v in args)
                or not isinstance(command_value, str)
            ):
                raise ValueError("configuration_shape_conflict")
            if "mailbox-mcp" in args or "agent-mailbox" in command_value:
                raise ValueError("existing_mailbox_requires_review")
        files = self.source_files()
        directory(self.records, create=True)
        name = "plan_" + uuid.uuid4().hex
        folder = directory(self.records / name, create=True)
        plan = {
            "version": 1,
            "id": name,
            "profile": str(self.profile),
            "expires_at": time.time() + TTL,
            "before_sha256": digest(raw),
            "entries": entries,
            "source_sha256": {n: digest(v) for n, v in files.items()},
            **runtime,
        }
        write_new(folder / "config.before", raw)
        write_new(folder / "plan.json", json.dumps(plan).encode())
        return {
            "plan_id": name,
            "state": "preview",
            "requires_confirmation": True,
            "will_activate": False,
            "will_write_injection_grant": entries[PLUGIN_KEY]["allow_gateway_injection"],
            "session_verified": False,
            "delivery_verified": False,
        }

    def load(self, name):
        if (
            not isinstance(name, str)
            or len(name) != 37
            or not name.startswith("plan_")
            or any(c not in "0123456789abcdef" for c in name[5:])
        ):
            raise ValueError("invalid_plan_id")
        directory(self.records)
        folder = directory(self.records / name)
        plan = json.loads(read_file(folder / "plan.json"))
        if plan["version"] != 1 or plan["id"] != name or plan["profile"] != str(self.profile):
            raise ValueError("plan_scope_mismatch")
        return folder, plan

    @contextlib.contextmanager
    def operation(self):
        import fcntl

        directory(self.records)
        fd = os.open(
            self.records / ".operation.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise ValueError("unsafe_operation_lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("preparation_operation_busy") from None
            yield
        finally:
            os.close(fd)

    def apply(self, name, *, confirmed=False):
        with self.operation():
            return self._apply(name, confirmed=confirmed)

    def _apply(self, name, *, confirmed=False):
        if confirmed is not True:
            raise ValueError("explicit_confirmation_required")
        folder, plan = self.load(name)
        if (folder / "removed.json").exists():
            raise ValueError("plan_closed")
        if time.time() > plan["expires_at"]:
            raise ValueError("plan_expired")
        self.writable(plan["entries"])
        raw, initial = self.snapshot()
        if digest(raw) != plan["before_sha256"]:
            raise ValueError("configuration_changed_refresh_required")
        files = self.source_files()
        if {n: digest(v) for n, v in files.items()} != plan["source_sha256"]:
            raise ValueError("component_changed_refresh_required")
        if MCP_KEY in plan["entries"]:
            mcp = plan["entries"][MCP_KEY]
            if (
                digest(read_file(Path(mcp["args"][2]))) != plan["session_file_sha256"]
                or digest(read_file(Path(mcp["command"]), private=False, limit=256 * 1024 * 1024))
                != plan["command_sha256"]
            ):
                raise ValueError("mailbox_runtime_changed_refresh_required")
        else:
            entry = plan["entries"][PLUGIN_KEY]
            settings = entry.get("settings", {})
            if (set(plan["entries"]) != {PLUGIN_KEY}
                    or set(entry) != {"allow_gateway_injection", "settings"}
                    or entry["allow_gateway_injection"] is not False
                    or set(settings) != {"discovery_only", "socket_path"}
                    or settings["discovery_only"] is not True):
                raise ValueError("discovery_plan_permissions_changed")
        # Recheck even when the preview is still fresh: never prepare over an
        # endpoint that appeared since confirmation was requested.
        self.new_socket(plan["entries"][PLUGIN_KEY]["settings"]["socket_path"])
        # Exclusive claim means interruption/unknown is inspected, never blindly retried.
        write_new(folder / "started.json", json.dumps({"started_at": time.time()}).encode())
        try:
            directory(self.plugins, private=False, create=True)
            if self.target.exists() or self.target.is_symlink():
                raise ValueError("existing_plugin_requires_review")
            staging = directory(folder / "component", create=True)
            for n, v in files.items():
                write_new(staging / n, v)
            # mkdir reserves the name without overwriting a concurrent installation.
            self.target.mkdir(mode=0o700)
            sync_directory(self.plugins)
            info = self.target.lstat()
            write_new(
                folder / "publication.json",
                json.dumps({"device": info.st_dev, "inode": info.st_ino}).encode(),
            )
            for n in FILES:
                os.rename(staging / n, self.target / n)
            sync_directory(self.target)
            sync_directory(self.plugins)
            expected = digest(raw)
            for index, (key, value) in enumerate(plan["entries"].items()):
                if digest(read_file(self.config)) != expected:
                    raise ValueError("configuration_changed_during_prepare")
                self.writable({key: value})
                write_new(
                    folder / f"intent-{index}.json",
                    json.dumps({"key": key, "before_sha256": expected}).encode(),
                )
                # Use the official raw-config writer; suppress config/path output.
                with (
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    self.api.config.set_config_value(key, json.dumps(value))
                after, current = self.snapshot()
                if item(current, key) != value or without_entries(current, plan["entries"]) != without_entries(
                    initial, plan["entries"]
                ):
                    raise ValueError("host_write_unconfirmed")
                expected = digest(after)
                write_new(
                    folder / f"step-{index}.json",
                    json.dumps({"key": key, "after_sha256": expected}).encode(),
                )
            write_new(folder / "prepared.json", json.dumps({"after_sha256": expected}).encode())
        except (Exception, SystemExit):  # noqa: BLE001 - redact host errors; retain private recovery record
            # Do not restore a whole old config or erase evidence on partial failure.
            raise ValueError("preparation_incomplete_inspect_record") from None
        return self.status(name)

    def remove(self, name, *, confirmed=False):
        """Undo this unactivated preparation, never whole-config restoration.

        A changed enabled selection or other unrelated config refuses cleanup.
        The workbench must revoke any separately activated mailbox binding first.
        """
        with self.operation():
            if confirmed is not True:
                raise ValueError("explicit_confirmation_required")
            folder, plan = self.load(name)
            if (folder / "removed.json").exists():
                return self.status(name)
            if not (folder / "started.json").exists():
                write_new(folder / "removed.json", b'{"cancelled_before_apply":true}')
                return self.status(name)
            self.writable(plan["entries"])
            backup = read_file(folder / "config.before")
            if digest(backup) != plan["before_sha256"]:
                raise ValueError("backup_changed")
            original = self.api.config.require_readable_config_before_write(
                folder / "config.before"
            )
            raw, current = self.snapshot()
            if without_entries(current, plan["entries"]) != without_entries(original, plan["entries"]):
                raise ValueError("unrelated_configuration_changed")
            for index, (key, value) in enumerate(plan["entries"].items()):
                observed = item(current, key)
                if contains(current, key) and observed != value:
                    raise ValueError("owned_configuration_changed")
                if contains(current, key):
                    try:
                        intent = json.loads(read_file(folder / f"intent-{index}.json"))
                    except FileNotFoundError:
                        raise ValueError("configuration_ownership_unconfirmed") from None
                    if intent.get("key") != key:
                        raise ValueError("configuration_ownership_unconfirmed")
            # Refuse unfamiliar files/edits; missing members are allowed after a
            # partial publication or interrupted removal, never recreated here.
            if self.target.exists() or self.target.is_symlink():
                directory(self.target)
                try:
                    publication = json.loads(read_file(folder / "publication.json"))
                except FileNotFoundError:
                    raise ValueError("component_ownership_unconfirmed") from None
                info = self.target.lstat()
                if publication != {"device": info.st_dev, "inode": info.st_ino}:
                    raise ValueError("component_ownership_unconfirmed")
                for child in self.target.iterdir():
                    if (
                        child.name not in plan["source_sha256"]
                        or digest(read_file(child, private=False))
                        != plan["source_sha256"][child.name]
                    ):
                        raise ValueError("installed_component_changed")
            expected = digest(raw)
            for key in reversed(plan["entries"]):
                if not contains(current, key):
                    continue
                if digest(read_file(self.config)) != expected:
                    raise ValueError("configuration_changed_during_remove")
                with (
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    self.api.config.unset_config_value(key)
                after, current = self.snapshot()
                if (contains(current, key)
                        or without_entries(current, plan["entries"]) != without_entries(original, plan["entries"])):
                    raise ValueError("removal_unconfirmed")
                expected = digest(after)
            if self.target.exists():
                for child in self.target.iterdir():
                    if (
                        child.name not in plan["source_sha256"]
                        or digest(read_file(child, private=False))
                        != plan["source_sha256"][child.name]
                    ):
                        raise ValueError("installed_component_changed")
                for child in self.target.iterdir():
                    child.unlink()
                self.target.rmdir()
                sync_directory(self.plugins)
            write_new(folder / "removed.json", json.dumps({"after_sha256": expected}).encode())
            return self.status(name)

    def status(self, name):
        folder, plan = self.load(name)
        ready = folder / "prepared.json"
        state = "preparation_incomplete" if (folder / "started.json").exists() else "preview"
        if (folder / "removed.json").exists():
            state = "preparation_removed"
        elif ready.exists():
            recorded = json.loads(read_file(ready))
            try:
                same = digest(read_file(self.config)) == recorded["after_sha256"]
                same = same and all(
                    digest(read_file(self.target / n, private=False)) == h
                    for n, h in plan["source_sha256"].items()
                )
            except (OSError, ValueError):
                same = False
            state = "prepared_not_activated" if same else "configuration_changed"
        return {
            "plan_id": name,
            "state": state,
            "activation_requested": False,
            "session_verified": False,
            "delivery_verified": False,
        }


class DiagnosticAdmission:
    """Admit a prepared diagnostic plugin through Hermes PM, without host reload.

    This lower-level transaction is deliberately separate from Preparation. The
    caller must prove a stopped Gateway and obtain Owner confirmation; even a
    successful PM publication only makes the plugin eligible on the next start.
    """

    def __init__(self, profile, *, api=None, source=None, admit=None):
        self.preparation = Preparation(profile, api=api, source=source)
        self.records = self.preparation.profile / "mailbox-diagnostic-admissions"
        self.admit = admit

    def _scope(self, preparation_id):
        if self.preparation.status(preparation_id)["state"] != "prepared_not_activated":
            raise ValueError("diagnostic_preparation_changed")
        folder, prepared = self.preparation.load(preparation_id)
        entry = prepared["entries"].get(PLUGIN_KEY)
        if (set(prepared["entries"]) != {PLUGIN_KEY}
                or not isinstance(entry, dict)
                or entry.get("allow_gateway_injection") is not False
                or not isinstance(entry.get("settings"), dict)
                or entry["settings"].get("discovery_only") is not True):
            raise ValueError("diagnostic_preparation_required")
        before, config = self.preparation.snapshot()
        plugins = item(config, "plugins")
        if not isinstance(plugins, dict):
            raise ValueError("configuration_shape_conflict")  # noqa: TRY004 - persisted config shape
        enabled, disabled = plugins.get("enabled", []), plugins.get("disabled", [])
        if (not isinstance(enabled, list) or not isinstance(disabled, list)
                or any(not isinstance(name, str) for name in enabled + disabled)
                or PLUGIN in enabled or PLUGIN in disabled):
            raise ValueError("plugin_selection_requires_review")
        if item(config, PLUGIN_KEY) != entry:
            raise ValueError("diagnostic_configuration_changed")
        socket_path = Path(entry["settings"]["socket_path"])
        if socket_path.exists() or socket_path.is_symlink():
            raise ValueError("diagnostic_socket_already_present")
        return before, config, enabled, disabled, folder

    def preview(self, preparation_id):
        before, config, enabled, disabled, _ = self._scope(preparation_id)
        self.preparation.writable({"plugins.enabled": enabled + [PLUGIN],
                                   "plugins.disabled": disabled})
        directory(self.records, create=True)
        name = "plan_" + uuid.uuid4().hex
        folder = directory(self.records / name, create=True)
        plan = {
            "version": 1, "id": name, "profile": str(self.preparation.profile),
            "preparation_id": preparation_id, "config_sha256": digest(before),
            "enabled_before": enabled, "disabled_before": disabled,
            "enabled_was_present": contains(config, "plugins.enabled"),
            "disabled_was_present": contains(config, "plugins.disabled"),
            "expires_at": time.time() + TTL,
        }
        write_new(folder / "config.before", before)
        write_new(folder / "plan.json", json.dumps(plan, sort_keys=True).encode())
        return self.status(name)

    def _plan(self, name):
        if (not isinstance(name, str) or len(name) != 37 or not name.startswith("plan_")
                or any(c not in "0123456789abcdef" for c in name[5:])):
            raise ValueError("invalid_plan_id")
        folder = directory(self.records / name)
        plan = json.loads(read_file(folder / "plan.json"))
        if (plan.get("version") != 1 or plan.get("id") != name
                or plan.get("profile") != str(self.preparation.profile)):
            raise ValueError("plan_scope_mismatch")
        return folder, plan

    def _admit(self, enabled, disabled, expected):
        if self.admit is None:
            from hermes_cli.plugins_admission import admit_plugin_set_change

            admit = admit_plugin_set_change
        else:
            admit = self.admit
        admit(set(enabled), set(disabled), active_plugins_dir=self.preparation.plugins,
              expected_config=digest(expected), plugin=PLUGIN)

    def apply(self, name, *, confirmed=False, gateway_stopped=False):
        if confirmed is not True or gateway_stopped is not True:
            raise ValueError("explicit_stopped_gateway_confirmation_required")
        with self.preparation.operation():
            folder, plan = self._plan(name)
            if (folder / "intent.json").exists():
                raise ValueError("admission_started_inspect_status")
            if time.time() > plan["expires_at"]:
                raise ValueError("plan_expired")
            before, config, enabled, disabled, _ = self._scope(plan["preparation_id"])
            if (digest(before) != plan["config_sha256"]
                    or enabled != plan["enabled_before"]
                    or disabled != plan["disabled_before"]):
                raise ValueError("configuration_changed_refresh_required")
            self.preparation.writable({"plugins.enabled": enabled + [PLUGIN],
                                       "plugins.disabled": disabled})
            write_new(folder / "intent.json", json.dumps({"before_sha256": digest(before)}).encode())
            try:
                with (contextlib.redirect_stdout(io.StringIO()),
                      contextlib.redirect_stderr(io.StringIO())):
                    self._admit(enabled + [PLUGIN], disabled, before)
                after, current = self.preparation.snapshot()
                new_enabled = item(current, "plugins.enabled")
                new_disabled = item(current, "plugins.disabled")
                if (not isinstance(new_enabled, list) or not isinstance(new_disabled, list)
                        or set(new_enabled) != set(enabled + [PLUGIN])
                        or set(new_disabled) != set(disabled)
                        or without_entries(current, ("plugins.enabled", "plugins.disabled"))
                        != without_entries(config, ("plugins.enabled", "plugins.disabled"))):
                    raise ValueError("admission_result_unconfirmed")
                write_new(folder / "published.json", json.dumps({"after_sha256": digest(after)}).encode())
            except (Exception, SystemExit):  # noqa: BLE001 - an unknown PM result must not be retried
                raise ValueError("admission_unconfirmed_inspect_record") from None
            return self.status(name)

    def rollback(self, name, *, confirmed=False, gateway_stopped=False):
        """Undo only this admission after a separately proven stopped Gateway."""
        if confirmed is not True or gateway_stopped is not True:
            raise ValueError("explicit_stopped_gateway_confirmation_required")
        with self.preparation.operation():
            folder, plan = self._plan(name)
            if (folder / "rollback-intent.json").exists():
                raise ValueError("rollback_started_inspect_status")
            if self.status(name)["state"] != "diagnostic_enabled_restart_required":
                raise ValueError("admission_state_requires_review")
            original_raw = read_file(folder / "config.before")
            if digest(original_raw) != plan["config_sha256"]:
                raise ValueError("admission_backup_changed")
            original = self.preparation.api.config.require_readable_config_before_write(
                folder / "config.before")
            before, current = self.preparation.snapshot()
            if (without_entries(current, ("plugins.enabled", "plugins.disabled"))
                    != without_entries(original, ("plugins.enabled", "plugins.disabled"))):
                raise ValueError("unrelated_configuration_changed")
            entry = item(current, PLUGIN_KEY)
            socket_path = Path(entry["settings"]["socket_path"])
            if socket_path.exists() or socket_path.is_symlink():
                raise ValueError("gateway_receiver_still_present")
            self.preparation.writable({"plugins.enabled": plan["enabled_before"],
                                       "plugins.disabled": plan["disabled_before"]})
            write_new(folder / "rollback-intent.json", json.dumps({
                "before_sha256": digest(before),
            }).encode())
            try:
                with (contextlib.redirect_stdout(io.StringIO()),
                      contextlib.redirect_stderr(io.StringIO())):
                    self._admit(plan["enabled_before"], plan["disabled_before"], before)
                after, current = self.preparation.snapshot()
                if (not isinstance(item(current, "plugins.enabled"), list)
                        or not isinstance(item(current, "plugins.disabled"), list)
                        or set(item(current, "plugins.enabled")) != set(plan["enabled_before"])
                        or set(item(current, "plugins.disabled")) != set(plan["disabled_before"])
                        or without_entries(current, ("plugins.enabled", "plugins.disabled"))
                        != without_entries(original, ("plugins.enabled", "plugins.disabled"))):
                    raise ValueError("admission_rollback_unconfirmed")
                # PM publishes a normalized selection. Restore key presence and
                # original list order through Hermes' own config writer, with a
                # byte-level CAS before each narrow cleanup write.
                expected = digest(after)
                for key, was_present, prior in (
                    ("plugins.enabled", plan["enabled_was_present"], plan["enabled_before"]),
                    ("plugins.disabled", plan["disabled_was_present"], plan["disabled_before"]),
                ):
                    if digest(read_file(self.preparation.config)) != expected:
                        raise ValueError("configuration_changed_during_rollback")
                    with (contextlib.redirect_stdout(io.StringIO()),
                          contextlib.redirect_stderr(io.StringIO())):
                        if was_present:
                            if item(current, key) != prior:
                                self.preparation.api.config.set_config_value(key, json.dumps(prior))
                        else:
                            self.preparation.api.config.unset_config_value(key)
                    after, current = self.preparation.snapshot()
                    if (contains(current, key) != was_present
                            or (was_present and item(current, key) != prior)):
                        raise ValueError("admission_rollback_unconfirmed")
                    expected = digest(after)
                if current != original:
                    raise ValueError("admission_rollback_unconfirmed")
                write_new(folder / "rolled_back.json", json.dumps({"after_sha256": expected}).encode())
            except (Exception, SystemExit):  # noqa: BLE001 - no blind retry after PM or config writes
                raise ValueError("admission_rollback_unknown_inspect_record") from None
            return self.status(name)

    def status(self, name):
        folder, plan = self._plan(name)
        if not (folder / "intent.json").exists():
            try:
                raw, _config, _enabled, _disabled, _folder = self._scope(
                    plan["preparation_id"])
                state = "preview" if digest(raw) == plan["config_sha256"] else "inspection_required"
            except (OSError, ValueError, KeyError, TypeError):
                state = "inspection_required"
        elif (folder / "rollback-intent.json").exists() and not (folder / "rolled_back.json").exists():
            state = "rollback_unknown"
        elif not (folder / "published.json").exists():
            state = "admission_unknown"
        else:
            try:
                recorded = json.loads(read_file(folder / "published.json"))
                raw, config = self.preparation.snapshot()
                enabled = item(config, "plugins.enabled")
                entry = item(config, PLUGIN_KEY)
                _prepared_folder, prepared = self.preparation.load(plan["preparation_id"])
                component_matches = all(
                    digest(read_file(self.preparation.target / file_name, private=False)) == sha
                    for file_name, sha in prepared["source_sha256"].items()
                )
                if (folder / "rolled_back.json").exists():
                    rollback = json.loads(read_file(folder / "rolled_back.json"))
                    original = self.preparation.api.config.require_readable_config_before_write(
                        folder / "config.before")
                    state = ("rolled_back" if digest(raw) == rollback["after_sha256"]
                             and config == original and component_matches else "inspection_required")
                else:
                    state = ("diagnostic_enabled_restart_required"
                             if digest(raw) == recorded["after_sha256"]
                             and component_matches
                             and isinstance(enabled, list) and PLUGIN in enabled
                             and entry["allow_gateway_injection"] is False
                             and entry["settings"]["discovery_only"] is True
                             else "inspection_required")
            except (OSError, ValueError, KeyError, TypeError):
                state = "inspection_required"
        return {"plan_id": name, "state": state,
                "requires_confirmation": True, "requires_stopped_gateway": True,
                "runtime_verified": False, "session_verified": False,
                "delivery_verified": False}


class Replacement:
    """Replace only the owned Hermes component after its receiver has stopped.

    This transaction does not edit config or reload Gateway. The old directory
    stays in a private recovery record until a separate runtime acceptance.
    """

    LEGACY_INIT = "d5ea9dc4449225564eecbc291f5faf3567447e73d7e69d35c3f57edb2842d1b7"
    LEGACY_FILES = frozenset({"__init__.py", "plugin.yaml", "README.md"})
    # Exact five-file predecessor installed before session reauthorization was
    # added.  Match every file, not merely __init__.py: an owner must never
    # replace an unknown or locally edited host component.
    PRE_REAUTH: ClassVar[dict[str, str]] = {
        "__init__.py": "201d5c58fb523130ba608e28a87313b9d54428905bf09dc98666eb20be7cabaa",
        "pinned.py": "9defd711f258dce60c55f018157369a6c2f45e9546ab66a0c478894bec6cce23",
        "live.py": "b59e7eec89420dfed63fed9ceb0dcafab45f84931d94b83ce3ac507341424657",
        "plugin.yaml": "6f7ee82a4d6372138465f0ba5adf1f8789597da4c0d44d0d5341363e543d0a6f",
        "README.md": "01313974c4b75549c576a667c70368db1e53e21f85b0cccbe2b07b9b338b9429",
    }
    # Exact five-file component in the installed 0.8.2 App. Unknown local edits
    # remain ineligible for automated replacement.
    PRE_PRIVATE_082: ClassVar[dict[str, str]] = {
        "__init__.py": "c2498df076a7a4061f9a6bfdde786e8664e232beba5865f7e82d222f73322c9d",
        "pinned.py": "c6a5b280ee7b3bde2dcc85d9f87ed4261b4a1301c718ed79a525a5343214a988",
        "live.py": "b59e7eec89420dfed63fed9ceb0dcafab45f84931d94b83ce3ac507341424657",
        "plugin.yaml": "6f7ee82a4d6372138465f0ba5adf1f8789597da4c0d44d0d5341363e543d0a6f",
        "README.md": "01313974c4b75549c576a667c70368db1e53e21f85b0cccbe2b07b9b338b9429",
    }
    # Exact installed six-file component before the native-adapter identity fix.
    # Restrict replacement to this measured generation; local edits fail closed.
    PRE_AUTHORITY_WRAPPER: ClassVar[dict[str, str]] = {
        "__init__.py": "d162cc58209dc524d4ef55a251471488fb37d31c680b118a1922af4fbc7c2650",
        "pinned.py": "7e9c626ceabf406595b9255400d4421f5b4f7627b7178915c74e934846c145d5",
        "live.py": "2fed9dd63d3165b47b53dc3b240e6e4363a292850c53ac1679599bc2e2cf33f0",
        "private_platform.py": "552e9142298f040991ae690f8fb68aff26a6c573d06450a03e8a5b8348bf4bf5",
        "plugin.yaml": "6f7ee82a4d6372138465f0ba5adf1f8789597da4c0d44d0d5341363e543d0a6f",
        "README.md": "01313974c4b75549c576a667c70368db1e53e21f85b0cccbe2b07b9b338b9429",
    }
    # The exact six-file installation after the admission pin was updated to
    # the session-authority SDK while its read-only metadata reader remained
    # on the previous pin. Only this measured mixed installation is eligible.
    PRE_AUTHORITY_METADATA: ClassVar[dict[str, str]] = {
        **PRE_AUTHORITY_WRAPPER,
        "pinned.py": "54004ff871f89411bec183da0e26b8495f3f68d206b28e5666ed6490fdd87b02",
    }

    @staticmethod
    def _identity(path):
        info = Path(path).lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("component_directory_changed")
        return {"device": info.st_dev, "inode": info.st_ino}

    def __init__(self, profile, *, source=None, api=None):
        self.preparation = Preparation(profile, source=source, api=api)
        self.profile = self.preparation.profile
        self.target = self.preparation.target
        self.plugins = self.preparation.plugins
        self.records = self.profile / "mailbox-upgrade-plans"

    def _installed(self, path=None):
        path = directory(path or self.target)
        names = {child.name for child in path.iterdir()}
        if "__pycache__" in names:
            directory(path / "__pycache__", private=False)
            names.remove("__pycache__")
        if names not in (self.LEGACY_FILES, frozenset(self.PRE_REAUTH), frozenset(FILES)):
            raise ValueError("installed_component_shape_unrecognized")
        return {name: digest(read_file(path / name, private=False)) for name in names}

    def _scope(self):
        before, config = self.preparation.snapshot()
        entry = item(config, PLUGIN_KEY)
        if (not isinstance(entry, dict)
                or entry.get("allow_gateway_injection") is not True
                or not isinstance(entry.get("settings"), dict)
                or entry["settings"].get("discovery_only") is True):
            raise ValueError("active_hermes_component_required")
        socket_path = entry["settings"].get("socket_path")
        if (not isinstance(socket_path, str) or not Path(socket_path).is_absolute()
                or Path(socket_path).resolve() != Path(socket_path)):
            raise ValueError("canonical_receiver_socket_required")
        return before, socket_path

    def preview(self):
        before, socket_path = self._scope()
        old = self._installed()
        legacy = (set(old) == self.LEGACY_FILES
                  and old.get("__init__.py") == self.LEGACY_INIT
                  and old.get("plugin.yaml") == digest(self.preparation.source_files()["plugin.yaml"]))
        if not legacy and old not in (self.PRE_REAUTH, self.PRE_PRIVATE_082,
                                      self.PRE_AUTHORITY_WRAPPER, self.PRE_AUTHORITY_METADATA):
            raise ValueError("unrecognized_hermes_component_version")
        new = {name: digest(raw) for name, raw in self.preparation.source_files().items()}
        directory(self.records, create=True)
        name = "plan_" + uuid.uuid4().hex
        folder = directory(self.records / name, create=True)
        plan = {
            "version": 1, "id": name, "profile": str(self.profile),
            "expires_at": time.time() + TTL, "config_sha256": digest(before),
            "old_sha256": old, "old_identity": self._identity(self.target),
            "new_sha256": new, "socket_path": socket_path,
        }
        write_new(folder / "plan.json", json.dumps(plan, sort_keys=True).encode())
        return {"plan_id": name, "state": "preview", "requires_confirmation": True,
                "requires_stopped_gateway": True, "will_write_config": False,
                "will_reload_gateway": False, "session_verified": False,
                "delivery_verified": False}

    def _plan(self, name):
        if (not isinstance(name, str) or len(name) != 37 or not name.startswith("plan_")
                or any(c not in "0123456789abcdef" for c in name[5:])):
            raise ValueError("invalid_plan_id")
        folder = directory(self.records / name)
        plan = json.loads(read_file(folder / "plan.json"))
        if (plan.get("version") != 1 or plan.get("id") != name
                or plan.get("profile") != str(self.profile)):
            raise ValueError("plan_scope_mismatch")
        return folder, plan

    def _stopped(self, plan):
        # A clean host unload removes its socket. This is a necessary condition,
        # not independent proof of a stopped Gateway; the owner must confirm it.
        path = Path(plan["socket_path"])
        if path.exists() or path.is_symlink():
            raise ValueError("gateway_receiver_still_present")

    def apply(self, name, *, confirmed=False, gateway_stopped=False):
        directory(self.preparation.records, create=True)
        with self.preparation.operation():
            return self._apply(name, confirmed=confirmed, gateway_stopped=gateway_stopped)

    def _apply(self, name, *, confirmed=False, gateway_stopped=False):
        if confirmed is not True or gateway_stopped is not True:
            raise ValueError("explicit_stopped_gateway_confirmation_required")
        folder, plan = self._plan(name)
        if (folder / "intent.json").exists():
            raise ValueError("upgrade_already_started_inspect_status")
        if time.time() > plan["expires_at"]:
            raise ValueError("plan_expired")
        before, socket_path = self._scope()
        if digest(before) != plan["config_sha256"] or socket_path != plan["socket_path"]:
            raise ValueError("configuration_changed_refresh_required")
        if (self._identity(self.target) != plan["old_identity"]
                or self._installed() != plan["old_sha256"]):
            raise ValueError("installed_component_changed_refresh_required")
        files = self.preparation.source_files()
        if {n: digest(raw) for n, raw in files.items()} != plan["new_sha256"]:
            raise ValueError("component_changed_refresh_required")
        self._stopped(plan)
        if self.target.stat().st_dev != folder.stat().st_dev:
            raise ValueError("upgrade_requires_same_filesystem")
        staging = directory(folder / "next", create=True)
        for file_name, raw in files.items():
            write_new(staging / file_name, raw)
        sync_directory(staging)
        write_new(folder / "intent.json", json.dumps({
            "operation": "replace-component-only",
            "next_identity": self._identity(staging),
        }, sort_keys=True).encode())
        os.rename(self.target, folder / "previous")
        sync_directory(self.plugins)
        sync_directory(folder)
        # A crash here leaves an exact previous directory for explicit recovery.
        os.rename(staging, self.target)
        sync_directory(self.plugins)
        sync_directory(folder)
        write_new(folder / "published.json", b'{"runtime_verified":false}')
        return self.status(plan["id"])

    def status(self, name):
        folder, plan = self._plan(name)
        if not (folder / "intent.json").exists():
            state = "preview"
        else:
            intent = json.loads(read_file(folder / "intent.json"))
            try:
                old_saved = (self._identity(folder / "previous") == plan["old_identity"]
                             and self._installed(folder / "previous") == plan["old_sha256"])
            except (OSError, ValueError):
                old_saved = False
            try:
                new_live = (self._identity(self.target) == intent["next_identity"]
                            and self._installed() == plan["new_sha256"])
            except (OSError, ValueError):
                new_live = False
            try:
                old_live = (self._identity(self.target) == plan["old_identity"]
                            and self._installed() == plan["old_sha256"])
            except (OSError, ValueError):
                old_live = False
            if (folder / "rolled_back.json").exists() and old_live:
                state = "rolled_back"
            elif old_live and (folder / "failed").exists() and not (folder / "previous").exists():
                state = "rollback_completed_unrecorded"
            elif old_saved and new_live:
                state = "published_runtime_unverified"
            elif old_saved and not self.target.exists() and not self.target.is_symlink():
                state = "interrupted_before_publication"
            else:
                state = "inspection_required"
        return {"plan_id": name, "state": state, "runtime_verified": False,
                "session_verified": False, "delivery_verified": False}

    def rollback(self, name, *, confirmed=False, gateway_stopped=False):
        directory(self.preparation.records, create=True)
        with self.preparation.operation():
            return self._rollback(name, confirmed=confirmed, gateway_stopped=gateway_stopped)

    def _rollback(self, name, *, confirmed=False, gateway_stopped=False):
        if confirmed is not True or gateway_stopped is not True:
            raise ValueError("explicit_stopped_gateway_confirmation_required")
        folder, plan = self._plan(name)
        state = self.status(name)["state"]
        if state not in {
            "published_runtime_unverified", "interrupted_before_publication",
            "rollback_completed_unrecorded",
        }:
            raise ValueError("upgrade_rollback_state_conflict")
        before, socket_path = self._scope()
        if digest(before) != plan["config_sha256"] or socket_path != plan["socket_path"]:
            raise ValueError("configuration_changed_refresh_required")
        self._stopped(plan)
        if state == "rollback_completed_unrecorded":
            write_new(folder / "rolled_back.json", b'{"runtime_verified":false}')
            return self.status(name)
        if self.target.exists():
            os.rename(self.target, folder / "failed")
        os.rename(folder / "previous", self.target)
        sync_directory(self.plugins)
        sync_directory(folder)
        write_new(folder / "rolled_back.json", b'{"runtime_verified":false}')
        return self.status(name)


class PinUpdate:
    """Stopped-Gateway, owner-confirmed replacement of one session pin only.

    This is a lower-level transaction. Its caller must independently verify a
    drained/stopped Gateway and the Human's authorization, including old mail.
    It never starts/reloads the host or declares the new route ready.
    """

    def __init__(self, profile, *, api=None, source=None):
        self.preparation = Preparation(profile, api=api, source=source)
        self.records = self.preparation.profile / "mailbox-session-plans"

    def _scope(self, key):
        before, config = self.preparation.snapshot()
        target = directory(self.preparation.target, private=False)
        source_files = self.preparation.source_files()
        installed = {child.name for child in target.iterdir()}
        if "__pycache__" in installed:
            directory(target / "__pycache__", private=False)
            installed.remove("__pycache__")
        if (installed != set(source_files)
                or any(digest(read_file(target / name, private=False)) != digest(raw)
                       for name, raw in source_files.items())):
            raise ValueError("installed_component_changed_refresh_required")
        entry = item(config, PLUGIN_KEY)
        if (not isinstance(entry, dict) or entry.get("allow_gateway_injection") is not True
                or not isinstance(entry.get("settings"), dict)
                or not isinstance(entry["settings"].get("sessions"), dict)):
            raise ValueError("active_hermes_component_required")
        pin = entry["settings"]["sessions"].get(key)
        if (not isinstance(pin, dict) or set(pin) != {"session_id", "platform"}
                or any(not isinstance(v, str) or not v for v in pin.values())):
            raise ValueError("exact_session_pin_required")
        socket_path = entry["settings"].get("socket_path")
        if (not isinstance(socket_path, str) or not Path(socket_path).is_absolute()
                or Path(socket_path).resolve() != Path(socket_path)):
            raise ValueError("canonical_receiver_socket_required")
        return before, config, entry, pin, socket_path

    def preview(self, *, binding_id, session_key, candidate_fingerprint, candidate_session_id):
        if (not isinstance(binding_id, str) or not binding_id.startswith("binding_")
                or not isinstance(session_key, str) or not session_key
                or not isinstance(candidate_fingerprint, str) or len(candidate_fingerprint) != 64
                or any(c not in "0123456789abcdef" for c in candidate_fingerprint)
                or not isinstance(candidate_session_id, str) or not candidate_session_id
                or len(candidate_session_id) > 512
                or any(ord(c) < 32 for c in candidate_session_id)):
            raise ValueError("candidate_scope_invalid")
        before, _config, entry, pin, socket_path = self._scope(session_key)
        if candidate_session_id == pin["session_id"]:
            raise ValueError("candidate_session_invalid")
        fingerprint = digest(json.dumps([
            binding_id, session_key, pin["session_id"], candidate_session_id,
            pin["platform"],
        ], separators=(",", ":")).encode())
        if fingerprint != candidate_fingerprint:
            raise ValueError("candidate_changed_refresh_required")
        self.preparation.writable({PLUGIN_KEY: entry})
        directory(self.records, create=True)
        name = "plan_" + uuid.uuid4().hex
        folder = directory(self.records / name, create=True)
        plan = {"version": 1, "id": name, "profile": str(self.preparation.profile),
                "binding_id": binding_id, "session_key": session_key,
                "candidate_fingerprint": candidate_fingerprint,
                "candidate_session_id": candidate_session_id,
                "old_pin": pin, "config_sha256": digest(before),
                "socket_path": socket_path, "expires_at": time.time() + TTL}
        write_new(folder / "config.before", before)
        write_new(folder / "plan.json", json.dumps(plan, sort_keys=True).encode())
        return {"plan_id": name, "state": "preview", "requires_confirmation": True,
                "requires_stopped_gateway": True, "session_verified": False,
                "delivery_verified": False}

    def _plan(self, name):
        if (not isinstance(name, str) or len(name) != 37 or not name.startswith("plan_")
                or any(c not in "0123456789abcdef" for c in name[5:])):
            raise ValueError("invalid_plan_id")
        folder = directory(self.records / name)
        plan = json.loads(read_file(folder / "plan.json"))
        if (plan.get("version") != 1 or plan.get("id") != name
                or plan.get("profile") != str(self.preparation.profile)):
            raise ValueError("plan_scope_mismatch")
        return folder, plan

    def apply(self, name, *, expected_binding_id,
              confirmed=False, gateway_stopped=False):
        directory(self.preparation.records, create=True)
        with self.preparation.operation():
            if confirmed is not True or gateway_stopped is not True:
                raise ValueError("explicit_stopped_gateway_confirmation_required")
            folder, plan = self._plan(name)
            if expected_binding_id != plan["binding_id"]:
                raise ValueError("binding_changed_refresh_required")
            if (folder / "intent.json").exists():
                raise ValueError("pin_update_already_started_inspect_status")
            if time.time() > plan["expires_at"]:
                raise ValueError("plan_expired")
            new_session_id = plan.get("candidate_session_id")
            if (not isinstance(new_session_id, str) or not new_session_id
                    or len(new_session_id) > 512 or any(ord(c) < 32 for c in new_session_id)
                    or new_session_id == plan["old_pin"]["session_id"]):
                raise ValueError("candidate_session_invalid")
            fingerprint = digest(json.dumps([
                plan["binding_id"], plan["session_key"],
                plan["old_pin"]["session_id"], new_session_id,
                plan["old_pin"]["platform"],
            ], separators=(",", ":")).encode())
            if fingerprint != plan["candidate_fingerprint"]:
                raise ValueError("candidate_changed_refresh_required")
            before, config, entry, pin, socket_path = self._scope(plan["session_key"])
            if (digest(before) != plan["config_sha256"] or pin != plan["old_pin"]
                    or socket_path != plan["socket_path"]):
                raise ValueError("configuration_changed_refresh_required")
            path = Path(socket_path)
            if path.exists() or path.is_symlink():
                raise ValueError("gateway_receiver_still_present")
            updated = copy.deepcopy(entry)
            updated["settings"]["sessions"][plan["session_key"]]["session_id"] = new_session_id
            self.preparation.writable({PLUGIN_KEY: updated})
            write_new(folder / "intent.json", json.dumps({
                "old_sha256": digest(before), "new_entry_sha256": digest(json.dumps(updated, sort_keys=True).encode()),
            }, sort_keys=True).encode())
            try:
                with (contextlib.redirect_stdout(io.StringIO()),
                      contextlib.redirect_stderr(io.StringIO())):
                    self.preparation.api.config.set_config_value(PLUGIN_KEY, json.dumps(updated))
                after, current = self.preparation.snapshot()
                if (item(current, PLUGIN_KEY) != updated
                        or without_entries(current, (PLUGIN_KEY,)) != without_entries(config, (PLUGIN_KEY,))):
                    raise ValueError("pin_update_unconfirmed")
                write_new(folder / "configured.json", json.dumps({"after_sha256": digest(after)}).encode())
            except (Exception, SystemExit):  # noqa: BLE001 — preserve intent for explicit recovery
                raise ValueError("pin_update_incomplete_inspect_record") from None
            return self.status(name)


    def status(self, name, *, expected_binding_id=None):
        folder, plan = self._plan(name)
        if expected_binding_id is not None and expected_binding_id != plan["binding_id"]:
            raise ValueError("binding_changed_refresh_required")
        if not (folder / "intent.json").exists():
            if (folder / "rolled_back.json").exists():
                state = "cancelled_before_apply"
            else:
                try:
                    raw, _config, _entry, pin, socket_path = self._scope(plan["session_key"])
                    state = ("preview" if digest(raw) == plan["config_sha256"]
                             and pin == plan["old_pin"]
                             and socket_path == plan["socket_path"]
                             else "inspection_required")
                except (OSError, ValueError, KeyError, TypeError):
                    state = "inspection_required"
        else:
            try:
                raw, config, entry, _pin, _socket = self._scope(plan["session_key"])
                original_raw = read_file(folder / "config.before")
                if digest(original_raw) != plan["config_sha256"]:
                    raise ValueError("backup_changed")
                original = self.preparation.api.config.require_readable_config_before_write(
                    folder / "config.before")
                if without_entries(config, (PLUGIN_KEY,)) != without_entries(original, (PLUGIN_KEY,)):
                    raise ValueError("unrelated_configuration_changed")
                intent = json.loads(read_file(folder / "intent.json"))
                new_sha = digest(json.dumps(entry, sort_keys=True).encode())
                if (folder / "rollback_intent.json").exists():
                    if entry == item(original, PLUGIN_KEY):
                        state = ("rolled_back" if (folder / "rolled_back.json").exists()
                                 else "rollback_completed_unrecorded")
                    else:
                        state = "inspection_required"
                elif new_sha == intent["new_entry_sha256"]:
                    state = "configured_runtime_unverified"
                elif digest(raw) == intent["old_sha256"]:
                    state = "interrupted_before_config_write"
                else:
                    state = "inspection_required"
            except (OSError, ValueError):
                state = "inspection_required"
        return {"plan_id": name, "state": state,
                "candidate_fingerprint": plan["candidate_fingerprint"],
                "expires_at": plan["expires_at"],
                "session_verified": False, "delivery_verified": False}

    def rollback(self, name, *, expected_binding_id, confirmed=False, gateway_stopped=False):
        directory(self.preparation.records, create=True)
        with self.preparation.operation():
            if confirmed is not True or gateway_stopped is not True:
                raise ValueError("explicit_stopped_gateway_confirmation_required")
            folder, plan = self._plan(name)
            if expected_binding_id != plan["binding_id"]:
                raise ValueError("binding_changed_refresh_required")
            state = self.status(name)["state"]
            if state not in {"preview", "interrupted_before_config_write",
                             "configured_runtime_unverified", "rollback_completed_unrecorded"}:
                raise ValueError("pin_rollback_state_conflict")
            if Path(plan["socket_path"]).exists() or Path(plan["socket_path"]).is_symlink():
                raise ValueError("gateway_receiver_still_present")
            if state == "rollback_completed_unrecorded":
                write_new(folder / "rolled_back.json", b'{"runtime_verified":false}')
                return self.status(name)
            if state in {"preview", "interrupted_before_config_write"}:
                write_new(folder / "rolled_back.json", b'{"cancelled":true}')
                return self.status(name)
            raw, current, _entry, _pin, socket_path = self._scope(plan["session_key"])
            if socket_path != plan["socket_path"]:
                raise ValueError("configuration_changed_refresh_required")
            backup = read_file(folder / "config.before")
            if digest(backup) != plan["config_sha256"]:
                raise ValueError("backup_changed")
            original = self.preparation.api.config.require_readable_config_before_write(
                folder / "config.before")
            if without_entries(current, (PLUGIN_KEY,)) != without_entries(original, (PLUGIN_KEY,)):
                raise ValueError("unrelated_configuration_changed")
            old_entry = item(original, PLUGIN_KEY)
            self.preparation.writable({PLUGIN_KEY: old_entry})
            write_new(folder / "rollback_intent.json", json.dumps({
                "before_sha256": digest(raw), "old_entry_sha256": digest(json.dumps(old_entry, sort_keys=True).encode()),
            }, sort_keys=True).encode())
            try:
                with (contextlib.redirect_stdout(io.StringIO()),
                      contextlib.redirect_stderr(io.StringIO())):
                    self.preparation.api.config.set_config_value(PLUGIN_KEY, json.dumps(old_entry))
                _after, restored = self.preparation.snapshot()
                if (item(restored, PLUGIN_KEY) != old_entry
                        or without_entries(restored, (PLUGIN_KEY,)) != without_entries(original, (PLUGIN_KEY,))):
                    raise ValueError("pin_rollback_unconfirmed")
                write_new(folder / "rolled_back.json", b'{"runtime_verified":false}')
            except (Exception, SystemExit):  # noqa: BLE001 — inspect intent after an uncertain write
                raise ValueError("pin_rollback_incomplete_inspect_record") from None
            return self.status(name)

class McpCommandUpdate:
    """Replace only an existing mailbox MCP executable while Gateway is stopped.

    The session file and every other Hermes setting remain unchanged. A plan is
    scoped to exact config and executable bytes; runtime tool availability must
    be verified separately after restarting the original Gateway session.
    """

    def __init__(self, profile, *, api=None):
        self.preparation = Preparation(profile, api=api)
        self.records = self.preparation.profile / "mailbox-mcp-command-plans"

    def _scope(self):
        raw, config = self.preparation.snapshot()
        plugin = item(config, PLUGIN_KEY)
        entry = item(config, MCP_KEY)
        if (not isinstance(plugin, dict)
                or type(plugin.get("allow_gateway_injection")) is not bool
                or not isinstance(entry, dict) or set(entry) not in (
                    {"command", "args"}, {"command", "args", "enabled"})):
            raise ValueError("existing_mailbox_mcp_required")
        args = entry.get("args")
        command = entry.get("command")
        if (not isinstance(args, list) or len(args) != 3
                or args[:2] != ["mailbox-mcp", "--session-file"]
                or not isinstance(args[2], str) or not isinstance(command, str)):
            raise ValueError("existing_mailbox_mcp_shape_unrecognized")
        if "enabled" in entry and entry["enabled"] is not True:
            raise ValueError("existing_mailbox_mcp_disabled")
        executable = Path(command)
        session_file = Path(args[2])
        if (not executable.is_absolute() or executable.resolve() != executable
                or not session_file.is_absolute() or session_file.resolve() != session_file):
            raise ValueError("canonical_mcp_paths_required")
        directory(session_file.parent)
        session_sha = digest(read_file(session_file))
        command_sha = digest(read_file(executable, private=False, limit=256 * 1024 * 1024))
        return raw, config, entry, session_sha, command_sha

    def preview(self, *, new_command):
        path = Path(new_command)
        if not path.is_absolute() or path.resolve() != path or not os.access(path, os.X_OK):
            raise ValueError("fixed_executable_required")
        next_sha = digest(read_file(path, private=False, limit=256 * 1024 * 1024))
        raw, _config, old, session_sha, _old_sha = self._scope()
        if str(path) == old["command"]:
            raise ValueError("mcp_command_unchanged")
        next_entry = {**old, "command": str(path)}
        self.preparation.writable({MCP_KEY: next_entry})
        directory(self.records, create=True)
        name = "plan_" + uuid.uuid4().hex
        folder = directory(self.records / name, create=True)
        plan = {"version": 1, "id": name, "profile": str(self.preparation.profile),
                "expires_at": time.time() + TTL, "config_sha256": digest(raw),
                "old_entry": old, "next_entry": next_entry,
                "session_sha256": session_sha, "next_command_sha256": next_sha}
        write_new(folder / "config.before", raw)
        write_new(folder / "plan.json", json.dumps(plan, sort_keys=True).encode())
        return self.status(name)

    def _plan(self, name):
        if (not isinstance(name, str) or len(name) != 37 or not name.startswith("plan_")
                or any(c not in "0123456789abcdef" for c in name[5:])):
            raise ValueError("invalid_plan_id")
        folder = directory(self.records / name)
        plan = json.loads(read_file(folder / "plan.json"))
        if (plan.get("version") != 1 or plan.get("id") != name
                or plan.get("profile") != str(self.preparation.profile)):
            raise ValueError("plan_scope_mismatch")
        return folder, plan

    def status(self, name):
        folder, plan = self._plan(name)
        try:
            backup = read_file(folder / "config.before")
            if digest(backup) != plan["config_sha256"]:
                raise ValueError("backup_changed")
            original = self.preparation.api.config.require_readable_config_before_write(
                folder / "config.before")
            _raw, current, entry, session_sha, _command_sha = self._scope()
            if (session_sha != plan["session_sha256"]
                    or without_entries(current, (MCP_KEY,)) != without_entries(original, (MCP_KEY,))):
                raise ValueError("unrelated_configuration_changed")
            if (folder / "rollback_intent.json").exists():
                if entry == plan["old_entry"]:
                    state = ("rolled_back" if (folder / "rolled_back.json").exists()
                             else "rollback_completed_unrecorded")
                elif entry == plan["next_entry"]:
                    state = "rollback_incomplete_before_config_write"
                else:
                    state = "inspection_required"
            elif not (folder / "intent.json").exists():
                state = "cancelled" if (folder / "rolled_back.json").exists() else "preview"
            elif entry == plan["next_entry"]:
                state = "configured_runtime_unverified"
            elif entry == plan["old_entry"]:
                state = "interrupted_before_config_write"
            else:
                state = "inspection_required"
        except (OSError, ValueError, KeyError, TypeError):
            state = "inspection_required"
        return {"plan_id": name, "state": state, "requires_stopped_gateway": True,
                "requires_confirmation": True,
                "runtime_verified": False, "session_verified": False, "delivery_verified": False}

    def apply(self, name, *, confirmed=False, gateway_stopped=False):
        directory(self.preparation.records, create=True)
        with self.preparation.operation():
            if confirmed is not True or gateway_stopped is not True:
                raise ValueError("explicit_stopped_gateway_confirmation_required")
            folder, plan = self._plan(name)
            if (folder / "intent.json").exists() or self.status(name)["state"] != "preview":
                raise ValueError("mcp_command_plan_state_conflict")
            if time.time() > plan["expires_at"]:
                raise ValueError("plan_expired")
            raw, config, entry, session_sha, _command_sha = self._scope()
            if (digest(raw) != plan["config_sha256"] or entry != plan["old_entry"]
                    or session_sha != plan["session_sha256"]):
                raise ValueError("configuration_changed_refresh_required")
            next_entry = plan["next_entry"]
            next_command = Path(next_entry["command"])
            if (not os.access(next_command, os.X_OK)
                    or digest(read_file(next_command, private=False, limit=256 * 1024 * 1024))
                    != plan["next_command_sha256"]):
                raise ValueError("mcp_executable_changed_refresh_required")
            self.preparation.writable({MCP_KEY: next_entry})
            write_new(folder / "intent.json", json.dumps({"before_sha256": digest(raw)}).encode())
            try:
                with (contextlib.redirect_stdout(io.StringIO()),
                      contextlib.redirect_stderr(io.StringIO())):
                    self.preparation.api.config.set_config_value(MCP_KEY, json.dumps(next_entry))
                _after, current = self.preparation.snapshot()
                if (item(current, MCP_KEY) != next_entry
                        or without_entries(current, (MCP_KEY,))
                        != without_entries(config, (MCP_KEY,))):
                    raise ValueError("mcp_command_update_unconfirmed")
                write_new(folder / "configured.json", b'{"runtime_verified":false}')
            except (Exception, SystemExit):  # noqa: BLE001 - preserve intent after uncertain write
                raise ValueError("mcp_command_update_incomplete_inspect_record") from None
            return self.status(name)

    def rollback(self, name, *, confirmed=False, gateway_stopped=False):
        directory(self.preparation.records, create=True)
        with self.preparation.operation():
            if confirmed is not True or gateway_stopped is not True:
                raise ValueError("explicit_stopped_gateway_confirmation_required")
            folder, plan = self._plan(name)
            state = self.status(name)["state"]
            if state not in {"preview", "interrupted_before_config_write",
                             "configured_runtime_unverified", "rollback_incomplete_before_config_write",
                             "rollback_completed_unrecorded"}:
                raise ValueError("mcp_command_rollback_state_conflict")
            if state == "rollback_completed_unrecorded":
                write_new(folder / "rolled_back.json", b'{"runtime_verified":false}')
                return self.status(name)
            if state in {"preview", "interrupted_before_config_write"}:
                write_new(folder / "rolled_back.json", b'{"runtime_verified":false}')
                return self.status(name)
            _raw, config, entry, session_sha, _command_sha = self._scope()
            if entry != plan["next_entry"] or session_sha != plan["session_sha256"]:
                raise ValueError("configuration_changed_refresh_required")
            backup = read_file(folder / "config.before")
            original = self.preparation.api.config.require_readable_config_before_write(
                folder / "config.before")
            if (digest(backup) != plan["config_sha256"]
                    or without_entries(config, (MCP_KEY,))
                    != without_entries(original, (MCP_KEY,))):
                raise ValueError("unrelated_configuration_changed")
            old_entry = plan["old_entry"]
            self.preparation.writable({MCP_KEY: old_entry})
            if state == "configured_runtime_unverified":
                write_new(folder / "rollback_intent.json", b'{"runtime_verified":false}')
            try:
                with (contextlib.redirect_stdout(io.StringIO()),
                      contextlib.redirect_stderr(io.StringIO())):
                    self.preparation.api.config.set_config_value(MCP_KEY, json.dumps(old_entry))
                _after, restored = self.preparation.snapshot()
                if (item(restored, MCP_KEY) != old_entry
                        or without_entries(restored, (MCP_KEY,))
                        != without_entries(original, (MCP_KEY,))):
                    raise ValueError("mcp_command_rollback_unconfirmed")
                write_new(folder / "rolled_back.json", b'{"runtime_verified":false}')
            except (Exception, SystemExit):  # noqa: BLE001 - inspect intent after uncertain write
                raise ValueError("mcp_command_rollback_incomplete_inspect_record") from None
            return self.status(name)


class InjectionGrant:
    """Repair an existing single-conversation grant, never enroll a new route.

    The owner layer supplies the current mailbox home and bound conversation.
    This adapter changes permission only: it never reloads or starts a host.
    """

    def __init__(self, profile, *, api=None):
        self.preparation = Preparation(profile, api=api)

    def _scope(self, mailbox_home, session_key):
        p = self.preparation
        before, config = p.snapshot()
        entry = item(config, PLUGIN_KEY)
        if not isinstance(entry, dict) or type(entry.get("allow_gateway_injection")) is not bool:
            raise ValueError("existing_receiver_configuration_required")
        settings = entry.get("settings")
        if (not isinstance(settings, dict) or settings.get("discovery_only") is True
                or not isinstance(session_key, str) or not session_key
                or settings.get("home") != str(directory(mailbox_home))):
            raise ValueError("existing_receiver_scope_mismatch")
        sessions = settings.get("sessions")
        if not isinstance(sessions, dict) or set(sessions) != {session_key}:
            raise ValueError("single_existing_conversation_required")
        pin = sessions[session_key]
        if not isinstance(pin, dict) or not all(isinstance(pin.get(k), str) and pin[k]
                                               for k in ("session_id", "platform")):
            raise ValueError("existing_receiver_pin_required")
        directory(p.target, private=False)
        for key in ("state_dir", "socket_path"):
            value = settings.get(key)
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise ValueError("existing_receiver_configuration_required")
        p.writable({PLUGIN_KEY + ".allow_gateway_injection": True})
        return before, config, entry["allow_gateway_injection"]

    @staticmethod
    def _result(before, enabled, state):
        return {"state": state, "config_sha256": digest(before), "enabled": enabled,
                "runtime_verified": False, "delivery_verified": False}

    def preview(self, *, mailbox_home, session_key):
        before, _, enabled = self._scope(mailbox_home, session_key)
        return self._result(before, enabled, "already_enabled" if enabled else "grant_preview")

    def apply(self, *, mailbox_home, session_key, expected_config_sha256, confirmed=False):
        if confirmed is not True:
            raise ValueError("explicit_confirmation_required")
        p = self.preparation
        directory(p.records, create=True)
        with p.operation():
            before, config, enabled = self._scope(mailbox_home, session_key)
            if not isinstance(expected_config_sha256, str) or digest(before) != expected_config_sha256:
                raise ValueError("configuration_changed_refresh_required")
            if enabled:
                return self._result(before, True, "already_enabled")
            folder = directory(p.profile / "mailbox-connect-backups", create=True)
            write_new(folder / (uuid.uuid4().hex + ".yaml"), before)
            if read_file(p.config) != before:
                raise ValueError("configuration_changed_before_enable")
            expected = copy.deepcopy(config)
            expected["plugins"]["entries"][PLUGIN]["allow_gateway_injection"] = True
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                p.api.config.set_config_value(PLUGIN_KEY + ".allow_gateway_injection", "true")
            after, current = p.snapshot()
            if current != expected:
                raise ValueError("grant_write_unconfirmed_backup_retained")
            return self._result(after, True, "grant_enabled_runtime_unverified")
