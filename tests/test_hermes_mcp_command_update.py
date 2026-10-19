"""Existing Hermes mailbox MCP command migration, only in a synthetic profile."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "integrations/hermes-mailbox-host/setup.py"
spec = importlib.util.spec_from_file_location("mcp_command_setup", SOURCE)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


@pytest.fixture
def case(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o700)
    old_command = tmp_path / "old-mailbox"
    new_command = tmp_path / "new-mailbox"
    for command, marker in ((old_command, b"old"), (new_command, b"new")):
        command.write_bytes(b"#!/bin/sh\n# " + marker + b"\n")
        command.chmod(0o755)
    session_file = profile / "session.json"
    session_file.write_text('{"synthetic":"only"}')
    session_file.chmod(0o600)
    config = profile / "config.yaml"
    original = {
        "unrelated": {"leave": "alone"},
        "plugins": {"entries": {"agent-mailbox-host": {"allow_gateway_injection": True}}},
        "mcp_servers": {
            "agent-mailbox": {
                "command": str(old_command),
                "args": ["mailbox-mcp", "--session-file", str(session_file)],
                "enabled": True,
            }
        },
    }
    config.write_text(json.dumps(original))
    config.chmod(0o600)

    def set_value(key, value):
        current = json.loads(config.read_text())
        section, name = key.split(".")
        current[section][name] = json.loads(value)
        config.write_text(json.dumps(current))

    api = SimpleNamespace(
        config=SimpleNamespace(
            get_config_path=lambda: config,
            is_managed=lambda: False,
            require_readable_config_before_write=lambda p: json.loads(p.read_text()),
            set_config_value=set_value,
        ),
        managed=SimpleNamespace(
            is_key_managed=lambda _key: False,
            managed_config_keys=lambda: set(),
        ),
    )
    return SimpleNamespace(
        update=setup.McpCommandUpdate(profile, api=api),
        config=config,
        old_command=old_command,
        new_command=new_command,
        session_file=session_file,
        original=original,
    )


def preview(case):
    return case.update.preview(new_command=str(case.new_command))["plan_id"]


def test_command_only_update_and_rollback_keep_session_identity(case):
    original_bytes = case.config.read_bytes()
    session_bytes = case.session_file.read_bytes()
    plan = preview(case)
    assert case.config.read_bytes() == original_bytes
    assert case.update.status(plan)["state"] == "preview"
    with pytest.raises(ValueError, match="confirmation"):
        case.update.apply(plan)
    assert case.config.read_bytes() == original_bytes

    result = case.update.apply(plan, confirmed=True, gateway_stopped=True)
    assert result["state"] == "configured_runtime_unverified"
    assert result["session_verified"] is False
    assert result["delivery_verified"] is False
    current = json.loads(case.config.read_text())
    assert current["mcp_servers"]["agent-mailbox"] == {
        **case.original["mcp_servers"]["agent-mailbox"],
        "command": str(case.new_command),
    }
    assert current["unrelated"] == case.original["unrelated"]
    assert case.session_file.read_bytes() == session_bytes
    assert (
        case.update.rollback(plan, confirmed=True, gateway_stopped=True)["state"] == "rolled_back"
    )
    assert json.loads(case.config.read_text()) == case.original
    assert case.session_file.read_bytes() == session_bytes


@pytest.mark.parametrize("changed", ["config", "executable", "session"])
def test_preview_cas_refuses_changes_without_writing(case, changed):
    plan = preview(case)
    if changed == "config":
        case.config.write_bytes(case.config.read_bytes() + b" ")
    elif changed == "executable":
        case.new_command.write_bytes(case.new_command.read_bytes() + b"# replaced\n")
    else:
        case.session_file.write_bytes(b"changed")
    observed = case.config.read_bytes()
    with pytest.raises(ValueError):
        case.update.apply(plan, confirmed=True, gateway_stopped=True)
    assert case.config.read_bytes() == observed
    assert not (case.update.records / plan / "intent.json").exists()


def test_unrelated_change_after_update_requires_inspection(case):
    plan = preview(case)
    case.update.apply(plan, confirmed=True, gateway_stopped=True)
    current = json.loads(case.config.read_text())
    current["unrelated"]["leave"] = "third-party-edit"
    case.config.write_text(json.dumps(current))
    assert case.update.status(plan)["state"] == "inspection_required"
    with pytest.raises(ValueError, match="state_conflict"):
        case.update.rollback(plan, confirmed=True, gateway_stopped=True)


def test_uncertain_record_write_is_inspectable(case, monkeypatch):
    plan = preview(case)
    write = setup.write_new

    def interrupt(path, raw):
        if Path(path).name == "configured.json":
            raise OSError("synthetic interruption")
        return write(path, raw)

    with monkeypatch.context() as patch:
        patch.setattr(setup, "write_new", interrupt)
        with pytest.raises(ValueError, match="incomplete"):
            case.update.apply(plan, confirmed=True, gateway_stopped=True)
    assert case.update.status(plan)["state"] == "configured_runtime_unverified"
    with pytest.raises(ValueError, match="state_conflict"):
        case.update.apply(plan, confirmed=True, gateway_stopped=True)


def test_preview_rejects_unknown_existing_mcp_shape(case):
    current = json.loads(case.config.read_text())
    current["mcp_servers"]["agent-mailbox"]["args"] = ["mailbox-mcp"]
    case.config.write_text(json.dumps(current))
    with pytest.raises(ValueError, match="shape_unrecognized"):
        preview(case)
    assert not case.update.records.exists()


def test_migration_can_run_while_mailbox_injection_is_explicitly_paused(case):
    current = json.loads(case.config.read_text())
    current["plugins"]["entries"]["agent-mailbox-host"]["allow_gateway_injection"] = False
    case.config.write_text(json.dumps(current))
    plan = preview(case)
    assert case.update.apply(plan, confirmed=True, gateway_stopped=True)["state"] == (
        "configured_runtime_unverified"
    )
    after = json.loads(case.config.read_text())
    assert after["plugins"]["entries"]["agent-mailbox-host"]["allow_gateway_injection"] is False
    assert (
        case.update.rollback(plan, confirmed=True, gateway_stopped=True)["state"] == "rolled_back"
    )


def test_rollback_before_config_write_can_resume(case):
    plan = preview(case)
    case.update.apply(plan, confirmed=True, gateway_stopped=True)
    config_api = case.update.preparation.api.config
    original_set = config_api.set_config_value

    def fail_before_write(_key, _value):
        raise OSError("synthetic interruption")

    config_api.set_config_value = fail_before_write
    with pytest.raises(ValueError, match="rollback_incomplete"):
        case.update.rollback(plan, confirmed=True, gateway_stopped=True)
    assert case.update.status(plan)["state"] == "rollback_incomplete_before_config_write"
    config_api.set_config_value = original_set
    assert (
        case.update.rollback(plan, confirmed=True, gateway_stopped=True)["state"] == "rolled_back"
    )
    assert json.loads(case.config.read_text()) == case.original


def test_rollback_after_config_write_recovers_without_rewriting(case, monkeypatch):
    plan = preview(case)
    case.update.apply(plan, confirmed=True, gateway_stopped=True)
    write = setup.write_new

    def interrupt(path, raw):
        if Path(path).name == "rolled_back.json":
            raise OSError("synthetic interruption")
        return write(path, raw)

    with monkeypatch.context() as patch:
        patch.setattr(setup, "write_new", interrupt)
        with pytest.raises(ValueError, match="rollback_incomplete"):
            case.update.rollback(plan, confirmed=True, gateway_stopped=True)
    assert case.update.status(plan)["state"] == "rollback_completed_unrecorded"
    assert (
        case.update.rollback(plan, confirmed=True, gateway_stopped=True)["state"] == "rolled_back"
    )
    assert json.loads(case.config.read_text()) == case.original
