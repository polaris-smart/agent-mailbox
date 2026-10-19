"""First-install filesystem safety; host writes simulated here, upstream run separately."""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

if sys.platform == "win32":
    pytest.skip("Hermes Unix adapter is not Windows setup", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "hermes_setup", ROOT / "integrations/hermes-mailbox-host/setup.py"
)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


@pytest.fixture
def case():
    root = Path(tempfile.mkdtemp(prefix="hs-")).resolve()
    profile = root / "profile"
    profile.mkdir(mode=0o700)
    config = profile / "config.yaml"
    initial = {
        "display": {"language": "en-US"},
        "plugins": {"disabled": ["graft"]},
        "mcp_servers": {"unrelated": {"command": "EXISTING", "args": []}},
    }
    config.write_text(json.dumps(initial))
    config.chmod(0o600)

    def write(key, value):
        current = json.loads(config.read_text())
        keys = key.split(".")
        target = current
        for k in keys[:-1]:
            target = target.setdefault(k, {})
        target[keys[-1]] = json.loads(value)
        config.write_text(json.dumps(current))

    def remove(key):
        current = json.loads(config.read_text())
        keys = key.split(".")
        target = current
        for k in keys[:-1]:
            target = target[k]
        del target[keys[-1]]
        config.write_text(json.dumps(current))

    api = SimpleNamespace(
        config=SimpleNamespace(
            get_config_path=lambda: config,
            is_managed=lambda: False,
            require_readable_config_before_write=lambda p: json.loads(p.read_text()),
            set_config_value=write,
            unset_config_value=remove,
        ),
        managed=SimpleNamespace(
            is_key_managed=lambda key: False, managed_config_keys=lambda: set()
        ),
    )
    for name in ("mailbox", "state", "ipc", "credentials"):
        (root / name).mkdir(mode=0o700)
    command = root / "mailbox"
    command = command / "cli"
    command.write_text("#!/bin/sh\nexit 0\n")
    command.chmod(0o700)
    session = root / "credentials/session.json"
    session.write_text("SYNTHETIC_PRIVATE")
    session.chmod(0o600)
    args = {
        "mailbox_home": root / "mailbox",
        "state_dir": root / "state",
        "socket_path": root / "ipc/receiver.sock",
        "sessions": {"synthetic-key": {"session_id": "synthetic-session", "platform": "feishu"}},
        "command": command,
        "session_file": session,
    }
    adapter = setup.Preparation(profile, api=api)
    try:
        yield SimpleNamespace(
            root=root,
            profile=profile,
            config=config,
            initial=initial,
            api=api,
            adapter=adapter,
            args=args,
        )
    finally:
        shutil.rmtree(root)


def preview(c):
    return c.adapter.preview(**c.args)["plan_id"]


def test_preparation_preserves_other_settings_and_does_not_enable(case):
    before = case.config.read_bytes()
    plan = preview(case)
    assert case.config.read_bytes() == before and not case.adapter.target.exists()
    result = case.adapter.apply(plan, confirmed=True)
    assert result["state"] == "prepared_not_activated"
    assert not any(
        result[k] for k in ("activation_requested", "session_verified", "delivery_verified")
    )
    current = json.loads(case.config.read_text())
    assert setup.without_entries(current) == case.initial
    assert "enabled" not in current["plugins"]
    assert current["plugins"]["entries"][setup.PLUGIN]["allow_gateway_injection"] is True
    assert current["mcp_servers"]["agent-mailbox"]["args"][0] == "mailbox-mcp"
    for name in setup.FILES:
        assert (case.adapter.target / name).read_bytes() == (
            case.adapter.source / name
        ).read_bytes()
    folder = case.adapter.records / plan
    assert (folder / "config.before").read_bytes() == before
    assert (folder / "config.before").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mutation", ["config", "session", "command", "component"])
def test_change_after_preview_refuses_before_publication(case, mutation, monkeypatch):
    plan = preview(case)
    if mutation == "config":
        case.config.write_text(case.config.read_text() + " ")
    elif mutation == "session":
        case.args["session_file"].write_text("REPLACED")
    elif mutation == "command":
        case.args["command"].write_text("REPLACED")
    else:
        original = case.adapter.source_files()
        monkeypatch.setattr(
            case.adapter, "source_files", lambda: {**original, "README.md": b"changed"}
        )
    with pytest.raises(ValueError, match="changed"):
        case.adapter.apply(plan, confirmed=True)
    assert not case.adapter.target.exists()


@pytest.mark.parametrize(
    "kind", ["empty_entry", "mailbox_alias", "enabled", "installed", "managed", "managed_child"]
)
def test_existing_or_managed_configuration_not_overwritten(case, kind):
    value = case.initial
    if kind == "empty_entry":
        value["mcp_servers"]["agent-mailbox"] = {}
    elif kind == "mailbox_alias":
        value["mcp_servers"]["alias"] = {"command": "x", "args": ["mailbox-mcp"]}
    elif kind == "enabled":
        value["plugins"]["enabled"] = [setup.PLUGIN]
    elif kind == "installed":
        case.adapter.plugins.mkdir(mode=0o700)
        case.adapter.target.mkdir(mode=0o700)
    elif kind == "managed":
        case.api.managed.is_key_managed = lambda key: key.endswith("allow_gateway_injection")
    else:
        case.api.managed.managed_config_keys = lambda: {setup.MCP_KEY + ".env.PRIVATE_MANAGED"}
    case.config.write_text(json.dumps(value))
    before = case.config.read_bytes()
    with pytest.raises(ValueError):
        preview(case)
    assert case.config.read_bytes() == before and not case.adapter.records.exists()


def test_requires_confirmation_and_scope(case):
    plan = preview(case)
    with pytest.raises(ValueError, match="confirmation"):
        case.adapter.apply(plan)
    with pytest.raises(ValueError, match="plan_id"):
        case.adapter.status("../plan.json")
    assert not case.adapter.target.exists()


def test_interrupted_write_is_not_retried_or_rolled_back_over_new_content(case):
    plan = preview(case)
    real = case.api.config.set_config_value
    calls = []

    def interrupted(key, value):
        calls.append(key)
        real(key, value)
        raise OSError("private host failure")

    case.api.config.set_config_value = interrupted
    with pytest.raises(ValueError, match="incomplete"):
        case.adapter.apply(plan, confirmed=True)
    assert case.adapter.status(plan)["state"] == "preparation_incomplete"
    value = case.config.read_text()
    with pytest.raises((ValueError, FileExistsError)):
        case.adapter.apply(plan, confirmed=True)
    assert len(calls) == 1 and case.config.read_text() == value
    assert (case.adapter.records / plan / "config.before").exists()


def test_concurrent_unrelated_edit_during_writer_is_retained_not_claimed_ready(case):
    plan = preview(case)
    real = case.api.config.set_config_value

    def changed(key, value):
        real(key, value)
        current = json.loads(case.config.read_text())
        current["display"]["language"] = "zh-CN"
        case.config.write_text(json.dumps(current))

    case.api.config.set_config_value = changed
    with pytest.raises(ValueError, match="incomplete"):
        case.adapter.apply(plan, confirmed=True)
    assert json.loads(case.config.read_text())["display"]["language"] == "zh-CN"
    assert case.adapter.status(plan)["state"] == "preparation_incomplete"


@pytest.mark.parametrize(
    "unsafe",
    ["config_link", "config_fifo", "config_public", "session_link", "source_link", "session_cli"],
)
def test_unsafe_inputs_are_refused(case, unsafe):
    if unsafe == "config_link":
        original = case.profile / "original"
        case.config.rename(original)
        case.config.symlink_to(original)
    elif unsafe == "config_fifo":
        case.config.unlink()
        os.mkfifo(case.config, 0o600)
    elif unsafe == "config_public":
        case.config.chmod(0o644)
    elif unsafe == "session_link":
        original = case.args["session_file"]
        link = original.parent / "link"
        link.symlink_to(original)
        case.args["session_file"] = link
    elif unsafe == "source_link":
        link = case.root / "source-link"
        link.symlink_to(case.adapter.source)
        case.adapter.source = link
    else:
        case.args["sessions"]["synthetic-key"]["platform"] = "cli"
    with pytest.raises((ValueError, OSError)):
        preview(case)
    assert not case.adapter.records.exists()


def test_status_detects_external_removal(case):
    plan = preview(case)
    case.adapter.apply(plan, confirmed=True)
    (case.adapter.target / "plugin.yaml").unlink()
    assert case.adapter.status(plan)["state"] == "configuration_changed"


@pytest.mark.parametrize("partial", [False, True])
def test_remove_owned_preparation_preserves_unrelated_config_and_backup(case, partial):
    plan = preview(case)
    original = case.config.read_bytes()
    if partial:
        real = case.api.config.set_config_value

        def fail(key, value):
            real(key, value)
            raise OSError("interrupted")

        case.api.config.set_config_value = fail
        with pytest.raises(ValueError):
            case.adapter.apply(plan, confirmed=True)
    else:
        case.adapter.apply(plan, confirmed=True)
    result = case.adapter.remove(plan, confirmed=True)
    assert result["state"] == "preparation_removed" and not case.adapter.target.exists()
    assert setup.without_entries(json.loads(case.config.read_text())) == case.initial
    assert (case.adapter.records / plan / "config.before").read_bytes() == original
    assert case.adapter.remove(plan, confirmed=True) == result
    with pytest.raises(ValueError, match="closed"):
        case.adapter.apply(plan, confirmed=True)


@pytest.mark.parametrize(
    "change", ["other_config", "own_config", "empty_own_config", "component", "enabled"]
)
def test_remove_refuses_external_changes(case, change):
    plan = preview(case)
    case.adapter.apply(plan, confirmed=True)
    current = json.loads(case.config.read_text())
    if change == "other_config":
        current["display"]["language"] = "zh-CN"
    elif change == "own_config":
        current["mcp_servers"]["agent-mailbox"]["command"] = "OTHER"
    elif change == "empty_own_config":
        current["mcp_servers"]["agent-mailbox"] = {}
    elif change == "enabled":
        current["plugins"]["enabled"] = [setup.PLUGIN]
    else:
        (case.adapter.target / "plugin.yaml").write_text("MODIFIED")
    case.config.write_text(json.dumps(current))
    before = case.config.read_bytes()
    with pytest.raises(ValueError, match="changed"):
        case.adapter.remove(plan, confirmed=True)
    assert case.config.read_bytes() == before and case.adapter.target.exists()


def test_cancel_plan_and_operation_lock(case):
    plan = preview(case)
    with case.adapter.operation(), pytest.raises(ValueError, match="busy"):
        case.adapter.apply(plan, confirmed=True)
    assert case.adapter.remove(plan, confirmed=True)["state"] == "preparation_removed"
    with pytest.raises(ValueError, match="closed"):
        case.adapter.apply(plan, confirmed=True)


@pytest.mark.parametrize("value", [None, "--unexpected", [None]])
def test_malformed_existing_mcp_arguments_fail_before_any_preparation(case, value):
    case.initial["mcp_servers"]["unrelated"]["args"] = value
    case.config.write_text(json.dumps(case.initial))
    with pytest.raises(ValueError, match="configuration_shape_conflict"):
        preview(case)
    assert not case.adapter.records.exists()


def test_failed_publication_does_not_own_later_matching_installation(case):
    plan = preview(case)
    # Another installer wins after preview, with byte-identical approved files.
    case.adapter.plugins.mkdir(mode=0o700)
    case.adapter.target.mkdir(mode=0o700)
    for name, data in case.adapter.source_files().items():
        (case.adapter.target / name).write_bytes(data)
    with pytest.raises(ValueError, match="incomplete"):
        case.adapter.apply(plan, confirmed=True)
    with pytest.raises(ValueError, match="component_ownership_unconfirmed"):
        case.adapter.remove(plan, confirmed=True)
    assert case.adapter.target.exists()
    assert json.loads(case.config.read_text()) == case.initial


def test_matching_external_entry_without_write_intent_is_not_removed(case):
    plan = preview(case)
    folder, record = case.adapter.load(plan)
    setup.write_new(folder / "started.json", b"{}")
    # A startup claim alone does not prove that this adapter wrote configuration.
    case.api.config.set_config_value(setup.MCP_KEY, json.dumps(record["entries"][setup.MCP_KEY]))
    before = case.config.read_bytes()
    with pytest.raises(ValueError, match="configuration_ownership_unconfirmed"):
        case.adapter.remove(plan, confirmed=True)
    assert case.config.read_bytes() == before


def test_replaced_component_directory_is_not_removed_even_with_identical_bytes(case):
    plan = preview(case)
    case.adapter.apply(plan, confirmed=True)
    case.adapter.target.rename(case.adapter.target.with_name("other-install"))
    case.adapter.target.mkdir(mode=0o700)
    for name, data in case.adapter.source_files().items():
        (case.adapter.target / name).write_bytes(data)
    before = case.config.read_bytes()
    with pytest.raises(ValueError, match="component_ownership_unconfirmed"):
        case.adapter.remove(plan, confirmed=True)
    assert case.config.read_bytes() == before and case.adapter.target.exists()


def test_discovery_prepare_has_no_credentials_grant_or_mcp_write(case):
    case.args["command"].unlink()
    case.args["session_file"].unlink()
    before = case.config.read_bytes()
    result = case.adapter.preview_discovery(socket_path=case.args["socket_path"])
    plan = result["plan_id"]
    assert result["will_write_injection_grant"] is False
    assert case.config.read_bytes() == before
    record = json.loads((case.adapter.records / plan / "plan.json").read_text())
    assert set(record["entries"]) == {setup.PLUGIN_KEY}
    assert not {"session_file_sha256", "command_sha256"} & set(record)
    assert case.adapter.apply(plan, confirmed=True)["state"] == "prepared_not_activated"
    current = json.loads(case.config.read_text())
    assert current["mcp_servers"] == case.initial["mcp_servers"]
    entry = current["plugins"]["entries"][setup.PLUGIN]
    assert entry == {
        "allow_gateway_injection": False,
        "settings": {"discovery_only": True, "socket_path": str(case.args["socket_path"])},
    }
    assert "enabled" not in current["plugins"]
    assert not case.args["socket_path"].exists()
    assert case.adapter.remove(plan, confirmed=True)["state"] == "preparation_removed"
    assert not case.adapter.target.exists()
    assert (
        setup.without_entries(json.loads(case.config.read_text()), [setup.PLUGIN_KEY])
        == case.initial
    )


@pytest.mark.parametrize("mutation", ["grant", "pin", "mode", "mcp_during_write", "endpoint"])
def test_discovery_rejects_permission_changes_and_external_side_effects(case, mutation):
    plan = case.adapter.preview_discovery(socket_path=case.args["socket_path"])["plan_id"]
    record = case.adapter.records / plan / "plan.json"
    data = json.loads(record.read_text())
    entry = data["entries"][setup.PLUGIN_KEY]
    if mutation == "grant":
        entry["allow_gateway_injection"] = True
    elif mutation == "pin":
        entry["settings"]["sessions"] = case.args["sessions"]
    elif mutation == "mode":
        entry["settings"]["discovery_only"] = False
    elif mutation == "mcp_during_write":
        original = case.api.config.set_config_value

        def changed(key, value):
            original(key, value)
            original(setup.MCP_KEY, json.dumps({"command": "UNOWNED"}))

        case.api.config.set_config_value = changed
    else:
        case.args["socket_path"].write_text("appeared")
    record.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        case.adapter.apply(plan, confirmed=True)
    assert not (record.parent / "prepared.json").exists()
    if mutation == "mcp_during_write":
        before = case.config.read_bytes()
        with pytest.raises(ValueError, match="unrelated_configuration_changed"):
            case.adapter.remove(plan, confirmed=True)
        assert case.config.read_bytes() == before
    else:
        assert not case.adapter.target.exists()


def test_discovery_remove_does_not_ignore_unowned_mailbox_config(case):
    plan = case.adapter.preview_discovery(socket_path=case.args["socket_path"])["plan_id"]
    case.adapter.apply(plan, confirmed=True)
    case.api.config.set_config_value(setup.MCP_KEY, json.dumps({"command": "EXTERNAL"}))
    before = case.config.read_bytes()
    with pytest.raises(ValueError, match="unrelated_configuration_changed"):
        case.adapter.remove(plan, confirmed=True)
    assert case.config.read_bytes() == before
    assert case.adapter.target.is_dir()


def test_diagnostic_admission_requires_stopped_gateway_and_official_pm(case):
    prepared = case.adapter.preview_discovery(socket_path=case.args["socket_path"])["plan_id"]
    case.adapter.apply(prepared, confirmed=True)
    calls = []

    def official_admission(enabled, disabled, *, active_plugins_dir, expected_config, plugin):
        calls.append((enabled, disabled, active_plugins_dir, expected_config, plugin))
        assert active_plugins_dir == case.adapter.plugins
        assert expected_config == setup.digest(case.config.read_bytes())
        assert plugin == setup.PLUGIN
        case.api.config.set_config_value("plugins.enabled", json.dumps(sorted(enabled)))

    admission = setup.DiagnosticAdmission(case.profile, api=case.api, admit=official_admission)
    plan = admission.preview(prepared)
    assert plan["state"] == "preview" and plan["runtime_verified"] is False
    assert json.loads(case.config.read_text())["plugins"].get("enabled") is None
    with pytest.raises(ValueError, match="stopped_gateway"):
        admission.apply(plan["plan_id"], confirmed=True)
    assert not calls
    result = admission.apply(plan["plan_id"], confirmed=True, gateway_stopped=True)
    assert result["state"] == "diagnostic_enabled_restart_required"
    assert all(
        result[key] is False
        for key in ("runtime_verified", "session_verified", "delivery_verified")
    )
    assert len(calls) == 1 and calls[0][0] == {setup.PLUGIN}
    current = json.loads(case.config.read_text())
    assert current["plugins"]["enabled"] == [setup.PLUGIN]
    assert current["plugins"]["entries"][setup.PLUGIN]["settings"]["discovery_only"] is True
    assert current["plugins"]["entries"][setup.PLUGIN]["allow_gateway_injection"] is False
    assert not case.args["socket_path"].exists()
    with pytest.raises(ValueError, match="already_started|inspect_status"):
        admission.apply(plan["plan_id"], confirmed=True, gateway_stopped=True)
    assert len(calls) == 1


def test_diagnostic_admission_unknown_is_not_replayed(case):
    prepared = case.adapter.preview_discovery(socket_path=case.args["socket_path"])["plan_id"]
    case.adapter.apply(prepared, confirmed=True)
    calls = []

    def interrupted(enabled, disabled, **_kwargs):
        calls.append((enabled, disabled))
        case.api.config.set_config_value("plugins.enabled", json.dumps(sorted(enabled)))
        raise OSError("unknown_pm_result")

    admission = setup.DiagnosticAdmission(case.profile, api=case.api, admit=interrupted)
    plan = admission.preview(prepared)["plan_id"]
    with pytest.raises(ValueError, match="admission_unconfirmed"):
        admission.apply(plan, confirmed=True, gateway_stopped=True)
    assert admission.status(plan)["state"] == "admission_unknown"
    with pytest.raises(ValueError, match="inspect_status"):
        admission.apply(plan, confirmed=True, gateway_stopped=True)
    assert len(calls) == 1


def test_diagnostic_admission_rollback_restores_selection_and_allows_remove(case):
    prepared = case.adapter.preview_discovery(socket_path=case.args["socket_path"])["plan_id"]
    case.adapter.apply(prepared, confirmed=True)
    prepared_config = json.loads(case.config.read_text())
    calls = []

    def official_admission(enabled, disabled, *, expected_config, **_kwargs):
        assert expected_config == setup.digest(case.config.read_bytes())
        calls.append((set(enabled), set(disabled)))
        case.api.config.set_config_value("plugins.enabled", json.dumps(sorted(enabled)))
        case.api.config.set_config_value("plugins.disabled", json.dumps(sorted(disabled)))

    admission = setup.DiagnosticAdmission(case.profile, api=case.api, admit=official_admission)
    plan = admission.preview(prepared)["plan_id"]
    admission.apply(plan, confirmed=True, gateway_stopped=True)
    with pytest.raises(ValueError, match="stopped_gateway"):
        admission.rollback(plan, confirmed=True)
    assert len(calls) == 1
    assert admission.rollback(plan, confirmed=True, gateway_stopped=True)["state"] == "rolled_back"
    assert calls == [({setup.PLUGIN}, {"graft"}), (set(), {"graft"})]
    assert json.loads(case.config.read_text()) == prepared_config
    assert case.adapter.remove(prepared, confirmed=True)["state"] == "preparation_removed"
    with pytest.raises(ValueError, match="inspect_status"):
        admission.rollback(plan, confirmed=True, gateway_stopped=True)


def test_diagnostic_admission_rollback_refuses_live_socket(case):
    prepared = case.adapter.preview_discovery(socket_path=case.args["socket_path"])["plan_id"]
    case.adapter.apply(prepared, confirmed=True)
    calls = []

    def official_admission(enabled, disabled, **_kwargs):
        calls.append(1)
        case.api.config.set_config_value("plugins.enabled", json.dumps(sorted(enabled)))
        case.api.config.set_config_value("plugins.disabled", json.dumps(sorted(disabled)))

    admission = setup.DiagnosticAdmission(case.profile, api=case.api, admit=official_admission)
    plan = admission.preview(prepared)["plan_id"]
    admission.apply(plan, confirmed=True, gateway_stopped=True)
    case.args["socket_path"].write_text("synthetic_socket_marker")
    with pytest.raises(ValueError, match="gateway_receiver_still_present"):
        admission.rollback(plan, confirmed=True, gateway_stopped=True)
    assert len(calls) == 1
    assert not (admission.records / plan / "rollback-intent.json").exists()


def test_diagnostic_admission_refuses_changed_or_disabled_config(case):
    prepared = case.adapter.preview_discovery(socket_path=case.args["socket_path"])["plan_id"]
    case.adapter.apply(prepared, confirmed=True)
    admission = setup.DiagnosticAdmission(
        case.profile, api=case.api, admit=lambda *_args, **_kwargs: pytest.fail("PM called")
    )
    plan = admission.preview(prepared)["plan_id"]
    config = json.loads(case.config.read_text())
    config["display"]["language"] = "zh-CN"
    case.config.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="changed"):
        admission.apply(plan, confirmed=True, gateway_stopped=True)
    assert admission.status(plan)["state"] == "inspection_required"
    config["plugins"]["disabled"].append(setup.PLUGIN)
    case.config.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="changed|requires_review"):
        admission.apply(plan, confirmed=True, gateway_stopped=True)


def grant_case(case):
    case.adapter.apply(preview(case), confirmed=True)
    config = json.loads(case.config.read_text())
    config["plugins"]["entries"][setup.PLUGIN]["allow_gateway_injection"] = False
    case.config.write_text(json.dumps(config))
    return setup.InjectionGrant(case.profile, api=case.api), {
        "mailbox_home": case.args["mailbox_home"],
        "session_key": "synthetic-key",
    }


def test_grant_preview_and_enable_preserve_every_other_setting(case):
    grant, scope = grant_case(case)
    before = case.config.read_bytes()
    files = set(case.profile.rglob("*"))
    result = grant.preview(**scope)
    assert result["state"] == "grant_preview" and result["enabled"] is False
    assert case.config.read_bytes() == before and set(case.profile.rglob("*")) == files
    applied = grant.apply(**scope, expected_config_sha256=result["config_sha256"], confirmed=True)
    expected = json.loads(before)
    expected["plugins"]["entries"][setup.PLUGIN]["allow_gateway_injection"] = True
    assert json.loads(case.config.read_text()) == expected
    assert applied["enabled"] is True and applied["runtime_verified"] is False
    assert applied["delivery_verified"] is False
    backups = list((case.profile / "mailbox-connect-backups").glob("*.yaml"))
    assert len(backups) == 1 and backups[0].read_bytes() == before
    assert backups[0].stat().st_mode & 0o777 == 0o600
    again = grant.apply(**scope, expected_config_sha256=applied["config_sha256"], confirmed=True)
    assert again["state"] == "already_enabled"
    assert list((case.profile / "mailbox-connect-backups").glob("*.yaml")) == backups


@pytest.mark.parametrize("confirmation", [False, "true", 1, None])
def test_grant_requires_explicit_boolean_confirmation(case, confirmation):
    grant, scope = grant_case(case)
    before = case.config.read_bytes()
    with pytest.raises(ValueError, match="explicit_confirmation_required"):
        grant.apply(**scope, expected_config_sha256=setup.digest(before), confirmed=confirmation)
    assert case.config.read_bytes() == before


def test_grant_refuses_configuration_drift_without_losing_change(case):
    grant, scope = grant_case(case)
    previewed = grant.preview(**scope)
    changed = json.loads(case.config.read_text())
    changed["display"]["language"] = "zh-CN"
    case.config.write_text(json.dumps(changed))
    before = case.config.read_bytes()
    with pytest.raises(ValueError, match="configuration_changed_refresh_required"):
        grant.apply(**scope, expected_config_sha256=previewed["config_sha256"], confirmed=True)
    assert case.config.read_bytes() == before
    assert not (case.profile / "mailbox-connect-backups").exists()


@pytest.mark.parametrize(
    "change", ["other_home", "other_conversation", "multiple_conversations", "discovery", "managed"]
)
def test_grant_cannot_enroll_or_expand_scope(case, change):
    grant, scope = grant_case(case)
    current = json.loads(case.config.read_text())
    settings = current["plugins"]["entries"][setup.PLUGIN]["settings"]
    if change == "other_home":
        scope["mailbox_home"] = case.root / "state"
    if change == "other_conversation":
        scope["session_key"] = "wrong-key"
    if change == "multiple_conversations":
        settings["sessions"]["other-key"] = dict(settings["sessions"]["synthetic-key"])
    if change == "discovery":
        settings["discovery_only"] = True
    if change == "managed":
        case.api.managed.managed_config_keys = lambda: {
            setup.PLUGIN_KEY + ".allow_gateway_injection"
        }
    case.config.write_text(json.dumps(current))
    before = case.config.read_bytes()
    with pytest.raises(ValueError):
        grant.preview(**scope)
    with pytest.raises(ValueError):
        grant.apply(**scope, expected_config_sha256=setup.digest(before), confirmed=True)
    assert case.config.read_bytes() == before


def test_grant_detects_unexpected_official_write_and_retains_original(case):
    grant, scope = grant_case(case)
    before = case.config.read_bytes()
    original_write = case.api.config.set_config_value

    def bad_write(key, value):
        original_write(key, value)
        changed = json.loads(case.config.read_text())
        changed["display"]["language"] = "unexpected"
        case.config.write_text(json.dumps(changed))

    case.api.config.set_config_value = bad_write
    with pytest.raises(ValueError, match="grant_write_unconfirmed_backup_retained"):
        grant.apply(**scope, expected_config_sha256=setup.digest(before), confirmed=True)
    assert next((case.profile / "mailbox-connect-backups").glob("*.yaml")).read_bytes() == before
    assert json.loads(case.config.read_text())["display"]["language"] == "unexpected"
