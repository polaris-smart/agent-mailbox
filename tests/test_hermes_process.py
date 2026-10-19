"""Real bounded child processes; all files are synthetic, no host is activated."""

import hashlib
import json
import stat
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_mailbox import workbench_hermes_process as process

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Unix host adapter")


@pytest.fixture
def case(tmp_path):
    root = tmp_path.resolve()
    # The setup-python interpreter on hosted runners lives under a mutable
    # tool cache. The adapter correctly rejects it; use the system-owned
    # interpreter for this synthetic child-process test.
    python = (
        Path("/usr/bin/python3") if Path("/usr/bin/python3").is_file() else Path(sys.executable)
    )
    profile = root / "profile"
    profile.mkdir(mode=0o700)
    source = root / "payload"
    source.mkdir(mode=0o700)
    for name in process.FILES:
        (source / name).write_text("# fixture\n")
    request = {
        "version": 1,
        "operation": "status",
        "profile": str(profile),
        "plan_id": "plan_" + "0" * 32,
    }

    def invoke(code, *, timeout=2, mutate=None, request_value=None):
        (source / "bridge.py").write_text(code)
        pins = {
            name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in process.FILES
        }
        if mutate:
            mutate(source)
        return process.run(
            python,
            profile,
            request_value or request,
            source=source,
            expected_files=pins,
            timeout=timeout,
        )

    return profile, source, request, invoke


RESULT = {
    "plan_id": "plan_" + "0" * 32,
    "state": "preview",
    "session_verified": False,
    "delivery_verified": False,
}


def success(value=None):
    return (
        "print(" + repr(json.dumps({"version": 1, "ok": True, "result": value or RESULT})) + ")\n"
    )


def test_child_is_isolated_and_does_not_inherit_agent_environment(case, monkeypatch):
    profile, _, _, invoke = case
    monkeypatch.setenv("CODEX_DANGEROUS_SENTINEL", "PRIVATE")
    monkeypatch.setenv("HTTPS_PROXY", "SECRET_PROXY")
    monkeypatch.setenv("OPENAI_API_KEY", "SECRET_KEY")
    monkeypatch.setenv("PYTHONPATH", "SECRET_PATH")
    code = (
        "import os,sys,json\n"
        "assert sys.flags.isolated and sys.flags.dont_write_bytecode\n"
        "assert not any(k in os.environ for k in ['CODEX_DANGEROUS_SENTINEL','HTTPS_PROXY','OPENAI_API_KEY','PYTHONPATH'])\n"
        f"assert os.environ['HERMES_HOME']=={str(profile)!r}\n"
        "assert json.load(sys.stdin)['operation']=='status'\n"
        "assert os.getcwd()!=os.environ['HERMES_HOME']\n"
    )
    assert invoke(code + success()) == RESULT


@pytest.mark.parametrize(
    "code",
    [
        "print('private traceback or config')\n",
        "print('x'*70000)\n",
        "import time; time.sleep(10)\n",
        "import json; print(json.dumps({'version':1,'ok':'true'}))\n",
        success({**RESULT, "secret": "PRIVATE"}),
        success({**RESULT, "session_verified": True}),
        success({**RESULT, "activation_requested": True}),
        success({**RESULT, "will_activate": "false"}),
        success({**RESULT, "requires_confirmation": "true"}),
        success({**RESULT, "state": "activated"}),
        success({**RESULT, "state": ["preview"]}),
        success({**RESULT, "plan_id": "plan_" + "1" * 32}),
    ],
)
def test_invalid_or_unbounded_response_is_unknown_and_never_echoed(case, code):
    started = time.monotonic()
    with pytest.raises(ValueError, match="^hermes_preparation_result_unknown$"):
        case[3](code, timeout=0.2)
    assert time.monotonic() - started < 3


def test_nonzero_exit_cannot_claim_success(case):
    with pytest.raises(ValueError, match="^hermes_preparation_refused_or_incomplete$"):
        case[3](success() + "raise SystemExit(2)\n")


def test_mcp_command_child_result_exposes_only_unverified_state(case):
    request = {
        "version": 1,
        "operation": "mcp_command_status",
        "profile": str(case[0]),
        "plan_id": "plan_" + "0" * 32,
    }
    result = {
        **RESULT,
        "requires_confirmation": True,
        "requires_stopped_gateway": True,
        "runtime_verified": False,
    }
    assert case[3](success(result), request_value=request) == result
    for invalid in (
        {**result, "runtime_verified": True},
        {**result, "command": "/private/path"},
        {**result, "requires_stopped_gateway": False},
    ):
        with pytest.raises(ValueError, match="hermes_mcp_command_result_unknown"):
            case[3](success(invalid), request_value=request)


def test_diagnostic_admission_child_never_claims_runtime_or_delivery(case):
    request = {
        "version": 1,
        "operation": "diagnostic_admission_status",
        "profile": str(case[0]),
        "plan_id": "plan_" + "0" * 32,
    }
    result = {
        **RESULT,
        "state": "diagnostic_enabled_restart_required",
        "requires_confirmation": True,
        "requires_stopped_gateway": True,
        "runtime_verified": False,
    }
    assert case[3](success(result), request_value=request) == result
    for invalid in (
        {**result, "runtime_verified": True},
        {**result, "delivery_verified": True},
        {**result, "requires_stopped_gateway": False},
        {**result, "gateway_path": "/private/path"},
    ):
        with pytest.raises(ValueError, match="hermes_diagnostic_admission_result_unknown"):
            case[3](success(invalid), request_value=request)


def test_changed_component_not_executed(case):
    with pytest.raises(ValueError, match="component_changed"):
        case[3](success(), mutate=lambda root: (root / "setup.py").write_text("changed"))


def test_profile_mismatch_rejected(case):
    profile, _, request, invoke = case
    with pytest.raises(ValueError, match="profile_mismatch"):
        invoke(success(), request_value={**request, "profile": str(profile.parent)})


def test_world_writable_profile_rejected(case):
    case[0].chmod(0o777)
    with pytest.raises(ValueError, match="profile_private_required"):
        case[3](success())


def test_symlink_component_refused(case):
    def change(source):
        (source / "setup.py").rename(source / "other.py")
        (source / "setup.py").symlink_to(source / "other.py")

    with pytest.raises(ValueError, match="component_changed"):
        case[3](success(), mutate=change)


def test_only_exact_system_applications_ancestor_is_allowed(monkeypatch):
    monkeypatch.setattr(process.sys, "platform", "darwin")
    normal = SimpleNamespace(st_mode=stat.S_IFDIR | 0o775, st_uid=0, st_gid=80)
    assert process._macos_applications_ancestor(Path("/Applications"), normal)
    assert not process._macos_applications_ancestor(Path("/private/Applications"), normal)
    assert not process._macos_applications_ancestor(
        Path("/Applications"), SimpleNamespace(st_mode=stat.S_IFDIR | 0o777, st_uid=0, st_gid=80)
    )
    assert not process._macos_applications_ancestor(
        Path("/Applications"), SimpleNamespace(st_mode=stat.S_IFDIR | 0o775, st_uid=501, st_gid=80)
    )


def test_frozen_hermes_payload_requires_exact_signed_app(monkeypatch, tmp_path):
    bundle = tmp_path / "Agent Mailbox.app"
    binary = bundle / "Contents/MacOS/Agent Mailbox"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"synthetic")
    source = bundle / "Contents/Resources/agent_mailbox/host_receivers/hermes"
    source.mkdir(parents=True)
    monkeypatch.setattr(process.sys, "platform", "darwin")
    monkeypatch.setattr(process.sys, "frozen", True, raising=False)
    monkeypatch.setattr(process.sys, "executable", str(binary))
    calls = []

    def signed(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(process.subprocess, "run", signed)
    process._verify_frozen_bundle(source)
    assert calls == [["/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle)]]
    with pytest.raises(ValueError, match="hermes_bundle_scope_unconfirmed"):
        process._verify_frozen_bundle(tmp_path / "unrelated")
    monkeypatch.setattr(process.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    with pytest.raises(ValueError, match="hermes_bundle_signature_unconfirmed"):
        process._verify_frozen_bundle(source)


def test_frozen_payload_resolves_pyinstaller_frameworks_resource_link(monkeypatch, tmp_path):
    bundle = tmp_path / "Agent Mailbox.app"
    resources = bundle / "Contents/Resources/agent_mailbox"
    source = resources / "host_receivers/hermes"
    source.mkdir(parents=True)
    frameworks = bundle / "Contents/Frameworks"
    frameworks.mkdir()
    (frameworks / "agent_mailbox").symlink_to("../Resources/agent_mailbox")
    monkeypatch.setattr(process.sys, "frozen", True, raising=False)
    monkeypatch.setattr(process.sys, "_MEIPASS", str(frameworks), raising=False)
    assert process.payload() == source


def test_timeout_kills_only_the_created_child_group(case):
    # A child that exits while its own descendant holds stdout cannot leave a
    # runaway descendant. The fixed production bridge does not itself fork.
    with tempfile.TemporaryDirectory(prefix="h-child-") as folder:
        marker = Path(folder) / "late"
        code = (
            "import subprocess,sys\n"
            f"subprocess.Popen([sys.executable,'-c',\"import time,pathlib;time.sleep(1);pathlib.Path({str(marker)!r}).write_text('bad')\"])\n"
        )
        with pytest.raises(ValueError, match="result_unknown"):
            case[3](code, timeout=0.15)
        time.sleep(1.1)
        assert not marker.exists()


def test_actual_bridge_rejects_extra_operations_without_host_import(tmp_path):
    import subprocess

    bridge = process.payload() / "bridge.py"
    requests = [
        {"version": 1, "operation": "enable"},
        {"version": 1, "operation": "status", "profile": "PRIVATE", "plan_id": "x", "shell": "bad"},
    ]
    for request in requests:
        run = subprocess.run(
            [sys.executable, "-I", "-B", str(bridge)],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            timeout=2,
            check=False,
        )
        assert run.returncode == 2
        assert json.loads(run.stdout) == {
            "version": 1,
            "ok": False,
            "error": "preparation_refused_or_incomplete",
        }
        assert not run.stderr


def test_profiles_response_is_bounded_and_not_a_session_claim(case):
    profile, _, _, invoke = case
    request = {"version": 1, "operation": "profiles", "profile": str(profile)}
    value = {
        "state": "profile_candidates",
        "profiles": [{"profile_id": "default", "home": str(profile)}],
        "session_verified": False,
        "delivery_verified": False,
    }
    assert invoke(success(value), request_value=request) == value
    for broken in (
        {**value, "session_verified": True},
        {**value, "profiles": value["profiles"] * 65},
        {**value, "profiles": value["profiles"] * 2},
        {**value, "profiles": [{"profile_id": "default", "home": str(profile.parent)}]},
        {**value, "profiles": [{"profile_id": "../other", "home": str(profile)}]},
        {
            **value,
            "profiles": [{"profile_id": "default", "home": str(profile), "token": "PRIVATE"}],
        },
        {**value, "profiles": "PRIVATE"},
    ):
        with pytest.raises(ValueError, match="hermes_preparation_result_unknown"):
            invoke(success(broken), request_value=request)


def test_discovery_preview_parent_requires_explicit_no_grant(case):
    profile, _, _, invoke = case
    request = {
        "version": 1,
        "operation": "preview_discovery",
        "profile": str(profile),
        "options": {"socket_path": str(profile / "receiver.sock")},
    }
    value = {**RESULT, "will_write_injection_grant": False}
    assert invoke(success(value), request_value=request) == value
    for broken in (RESULT, {**value, "will_write_injection_grant": True}):
        with pytest.raises(ValueError, match="hermes_preparation_result_unknown"):
            invoke(success(broken), request_value=request)


def test_discovery_bridge_rejects_credential_or_enable_options(tmp_path):
    import subprocess

    for extra in (
        {"session_file": "PRIVATE"},
        {"allow_gateway_injection": True},
        {"enabled": True},
    ):
        request = {
            "version": 1,
            "operation": "preview_discovery",
            "profile": str(tmp_path),
            "options": {"socket_path": str(tmp_path / "r.sock"), **extra},
        }
        run = subprocess.run(
            [sys.executable, "-I", "-B", str(process.payload() / "bridge.py")],
            input=json.dumps(request),
            text=True,
            capture_output=True,
            env={"PATH": "/usr/bin:/bin"},
            timeout=2,
            check=False,
        )
        assert run.returncode == 2
        assert not run.stderr
        assert json.loads(run.stdout) == {
            "version": 1,
            "ok": False,
            "error": "preparation_refused_or_incomplete",
        }


def test_pin_update_projection_never_exposes_session_identity(case):
    profile, _, _, invoke = case
    request = {
        "version": 1,
        "operation": "pin_preview",
        "profile": str(profile),
        "options": {
            "binding_id": "binding_test",
            "session_key": "synthetic",
            "candidate_fingerprint": "a" * 64,
            "candidate_session_id": "private-session",
        },
    }
    value = {**RESULT, "requires_confirmation": True, "requires_stopped_gateway": True}
    assert invoke(success(value), request_value=request) == value
    for broken in (
        {**value, "candidate_session_id": "PRIVATE"},
        {**value, "session_verified": True},
        {**value, "state": "ready_to_deliver"},
    ):
        with pytest.raises(ValueError, match="hermes_pin_result_unknown"):
            invoke(success(broken), request_value=request)


@pytest.mark.parametrize(
    "change", [None, "runtime", "delivery", "secret", "state_type", "enabled", "digest"]
)
def test_grant_projection_never_claims_runtime_or_leaks_config(case, change):
    profile, _, _, invoke = case
    request = {"version": 1, "operation": "grant_apply", "profile": str(profile), "options": {}}
    result = {
        "state": "grant_enabled_runtime_unverified",
        "config_sha256": "a" * 64,
        "enabled": True,
        "runtime_verified": False,
        "delivery_verified": False,
    }
    if change == "runtime":
        result["runtime_verified"] = True
    if change == "delivery":
        result["delivery_verified"] = True
    if change == "secret":
        result["config"] = "PRIVATE"
    if change == "state_type":
        result["state"] = ["already_enabled"]
    if change == "enabled":
        result["enabled"] = False
    if change == "digest":
        result["config_sha256"] = "wrong"
    if change is None:
        assert invoke(success(result), request_value=request) == result
    else:
        with pytest.raises(ValueError, match="hermes_grant_result_unknown"):
            invoke(success(result), request_value=request)
