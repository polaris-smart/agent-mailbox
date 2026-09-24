#!/usr/bin/env python3
"""Align the local agent-mailbox registry with the HK fleet hub device book.

Two identity books must agree on every fleet ``deviceId``:

* HK hub ``~/.fleet/devices.json`` (fetched read-only over ssh) plus the Mac
  LAN identity in ``~/.dsh/fleet-lan.json`` -- the fleet side;
* the local mailbox registry ``~/.agent-mail/registry.json`` -- the relay side.

Every entry this script manages carries the literal marker ``fleet-device``
in its description so relay routing can recognise fleet devices. Entries
without the marker (HS/WB/boss/ZC/...) are never touched.

Modes
-----
(default)   Apply the planned additions/updates atomically. ZC acceptance
            only -- the registry is a live identity book.
--check     Read-only drift report. Exit 0 when aligned, 1 on drift,
            2 on hard conflict / operational error.
--dry-run   Print the unified diff that would be written; never write.

The script is stdlib-only and import-safe; the pure plan/comparison layer is
separated from IO so unit tests never need ssh or the real registry.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_REGISTRY = "~/.agent-mail/registry.json"
DEFAULT_LAN_FILE = "~/.dsh/fleet-lan.json"
DEFAULT_SSH_HOST = "hk-server"
DEFAULT_REMOTE_DEVICES = "/home/ubuntu/.fleet/devices.json"
DEFAULT_MARKER = "fleet-device"

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_ERROR = 2


class AlignmentError(Exception):
    """Bad input or an identity conflict that prevents safe progress."""


class FetchError(AlignmentError):
    """The remote (ssh) device book could not be retrieved."""


@dataclass(frozen=True)
class FleetDevice:
    """One device identity as seen on the fleet side."""

    id: str
    source: str
    name: str = ""
    owner: str = ""
    klass: str = ""
    detail: str = ""


@dataclass(frozen=True)
class ExpectedEntry:
    """The registry shape a fleet identity must converge to."""

    id: str
    owner: str
    description: str
    source: str


@dataclass
class Plan:
    """Result of comparing the expected fleet set with the registry."""

    adds: dict[str, dict] = field(default_factory=dict)
    updates: dict[str, tuple[dict, dict]] = field(default_factory=dict)
    synced: list[str] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.adds or self.updates)

    @property
    def aligned(self) -> bool:
        return not self.has_changes and not self.extras and not self.conflicts


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _first(record: dict, *keys: str) -> str:
    for key in keys:
        value = record.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def parse_devices(payload: object, source: str) -> list[FleetDevice]:
    """Parse a hub devices.json payload in either common shape.

    Accepted shapes (extra keys are ignored):

    * map:    ``{"hk-ws2": {"name": ..., "owner": ..., "class": ...}}``
    * list:   ``[{"deviceId": "hk-ws2", ...}, ...]``
    * wrapper: ``{"devices": [ ... ]}``

    Records without a usable id raise -- silent identity loss is not allowed.
    """
    if isinstance(payload, dict) and isinstance(payload.get("devices"), list):
        items: list[tuple[str, object]] = [("", item) for item in payload["devices"]]
    elif isinstance(payload, dict):
        items = [(str(key), value) for key, value in payload.items()]
    elif isinstance(payload, list):
        items = [("", item) for item in payload]
    else:
        raise AlignmentError(
            f"{source}: devices.json must be an object or array, got {type(payload).__name__}"
        )

    devices: list[FleetDevice] = []
    for key, record in items:
        if not isinstance(record, dict):
            raise AlignmentError(f"{source}: record for {key!r} must be an object")
        device_id = key or _first(record, "deviceId", "id", "device_id")
        if not device_id:
            raise AlignmentError(f"{source}: device record is missing deviceId/id: {record!r}")
        devices.append(
            FleetDevice(
                id=device_id,
                source=source,
                name=_first(record, "name", "label", "title"),
                owner=_first(record, "owner"),
                klass=_first(record, "class", "klass", "device_class"),
                detail=_first(record, "detail", "note"),
            )
        )
    return devices


def load_fleet_devices(
    *,
    devices_file: str | None = None,
    ssh_host: str = DEFAULT_SSH_HOST,
    remote_path: str = DEFAULT_REMOTE_DEVICES,
    runner: Callable[[list[str]], str] | None = None,
) -> list[FleetDevice]:
    """Load hub devices from a local snapshot or a read-only ssh ``cat``."""
    if devices_file:
        text = Path(os.path.expanduser(devices_file)).read_text(encoding="utf-8")
        source = f"file:{devices_file}"
    else:
        runner = runner or _ssh_cat
        text = runner(["ssh", ssh_host, "cat", remote_path])
        source = f"hub:{ssh_host}"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AlignmentError(f"{source}: invalid JSON ({exc})") from exc
    return parse_devices(payload, source)


def _ssh_cat(argv: list[str]) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise FetchError(f"{argv[0]} {argv[1]} failed: {exc}") from exc
    if proc.returncode != 0:
        raise FetchError(
            f"{' '.join(argv)} exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout


def load_lan_identity(path: str = DEFAULT_LAN_FILE) -> FleetDevice:
    """Read the Mac fleet LAN identity (``deviceId``; ``id`` accepted too)."""
    text = Path(os.path.expanduser(path)).read_text(encoding="utf-8")
    try:
        record = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AlignmentError(f"{path}: invalid JSON ({exc})") from exc
    if not isinstance(record, dict):
        raise AlignmentError(f"{path}: fleet-lan.json must be an object")
    device_id = _first(record, "deviceId", "id")
    if not device_id:
        raise AlignmentError(f"{path}: missing deviceId/id")
    capabilities = record.get("capabilities")
    dph_version = ""
    if isinstance(capabilities, dict):
        dph_version = _first(capabilities, "dphVersion", "dph_version")
    return FleetDevice(
        id=device_id,
        source="lan",
        name=_first(record, "name"),
        owner=_first(record, "owner"),
        klass="",
        detail=dph_version,
    )


# --------------------------------------------------------------------------- #
# Expected registry entries
# --------------------------------------------------------------------------- #


def marker_pattern(marker: str = DEFAULT_MARKER) -> re.Pattern[str]:
    return re.compile(r"(?<![\w-])" + re.escape(marker) + r"(?![\w-])")


def has_marker(description: str, pattern: re.Pattern[str]) -> bool:
    return isinstance(description, str) and pattern.search(description) is not None


def _segments(**parts: str) -> str:
    return " · ".join(f"{key}={value}" for key, value in parts.items() if value)


def describe_hub_device(device: FleetDevice, host: str, marker: str = DEFAULT_MARKER) -> str:
    tail = _segments(name=device.name, owner=device.owner, **{"class": device.klass})
    return f"{marker} · source=hub:{host}" + (f" · {tail}" if tail else "")


def describe_lan_device(device: FleetDevice, marker: str = DEFAULT_MARKER) -> str:
    tail = _segments(name=device.name, dph=device.detail)
    return f"{marker} · source=lan" + (f" · {tail}" if tail else "")


def build_expected(
    fleet: list[FleetDevice],
    lan: FleetDevice | None,
    extras: list[tuple[str, str]] | None = None,
    *,
    ssh_host: str = DEFAULT_SSH_HOST,
    marker: str = DEFAULT_MARKER,
) -> list[ExpectedEntry]:
    """Deterministic expected set: hub devices, then LAN, then --extra."""
    expected: list[ExpectedEntry] = []
    for device in fleet:
        expected.append(
            ExpectedEntry(
                id=device.id,
                owner=device.owner or "fleet",
                description=describe_hub_device(device, ssh_host, marker),
                source=device.source,
            )
        )
    if lan is not None:
        expected.append(
            ExpectedEntry(
                id=lan.id,
                owner=lan.owner or "fleet-lan",
                description=describe_lan_device(lan, marker),
                source="lan",
            )
        )
    for device_id, description in extras or []:
        pattern = marker_pattern(marker)
        if not has_marker(description, pattern):
            description = f"{marker} · {description}"
        expected.append(
            ExpectedEntry(id=device_id, owner="fleet", description=description, source="extra")
        )
    return expected


# --------------------------------------------------------------------------- #
# Registry IO + plan
# --------------------------------------------------------------------------- #


def load_registry(path: str) -> dict:
    target = Path(os.path.expanduser(path))
    if not target.exists():
        raise AlignmentError(f"registry not found: {target}")
    try:
        registry = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AlignmentError(f"{target}: invalid registry JSON ({exc})") from exc
    if not isinstance(registry, dict) or not isinstance(registry.get("agents"), dict):
        raise AlignmentError(f"{target}: registry must have an object-shaped 'agents' table")
    return registry


def now_utc_iso(moment: datetime | None = None) -> str:
    moment = moment or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_plan(
    registry: dict,
    expected: list[ExpectedEntry],
    *,
    marker: str = DEFAULT_MARKER,
    moment: datetime | None = None,
) -> Plan:
    agents = registry["agents"]
    pattern = marker_pattern(marker)
    plan = Plan()
    created_at = now_utc_iso(moment)

    expected_ids = {entry.id for entry in expected}
    for entry in expected:
        if entry.id not in agents:
            plan.adds[entry.id] = {
                "created_at": created_at,
                "owner": entry.owner,
                "description": entry.description,
            }
            continue
        current = agents[entry.id]
        if not isinstance(current, dict):
            raise AlignmentError(f"registry entry for {entry.id} must be an object")
        if not has_marker(str(current.get("description", "")), pattern):
            plan.conflicts.append(entry.id)
            continue
        wanted = {"owner": entry.owner, "description": entry.description}
        if (
            current.get("owner") != wanted["owner"]
            or current.get("description") != wanted["description"]
        ):
            plan.updates[entry.id] = (dict(current), wanted)
        else:
            plan.synced.append(entry.id)

    for agent_id, record in agents.items():
        if agent_id in expected_ids:
            continue
        if isinstance(record, dict) and has_marker(str(record.get("description", "")), pattern):
            plan.extras.append(agent_id)
    return plan


def render_registry(registry: dict) -> str:
    return json.dumps(registry, indent=1, ensure_ascii=False) + "\n"


def apply_plan(registry: dict, plan: Plan) -> dict:
    """Return a converged deep copy. Extras are never auto-removed."""
    converged = copy.deepcopy(registry)
    agents = converged["agents"]
    for agent_id, entry in plan.adds.items():
        agents[agent_id] = entry
    for agent_id, (_, wanted) in plan.updates.items():
        agents[agent_id]["owner"] = wanted["owner"]
        agents[agent_id]["description"] = wanted["description"]
    return converged


def write_registry_atomic(path: str, registry: dict) -> None:
    target = Path(os.path.expanduser(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    old_mode = target.stat().st_mode & 0o777 if target.exists() else 0o600
    fd, tmp_name = tempfile.mkstemp(prefix=".registry.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(render_registry(registry))
        os.chmod(tmp_name, old_mode)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def registry_diff(path: str, current_text: str, planned: dict) -> str:
    diff = difflib.unified_diff(
        current_text.splitlines(keepends=False),
        render_registry(planned).splitlines(keepends=False),
        fromfile=f"{path} (current)",
        tofile=f"{path} (planned)",
        lineterm="",
    )
    return "\n".join(diff)


# --------------------------------------------------------------------------- #
# Reporting + CLI
# --------------------------------------------------------------------------- #


def parse_extras(values: list[str] | None) -> list[tuple[str, str]]:
    extras: list[tuple[str, str]] = []
    for raw in values or []:
        if "=" not in raw:
            raise AlignmentError(f"--extra expects ID=DESCRIPTION, got {raw!r}")
        device_id, description = raw.split("=", 1)
        device_id, description = device_id.strip(), description.strip()
        if not device_id or not description:
            raise AlignmentError(f"--extra expects ID=DESCRIPTION, got {raw!r}")
        extras.append((device_id, description))
    return extras


def _gather(
    argv: argparse.Namespace,
) -> tuple[list[FleetDevice], FleetDevice | None, list[tuple[str, str]], str]:
    fleet = load_fleet_devices(
        devices_file=argv.devices_file,
        ssh_host=argv.ssh_host,
        remote_path=argv.devices_remote_path,
    )
    lan = load_lan_identity(argv.lan_file) if argv.lan_file.lower() != "none" else None
    extras = parse_extras(argv.extra)
    return fleet, lan, extras, argv.ssh_host


def _source_line(
    fleet: list[FleetDevice], lan: FleetDevice | None, argv: argparse.Namespace
) -> list[str]:
    if argv.devices_file:
        fleet_line = f"  fleet source : file {argv.devices_file} ({len(fleet)} devices)"
    else:
        fleet_line = (
            f"  fleet source : ssh {argv.ssh_host} cat {argv.devices_remote_path}"
            f" ({len(fleet)} devices)"
        )
    lines = [fleet_line]
    if lan is not None:
        lines.append(f"  lan identity : {lan.id} ({argv.lan_file})")
    else:
        lines.append("  lan identity : skipped")
    return lines


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        fleet, lan, extras, host = _gather(args)
        expected = build_expected(fleet, lan, extras, ssh_host=host, marker=args.marker)
        registry = load_registry(args.registry)
        plan = compute_plan(registry, expected, marker=args.marker)
    except AlignmentError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR

    expected_ids = ", ".join(entry.id for entry in expected) or "(none)"
    header = [
        "fleet registry alignment"
        + (" -- check" if args.check else " -- dry-run" if args.dry_run else ""),
        *_source_line(fleet, lan, args),
        f"  expected ids : {expected_ids}",
    ]

    if args.check:
        return _report_check(header, registry, plan, args)
    if args.dry_run:
        return _report_dry_run(header, registry, plan, args)
    return _apply(header, registry, plan, args)


def _report_check(header: list[str], registry: dict, plan: Plan, args: argparse.Namespace) -> int:
    print("\n".join(header))
    agents = registry["agents"]
    managed = [
        agent_id
        for agent_id, rec in agents.items()
        if isinstance(rec, dict)
        and has_marker(str(rec.get("description", "")), marker_pattern(args.marker))
    ]
    print(f"  managed here : {', '.join(managed) or '(none)'} ({len(managed)} entries)")
    for agent_id in plan.synced:
        print(f"  [OK]      {agent_id}: same identity in both books")
    for agent_id, wanted in plan.adds.items():
        print(f"  [MISSING] {agent_id}: not registered (owner={wanted['owner']})")
    for agent_id, (current, wanted) in plan.updates.items():
        print(f"  [DRIFT]   {agent_id}: identity differs")
        print(f"             registry owner={current.get('owner')!r} -> wanted {wanted['owner']!r}")
        print(f"             registry desc={current.get('description')!r}")
        print(f"             wanted desc ={wanted['description']!r}")
    for agent_id in plan.extras:
        print(
            f"  [EXTRA]   {agent_id}: fleet-markered in registry but absent from fleet book (manual review)"
        )
    for agent_id in plan.conflicts:
        print(
            f"  [CONFLICT] {agent_id}: id already taken by a non-fleet registry entry; refusing to touch"
        )

    if plan.conflicts:
        print(
            f"SUMMARY: CONFLICT -- {len(plan.conflicts)} id collision(s), manual resolution required"
        )
        return EXIT_ERROR
    if plan.aligned:
        print(f"SUMMARY: ALIGNED -- {len(plan.synced)} fleet id(s), zero drift")
        return EXIT_OK
    print(
        "SUMMARY: DRIFT -- "
        f"{len(plan.adds)} missing, {len(plan.updates)} drifted, {len(plan.extras)} extra"
    )
    return EXIT_DRIFT


def _report_dry_run(header: list[str], registry: dict, plan: Plan, args: argparse.Namespace) -> int:
    print("\n".join(header))
    print(f"  registry     : {args.registry} (read-only, nothing is written)")
    if plan.conflicts:
        for agent_id in plan.conflicts:
            print(
                f"  [CONFLICT] {agent_id}: id taken by a non-fleet entry; refusing to plan a write"
            )
        print(
            f"SUMMARY: CONFLICT -- {len(plan.conflicts)} collision(s), manual resolution required"
        )
        return EXIT_ERROR
    if not plan.has_changes and not plan.extras:
        print("SUMMARY: no changes -- registry already aligned")
        return EXIT_OK
    planned = apply_plan(registry, plan)
    current_text = Path(os.path.expanduser(args.registry)).read_text(encoding="utf-8")
    print(
        f"  plan         : add {len(plan.adds)}, update {len(plan.updates)}, "
        f"extra {len(plan.extras)} (extras are reported, never removed)"
    )
    print("SUMMARY: dry-run diff follows")
    diff = registry_diff(args.registry, current_text, planned)
    if diff:
        print(diff)
    return EXIT_OK


def _apply(header: list[str], registry: dict, plan: Plan, args: argparse.Namespace) -> int:
    print("\n".join(header))
    if plan.conflicts:
        for agent_id in plan.conflicts:
            print(
                f"  [CONFLICT] {agent_id}: id taken by a non-fleet entry; leaving registry untouched"
            )
        print(
            f"SUMMARY: CONFLICT -- {len(plan.conflicts)} collision(s), manual resolution required"
        )
        return EXIT_ERROR
    if not plan.has_changes:
        print(
            f"SUMMARY: no changes -- {len(plan.synced)} fleet id(s) already aligned"
            + (f", {len(plan.extras)} extra (manual review)" if plan.extras else "")
        )
        return EXIT_OK
    converged = apply_plan(registry, plan)
    write_registry_atomic(args.registry, converged)
    print(f"  wrote        : {args.registry}")
    for agent_id in plan.adds:
        print(f"  [ADD]    {agent_id}")
    for agent_id in plan.updates:
        print(f"  [UPDATE] {agent_id}")
    if plan.extras:
        print(f"  [EXTRA]  {', '.join(plan.extras)} left in place (reported, never auto-removed)")
    print(f"SUMMARY: applied -- add {len(plan.adds)}, update {len(plan.updates)}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fleet_registry_align.py",
        description="Align the local mailbox registry with HK fleet devices.json + Mac LAN identity.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check", action="store_true", help="read-only drift report; exit 1 on drift"
    )
    mode.add_argument(
        "--dry-run", action="store_true", help="print the planned unified diff; never write"
    )
    parser.add_argument("--registry", default=DEFAULT_REGISTRY, help="local registry.json path")
    parser.add_argument(
        "--lan-file", default=DEFAULT_LAN_FILE, help="Mac LAN identity; 'none' skips it"
    )
    parser.add_argument("--devices-file", help="use a local devices.json snapshot instead of ssh")
    parser.add_argument("--ssh-host", default=DEFAULT_SSH_HOST, help="ssh alias for the hub host")
    parser.add_argument(
        "--devices-remote-path", default=DEFAULT_REMOTE_DEVICES, help="remote devices.json path"
    )
    parser.add_argument(
        "--extra",
        action="append",
        metavar="ID=DESCRIPTION",
        help="register an extra fleet id (e.g. US/Windows nodes); repeatable",
    )
    parser.add_argument(
        "--marker", default=DEFAULT_MARKER, help="fleet marker token in descriptions"
    )
    return parser


if __name__ == "__main__":
    sys.exit(run())
