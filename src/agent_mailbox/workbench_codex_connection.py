"""Local owner installation of the existing-thread Codex receiver, never a new chat."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .workbench_host_bindings import _set_binding_locked, load_bindings
from .workbench_host_transport import (
    _codex_health,
    _codex_launcher_endpoint,
    endpoints,
    receiver_online,
)
from .workbench_runtime import discover_applications
from .workbench_store import WorkbenchError
from .workbench_wake import state_lock

CLI_VERSION = "codex-cli 0.163.0-alpha.10.2"
TESTED_CLI_VERSIONS = frozenset({"codex-cli 0.162.0-alpha.17.2", CLI_VERSION})
FILES = (
    "launcher.mjs",
    "client.mjs",
    "native.mjs",
    "cli-on-demand.mjs",
    "socket.mjs",
    "package.json",
)
SIGNATURE = 'anchor apple generic and identifier "com.openai.codex" and certificate leaf[subject.OU] = "2DC432GLL2"'


def _run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=15)


def _private(path, *, create=False):
    if create and not path.exists():
        path.mkdir(mode=0o700)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or path.resolve() != path
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("private_receiver_directory_required")
    return path


def _atomic(path, value):
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _paths(home):
    root = Path(home).resolve() / "workbench/receivers"
    # Short Unix socket path, partitioned by canonical mailbox home.
    key = hashlib.sha256(os.fsencode(Path(home).resolve())).hexdigest()[:16]
    ipc = Path.home().resolve() / ".agent-mailbox-hosts" / key
    if len(os.fsencode(ipc / "999999-123456789abc.mail.sock")) > 100:
        raise ValueError("receiver_socket_path_too_long")
    return root, ipc


def _codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve()


def _verify_cli_thread(thread_id, project_path):
    """Verify only CLI session metadata before binding; never read its conversation."""
    try:
        if not isinstance(thread_id, str) or str(uuid.UUID(thread_id)) != thread_id:
            raise ValueError
        matches = list(_codex_home().glob(f"sessions/*/*/*/rollout-*-{thread_id}.jsonl"))
        if len(matches) != 1:
            raise ValueError
        record = matches[0]
        info = record.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError
        with record.open("rb") as stream:
            first = stream.readline(65537)
        if len(first) > 65536:
            raise ValueError
        envelope = json.loads(first)
        payload = envelope.get("payload")
        if (
            envelope.get("type") != "session_meta"
            or not isinstance(payload, dict)
            or payload.get("id") != thread_id
            or payload.get("originator") != "codex_exec"
            or payload.get("source") != "exec"
            or payload.get("thread_source") != "user"
            or not isinstance(payload.get("cwd"), str)
            or not Path(payload["cwd"]).is_absolute()
            or Path(payload["cwd"]).resolve() != Path(project_path).resolve()
        ):
            raise ValueError
    except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
        raise ValueError("cli_thread_not_verified") from None
    return {"thread_id": thread_id, "workspace_verified": True}


def _payload():
    if getattr(sys, "frozen", False):
        bundle = Path(sys._MEIPASS)
        return bundle / "agent_mailbox/host_receivers/codex", bundle / "runtime/bin/node"
    source = Path(__file__).resolve().parents[2] / "integrations/codex-mailbox-host"
    node = shutil.which("node")
    if not node:
        raise ValueError("receiver_node_missing")
    return source, Path(node).resolve()


def _app():
    apps = [
        Path(a["entrypoint"])
        for a in discover_applications()
        if a.get("bundle_id") == "com.openai.codex"
    ]
    if len(apps) != 1:
        raise ValueError("codex_app_missing_or_ambiguous")
    app = apps[0].resolve()
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    executable = info.get("CFBundleExecutable")
    if (
        not isinstance(executable, str)
        or Path(executable).name != executable
        or executable in {"", ".", ".."}
    ):
        raise ValueError("invalid_codex_app_executable")
    cli = app / "Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
    if not cli.is_file():
        raise ValueError("codex_cli_missing")
    return app, cli, app / "Contents/MacOS" / executable


def _verified_app():
    app, cli, process = _app()
    _run(["/usr/bin/codesign", "--verify", "--deep", "--strict", "-R", "=" + SIGNATURE, str(app)])
    version = _run([str(cli), "--version"]).stdout.strip()
    if version not in TESTED_CLI_VERSIONS:
        raise ValueError("codex_cli_version_unverified")
    return app, cli, process, version


def _member(store, employee_id, project_id):
    with store._transaction(readonly=True) as db:
        employee = store._required(db, "employees", employee_id)
        store._required(db, "projects", project_id)
        if (
            employee["kind"] != "codex"
            or employee["lifecycle"] != "active"
            or employee["node_id"] != store.local_node_id(db)
            or not db.execute(
                "SELECT 1 FROM memberships WHERE employee_id=? AND project_id=?",
                (employee_id, project_id),
            ).fetchone()
        ):
            raise WorkbenchError("RECEIVER_MEMBER_REQUIRED", "Choose an active local Codex member.")
    return next(
        (
            b
            for b in load_bindings(store.root)
            if b["employee_id"] == employee_id
            and b["project_id"] == project_id
            and b["host_kind"] == "codex"
        ),
        None,
    )


def _require_desktop_employee(store, employee_id):
    with store._transaction(readonly=True) as db:
        if store._required(db, "employees", employee_id)["connection_type"] != "app":
            raise ValueError("desktop_employee_required")


def _require_desktop_entry(store, employee_id, binding):
    """Keep an already routed desktop chat under its original mailbox identity."""
    with store._transaction(readonly=True) as db:
        connection_type = store._required(db, "employees", employee_id)["connection_type"]
    if connection_type == "app":
        return
    _, ipc = _paths(store.root)
    if (
        connection_type == "cli"
        and binding
        and binding["employee_id"] == employee_id
        and binding["host_kind"] == "codex"
        and binding.get("receiver_surface") != "cli"
        and any(
            row["binding_id"] == binding["id"]
            and row["employee_id"] == employee_id
            and row["host_kind"] == "codex"
            and row.get("launcher_directory") == str(ipc)
            for row in endpoints(store.root)
        )
    ):
        return
    raise ValueError("desktop_employee_required")


def _installed(root):
    path = root / "codex.json"
    if not path.exists():
        return None
    _private(root)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("private_receiver_manifest_required")
    if info.st_size > 65536:
        raise ValueError("invalid_receiver_manifest")
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        # Malformed persisted manifest data is a value error, not a caller type error.
        raise ValueError("invalid_receiver_manifest")  # noqa: TRY004
    release = data.get("release")
    if (
        not isinstance(release, str)
        or len(release) != 64
        or any(c not in "0123456789abcdef" for c in release)
    ):
        raise ValueError("invalid_receiver_release")
    directory = _private(root / release)
    if hashlib.sha256(json.dumps(data["files"], sort_keys=True).encode()).hexdigest() != release:
        raise ValueError("receiver_manifest_changed")
    for relative, digest in data["files"].items():
        p = directory / relative
        if (
            not p.resolve().is_relative_to(directory)
            or p.resolve() != p
            or p.is_symlink()
            or not p.is_file()
        ):
            raise ValueError("invalid_receiver_file")
        info = p.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("private_receiver_file_required")
        if hashlib.sha256(p.read_bytes()).hexdigest() != digest:
            raise ValueError("receiver_files_changed")
    if data.get("version") != 1 or "codex-mailbox" not in data["files"]:
        raise ValueError("invalid_receiver_manifest")
    return directory


def _control(endpoint, operation, thread_id=None, *, entry_digest=None):
    if not endpoint:
        raise ValueError("receiver_not_online")
    protocol = "agent-mailbox.receiver-control.v1"
    if operation not in {"sessions", "inspect", "configuration", "identity"}:
        raise ValueError("invalid_receiver_control")
    request = {"protocol": protocol, "request_id": uuid.uuid4().hex, "operation": operation}
    if operation == "identity":
        if (
            not isinstance(entry_digest, str)
            or len(entry_digest) != 64
            or any(c not in "0123456789abcdef" for c in entry_digest)
            or thread_id is None
        ):
            raise ValueError("invalid_receiver_control")
        request["entry_digest"] = entry_digest
    elif entry_digest is not None:
        raise ValueError("invalid_receiver_control")
    if thread_id is not None:
        if (
            not isinstance(thread_id, str)
            or not 1 <= len(thread_id) <= 128
            or any(not (c.isascii() and (c.isalnum() or c in "_-")) for c in thread_id)
        ):
            raise ValueError("invalid_receiver_session")
        request["thread_id"] = thread_id
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        deadline = time.monotonic() + 9
        client.settimeout(9)
        client.connect(endpoint)
        client.sendall(json.dumps(request).encode() + b"\n")
        data = bytearray()
        while b"\n" not in data:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("receiver_control_timeout")
            client.settimeout(remaining)
            chunk = client.recv(8192)
            if not chunk:
                raise ValueError("receiver_control_incomplete")
            data.extend(chunk)
            if len(data) > 262144:
                raise ValueError("receiver_control_too_large")
    result = json.loads(data.split(b"\n", 1)[0])
    if (
        not isinstance(result, dict)
        or result.get("protocol") != protocol
        or result.get("request_id") != request["request_id"]
    ):
        raise ValueError("receiver_control_unavailable")
    return result


def _sessions(endpoint, thread_id=None):
    result = _control(endpoint, "inspect" if thread_id is not None else "sessions", thread_id)
    if (
        not isinstance(result.get("sessions"), list)
        or len(result["sessions"]) > 50
        or type(result.get("truncated")) is not bool
    ):
        raise ValueError("receiver_control_unavailable")
    sessions = []
    for item in result["sessions"]:
        if (
            not isinstance(item, dict)
            or any(not isinstance(item.get(k), str) for k in ("id", "name", "cwd"))
            or not item["id"]
            or len(item["id"]) > 128
            or len(item["name"]) > 200
            or len(item["cwd"]) > 4096
        ):
            raise ValueError("invalid_receiver_sessions")
        sessions.append({k: item[k] for k in ("id", "name", "cwd")})
    if len({s["id"] for s in sessions}) != len(sessions) or (
        thread_id is not None and (len(sessions) != 1 or sessions[0]["id"] != thread_id)
    ):
        raise ValueError("receiver_session_not_loaded")
    return {"sessions": sessions, "truncated": result["truncated"]}


def _configuration(endpoint, thread_id, project_path):
    if thread_id is None:
        raise ValueError("receiver_session_required")
    result = _control(endpoint, "configuration", thread_id)
    cwd = result.get("cwd")
    digest = result.get("config_digest")
    if (
        result.get("thread_id") != thread_id
        or not isinstance(cwd, str)
        or not 1 <= len(cwd) < 4096
        or not Path(cwd).is_absolute()
        or Path(cwd).resolve() != Path(project_path).resolve()
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ValueError("receiver_configuration_scope_mismatch")

    def rows(value, *, runtime=False):
        if not isinstance(value, list) or len(value) > 64:
            raise ValueError("invalid_receiver_configuration")
        flags = (
            ("mailbox_like", "connected", "tools_available")
            if runtime
            else ("mailbox_like", "enabled")
        )
        seen, projected = set(), []
        for row in value:
            name = row.get("name") if isinstance(row, dict) else None
            if (
                not isinstance(name, str)
                or not 1 <= len(name) <= 160
                or any(ord(c) < 32 or ord(c) == 127 for c in name)
                or name in seen
                or any(type(row.get(key)) is not bool for key in flags)
            ):
                raise ValueError("invalid_receiver_configuration")
            seen.add(name)
            projected.append({"name": name, **{key: row[key] for key in flags}})
            if not runtime and name == "agent-mailbox-project" and "entry_digest" in row:
                entry_digest = row["entry_digest"]
                if entry_digest is not None and (
                    not isinstance(entry_digest, str)
                    or len(entry_digest) != 64
                    or any(c not in "0123456789abcdef" for c in entry_digest)
                ):
                    raise ValueError("invalid_receiver_configuration")
                projected[-1]["entry_digest"] = entry_digest
        return projected

    effective = rows(result.get("effective_servers"))
    runtime = rows(result.get("runtime_servers"), runtime=True)
    layers = result.get("layers")
    if not isinstance(layers, list) or len(layers) > 32:
        raise ValueError("invalid_receiver_configuration")
    projected = []
    scopes = {
        "packagedDefaults",
        "mdm",
        "system",
        "enterpriseManaged",
        "user",
        "project",
        "sessionFlags",
        "legacyManagedConfigTomlFromFile",
        "legacyManagedConfigTomlFromMdm",
    }
    for layer in layers:
        if (
            not isinstance(layer, dict)
            or not isinstance(layer.get("scope"), str)
            or layer["scope"] not in scopes
            or any(type(layer.get(k)) is not bool for k in ("disabled", "workspace_layer"))
            or (layer["workspace_layer"] and layer["scope"] != "project")
        ):
            raise ValueError("invalid_receiver_configuration")
        projected.append({k: layer[k] for k in ("scope", "disabled", "workspace_layer")})
        projected[-1]["servers"] = rows(layer.get("servers"))
    return {
        "state": "inspection_complete",
        "thread_id": thread_id,
        "cwd": cwd,
        "config_digest": digest,
        "effective_servers": effective,
        "layers": projected,
        "runtime_servers": runtime,
    }


def _active_receiver_surfaces(store, employee_id, project_id):
    """Use immutable App identity for legacy routes; unknown CLI routes fail closed."""
    home = store.root
    active = {
        row["id"]: row
        for row in load_bindings(home)
        if row["employee_id"] == employee_id and row["host_kind"] == "codex"
    }
    surfaces = {
        row["receiver_surface"]
        for row in active.values()
        if row.get("receiver_surface") in {"cli", "desktop"}
    }
    classified_ids = set()
    for row in endpoints(home):
        binding = active.get(row["binding_id"])
        if binding is None or row["employee_id"] != employee_id or row["host_kind"] != "codex":
            continue
        endpoint_surface = (
            "cli"
            if row.get("mode") == "on_demand_cli"
            else "desktop"
            if "launcher_directory" in row
            else None
        )
        if endpoint_surface:
            surfaces.add(endpoint_surface)
            classified_ids.add(binding["id"])
    if any(
        binding["project_id"] != project_id
        and not binding.get("receiver_surface")
        and binding["id"] not in classified_ids
        for binding in active.values()
    ):
        with store._transaction(readonly=True) as db:
            connection_type = store._required(db, "employees", employee_id)["connection_type"]
        surfaces.add("desktop" if connection_type == "app" else "unknown")
    return surfaces


def connection(
    store,
    employee_id,
    project_id,
    action="status",
    *,
    thread_id=None,
    expected_binding_id=...,
    confirmed=False,
    activate=None,
    retire=None,
):
    """Called only behind workbench owner authentication; no employee capability."""
    if action not in {
        "status",
        "install",
        "connect",
        "launch",
        "sessions",
        "bind",
        "configuration",
        "cli_preflight",
        "cli_bind",
        "cli_enable",
        "shortcut",
        "shortcut_status",
        "reveal_shortcut",
    }:
        raise WorkbenchError("INVALID_REQUEST", "Unknown receiver action.")
    if sys.platform != "darwin":
        binding = _member(store, employee_id, project_id)
        if action != "status":
            raise WorkbenchError("RECEIVER_UNSUPPORTED", "This receiver currently requires macOS.")
        return {"state": "unsupported", "binding_configured": bool(binding)}
    try:
        with state_lock(store.root):
            binding = _member(store, employee_id, project_id)
            if action != "status" and store.update_maintenance()["paused"]:
                raise WorkbenchError(
                    "UPDATE_PAUSED", "Resume the workbench before changing receivers."
                )
            if action in {"shortcut", "shortcut_status", "reveal_shortcut"}:
                from .workbench_codex_shortcut import shortcut

                if {"cli", "unknown"} & _active_receiver_surfaces(store, employee_id, project_id):
                    raise ValueError("separate_employee_required_for_desktop")
                _require_desktop_entry(store, employee_id, binding)
                if action == "shortcut" and confirmed is not True:
                    raise ValueError("explicit_receiver_authorization_required")
                return shortcut(
                    store,
                    employee_id,
                    project_id,
                    create=action == "shortcut",
                    reveal=action == "reveal_shortcut",
                )
            if action == "cli_preflight":
                with store._transaction(readonly=True) as db:
                    project_path = store._required(db, "projects", project_id)["path"]
                return _verify_cli_thread(thread_id, project_path)
            if action == "cli_bind":
                if confirmed is not True or expected_binding_id is ...:
                    raise ValueError("explicit_receiver_authorization_required")
                with store._transaction(readonly=True) as db:
                    project_path = store._required(db, "projects", project_id)["path"]
                    if store._required(db, "employees", employee_id)["connection_type"] != "cli":
                        raise ValueError("cli_employee_required")
                _verify_cli_thread(thread_id, project_path)
                if {"desktop", "unknown"} & _active_receiver_surfaces(
                    store, employee_id, project_id
                ):
                    raise ValueError("separate_employee_required_for_cli")
                route = store.root / "wake/host-endpoints.json"
                previous = None
                if route.exists():
                    rows = endpoints(store.root)
                    if binding and any(
                        row["binding_id"] == binding["id"] and "launcher_directory" in row
                        for row in rows
                    ):
                        raise ValueError("separate_employee_required_for_cli")
                    live_ids = {
                        row["id"]
                        for row in load_bindings(store.root)
                        if row["employee_id"] == employee_id
                    }
                    retained = [
                        row
                        for row in rows
                        if not (
                            row["employee_id"] == employee_id
                            and row["host_kind"] == "codex"
                            and (
                                row["binding_id"] not in live_ids
                                or binding
                                and row["binding_id"] == binding["id"]
                            )
                        )
                    ]
                    if retained != rows:
                        previous = route.read_bytes()
                        _atomic(route, json.dumps({"version": 1, "endpoints": retained}).encode())
                try:
                    bound = _set_binding_locked(
                        store,
                        employee_id,
                        project_id,
                        host_kind="codex",
                        host_session_id=thread_id,
                        expected_binding_id=expected_binding_id,
                        receiver_surface="cli",
                    )
                except Exception:
                    if previous is not None:
                        _atomic(route, previous)
                    raise
                if retire is not None:
                    retire()
                return {
                    "state": "cli_install_required",
                    "binding_id": bound["id"],
                    "thread_id": thread_id,
                    "receiver_online": False,
                }
            if action == "cli_enable":
                if (
                    confirmed is not True
                    or expected_binding_id is ...
                    or not binding
                    or binding["id"] != expected_binding_id
                    or binding["host_session_id"] != thread_id
                    or activate is None
                ):
                    raise ValueError("explicit_receiver_authorization_required")
                with store._transaction(readonly=True) as db:
                    project_path = store._required(db, "projects", project_id)["path"]
                    if store._required(db, "employees", employee_id)["connection_type"] != "cli":
                        raise ValueError("cli_employee_required")
                _verify_cli_thread(thread_id, project_path)
                if {"desktop", "unknown"} & _active_receiver_surfaces(
                    store, employee_id, project_id
                ):
                    raise ValueError("separate_employee_required_for_cli")
                root, ipc = _paths(store.root)
                if not _installed(root):
                    raise ValueError("receiver_install_required")
                _, cli, _, _ = _verified_app()
                _, node = _payload()
                if not node.is_file():
                    raise ValueError("receiver_node_missing")
                for directory in (root, root / "cli", ipc.parent, ipc):
                    _private(directory, create=True)
                socket_path = ipc / ("cli-" + binding["id"][-16:] + ".sock")
                if len(os.fsencode(socket_path)) > 100:
                    raise ValueError("receiver_socket_path_too_long")
                config_path = root / "cli" / (binding["id"] + ".json")
                config = {
                    "version": 1,
                    "binary": str(cli),
                    "codex_home": str(_codex_home()),
                    "cwd": str(Path(project_path).resolve()),
                    "binding_id": binding["id"],
                    "employee_id": employee_id,
                    "project_id": project_id,
                    "thread_id": thread_id,
                    "socket_path": str(socket_path),
                }
                route = store.root / "wake/host-endpoints.json"
                current_rows = endpoints(store.root)
                if any(
                    row["binding_id"] == binding["id"] and "launcher_directory" in row
                    for row in current_rows
                ):
                    raise ValueError("separate_employee_required_for_cli")
                live_ids = {
                    row["id"]
                    for row in load_bindings(store.root)
                    if row["employee_id"] == employee_id
                }
                previous_route = route.read_bytes() if route.exists() else None
                previous_config = None
                if config_path.exists():
                    info = config_path.lstat()
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.getuid()
                        or info.st_mode & 0o077
                        or info.st_size > 8192
                    ):
                        raise ValueError("private_cli_service_config_required")
                    previous_config = config_path.read_bytes()
                rows = [
                    row
                    for row in current_rows
                    if row["binding_id"] != binding["id"]
                    and not (
                        row["employee_id"] == employee_id
                        and row["host_kind"] == "codex"
                        and row["binding_id"] not in live_ids
                    )
                ]
                rows.append(
                    {
                        "binding_id": binding["id"],
                        "employee_id": employee_id,
                        "host_kind": "codex",
                        "mode": "on_demand_cli",
                        "socket_path": str(socket_path),
                    }
                )
                try:
                    _atomic(config_path, json.dumps(config, sort_keys=True).encode())
                    _atomic(route, json.dumps({"version": 1, "endpoints": rows}).encode())
                    activate(config_path)
                except Exception:
                    if previous_config is None:
                        config_path.unlink(missing_ok=True)
                    else:
                        _atomic(config_path, previous_config)
                    if previous_route is None:
                        route.unlink(missing_ok=True)
                    else:
                        _atomic(route, previous_route)
                    raise
                return {
                    "state": "cli_launch_requested",
                    "binding_id": binding["id"],
                    "receiver_online": False,
                }
            if action in {"sessions", "bind", "configuration"}:
                root, ipc = _paths(store.root)
                if not _installed(root):
                    raise ValueError("receiver_install_required")
                endpoint = (
                    _codex_launcher_endpoint(store.root, ipc, thread_id) if ipc.exists() else None
                )
                if action == "configuration":
                    with store._transaction(readonly=True) as db:
                        project_path = store._required(db, "projects", project_id)["path"]
                    return _configuration(endpoint, thread_id, project_path)
                if action == "sessions":
                    return {
                        **_sessions(endpoint),
                        "binding_id": binding["id"] if binding else None,
                        "bound_thread_id": binding["host_session_id"] if binding else None,
                    }
                if confirmed is not True or expected_binding_id is ... or thread_id is None:
                    raise ValueError("explicit_receiver_authorization_required")
                _sessions(endpoint, thread_id)
                if {"cli", "unknown"} & _active_receiver_surfaces(store, employee_id, project_id):
                    raise ValueError("separate_employee_required_for_desktop")
                _require_desktop_employee(store, employee_id)
                binding = _set_binding_locked(
                    store,
                    employee_id,
                    project_id,
                    host_kind="codex",
                    host_session_id=thread_id,
                    expected_binding_id=expected_binding_id,
                    receiver_surface="desktop",
                )
                return {
                    "state": "connect_required",
                    "installed": True,
                    "binding_configured": True,
                    "binding_id": binding["id"],
                    "transport_online": True,
                    "receiver_online": False,
                }
            if action in {"connect", "launch"} and {"cli", "unknown"} & _active_receiver_surfaces(
                store, employee_id, project_id
            ):
                raise ValueError("separate_employee_required_for_desktop")
            if action in {"connect", "launch"}:
                _require_desktop_entry(store, employee_id, binding)
            return _connection(store, binding, action)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # Do not expose captured CLI output, environment, credentials or paths.
        reason = str(exc) if isinstance(exc, ValueError) else "receiver_operation_failed"
        raise WorkbenchError("RECEIVER_UNAVAILABLE", reason) from None


def _connection(store, binding, action):
    root, ipc = _paths(store.root)
    installed = _installed(root)
    if action == "status" and binding:
        route = next(
            (
                row
                for row in endpoints(store.root)
                if row["binding_id"] == binding["id"] and row.get("mode") == "on_demand_cli"
            ),
            None,
        )
        if route:
            ready = receiver_online(store.root, binding)
            return {
                "state": "cli_receiver_online" if ready else "cli_receiver_unavailable",
                "installed": bool(installed),
                "binding_configured": True,
                "binding_id": binding["id"],
                "bound_thread_id": binding["host_session_id"],
                "transport_online": ready,
                "receiver_online": ready,
                "ordinary_icon_supported": True,
            }
        if binding.get("receiver_surface") == "cli":
            return {
                "state": "cli_install_required",
                "installed": bool(installed),
                "binding_configured": True,
                "binding_id": binding["id"],
                "bound_thread_id": binding["host_session_id"],
                "transport_online": False,
                "receiver_online": False,
                "ordinary_icon_supported": True,
            }
    app, cli, process = _app()
    running = str(process) in {
        p.strip() for p in _run(["/bin/ps", "-axo", "comm="]).stdout.splitlines()
    }
    online = (
        _codex_launcher_endpoint(store.root, ipc, binding["host_session_id"] if binding else None)
        if ipc.exists()
        else None
    )
    if action == "install":
        _, _, _, verified_version = _verified_app()
        source, node = _payload()
        if not node.is_file() or not source.is_dir():
            raise ValueError("receiver_assets_missing")
        ws = source / "node_modules/ws"
        if json.loads((ws / "package.json").read_text())["version"] != "8.22.0":
            raise ValueError("receiver_dependency_unverified")
        payload = {name: (source / name).read_bytes() for name in FILES}
        for p in ws.rglob("*"):
            if p.is_symlink():
                raise ValueError("receiver_dependency_symlink")
            if p.is_file():
                payload[str(p.relative_to(source))] = p.read_bytes()
        script = (
            "#!/bin/sh\n"
            f"export AGENT_MAILBOX_CODEX_BINARY={shlex.quote(str(cli))}\n"
            f"export AGENT_MAILBOX_CODEX_MAILBOX_HOME={shlex.quote(str(store.root.resolve()))}\n"
            f"export AGENT_MAILBOX_CODEX_SOCKET_DIR={shlex.quote(str(ipc))}\n"
            'actual_version=$("$AGENT_MAILBOX_CODEX_BINARY" --version) || exit $?\n'
            f'if [ "$actual_version" != {shlex.quote(verified_version)} ]; then\n'
            ' echo "mailbox: CLI changed; receiver not loaded" >&2\n'
            ' exec "$AGENT_MAILBOX_CODEX_BINARY" "$@"\nfi\n'
            'receiver_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit $?\n'
            f'exec {shlex.quote(str(node))} "$receiver_dir/launcher.mjs" "$@"\n'
        )
        payload["codex-mailbox"] = script.encode()
        hashes = {name: hashlib.sha256(value).hexdigest() for name, value in payload.items()}
        release = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        for parent in (root.parent, root, ipc.parent, ipc):
            _private(parent, create=True)
        target = root / release
        if not target.exists():
            staging = Path(tempfile.mkdtemp(prefix=".install-", dir=root))
            try:
                for name, value in payload.items():
                    p = staging / name
                    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    p.write_bytes(value)
                    p.chmod(0o700 if name == "codex-mailbox" else 0o600)
                staging.rename(target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        # Retain old content-addressed releases; do not rewrite running adapters.
        descriptor = {"version": 1, "release": release, "files": hashes}
        old = (root / "codex.json").read_bytes() if installed else None
        _atomic(root / "codex.json", json.dumps(descriptor, sort_keys=True).encode())
        try:
            installed = _installed(root)
        except Exception:
            if old is not None:
                _atomic(root / "codex.json", old)
            else:
                (root / "codex.json").unlink(missing_ok=True)
            raise
    requested = False
    if action in {"connect", "launch"}:
        if not installed:
            raise ValueError("receiver_install_required")
        if action == "connect" and not binding:
            raise ValueError("original_session_binding_required")
        if running and not online:
            return {
                "state": "restart_required",
                "binding_configured": bool(binding),
                "installed": True,
                "host_running": True,
                "transport_online": False,
                "receiver_online": False,
            }
        if online and not running:
            raise ValueError("receiver_without_app_requires_inspection")
        _verified_app()
        # Only this bound route changes. Existing Hermes/DSH and other projects stay intact.
        path = store.root / "wake/host-endpoints.json"
        previous = path.read_bytes() if path.exists() else None
        if action == "connect":
            rows = endpoints(store.root)
            # Rebinding creates a new ID for the same employee/project. Prune
            # only this employee's obsolete Codex launcher routes; other host
            # integrations own their endpoint rows, even if currently unbound.
            live_ids = {
                row["id"]
                for row in load_bindings(store.root)
                if row["employee_id"] == binding["employee_id"] and row["host_kind"] == "codex"
            }
            rows = [
                r
                for r in rows
                if r["binding_id"] != binding["id"]
                and not (
                    r["employee_id"] == binding["employee_id"]
                    and r["host_kind"] == "codex"
                    and r.get("launcher_directory") == str(ipc)
                    and r["binding_id"] not in live_ids
                )
            ]
            rows.append(
                {
                    "binding_id": binding["id"],
                    "employee_id": binding["employee_id"],
                    "host_kind": "codex",
                    "launcher_directory": str(ipc),
                }
            )
            _atomic(path, json.dumps({"version": 1, "endpoints": rows}).encode())
        if not running:
            try:
                _run(
                    [
                        "/usr/bin/open",
                        "-a",
                        str(app),
                        "--env",
                        "CODEX_CLI_PATH=" + str(installed / "codex-mailbox"),
                    ]
                )
                requested = True
            except Exception:
                if action != "connect":
                    raise
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic(path, previous)
                raise
    routed = bool(
        binding
        and any(
            r["binding_id"] == binding["id"]
            and r["employee_id"] == binding["employee_id"]
            and r["host_kind"] == binding["host_kind"]
            and r.get("launcher_directory") == str(ipc)
            for r in endpoints(store.root)
        )
    )
    ready = bool(
        online and routed and binding and _codex_health(Path(online), binding["host_session_id"])
    )
    if action == "connect" and ready:
        # A recovered original-session route makes committed mail deliverable.
        # Signal the worker now; its bounded scan remains only a crash fallback.
        event = getattr(store, "mail_wake_event", None)
        if event is not None:
            event.set()
    state = (
        "launch_requested"
        if requested
        else "install_required"
        if not installed
        else "binding_required"
        if not binding
        else "connect_required"
        if online and not routed
        else "receiver_online"
        if ready
        else "receiver_not_ready"
        if online and routed
        else "restart_required"
        if running
        else "ready_to_start"
    )
    return {
        "state": state,
        "installed": bool(installed),
        "binding_configured": bool(binding),
        "binding_id": binding["id"] if binding else None,
        "bound_thread_id": binding["host_session_id"] if binding else None,
        "host_running": running,
        "transport_online": bool(online),
        "receiver_online": ready,
        "ordinary_icon_supported": False,
    }
