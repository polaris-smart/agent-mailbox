"""Host-control evidence must fail closed before any pin mutation."""

import hashlib
import json
import os
import plistlib
import socket
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_mailbox import workbench_hermes_gateway_control as gateway_control
from agent_mailbox.workbench_hermes_gateway_control import (
    HermesGatewayControl,
    HermesStopAfterMarkerUnconfirmed,
    _loaded_service_domain,
    _service_launcher_matches,
)


def control(tmp_path):
    instance = HermesGatewayControl.__new__(HermesGatewayControl)
    instance.profile = tmp_path
    instance.python = Path("/private/fake/bin/python")
    instance.install = tmp_path
    instance.command = Path("/private/fake/bin/hermes")
    return instance


def test_first_contact_live_status_is_answered_by_same_gateway(monkeypatch, tmp_path):
    instance = control(tmp_path)
    instance.domain = f"gui/{os.getuid()}"
    instance.plist = tmp_path / "ai.hermes.gateway.plist"
    monkeypatch.setattr(gateway_control, "_loaded_service_domain", lambda *_args: instance.domain)
    monkeypatch.setattr(instance, "_same_installation", lambda: True)
    runtime = {
        "kind": "hermes-gateway",
        "gateway_state": "draining",
        "hermes_home": str(tmp_path),
        "pid": 123,
        "start_time": 456,
    }
    monkeypatch.setattr(instance, "runtime", lambda: runtime.copy())
    monkeypatch.setattr(instance, "pid_identity", lambda _runtime: True)
    calls = []
    status = {**runtime, "protocol": 1, "answering_pid": 123, "answered_at": 100.0}
    monkeypatch.setattr(
        instance, "_official", lambda source: calls.append(source) or json.dumps(status).encode()
    )
    assert instance.live_status() == (runtime, status)
    assert len(calls) == 1 and "query_gateway_control" in calls[0]
    compile(calls[0], "<official-hermes-status>", "exec")
    calls.clear()
    monkeypatch.setattr(
        instance,
        "_official",
        lambda source: (
            calls.append(source) or json.dumps({**status, "answering_pid": 999}).encode()
        ),
    )
    with pytest.raises(ValueError, match="live_status_unconfirmed"):
        instance.live_status()
    assert len(calls) == 1
    calls.clear()
    monkeypatch.setattr(instance, "pid_identity", lambda _runtime: False)
    with pytest.raises(ValueError, match="live_status_unconfirmed"):
        instance.live_status()
    assert not calls


def test_pm_restore_reselects_committed_python_but_preserves_original_service(
    monkeypatch, tmp_path
):
    user = tmp_path / "user"
    profile = user / ".hermes"
    original = profile / "environments/old/venv/bin/python"
    current = profile / "environments/new/venv/bin/python"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user))
    monkeypatch.setattr(
        gateway_control,
        "_installed_python",
        lambda selected, _install: current if selected == profile else None,
    )
    calls = []

    class Synthetic(HermesGatewayControl):
        def __init__(self, python, selected, *, require_stopped=False):
            assert require_stopped is True and selected == profile
            calls.append(("selected", python))
            self.plist_digest = bytes.fromhex("a" * 64)
            self.command_digest = bytes.fromhex("b" * 64)

        def start_stopped_original(self, domain, *, plist_sha256, command_sha256):
            calls.append(("restore", domain, plist_sha256, command_sha256))

    domain = f"gui/{os.getuid()}"
    result = Synthetic.restore_after_pm(
        profile, domain, original_plist_sha256="a" * 64, original_command_sha256="b" * 64
    )
    assert isinstance(result, Synthetic)
    assert original != current and calls == [
        ("selected", current),
        ("restore", domain, "a" * 64, "b" * 64),
    ]
    calls.clear()
    with pytest.raises(ValueError, match="identity_changed"):
        Synthetic.restore_after_pm(
            profile, domain, original_plist_sha256="c" * 64, original_command_sha256="b" * 64
        )
    assert calls == [("selected", current)]  # Never bootstraps a changed service.


def test_diagnostic_reload_requires_same_gateway_and_private_socket(monkeypatch, tmp_path):
    instance = control(tmp_path)
    instance.domain = f"gui/{os.getuid()}"
    instance.plist = tmp_path / "ai.hermes.gateway.plist"
    endpoint = tmp_path / "diagnostic.sock"
    calls = []
    monkeypatch.setattr(gateway_control, "_loaded_service_domain", lambda *_args: instance.domain)
    monkeypatch.setattr(instance, "_same_installation", lambda: True)
    monkeypatch.setattr(instance, "receiver_socket_absent", lambda _path: True)
    runtime = {
        "kind": "hermes-gateway",
        "gateway_state": "running",
        "hermes_home": str(tmp_path),
        "pid": 123,
        "start_time": 456,
    }
    monkeypatch.setattr(instance, "runtime", lambda: runtime.copy())
    monkeypatch.setattr(instance, "pid_identity", lambda _runtime: True)
    monkeypatch.setattr(
        instance,
        "_official",
        lambda source: (
            calls.append(source)
            or json.dumps({"reloaded": True, "home": str(tmp_path), "plugin_found": True}).encode()
        ),
    )
    route = {
        "protocol": "agent-mailbox.hermes-routing.v1",
        "host_kind": "hermes",
        "pid": 123,
        "profile_home": str(tmp_path),
        "routing_home": str(tmp_path),
        "instance_id": "same-gateway",
        "session_verified": False,
        "delivery_verified": False,
    }
    monkeypatch.setattr(instance, "routing", lambda _path: route)
    health = {
        "protocol": "agent-mailbox.host-health.v1",
        "host_kind": "hermes",
        "gateway_injection_available": False,
        "pinned_sessions": 0,
        "unconfirmed_deliveries": 0,
        "recorded_turn_receipts": 0,
    }
    monkeypatch.setattr(
        gateway_control,
        "_request",
        lambda _path, request: (
            health if request == {"operation": "probe"} else pytest.fail("unexpected request")
        ),
    )
    assert instance.reload_diagnostic(endpoint, readiness_seconds=0) == {
        "state": "diagnostic_runtime_observed",
        "runtime_verified": True,
        "session_verified": False,
        "delivery_verified": False,
    }
    assert len(calls) == 1 and "reload_gateway_plugins" in calls[0]
    compile(calls[0], "<official-hermes-reload>", "exec")
    calls.clear()
    assert instance.observe_diagnostic(endpoint)["runtime_verified"] is True
    assert not calls  # Observation never requests a reload.
    monkeypatch.setattr(instance, "receiver_socket_absent", lambda _path: False)
    with pytest.raises(ValueError, match="reload_scope_unconfirmed"):
        instance.reload_diagnostic(endpoint, readiness_seconds=0)
    assert not calls
    monkeypatch.setattr(instance, "receiver_socket_absent", lambda _path: True)
    monkeypatch.setattr(instance, "_official", lambda _source: b'{"reloaded": false}')
    with pytest.raises(ValueError, match="reload_unconfirmed"):
        instance.reload_diagnostic(endpoint, readiness_seconds=0)
    monkeypatch.setattr(
        instance,
        "_official",
        lambda _source: json.dumps(
            {"reloaded": True, "home": str(tmp_path), "plugin_found": True}
        ).encode(),
    )
    monkeypatch.setattr(instance, "routing", lambda _path: {**route, "pid": 999})
    with pytest.raises(ValueError, match="reload_unconfirmed"):
        instance.reload_diagnostic(endpoint, readiness_seconds=0)
    with pytest.raises(ValueError, match="runtime_unconfirmed"):
        instance.observe_diagnostic(endpoint)
    monkeypatch.setattr(instance, "routing", lambda _path: route)
    monkeypatch.setattr(
        gateway_control,
        "_request",
        lambda _path, _request: {**health, "gateway_injection_available": True},
    )
    with pytest.raises(ValueError, match="reload_unconfirmed"):
        instance.reload_diagnostic(endpoint, readiness_seconds=0)
    monkeypatch.setattr(gateway_control, "_request", lambda _path, _request: health)
    attempts = iter((None, route))
    monkeypatch.setattr(instance, "routing", lambda _path: next(attempts))
    monkeypatch.setattr(gateway_control.time, "sleep", lambda _seconds: None)
    assert instance.reload_diagnostic(endpoint, readiness_seconds=0.1)["runtime_verified"] is True
    monkeypatch.setattr(instance, "routing", lambda _path: route)
    observations = iter((runtime, {**runtime, "start_time": 789}))
    monkeypatch.setattr(instance, "runtime", lambda: next(observations))
    with pytest.raises(ValueError, match="reload_unconfirmed"):
        instance.reload_diagnostic(endpoint, readiness_seconds=0)


def test_gateway_control_rejects_a_different_installed_launcher(tmp_path):
    old = tmp_path / "old/bin/hermes"
    current = tmp_path / "pm/bin/hermes"
    plist = tmp_path / "ai.hermes.gateway.plist"
    script = (
        f'const status=$.system("exec {current} --run-module '
        f"hermes_cli.stderr_timestamp --error-log /tmp/gateway.error.log -- "
        f'{current} gateway run --external-supervisor");'
    )
    plist.write_bytes(
        plistlib.dumps(
            {
                "Label": "ai.hermes.gateway",
                "ProgramArguments": ["/usr/bin/osascript", "-l", "JavaScript", "-e", script],
            }
        )
    )
    assert _service_launcher_matches(current, plist)
    assert not _service_launcher_matches(old, plist)
    plist.write_bytes(
        plistlib.dumps(
            {
                "Label": "ai.hermes.gateway",
                "ProgramArguments": [
                    "/usr/bin/osascript",
                    "-l",
                    "JavaScript",
                    "-e",
                    "unrecognized",
                ],
            }
        )
    )
    assert not _service_launcher_matches(current, plist)


def test_loaded_launchd_job_must_match_the_published_service(monkeypatch, tmp_path):
    command = tmp_path / "pm/bin/hermes"
    profile = tmp_path / "home"
    plist = tmp_path / "ai.hermes.gateway.plist"
    script = (
        f'const status=$.system("exec {command} --run-module '
        f"hermes_cli.stderr_timestamp --error-log /tmp/gateway.error.log -- "
        f'{command} gateway run --external-supervisor");'
    )
    arguments = ["/usr/bin/osascript", "-l", "JavaScript", "-e", script]
    service = {
        "Label": "ai.hermes.gateway",
        "ProgramArguments": arguments,
        "WorkingDirectory": str(profile),
        "EnvironmentVariables": {"HERMES_HOME": str(profile), "PATH": "/usr/bin:/bin"},
    }
    plist.write_bytes(plistlib.dumps(service))
    uid = os.getuid()

    def printed(*, changed_argument=False, changed_home=False):
        args = [*arguments]
        if changed_argument:
            args[-1] = "another launcher"
        home = str(profile / "other") if changed_home else str(profile)
        lines = [
            f"gui/{uid}/ai.hermes.gateway = {{",
            f"\tpath = {plist}",
            "\ttype = LaunchAgent",
            "\tstate = running",
            "\tprogram = /usr/bin/osascript",
            "\targuments = {",
            *(f"\t\t{arg}" for arg in args),
            "\t}",
            f"\tworking directory = {profile}",
            "\tenvironment = {",
            f"\t\tHERMES_HOME => {home}",
            "\t\tPATH => /usr/bin:/bin",
            "\t}",
            "\tpid = 1234",
            "}",
        ]
        return "\n".join(lines).encode()

    current = printed()

    def launchctl(argv, **_kwargs):
        if argv[-1].startswith("gui/"):
            return SimpleNamespace(returncode=0, stdout=current, stderr=b"")
        return SimpleNamespace(returncode=113, stdout=b"", stderr=b"Could not find service")

    monkeypatch.setattr(subprocess, "run", launchctl)
    monkeypatch.setattr(os, "uname", lambda: SimpleNamespace(sysname="Darwin"))
    assert _loaded_service_domain(command, plist, profile) == f"gui/{uid}"

    current = printed(changed_argument=True)
    assert _loaded_service_domain(command, plist, profile) is None
    current = printed(changed_home=True)
    assert _loaded_service_domain(command, plist, profile) is None
    current = printed()

    def duplicate(_argv, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=current, stderr=b"")

    monkeypatch.setattr(subprocess, "run", duplicate)
    assert _loaded_service_domain(command, plist, profile) is None


def test_pm_gateway_control_targets_only_the_proven_launchd_job(monkeypatch, tmp_path):
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
    domain = f"gui/{os.getuid()}"
    loaded = [domain]
    monkeypatch.setattr(gateway_control, "_loaded_service_domain", lambda *_args: loaded[0])
    instance = HermesGatewayControl(python, profile)
    assert instance.domain == domain
    calls = []
    monkeypatch.setattr(instance, "runtime", lambda: {"pid": 1234, "start_time": 1})
    monkeypatch.setattr(instance, "pid_identity", lambda _runtime: True)
    monkeypatch.setattr(
        instance, "_official", lambda source, *args: calls.append(("planned_stop", source, args))
    )

    def launchctl(argv, **_kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", launchctl)
    instance.stop()
    assert calls[0][0] == "planned_stop"
    assert "write_planned_stop_marker" in calls[0][1] and calls[0][2] == ("1234",)
    assert calls[1] == ["/bin/launchctl", "bootout", f"{domain}/ai.hermes.gateway"]
    monkeypatch.setattr(instance, "service_stopped", lambda: True)
    instance.start()
    assert calls[2:] == [
        ["/bin/launchctl", "bootstrap", domain, str(plist)],
        ["/bin/launchctl", "kickstart", f"{domain}/ai.hermes.gateway"],
    ]

    def failed_launchctl(argv, **_kwargs):
        calls.append(argv)
        failure = argv[1] if argv[1] in {"bootout", "bootstrap", "kickstart"} else ""
        return SimpleNamespace(
            returncode=5 if failure == failed_launchctl.step else 0, stdout=b"", stderr=b""
        )

    monkeypatch.setattr(subprocess, "run", failed_launchctl)
    for step, expected in (
        ("bootstrap", [["/bin/launchctl", "bootstrap", domain, str(plist)]]),
        (
            "kickstart",
            [
                ["/bin/launchctl", "bootstrap", domain, str(plist)],
                ["/bin/launchctl", "kickstart", f"{domain}/ai.hermes.gateway"],
            ],
        ),
    ):
        failed_launchctl.step = step
        calls.clear()
        with pytest.raises(ValueError, match="start_unconfirmed"):
            instance.start()
        assert calls == expected
    failed_launchctl.step = "bootout"
    calls.clear()
    with pytest.raises(HermesStopAfterMarkerUnconfirmed, match="marker_requires_recovery"):
        instance.stop()
    assert calls[0][0] == "planned_stop"
    assert calls[1] == ["/bin/launchctl", "bootout", f"{domain}/ai.hermes.gateway"]
    monkeypatch.setattr(subprocess, "run", launchctl)
    marker = profile / ".gateway-planned-stop.json"
    assert instance.planned_stop_marker_absent()
    marker.write_text('{"target_pid":1234}')
    assert not instance.planned_stop_marker_absent()
    calls.clear()
    with pytest.raises(ValueError, match="marker_preexisting"):
        instance.stop()
    assert calls == []
    marker.unlink()
    monkeypatch.setattr(instance, "pid_identity", lambda _runtime: False)
    with pytest.raises(ValueError, match="pid_unconfirmed"):
        instance.stop()
    assert calls == []
    monkeypatch.setattr(instance, "pid_identity", lambda _runtime: True)
    loaded[0] = None
    with pytest.raises(ValueError, match="loaded_service_unconfirmed"):
        instance.stop()
    assert calls == []
    monkeypatch.setattr(HermesGatewayControl, "service_stopped", lambda self: True)
    stopped_control = HermesGatewayControl(python, profile, require_stopped=True)
    assert stopped_control.domain is None
    with pytest.raises(ValueError, match="loaded_service_unconfirmed"):
        stopped_control.stop()
    with pytest.raises(ValueError, match="start_scope_unconfirmed"):
        stopped_control.start()
    monkeypatch.setattr(HermesGatewayControl, "service_stopped", lambda self: False)
    with pytest.raises(ValueError, match="not_stopped"):
        HermesGatewayControl(python, profile, require_stopped=True)
    loaded[0] = domain
    launcher.write_text("#!/bin/sh\n# changed after identity check\n")
    with pytest.raises(ValueError, match="loaded_service_unconfirmed"):
        instance.stop()
    assert calls == []
    launcher.write_text("#!/bin/sh\n")
    plist.write_bytes(plist.read_bytes() + b" ")
    with pytest.raises(ValueError, match="start_scope_unconfirmed"):
        instance.start()
    assert calls == []


def test_official_drain_and_clear_are_scoped_to_selected_profile(monkeypatch, tmp_path):
    instance = control(tmp_path)
    calls = []

    plan_id = "plan_" + "a" * 32

    # Simulate the official child's durable marker, not a live Gateway.
    def official(source, *args):
        calls.append((source, args))
        marker = tmp_path / ".drain_request.json"
        if "write_drain_request" in source:
            marker.write_text('{"principal":"agent-mailbox-cutover:' + plan_id + '"}')
        else:
            marker.unlink()

    monkeypatch.setattr(instance, "_official", official)
    instance.request_drain(plan_id)
    instance.clear_drain(plan_id)
    assert len(calls) == 2
    assert "write_drain_request" in calls[0][0]
    assert calls[0][1] == ("agent-mailbox-cutover:" + plan_id,)
    assert "clear_drain_request" in calls[1][0]


def test_service_stopped_requires_user_and_system_domains_proven_unloaded(monkeypatch, tmp_path):
    instance = control(tmp_path)
    monkeypatch.setattr(os, "uname", lambda: SimpleNamespace(sysname="Darwin"))
    results = [
        SimpleNamespace(returncode=113, stdout=b"", stderr=b"Could not find service"),
        SimpleNamespace(returncode=3, stdout=b"", stderr=b"Could not find service"),
        SimpleNamespace(returncode=113, stdout=b"", stderr=b"Could not find service"),
    ]
    calls = []

    def absent(argv, **_kwargs):
        calls.append(argv)
        return results.pop(0)

    monkeypatch.setattr(subprocess, "run", absent)
    assert instance.service_stopped()
    assert [argv[-1] for argv in calls] == [
        f"gui/{os.getuid()}/ai.hermes.gateway",
        f"user/{os.getuid()}/ai.hermes.gateway",
        "system/ai.hermes.gateway",
    ]
    for ambiguous in (
        SimpleNamespace(returncode=0, stdout=b"state = waiting", stderr=b""),
        SimpleNamespace(returncode=5, stdout=b"", stderr=b"Input/output error"),
        SimpleNamespace(returncode=113, stdout=b"", stderr=b"Permission denied"),
    ):
        monkeypatch.setattr(subprocess, "run", lambda *args, result=ambiguous, **kwargs: result)
        assert not instance.service_stopped()
    user_absent = SimpleNamespace(returncode=113, stdout=b"", stderr=b"Could not find service")
    system_loaded = SimpleNamespace(returncode=0, stdout=b"state = running", stderr=b"")
    answers = iter((user_absent, user_absent, system_loaded))
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: next(answers))
    assert not instance.service_stopped()


def test_strict_gateway_absence_requires_exact_official_result_and_safe_files(
    monkeypatch, tmp_path
):
    instance = control(tmp_path)
    calls = []
    monkeypatch.setattr(instance, "_official", lambda source: calls.append(source) or b"absent\n")
    assert instance.strict_gateway_absent() is True
    assert "get_running_pid_identity_strict" in calls[0]
    monkeypatch.setattr(instance, "_official", lambda _source: b"present\n")
    assert instance.strict_gateway_absent() is False
    monkeypatch.setattr(instance, "_official", lambda _source: b"noise\nabsent\n")
    assert instance.strict_gateway_absent() is False
    (tmp_path / "gateway.lock").symlink_to(tmp_path / "missing")
    monkeypatch.setattr(
        instance, "_official", lambda _source: pytest.fail("must not probe symlink")
    )
    assert instance.strict_gateway_absent() is False


def test_restore_original_domain_rechecks_scope_and_never_leaks_domain(monkeypatch, tmp_path):
    instance = control(tmp_path)
    instance.domain = None
    instance.plist_digest = bytes.fromhex("ab" * 32)
    instance.command_digest = bytes.fromhex("cd" * 32)
    monkeypatch.setattr(instance, "_same_installation", lambda: True)
    monkeypatch.setattr(instance, "planned_stop_marker_absent", lambda: True)
    monkeypatch.setattr(instance, "service_stopped", lambda: True)
    monkeypatch.setattr(instance, "strict_gateway_absent", lambda: True)
    started = []
    monkeypatch.setattr(instance, "start", lambda: started.append(instance.domain))
    domain = f"gui/{os.getuid()}"
    hashes = {"plist_sha256": "ab" * 32, "command_sha256": "cd" * 32}
    instance.start_stopped_original(domain, **hashes)
    assert started == [domain] and instance.domain is None
    with pytest.raises(ValueError, match="restore_scope_unconfirmed"):
        instance.start_stopped_original("system", **hashes)
    with pytest.raises(ValueError, match="restore_scope_unconfirmed"):
        instance.start_stopped_original(
            domain, plist_sha256="00" * 32, command_sha256=hashes["command_sha256"]
        )
    monkeypatch.setattr(instance, "strict_gateway_absent", lambda: False)
    with pytest.raises(ValueError, match="restore_scope_unconfirmed"):
        instance.start_stopped_original(domain, **hashes)
    assert started == [domain] and instance.domain is None


def test_existing_other_drain_marker_is_never_overwritten_or_cleared(monkeypatch, tmp_path):
    instance = control(tmp_path)
    marker = tmp_path / ".drain_request.json"
    marker.write_text('{"principal":"another-operator"}')
    monkeypatch.setattr(instance, "_official", lambda *args: pytest.fail("official call forbidden"))
    plan = "plan_" + "a" * 32
    with pytest.raises(ValueError, match="other_drain_in_progress"):
        instance.request_drain(plan)
    with pytest.raises(ValueError, match="other_drain_in_progress"):
        instance.clear_drain(plan)
    assert marker.read_text() == '{"principal":"another-operator"}'


def test_stale_socket_can_only_be_removed_after_independent_stop(tmp_path):
    with tempfile.TemporaryDirectory(prefix="hgc-", dir="/private/tmp") as location:
        private = Path(location)
        path = private / "receiver.sock"
        instance = control(tmp_path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as live:
            live.bind(str(path))
            os.chmod(path, 0o600)
            live.listen()
            with pytest.raises(ValueError, match="stop_unconfirmed"):
                instance.remove_stale_receiver_socket(
                    path, original_pid_gone=False, service_stopped=True
                )
            with pytest.raises(ValueError, match="still_live"):
                instance.remove_stale_receiver_socket(
                    path, original_pid_gone=True, service_stopped=True
                )
            assert path.exists()
        instance.remove_stale_receiver_socket(path, original_pid_gone=True, service_stopped=True)
        assert instance.receiver_socket_absent(path)


def test_stale_socket_never_unlinks_regular_file(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "receiver.sock"
    path.write_text("not a socket")
    instance = control(tmp_path)
    with pytest.raises(ValueError, match="scope_unconfirmed"):
        instance.remove_stale_receiver_socket(path, original_pid_gone=True, service_stopped=True)
    assert path.read_text() == "not a socket"
