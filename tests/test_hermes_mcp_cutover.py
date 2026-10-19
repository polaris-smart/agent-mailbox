"""An MCP command plan cannot turn a browser assertion into stopped-host evidence."""

import hashlib
import json
import plistlib
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_mailbox import workbench_hermes_mcp_cutover as cutover


@pytest.fixture
def scope(monkeypatch, tmp_path):
    component = tmp_path / "bridge.py"
    component.write_bytes(b"frozen synthetic bridge")
    monkeypatch.setattr(
        cutover.catalog, "resolve", lambda *args: (Path("/hermes/python"), tmp_path)
    )
    binding = {"id": "binding-1", "host_session_id": "session-1"}
    monkeypatch.setattr(cutover, "_binding", lambda *args: binding)
    monkeypatch.setattr(cutover, "endpoint_for", lambda *args: "/private/receiver.sock")
    monkeypatch.setattr(cutover, "_exclusive_cutover", lambda *args: nullcontext())
    monkeypatch.setattr(cutover.process, "payload", lambda: tmp_path)
    monkeypatch.setattr(cutover.process, "FILES", ("bridge.py",))
    monkeypatch.setattr(cutover, "_installed_command", lambda: Path("/installed/Agent Mailbox"))
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return {
            "state": "configured_runtime_unverified",
            "runtime_verified": False,
            "session_verified": False,
            "delivery_verified": False,
        }

    monkeypatch.setattr(cutover.process, "run", run)
    control = SimpleNamespace(
        runtime=lambda: {
            "pid": 42,
            "kind": "hermes-gateway",
            "gateway_state": "stopped",
            "hermes_home": str(tmp_path),
        },
        service_stopped=lambda: True,
        original_pid_gone=lambda pid: pid == 42,
        receiver_socket_absent=lambda endpoint: endpoint == "/private/receiver.sock",
    )
    factory = lambda *args, **kwargs: control
    store = SimpleNamespace(root=tmp_path)
    return store, control, factory, calls


def invoke(scope, action, **kwargs):
    store, _control, factory, _calls = scope
    return cutover.operation(
        store, "employee", "project", "profile", action, control_factory=factory, **kwargs
    )


def test_preview_fixes_target_and_never_claims_a_stopped_gateway(scope):
    result = invoke(scope, "preview")
    request = scope[3][0][0][2]
    assert result["runtime_verified"] is False
    assert request["operation"] == "mcp_command_preview"
    assert request["new_command"] == "/installed/Agent Mailbox"
    assert "gateway_stopped" not in request
    with pytest.raises(cutover.WorkbenchError, match="preview has no existing plan"):
        invoke(scope, "preview", plan_id="plan_" + "a" * 32)


def test_running_or_ambiguous_gateway_cannot_apply_or_rollback(scope):
    _, control, _, calls = scope
    plan = "plan_" + "a" * 32
    assert invoke(scope, "apply", plan_id=plan)["state"] == "confirmation_required"
    saved_runtime = control.runtime
    control.runtime = lambda: {**saved_runtime(), "hermes_home": "/different/profile"}
    assert (
        invoke(scope, "apply", plan_id=plan, confirmed=True)["state"] == "gateway_stop_unconfirmed"
    )
    control.runtime = lambda: {**saved_runtime(), "gateway_state": "running"}
    assert (
        invoke(scope, "apply", plan_id=plan, confirmed=True)["state"] == "gateway_stop_unconfirmed"
    )
    control.runtime = saved_runtime
    control.service_stopped = lambda: False
    assert (
        invoke(scope, "apply", plan_id=plan, confirmed=True)["state"] == "gateway_stop_unconfirmed"
    )
    control.service_stopped = lambda: True
    control.original_pid_gone = lambda pid: False
    assert (
        invoke(scope, "apply", plan_id=plan, confirmed=True)["state"] == "gateway_stop_unconfirmed"
    )
    control.original_pid_gone = lambda pid: True
    control.receiver_socket_absent = lambda endpoint: False
    assert (
        invoke(scope, "rollback", plan_id=plan, confirmed=True)["state"]
        == "gateway_stop_unconfirmed"
    )
    assert calls == []


def test_independently_stopped_gateway_passes_only_bounded_bridge_request(scope):
    plan = "plan_" + "a" * 32
    result = invoke(scope, "apply", plan_id=plan, confirmed=True)
    assert result["state"] == "configured_runtime_unverified"
    args, kwargs = scope[3][0]
    assert args[2] == {
        "version": 1,
        "operation": "mcp_command_apply",
        "profile": str(scope[0].root),
        "plan_id": plan,
        "confirmed": True,
        "gateway_stopped": True,
    }
    assert kwargs["expected_files"]["bridge.py"]


def test_apply_uses_a_stopped_host_control_instead_of_a_running_job_control(scope):
    store, control, _factory, _calls = scope
    constructed = []

    def factory(python, profile, *, require_stopped=False):
        constructed.append((python, profile, require_stopped))
        if require_stopped is not True:
            raise ValueError("running_job_required_but_gateway_is_stopped")
        return control

    result = cutover.operation(
        store,
        "employee",
        "project",
        "profile",
        "apply",
        plan_id="plan_" + "a" * 32,
        confirmed=True,
        control_factory=factory,
    )
    assert result["state"] == "configured_runtime_unverified"
    assert constructed == [(Path("/hermes/python"), store.root, True)]


def test_apply_reaches_bridge_with_real_stopped_control_constructor(scope, monkeypatch, tmp_path):
    store, _control, _factory, calls = scope
    user = tmp_path / "user"
    profile = user / ".hermes"
    install = profile / "hermes-agent"
    launcher = install / ".hermes/bin/hermes"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    key = hashlib.sha256(str(install).encode()).hexdigest()[:16]
    venv = profile / "installs" / key / "environments" / ("a" * 32) / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("version = 3.14\n")
    python = venv / "bin/python"
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    facts = profile / "installs" / key / "facts.json"
    facts.write_text(json.dumps({"packages": {"venv": {"environment": str(venv)}}}))
    facts.chmod(0o600)
    plist = user / "Library/LaunchAgents/ai.hermes.gateway.plist"
    plist.parent.mkdir(parents=True)
    script = (
        f'const status=$.system("exec {launcher} --run-module '
        f"hermes_cli.stderr_timestamp --error-log /tmp/gateway.error.log -- "
        f'{launcher} gateway run --external-supervisor");'
    )
    plist.write_bytes(
        plistlib.dumps(
            {
                "Label": "ai.hermes.gateway",
                "ProgramArguments": ["/usr/bin/osascript", "-l", "JavaScript", "-e", script],
                "WorkingDirectory": str(profile),
                "EnvironmentVariables": {"HERMES_HOME": str(profile), "PATH": "/usr/bin:/bin"},
            }
        )
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user))
    monkeypatch.setattr(cutover.catalog, "resolve", lambda *_args: (python, profile))
    monkeypatch.setattr(cutover.HermesGatewayControl, "service_stopped", lambda self: True)
    monkeypatch.setattr(
        cutover.HermesGatewayControl,
        "runtime",
        lambda self: {
            "kind": "hermes-gateway",
            "gateway_state": "stopped",
            "hermes_home": str(profile),
            "pid": 42,
        },
    )
    monkeypatch.setattr(
        cutover.HermesGatewayControl, "original_pid_gone", lambda self, pid: pid == 42
    )
    monkeypatch.setattr(
        cutover.HermesGatewayControl,
        "receiver_socket_absent",
        lambda self, endpoint: endpoint == "/private/receiver.sock",
    )
    result = cutover.operation(
        store,
        "employee",
        "project",
        "profile",
        "apply",
        plan_id="plan_" + "a" * 32,
        confirmed=True,
        control_factory=cutover.HermesGatewayControl,
    )
    assert result["state"] == "configured_runtime_unverified"
    assert len(calls) == 1


def test_changed_binding_after_lock_refuses_config_write(scope, monkeypatch):
    binding = {"id": "binding-1", "host_session_id": "session-1"}
    changes = iter((binding, {"id": "binding-2", "host_session_id": "session-1"}))
    monkeypatch.setattr(cutover, "_binding", lambda *args: next(changes))
    result = invoke(scope, "apply", plan_id="plan_" + "a" * 32, confirmed=True)
    assert result["state"] == "configuration_changed"
    assert scope[3] == []
