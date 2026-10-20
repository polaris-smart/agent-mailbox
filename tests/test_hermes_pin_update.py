"""One-pin config transaction; synthetic profile and no running Gateway."""

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "integrations/hermes-mailbox-host/setup.py"
spec = importlib.util.spec_from_file_location("pin_update_setup", SOURCE)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


@pytest.fixture
def case(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o700)
    socket = profile / "receiver.sock"
    config = profile / "config.yaml"
    original = {
        "unrelated": {"preserve": "exact"},
        "plugins": {
            "entries": {
                "third-party": {"enabled": True},
                "agent-mailbox-host": {
                    "allow_gateway_injection": True,
                    "settings": {
                        "home": str(tmp_path / "mailbox"),
                        "socket_path": str(socket),
                        "sessions": {
                            "agent:main:feishu:dm:synthetic": {
                                "session_id": "original",
                                "platform": "feishu",
                            }
                        },
                    },
                },
            }
        },
    }
    config.write_text(json.dumps(original))
    config.chmod(0o600)

    def set_value(key, value):
        current = json.loads(config.read_text())
        keys = key.split(".")
        target = current
        for part in keys[:-1]:
            target = target[part]
        target[keys[-1]] = json.loads(value)
        config.write_text(json.dumps(current))

    api = SimpleNamespace(
        config=SimpleNamespace(
            get_config_path=lambda: config,
            is_managed=lambda: False,
            require_readable_config_before_write=lambda p: json.loads(p.read_text()),
            set_config_value=set_value,
        ),
        managed=SimpleNamespace(
            is_key_managed=lambda _key: False, managed_config_keys=lambda: set()
        ),
    )
    update = setup.PinUpdate(profile, api=api)
    update.preparation.plugins.mkdir(mode=0o700)
    update.preparation.target.mkdir(mode=0o700)
    for name, raw in update.preparation.source_files().items():
        (update.preparation.target / name).write_bytes(raw)
    key = "agent:main:feishu:dm:synthetic"
    binding_id = "binding_test"
    candidate = "new-session"
    fingerprint = setup.digest(
        json.dumps(
            [binding_id, key, "original", candidate, "feishu"], separators=(",", ":")
        ).encode()
    )
    return SimpleNamespace(
        update=update,
        config=config,
        socket=socket,
        original=original,
        key=key,
        binding_id=binding_id,
        candidate=candidate,
        fingerprint=fingerprint,
    )


def preview(case):
    return case.update.preview(
        binding_id=case.binding_id,
        session_key=case.key,
        candidate_fingerprint=case.fingerprint,
        candidate_session_id=case.candidate,
    )["plan_id"]


def test_one_pin_changes_only_after_stopped_gateway_confirmation(case):
    before = case.config.read_bytes()
    plan = preview(case)
    preview_status = case.update.status(plan, expected_binding_id=case.binding_id)
    assert preview_status["candidate_fingerprint"] == case.fingerprint
    assert preview_status["expires_at"] > 0
    assert "candidate_session_id" not in preview_status
    assert case.config.read_bytes() == before
    record = case.update.records / plan / "plan.json"
    assert record.stat().st_mode & 0o077 == 0
    assert json.loads(record.read_text())["candidate_session_id"] == case.candidate
    with pytest.raises(ValueError, match="confirmation"):
        case.update.apply(plan, expected_binding_id=case.binding_id)
    assert case.config.read_bytes() == before
    result = case.update.apply(
        plan, expected_binding_id=case.binding_id, confirmed=True, gateway_stopped=True
    )
    assert result["state"] == "configured_runtime_unverified"
    assert result["session_verified"] is False and result["delivery_verified"] is False
    current = json.loads(case.config.read_text())
    assert current["unrelated"] == case.original["unrelated"]
    assert current["plugins"]["entries"]["third-party"] == {"enabled": True}
    assert current["plugins"]["entries"]["agent-mailbox-host"]["settings"]["sessions"][
        case.key
    ] == {"session_id": case.candidate, "platform": "feishu"}
    with pytest.raises(ValueError, match="already_started"):
        case.update.apply(
            plan, expected_binding_id=case.binding_id, confirmed=True, gateway_stopped=True
        )
    assert (
        case.update.rollback(
            plan, expected_binding_id=case.binding_id, confirmed=True, gateway_stopped=True
        )["state"]
        == "rolled_back"
    )
    assert json.loads(case.config.read_text()) == case.original


def test_preview_refuses_candidate_identity_mismatch_without_record(case):
    with pytest.raises(ValueError, match="candidate_changed"):
        case.update.preview(
            binding_id=case.binding_id,
            session_key=case.key,
            candidate_fingerprint=case.fingerprint,
            candidate_session_id="different-session",
        )
    assert not case.update.records.exists()


def test_pin_status_does_not_report_preview_after_external_config_change(case):
    plan = preview(case)
    changed = {**case.original, "unrelated": {"preserve": "changed"}}
    case.config.write_text(json.dumps(changed))
    assert case.update.status(plan, expected_binding_id=case.binding_id)["state"] == (
        "inspection_required"
    )


def test_expired_unchanged_plan_can_be_refreshed_once_without_changing_pin(case, monkeypatch):
    plan = preview(case)
    before = case.config.read_bytes()
    record = case.update.records / plan / "plan.json"
    original = json.loads(record.read_text())
    monkeypatch.setattr(setup.time, "time", lambda: original["expires_at"] + 1)
    with pytest.raises(ValueError, match="plan_expired"):
        case.update.apply(
            plan, expected_binding_id=case.binding_id,
            confirmed=True, gateway_stopped=True,
        )
    refreshed = case.update.refresh(
        plan, expected_binding_id=case.binding_id,
        expected_candidate_fingerprint=case.fingerprint, confirmed=True,
    )
    assert refreshed["state"] == "preview"
    assert refreshed["expires_at"] > original["expires_at"]
    assert json.loads(record.read_text()) == original
    assert case.config.read_bytes() == before
    with pytest.raises(ValueError, match="already_refreshed"):
        case.update.refresh(
            plan, expected_binding_id=case.binding_id,
            expected_candidate_fingerprint=case.fingerprint, confirmed=True,
        )
    assert case.update.apply(
        plan, expected_binding_id=case.binding_id,
        confirmed=True, gateway_stopped=True,
    )["state"] == "configured_runtime_unverified"


def test_refresh_refuses_changed_config_or_candidate(case):
    plan = preview(case)
    with pytest.raises(ValueError, match="candidate_changed"):
        case.update.refresh(
            plan, expected_binding_id=case.binding_id,
            expected_candidate_fingerprint="different", confirmed=True,
        )
    before = case.config.read_bytes()
    case.config.write_bytes(before + b" ")
    with pytest.raises(ValueError, match="configuration_changed"):
        case.update.refresh(
            plan, expected_binding_id=case.binding_id,
            expected_candidate_fingerprint=case.fingerprint, confirmed=True,
        )
    assert not (case.update.records / plan / "renewal.json").exists()


@pytest.mark.parametrize("changed", ["config", "socket"])
def test_changed_candidate_config_or_live_socket_refuses_without_write(case, changed):
    plan = preview(case)
    before = case.config.read_bytes()
    if changed == "config":
        case.config.write_bytes(before + b" ")
    else:
        case.socket.touch()
    observed = case.config.read_bytes()
    with pytest.raises(ValueError):
        case.update.apply(
            plan, expected_binding_id=case.binding_id, confirmed=True, gateway_stopped=True
        )
    assert case.config.read_bytes() == observed
    assert not (case.update.records / plan / "intent.json").exists()


def test_interrupted_record_after_config_write_stays_inspectable(case, monkeypatch):
    plan = preview(case)
    write = setup.write_new

    def interrupt(path, raw):
        if Path(path).name == "configured.json":
            raise OSError("synthetic record interruption")
        return write(path, raw)

    with monkeypatch.context() as patch:
        patch.setattr(setup, "write_new", interrupt)
        with pytest.raises(ValueError, match="incomplete"):
            case.update.apply(
                plan, expected_binding_id=case.binding_id, confirmed=True, gateway_stopped=True
            )
    assert case.update.status(plan)["state"] == "configured_runtime_unverified"
    with pytest.raises(ValueError, match="already_started"):
        case.update.apply(
            plan, expected_binding_id=case.binding_id, confirmed=True, gateway_stopped=True
        )


def test_third_party_change_after_apply_is_not_called_verified(case):
    plan = preview(case)
    case.update.apply(
        plan, expected_binding_id=case.binding_id, confirmed=True, gateway_stopped=True
    )
    current = json.loads(case.config.read_text())
    current["unrelated"]["preserve"] = "third-party-change"
    case.config.write_text(json.dumps(current))
    assert case.update.status(plan)["state"] == "inspection_required"
    with pytest.raises(ValueError, match="state_conflict"):
        case.update.rollback(
            plan, expected_binding_id=case.binding_id, confirmed=True, gateway_stopped=True
        )


def test_unknown_installed_component_refuses_preview(case):
    (case.update.preparation.target / "__init__.py").write_bytes(b"different plugin")
    with pytest.raises(ValueError, match="installed_component_changed"):
        preview(case)
    assert not case.update.records.exists()


def test_other_binding_cannot_apply_an_existing_plan(case):
    plan = preview(case)
    before = case.config.read_bytes()
    with pytest.raises(ValueError, match="binding_changed"):
        case.update.apply(
            plan, expected_binding_id="binding_other", confirmed=True, gateway_stopped=True
        )
    assert case.config.read_bytes() == before


def test_status_cannot_certify_a_different_binding(case):
    plan = preview(case)
    case.update.apply(
        plan, expected_binding_id=case.binding_id, confirmed=True, gateway_stopped=True
    )
    with pytest.raises(ValueError, match="binding_changed"):
        case.update.status(plan, expected_binding_id="binding_other")
    assert case.update.status(plan, expected_binding_id=case.binding_id)["state"] == (
        "configured_runtime_unverified"
    )


def test_rollback_record_interruption_is_recovered_without_second_write(case, monkeypatch):
    plan = preview(case)
    case.update.apply(
        plan, expected_binding_id=case.binding_id, confirmed=True, gateway_stopped=True
    )
    write = setup.write_new

    def interrupt(path, raw):
        if Path(path).name == "rolled_back.json":
            raise OSError("synthetic rollback record interruption")
        return write(path, raw)

    with monkeypatch.context() as patch:
        patch.setattr(setup, "write_new", interrupt)
        with pytest.raises(ValueError, match="rollback_incomplete"):
            case.update.rollback(
                plan, expected_binding_id=case.binding_id, confirmed=True, gateway_stopped=True
            )
    assert case.update.status(plan)["state"] == "rollback_completed_unrecorded"
    before = case.config.read_bytes()
    assert (
        case.update.rollback(
            plan, expected_binding_id=case.binding_id, confirmed=True, gateway_stopped=True
        )["state"]
        == "rolled_back"
    )
    assert case.config.read_bytes() == before


def test_real_hermes_interpreter_updates_only_synthetic_profile(tmp_path):
    from agent_mailbox import workbench_hermes_process as process

    python = Path.home() / ".hermes/hermes-agent/.venv/bin/python"
    if os.name == "nt" or not python.exists():
        pytest.skip("Local Hermes interpreter unavailable")
    profile = tmp_path / "hermes-profile"
    profile.mkdir(mode=0o700)
    config = profile / "config.yaml"
    key = "agent:main:feishu:dm:synthetic"
    original = {
        "unrelated": {"keep": "yes"},
        "plugins": {
            "entries": {
                "agent-mailbox-host": {
                    "allow_gateway_injection": True,
                    "settings": {
                        "home": str(tmp_path / "mailbox"),
                        "socket_path": str(profile / "receiver.sock"),
                        "sessions": {key: {"session_id": "old-synthetic", "platform": "feishu"}},
                    },
                },
            }
        },
    }
    config.write_text(json.dumps(original))
    config.chmod(0o600)
    source = process.payload()
    pins = {name: setup.digest((source / name).read_bytes()) for name in process.FILES}
    target = profile / "plugins/agent-mailbox-host"
    target.mkdir(parents=True, mode=0o700)
    for name in setup.FILES:
        (target / name).write_bytes((source / name).read_bytes())
    fingerprint = setup.digest(
        json.dumps(
            [
                "binding_synthetic",
                key,
                "old-synthetic",
                "new-synthetic",
                "feishu",
            ],
            separators=(",", ":"),
        ).encode()
    )

    def call(operation, **arguments):
        return process.run(
            python,
            profile,
            {"version": 1, "operation": operation, "profile": str(profile), **arguments},
            source=source,
            expected_files=pins,
        )

    plan = call(
        "pin_preview",
        options={
            "binding_id": "binding_synthetic",
            "session_key": key,
            "candidate_fingerprint": fingerprint,
            "candidate_session_id": "new-synthetic",
        },
    )["plan_id"]
    assert json.loads(config.read_text()) == original
    result = call(
        "pin_apply",
        plan_id=plan,
        expected_binding_id="binding_synthetic",
        confirmed=True,
        gateway_stopped=True,
    )
    assert result["state"] == "configured_runtime_unverified"
    assert call("pin_status", plan_id=plan, expected_binding_id="binding_synthetic")["state"] == (
        "configured_runtime_unverified"
    )
    with pytest.raises(ValueError):
        call("pin_status", plan_id=plan, expected_binding_id="binding_other")
    changed = json.loads(config.read_text())
    assert changed["unrelated"] == original["unrelated"]
    assert (
        changed["plugins"]["entries"]["agent-mailbox-host"]["settings"]["sessions"][key][
            "session_id"
        ]
        == "new-synthetic"
    )
    assert (
        call(
            "pin_rollback",
            plan_id=plan,
            expected_binding_id="binding_synthetic",
            confirmed=True,
            gateway_stopped=True,
        )["state"]
        == "rolled_back"
    )
    assert json.loads(config.read_text()) == original
