"""Explicit headless execution nodes; no browser or human HTTP listener."""

from __future__ import annotations

import argparse
import json
import os
import signal
import stat
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .workbench_fleet import FleetClient, _write_private
from .workbench_lock import WorkbenchLock
from .workbench_remote import RemoteWorker
from .workbench_store import WorkbenchError


def _read_private_json(path):
    """Never accept a world-readable invitation or print its contents on error."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                raise WorkbenchError("invalid_file", "Choose a JSON file smaller than 1 MiB.")
            if os.name != "nt" and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
                raise WorkbenchError(
                    "private_file_required", "Use a JSON file owned by this account with mode 0600."
                )
            value = json.loads(stream.read(1024 * 1024 + 1))
            if not isinstance(value, dict):
                raise WorkbenchError("invalid_file", "The private JSON file needs a JSON object.")
            return value
    except WorkbenchError:
        raise
    except (OSError, ValueError, UnicodeError) as exc:
        raise WorkbenchError("invalid_file", "The private JSON file could not be read.") from exc


@contextmanager
def _owner(home):
    home = Path(home).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = home / "workbench"
    directory.mkdir(exist_ok=True, mode=0o700)
    lock = WorkbenchLock(home)
    try:
        yield home
    finally:
        lock.close()


def _url(value):
    if not isinstance(value, str):
        raise WorkbenchError("invalid_field", "Use a valid HTTPS coordinator address.")
    value = value.rstrip("/")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.port
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid endpoint")
    except (ValueError, TypeError) as exc:
        raise WorkbenchError(
            "invalid_field", "Use an HTTPS coordinator address with an explicit port and no path."
        ) from exc
    return value.rstrip("/"), parsed.hostname, parsed.port


def _client(home, invite=None, coordinator_url=None):
    saved_path = home / "workbench/fleet/client.json"
    saved = _read_private_json(saved_path) if saved_path.exists() else None
    if saved is None:
        if invite is None:
            raise WorkbenchError(
                "device_unpaired", "Join with a private invitation JSON file first."
            )
        if coordinator_url:
            invite = {**invite, "base_url": _url(coordinator_url)[0]}
        return FleetClient(home, invite)
    if invite is not None and invite.get("fingerprint") != saved.get("fingerprint"):
        raise WorkbenchError(
            "permission_denied", "This home is already paired with another coordinator."
        )
    client = FleetClient(home, saved)
    if coordinator_url and coordinator_url != client.base_url:
        dial_url, host, port = _url(coordinator_url)
        client.base_url, client.host, client.port = dial_url, host, port
        metadata = client.device()  # Pin and bearer identity are checked at the new address first.
        if metadata["coordinator"]["id"] != saved.get("coordinator_id"):
            raise WorkbenchError("permission_denied", "The coordinator identity does not match.")
        client.credentials = {**client.credentials, "base_url": dial_url}
        _write_private(saved_path, json.dumps(client.credentials).encode("utf-8"))
    return client


def run_node(client, projects=None, stopping=None, bridge_command=None):
    """Run only explicitly mapped scopes; the worker reconciles without model replay."""
    stopping = stopping or threading.Event()
    authorized = {row["id"] for row in client.projects()}
    selected = list(dict.fromkeys(projects if projects is not None else client.mappings))
    if not selected:
        raise WorkbenchError(
            "project_unmapped", "Map an authorized project before running this node."
        )
    if not set(selected) <= authorized:
        raise WorkbenchError(
            "permission_denied", "A mapped project is no longer authorized. Review its mapping."
        )
    for project_id in selected:
        client.local_project(project_id)
    worker = RemoteWorker(client, bridge_command=bridge_command)
    try:
        for project_id in selected:
            if stopping.is_set():
                break
            worker.start_project(project_id)
        print(
            json.dumps(
                {
                    "status": "running",
                    "device_id": client.credentials["device_id"],
                    "project_ids": selected,
                }
            ),
            flush=True,
        )
        stopping.wait()
    finally:
        worker.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run an optional headless agent-mailbox node.")
    parser.add_argument("--home", type=Path, default=Path.home() / ".agent-mailbox")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("join", "map", "employee", "run"):
        command = commands.add_parser(name)
        # Accept --home on either side of the subcommand without overwriting a global value.
        command.add_argument("--home", type=Path, default=argparse.SUPPRESS)
        command.add_argument("--coordinator-url")
        if name == "join":
            command.add_argument("--invite", type=Path, required=True)
        elif name == "map":
            command.add_argument("--project", required=True)
            command.add_argument("--path", type=Path, required=True)
        elif name == "employee":
            command.add_argument("--project", required=True)
            command.add_argument("--name", required=True)
            command.add_argument("--kind", choices=("codex", "claude"), required=True)
        else:
            command.add_argument("--project", action="append")
    args = parser.parse_args(argv)
    stopping = threading.Event()
    previous = {}
    if args.command == "run" and threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, lambda *_: stopping.set())
    try:
        with _owner(args.home) as home:
            invite = _read_private_json(args.invite) if args.command == "join" else None
            client = _client(home, invite, args.coordinator_url)
            if args.command == "run":
                run_node(client, args.project, stopping)
                return 0
            if args.command == "join":
                result = {
                    "device_id": client.credentials["device_id"],
                    "projects": client.projects(),
                }
            elif args.command == "map":
                result = client.map_project(args.project, args.path)
            else:
                result = client.register_employee(args.project, args.name, args.kind)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return 0
    except WorkbenchError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=sys.stderr)
        return 1
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
