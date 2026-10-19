import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ADAPTER = Path(__file__).resolve().parents[1] / "integrations/hermes-mailbox-host"
spec = importlib.util.spec_from_file_location("hermes_connect", ADAPTER / "connect.py")
connect = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(ADAPTER))
try:
    spec.loader.exec_module(connect)
finally:
    sys.path.pop(0)


def test_existing_receiver_enable_uses_official_command_and_preserves_configuration(tmp_path):
    tmp_path.chmod(0o700)
    (tmp_path / "plugins/agent-mailbox-host").mkdir(parents=True)
    value = {
        "unrelated": {"secret": "SYNTHETIC"},
        "plugins": {
            "entries": {
                "agent-mailbox-host": {
                    "allow_gateway_injection": False,
                    "settings": {
                        "home": "/tmp/mailbox",
                        "state_dir": "/tmp/state",
                        "socket_path": "/tmp/test.sock",
                        "sessions": {
                            "original": {"session_id": "original-session", "platform": "feishu"}
                        },
                    },
                }
            }
        },
    }
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(value))
    config.chmod(0o600)
    before = config.read_bytes()
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[1:] == [
            "config",
            "set",
            "plugins.entries.agent-mailbox-host.allow_gateway_injection",
            "true",
        ]
        assert kwargs["env"]["HERMES_HOME"] == str(tmp_path)
        value["plugins"]["entries"]["agent-mailbox-host"]["allow_gateway_injection"] = True
        config.write_text(yaml.safe_dump(value))
        return SimpleNamespace(returncode=0)

    assert connect.enable(tmp_path, Path("/synthetic/hermes"), run=run)["state"] == "preview"
    assert config.read_bytes() == before and calls == []
    result = connect.enable(tmp_path, Path("/synthetic/hermes"), confirmed=True, run=run)
    assert result["state"] == "enabled"
    assert Path(result["backup"]).read_bytes() == before
    assert Path(result["backup"]).stat().st_mode & 0o777 == 0o600
    assert yaml.safe_load(config.read_bytes()) == value
    assert (
        connect.enable(tmp_path, Path("/synthetic/hermes"), confirmed=True, run=run)["state"]
        == "already_enabled"
    )
    assert len(calls) == 1


def test_unconfigured_profile_is_not_silently_enrolled(tmp_path):
    tmp_path.chmod(0o700)
    (tmp_path / "plugins/agent-mailbox-host").mkdir(parents=True)
    config = tmp_path / "config.yaml"
    config.write_text("plugins: {}\n")
    config.chmod(0o600)
    with pytest.raises(ValueError, match="existing_receiver_configuration_required"):
        connect.enable(tmp_path, Path("/must-not-run"), confirmed=True)


@pytest.mark.parametrize(
    "changed", ["none", "status", "session_id", "platform", "message", "binding"]
)
def test_probe_requires_exact_turn_receipt(changed):
    spec = importlib.util.spec_from_file_location("hermes_probe", ADAPTER / "probe.py")
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    binding = {
        "id": "binding_test",
        "employee_id": "employee_test",
        "project_id": "project_test",
        "host_kind": "hermes",
        "host_session_id": "original-route",
    }
    pin = {"session_id": "original-session", "platform": "feishu"}
    receipt = {
        **{k: v for k, v in binding.items() if k != "id"},
        "binding_id": binding["id"],
        "message_ids": ["message_test"],
    }
    row = {"status": "received", **pin}
    if changed in row:
        row[changed] = "wrong"
    if changed == "message":
        receipt["message_ids"] = ["message_other"]
    if changed == "binding":
        receipt["binding_id"] = "binding_other"
    row["receipt"] = json.dumps(receipt)
    assert probe.receipt_matches(row, binding, pin, "message_test") is (changed == "none")
