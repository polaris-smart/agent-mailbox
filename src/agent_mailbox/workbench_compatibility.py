"""Optional authenticated node self reports; not software supply-chain proof."""

from __future__ import annotations

import json
import re

from . import __version__

FLEET_PROTOCOL = "1"
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[ab][0-9]+|rc[0-9]+)?")


def node_report(version=None, protocol=None):
    # Keep malformed protocol distinguishable from a legacy absent report.
    version = version if isinstance(version, str) and _VERSION.fullmatch(version) else None
    protocol = (
        str(protocol)
        if isinstance(protocol, (str, int)) and not isinstance(protocol, bool)
        else None
    )
    if protocol is not None and (
        len(protocol) > 32 or not protocol.isascii() or not protocol.isdigit()
    ):
        protocol = "invalid"
    return {"version": version, "protocol": protocol}


def compatibility_status(version, protocol):
    if protocol is not None and protocol != FLEET_PROTOCOL:
        return "incompatible"
    if not version or protocol is None:
        return "unknown"
    return "matched" if version == __version__ else "compatible"


def compatibility_nodes(store, fleet=None):
    local = store.local_node()
    if fleet:
        records = {row["id"]: row for row in fleet.public_devices(include_reports=True)}
    else:
        # A stopped listener must still expose the last authenticated report.
        # Atomic coordinator writes make this a coherent point-in-time view.
        path = store.root / "workbench" / "fleet" / "server.json"
        records = {}
        if path.is_file() and not path.is_symlink() and path.stat().st_size <= 4 * 1024 * 1024:
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                devices = state.get("devices", {}) if isinstance(state, dict) else {}
                if isinstance(devices, dict):
                    records = {
                        key: node_report(value.get("version"), value.get("protocol"))
                        for key, value in devices.items()
                        if isinstance(value, dict)
                    }
            except (OSError, ValueError):
                pass  # No valid report means unknown, never implied compatibility.
    result = []
    for device in store.snapshot()["devices"]:
        is_local = device["id"] == local["id"]
        record = records.get(device["id"], {})
        report = (
            node_report(__version__, FLEET_PROTOCOL)
            if is_local
            else node_report(record.get("version"), record.get("protocol"))
        )
        result.append(
            {
                "id": device["id"],
                "name": device["name"],
                **report,
                "status": compatibility_status(**report),
                "is_local": is_local,
            }
        )
    return result
