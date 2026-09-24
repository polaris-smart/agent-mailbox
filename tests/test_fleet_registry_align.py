"""T5 fleet <-> mailbox registry alignment (scripts/fleet_registry_align.py).

Every test runs against tmp_path fixtures only: the real
``~/.agent-mail/registry.json`` and ``~/.dsh/fleet-lan.json`` are never read
or written here (all CLI invocations pass explicit paths), and ssh is never
invoked (``--devices-file`` replaces the remote fetch).

Required scenarios (T5 task book §3.4):
- aligned registry -> --check exit 0;
- drift -> --check exit 1;
- apply twice -> second run performs zero writes (idempotent).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "fleet_registry_align.py"
_spec = importlib.util.spec_from_file_location("fleet_registry_align", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
align = importlib.util.module_from_spec(_spec)
sys.modules["fleet_registry_align"] = align  # dataclasses resolves cls.__module__ here
_spec.loader.exec_module(align)

HUB_DEVICES = {
    "hk-ws2": {"name": "第二车间", "owner": "boss", "class": "shared"},
    "nj-ws3": {"name": "南京", "owner": "boss", "class": "shared"},
}

LAN_IDENTITY = {
    "deviceId": "dev-a56fa58c53116054",
    "name": "interiadeMac-mini.local",
    "key": "fleet-d-295c8d7a0873932d",
    "port": 5354,
    "hub": "",
    "capabilities": {"os": "darwin arm64", "dph": True, "dphVersion": "dph-fleet@0.2.0"},
}

BASE_AGENTS = {
    "HS": {"created_at": "2026-09-04T16:43:21Z", "owner": "Hermes", "description": "PM"},
    "boss": {"created_at": "2026-09-04T16:43:21Z", "owner": "老板", "description": "决策者"},
    "ZC": {"created_at": "2026-09-04T17:05:12Z", "owner": "ZCode", "description": "基础设施"},
}

FIXED_NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)


def _write_json(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture()
def paths(tmp_path):
    registry = _write_json(tmp_path / "registry.json", {"agents": dict(BASE_AGENTS)})
    devices = _write_json(tmp_path / "devices.json", HUB_DEVICES)
    lan = _write_json(tmp_path / "fleet-lan.json", LAN_IDENTITY)
    return {"registry": registry, "devices": devices, "lan": lan, "root": tmp_path}


def _argv(paths, *extra, mode=None):
    argv = [
        "--registry",
        str(paths["registry"]),
        "--devices-file",
        str(paths["devices"]),
        "--lan-file",
        str(paths["lan"]),
    ]
    if mode:
        argv.insert(0, mode)
    return argv + list(extra)


def _expected_entries(paths):
    fleet = align.load_fleet_devices(devices_file=str(paths["devices"]))
    lan = align.load_lan_identity(str(paths["lan"]))
    return align.build_expected(fleet, lan)


def _aligned_registry(paths) -> dict:
    """A registry that has already converged to the expected fleet set."""
    registry = {"agents": dict(BASE_AGENTS)}
    expected = _expected_entries(paths)
    plan = align.compute_plan(registry, expected, moment=FIXED_NOW)
    return align.apply_plan(registry, plan)


# --------------------------------------------------------------------------- #
# Parsing / fetch layer
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "payload",
    [
        {"hk-ws2": {"name": "第二车间", "owner": "boss", "class": "shared"}},
        [{"deviceId": "hk-ws2", "name": "第二车间", "owner": "boss", "class": "shared"}],
        {"devices": [{"id": "hk-ws2", "name": "第二车间", "owner": "boss", "class": "shared"}]},
    ],
)
def test_parse_devices_accepts_map_list_and_wrapper(payload):
    devices = align.parse_devices(payload, source="hub:test")
    assert len(devices) == 1
    device = devices[0]
    assert device.id == "hk-ws2"
    assert (device.name, device.owner, device.klass) == ("第二车间", "boss", "shared")


def test_parse_devices_missing_id_is_loud():
    with pytest.raises(align.AlignmentError, match="missing deviceId"):
        align.parse_devices([{"name": "ghost"}], source="hub:test")


def test_load_fleet_devices_uses_readonly_ssh_cat():
    captured = {}

    def fake_runner(argv):
        captured["argv"] = argv
        return json.dumps(HUB_DEVICES)

    devices = align.load_fleet_devices(
        ssh_host="hk-server",
        remote_path="/home/ubuntu/.fleet/devices.json",
        runner=fake_runner,
    )
    assert captured["argv"] == ["ssh", "hk-server", "cat", "/home/ubuntu/.fleet/devices.json"]
    assert [device.id for device in devices] == ["hk-ws2", "nj-ws3"]


def test_load_fleet_devices_ssh_failure_raises():
    def failing_runner(argv):
        raise align.FetchError("ssh hk-server exited 255: Operation not permitted")

    with pytest.raises(align.FetchError, match="Operation not permitted"):
        align.load_fleet_devices(runner=failing_runner)


def test_lan_identity_reads_device_id_and_dph_version(tmp_path):
    lan_file = _write_json(tmp_path / "fleet-lan.json", LAN_IDENTITY)
    device = align.load_lan_identity(str(lan_file))
    assert device.id == "dev-a56fa58c53116054"
    assert device.name == "interiadeMac-mini.local"
    assert device.detail == "dph-fleet@0.2.0"

    legacy = dict(LAN_IDENTITY)
    legacy.pop("deviceId")
    legacy["id"] = "dev-legacy"
    legacy_file = _write_json(tmp_path / "legacy-lan.json", legacy)
    assert align.load_lan_identity(str(legacy_file)).id == "dev-legacy"


def test_description_contract_is_literal():
    hub = align.FleetDevice(
        id="hk-ws2", source="hub:hk-server", name="第二车间", owner="boss", klass="shared"
    )
    assert align.describe_hub_device(hub, "hk-server") == (
        "fleet-device · source=hub:hk-server · name=第二车间 · owner=boss · class=shared"
    )
    lan = align.FleetDevice(
        id="dev-a56fa58c53116054",
        source="lan",
        name="interiadeMac-mini.local",
        detail="dph-fleet@0.2.0",
    )
    assert align.describe_lan_device(lan) == (
        "fleet-device · source=lan · name=interiadeMac-mini.local · dph=dph-fleet@0.2.0"
    )


# --------------------------------------------------------------------------- #
# Scenario 1: aligned -> --check exit 0
# --------------------------------------------------------------------------- #


def test_check_aligned_exit0(paths, capsys):
    _write_json(paths["registry"], _aligned_registry(paths))
    assert align.run(_argv(paths, mode="--check")) == 0
    out = capsys.readouterr().out
    assert "SUMMARY: ALIGNED" in out
    assert "dev-a56fa58c53116054" in out
    assert "[MISSING]" not in out and "[DRIFT]" not in out


# --------------------------------------------------------------------------- #
# Scenario 2: drift -> --check exit 1
# --------------------------------------------------------------------------- #


def test_check_drift_exit1_when_fleet_ids_missing(paths, capsys):
    assert align.run(_argv(paths, mode="--check")) == 1
    out = capsys.readouterr().out
    for missing in ("hk-ws2", "nj-ws3", "dev-a56fa58c53116054"):
        assert f"[MISSING] {missing}" in out
    assert "SUMMARY: DRIFT" in out
    # non-fleet entries are out of scope and never mentioned as drift
    assert "[EXTRA] HS" not in out


def test_check_reports_extra_managed_entry(paths, capsys):
    registry = _aligned_registry(paths)
    registry["agents"]["stale-ws9"] = {
        "created_at": "2026-09-01T00:00:00Z",
        "owner": "fleet",
        "description": "fleet-device · source=hub:hk-server · name=退役节点",
    }
    _write_json(paths["registry"], registry)
    assert align.run(_argv(paths, mode="--check")) == 1
    out = capsys.readouterr().out
    assert "[EXTRA]   stale-ws9" in out


# --------------------------------------------------------------------------- #
# --dry-run: diff printed, disk untouched
# --------------------------------------------------------------------------- #


def test_dry_run_prints_diff_and_never_writes(paths, capsys):
    before = paths["registry"].read_bytes()
    assert align.run(_argv(paths, mode="--dry-run")) == 0
    out = capsys.readouterr().out
    assert paths["registry"].read_bytes() == before
    assert "read-only, nothing is written" in out
    assert "fleet-device" in out
    assert '"hk-ws2"' in out and '"dev-a56fa58c53116054"' in out
    assert "registry.json (current)" in out and "registry.json (planned)" in out


def test_dry_run_zero_diff_when_aligned(paths, capsys):
    _write_json(paths["registry"], _aligned_registry(paths))
    assert align.run(_argv(paths, mode="--dry-run")) == 0
    assert "no changes" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Scenario 3: apply + idempotency (second run = zero writes)
# --------------------------------------------------------------------------- #


def test_apply_adds_fleet_ids_without_touching_existing_identities(paths, capsys):
    assert align.run(_argv(paths)) == 0
    registry = json.loads(paths["registry"].read_text(encoding="utf-8"))
    assert set(registry["agents"]) == {
        "HS",
        "boss",
        "ZC",
        "hk-ws2",
        "nj-ws3",
        "dev-a56fa58c53116054",
    }
    hub_entry = registry["agents"]["hk-ws2"]
    assert set(hub_entry) == {"created_at", "owner", "description"}
    assert hub_entry["owner"] == "boss"
    assert hub_entry["description"].startswith("fleet-device · source=hub:hk-server")
    assert "class=shared" in hub_entry["description"]
    lan_entry = registry["agents"]["dev-a56fa58c53116054"]
    assert lan_entry["owner"] == "fleet-lan"
    assert "dph=dph-fleet@0.2.0" in lan_entry["description"]
    # pre-existing identities survive byte-for-byte at the value level
    assert registry["agents"]["HS"] == BASE_AGENTS["HS"]
    assert registry["agents"]["boss"] == BASE_AGENTS["boss"]
    out = capsys.readouterr().out
    assert "[ADD]    hk-ws2" in out and "[ADD]    nj-ws3" in out


def test_apply_is_idempotent_second_run_zero_writes(paths):
    assert align.run(_argv(paths)) == 0
    first = paths["registry"].read_bytes()
    mtime = paths["registry"].stat().st_mtime_ns

    second = align.run(_argv(paths))
    assert second == 0
    assert paths["registry"].read_bytes() == first
    assert paths["registry"].stat().st_mtime_ns == mtime

    # and a follow-up --check is fully green
    assert align.run(_argv(paths, mode="--check")) == 0


def test_apply_preserves_created_at_when_updating_drifted_fields(paths):
    registry = {
        "agents": {
            "hk-ws2": {
                "created_at": "2026-09-01T00:00:00Z",
                "owner": "someone-else",
                "description": "fleet-device · source=hub:hk-server · stale",
            }
        }
    }
    _write_json(paths["registry"], registry)
    assert align.run(_argv(paths)) == 0
    entry = json.loads(paths["registry"].read_text(encoding="utf-8"))["agents"]["hk-ws2"]
    assert entry["created_at"] == "2026-09-01T00:00:00Z"
    assert entry["owner"] == "boss"
    assert entry["description"].endswith("class=shared")


# --------------------------------------------------------------------------- #
# Safety: conflicts, extras, bad inputs
# --------------------------------------------------------------------------- #


def test_non_fleet_id_collision_is_conflict_and_refuses_all_writes(paths, capsys):
    registry = {
        "agents": {
            "hk-ws2": {
                "created_at": "2026-09-01T00:00:00Z",
                "owner": "legacy",
                "description": "旧手工条目",
            }
        }
    }
    _write_json(paths["registry"], registry)
    before = paths["registry"].read_bytes()
    for mode in ("--check", "--dry-run", None):
        assert align.run(_argv(paths, mode=mode)) == 2
        assert paths["registry"].read_bytes() == before
    out = capsys.readouterr().out
    assert "CONFLICT" in out and "hk-ws2" in out


def test_extra_managed_entry_is_never_auto_removed(paths):
    registry = _aligned_registry(paths)
    registry["agents"]["stale-ws9"] = {
        "created_at": "2026-09-01T00:00:00Z",
        "owner": "fleet",
        "description": "fleet-device · source=hub:hk-server · name=退役",
    }
    _write_json(paths["registry"], registry)
    assert align.run(_argv(paths)) == 0
    assert "stale-ws9" in json.loads(paths["registry"].read_text(encoding="utf-8"))["agents"]


def test_extra_cli_registers_future_us_windows_node(paths, capsys):
    argv = _argv(paths, "--extra", "us-ws1=US 生产节点（无通道，预留）")
    assert align.run(argv) == 0
    entry = json.loads(paths["registry"].read_text(encoding="utf-8"))["agents"]["us-ws1"]
    assert entry["owner"] == "fleet"
    assert entry["description"] == "fleet-device · US 生产节点（无通道，预留）"
    # marker is auto-prefixed when the caller omitted it
    assert (
        align.run(_argv(paths, "--extra", "us-ws1=US 生产节点（无通道，预留）", mode="--check"))
        == 0
    )
    capsys.readouterr()


def test_extra_cli_requires_id_equals_description(paths, capsys):
    assert align.run(_argv(paths, "--extra", "no-separator", mode="--check")) == 2
    assert "--extra expects ID=DESCRIPTION" in capsys.readouterr().err
    # the rejected run must not have touched the registry
    assert json.loads(paths["registry"].read_text(encoding="utf-8"))["agents"] == BASE_AGENTS


def test_missing_or_broken_registry_is_exit2(paths, capsys):
    paths["registry"].unlink()
    assert align.run(_argv(paths, mode="--check")) == 2
    assert "registry not found" in capsys.readouterr().err

    _write_json(paths["registry"], {"agents": []})
    assert align.run(_argv(paths, mode="--check")) == 2
    assert "object-shaped" in capsys.readouterr().err
