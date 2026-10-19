"""Lightweight Codex CLI receivers owned by the ordinary Workbench lifetime."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
import time
from pathlib import Path

from .workbench_codex_connection import (
    _codex_home,
    _installed,
    _member,
    _paths,
    _payload,
    _private,
    _verified_app,
    _verify_cli_thread,
)
from .workbench_host_bindings import load_bindings
from .workbench_host_transport import _codex_cli_health, endpoints

STABLE_RUNTIME_SECONDS = 300


class CodexCliManager:
    """Restore approved routes at normal startup; never start a model until mail arrives."""

    def __init__(self, store):
        self.store = store
        self.closing = threading.Event()
        self.lock = threading.Lock()
        self.processes = {}
        self.errors = {}
        self.retired = set()
        self.clock = time.monotonic

    def retire_stale(self):
        """Stop running CLI workers whose project binding was replaced."""
        try:
            live_ids = {
                binding["id"]
                for binding in load_bindings(self.store.root)
                if binding["host_kind"] == "codex"
            }
        except (OSError, ValueError) as exc:
            self.errors["retire"] = type(exc).__name__
            return 0
        with self.lock:
            stale = [
                (path, process)
                for path, process in self.processes.items()
                if path.stem not in live_ids
            ]
            self.retired.update(path for path, _ in stale)
        for path, process in stale:
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    self.errors[path.name] = "receiver_retire_failed"
        return len(stale)

    def _command(self, config_file):
        root, ipc = _paths(self.store.root)
        directory = root / "cli"
        _private(directory)
        if (
            config_file.parent != directory
            or config_file.suffix != ".json"
            or config_file.is_symlink()
        ):
            raise ValueError("invalid_cli_service_config")
        info = config_file.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > 8192
        ):
            raise ValueError("private_cli_service_config_required")
        config = json.loads(config_file.read_text())
        fields = {
            "version",
            "binary",
            "codex_home",
            "cwd",
            "binding_id",
            "employee_id",
            "project_id",
            "thread_id",
            "socket_path",
        }
        if (
            not isinstance(config, dict)
            or set(config) != fields
            or config["version"] != 1
            or any(not isinstance(config[k], str) for k in fields - {"version"})
            or config_file.name != config["binding_id"] + ".json"
        ):
            raise ValueError("invalid_cli_service_config")
        binding = next(
            (b for b in load_bindings(self.store.root) if b["id"] == config["binding_id"]), None
        )
        if (
            binding is None
            or any(binding[k] != config[k] for k in ("employee_id", "project_id"))
            or binding["host_session_id"] != config["thread_id"]
            or binding["host_kind"] != "codex"
        ):
            raise ValueError("cli_binding_changed")
        route = next(
            (row for row in endpoints(self.store.root) if row["binding_id"] == binding["id"]), None
        )
        if (
            route is None
            or route.get("mode") != "on_demand_cli"
            or route["employee_id"] != binding["employee_id"]
            or route["socket_path"] != config["socket_path"]
        ):
            raise ValueError("cli_route_changed")
        expected_socket = ipc / ("cli-" + binding["id"][-16:] + ".sock")
        if config["socket_path"] != str(expected_socket) or len(os.fsencode(expected_socket)) > 100:
            raise ValueError("cli_socket_changed")
        with self.store._transaction(readonly=True) as db:
            project_path = self.store._required(db, "projects", binding["project_id"])["path"]
        _member(self.store, binding["employee_id"], binding["project_id"])
        if config["cwd"] != str(Path(project_path).resolve()) or config["codex_home"] != str(
            _codex_home()
        ):
            raise ValueError("cli_workspace_changed")
        _verify_cli_thread(config["thread_id"], project_path)
        _, cli, _, _ = _verified_app()
        if config["binary"] != str(cli):
            raise ValueError("cli_binary_changed")
        release = _installed(root)
        if release is None:
            raise ValueError("receiver_install_required")
        _, node = _payload()
        if not node.is_file():
            raise ValueError("receiver_node_missing")
        _private(ipc.parent)
        _private(ipc, create=True)
        # One registered CLI employee is one execution identity across projects.
        # Separate project routes share the durable admission/unknown lock, so
        # two project messages cannot resume that employee concurrently.
        state_dir = directory / binding["employee_id"] / "state"
        _private(state_dir.parent, create=True)
        _private(state_dir, create=True)
        return [
            str(node),
            str(release / "cli-on-demand.mjs"),
            str(self.store.root.resolve()),
            str(expected_socket),
            str(state_dir),
            str(config_file),
        ], config["cwd"]

    def _spawn(self, config_file):
        command, cwd = self._command(config_file)
        return subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )

    def activate(self, config_file):
        config_file = Path(config_file)
        with self.lock:
            if self.closing.is_set():
                raise ValueError("cli_service_closing")
            if config_file in self.processes:
                return
            process = self._spawn(config_file)
            self.processes[config_file] = process
            self.errors.pop(config_file.name, None)
            started_at = self.clock()

        def signal_ready(current):
            try:
                config = json.loads(config_file.read_text())
                socket_path, thread_id = config["socket_path"], config["thread_id"]
            except (OSError, ValueError, KeyError, TypeError):
                return
            for _ in range(60):  # Bounded startup probe, not a periodic mail poll.
                if (
                    self.closing.is_set()
                    or config_file in self.retired
                    or current.poll() is not None
                ):
                    return
                try:
                    ready = _codex_cli_health(Path(socket_path), thread_id, timeout=0.1)
                except (OSError, ValueError):
                    return
                if ready:
                    event = getattr(self.store, "mail_wake_event", None)
                    if event is not None:
                        event.set()
                    return
                if self.closing.wait(0.05):
                    return

        threading.Thread(
            target=signal_ready, args=(process,), name="codex-cli-ready", daemon=True
        ).start()

        def monitor():
            nonlocal process, started_at
            retries = 0
            while True:
                process.wait()
                with self.lock:
                    if self.closing.is_set() or config_file in self.retired:
                        self.processes.pop(config_file, None)
                        self.retired.discard(config_file)
                        return
                if self.clock() - started_at >= STABLE_RUNTIME_SECONDS:
                    retries = 0
                retries += 1
                if retries > 3:
                    with self.lock:
                        self.processes.pop(config_file, None)
                        self.errors[config_file.name] = "receiver_restart_limit"
                    return
                if self.closing.wait(min(2**retries, 8)):
                    with self.lock:
                        self.processes.pop(config_file, None)
                    return
                with self.lock:
                    if config_file in self.retired:
                        self.processes.pop(config_file, None)
                        self.retired.discard(config_file)
                        return
                try:
                    replacement = self._spawn(config_file)
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    with self.lock:
                        self.processes.pop(config_file, None)
                        self.errors[config_file.name] = type(exc).__name__
                    return
                with self.lock:
                    self.processes[config_file] = replacement
                process = replacement
                started_at = self.clock()
                threading.Thread(
                    target=signal_ready, args=(process,), name="codex-cli-ready", daemon=True
                ).start()

        threading.Thread(target=monitor, name="codex-cli-receiver", daemon=True).start()

    def restore(self):
        root, _ = _paths(self.store.root)
        directory = root / "cli"
        if not directory.exists():
            return
        _private(directory)
        # Rebinding replaces a binding ID. Old private configs may remain on
        # disk, but only current bindings are eligible to start a receiver.
        live_ids = {
            binding["id"]
            for binding in load_bindings(self.store.root)
            if binding["host_kind"] == "codex"
        }
        candidates = sorted(
            path for path in directory.glob("binding_*.json") if path.stem in live_ids
        )
        if len(candidates) > 32:
            raise ValueError("too_many_cli_receivers")
        for config_file in candidates:
            try:
                self.activate(config_file)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                self.errors[config_file.name] = type(exc).__name__

    def close(self):
        self.closing.set()
        with self.lock:
            processes = list(self.processes.values())
        for process in processes:
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    self.errors["shutdown"] = "terminate_failed"
        for process in processes:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    self.errors["shutdown"] = "receiver_exit_unconfirmed"
            except OSError:
                self.errors["shutdown"] = "receiver_exit_unconfirmed"
