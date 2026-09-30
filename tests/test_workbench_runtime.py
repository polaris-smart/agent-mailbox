"""Runtime readiness and model metadata use isolated native process fixtures."""

import json
import os
import platform
import plistlib
import sys
from types import SimpleNamespace

import pytest

from agent_mailbox import __version__
from agent_mailbox import workbench_runtime as runtime


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def fake_binary(path, source):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n" + source)
    path.chmod(0o755)


@pytest.fixture
def installed(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("Native process fixtures use POSIX shebangs")
    monkeypatch.delenv("AGENT_MAIL_RUNTIME_DIR", raising=False)
    root = tmp_path / "state"
    deps = root / "workbench/runtime/deps"
    versions = runtime.runtime_dependencies()
    for package, version in versions.items():
        write_json(deps / "node_modules" / package / "package.json", {"version": version})
    arch = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x64", "amd64": "x64"}[
        platform.machine().lower()
    ]
    target = {
        ("darwin", "arm64"): "aarch64-apple-darwin",
        ("darwin", "x64"): "x86_64-apple-darwin",
        ("linux", "arm64"): "aarch64-unknown-linux-musl",
        ("linux", "x64"): "x86_64-unknown-linux-musl",
    }[(sys.platform, arch)]
    native = deps / "node_modules" / f"@openai/codex-{sys.platform}-{arch}"
    write_json(
        native / "package.json", {"version": f"{versions['@openai/codex']}-{sys.platform}-{arch}"}
    )
    cli, host = (
        native / "vendor" / target / "bin/codex",
        native / "vendor" / target / "bin/codex-code-mode-host",
    )
    fake_binary(cli, "raise SystemExit('No model invocation is permitted')\n")
    fake_binary(host, "raise SystemExit('Host validation belongs to first-run bridge preflight')\n")
    node = tmp_path / "node"
    fake_binary(
        node,
        f"import sys\nprint('v22.23.1' if sys.argv[1]=='--version' else '{sys.platform}-{arch}')\n",
    )
    monkeypatch.setenv("AGENT_MAIL_NODE_BIN", str(node))
    return root, deps, cli, host


@pytest.mark.parametrize(
    "fault", ["codex_missing", "codex_wrong", "host_missing", "native_wrong", "host_not_executable"]
)
def test_incomplete_execution_runtime_is_not_installed(installed, fault):
    root, deps, cli, host = installed
    if fault == "codex_missing":
        (deps / "node_modules/@openai/codex/package.json").unlink()
    elif fault == "codex_wrong":
        write_json(deps / "node_modules/@openai/codex/package.json", {"version": "0.0.0"})
    elif fault == "host_missing":
        host.unlink()
    elif fault == "native_wrong":
        write_json(cli.parents[3] / "package.json", {"version": "0.0.0"})
    else:
        host.chmod(0o600)
    status = runtime.runtime_status(root)
    assert status["node_available"]
    assert not status["installed"] and not status["native_codex_available"]
    assert status["execution_verified"] is False


def test_installed_metadata_is_not_claimed_as_validated_execution(installed):
    root, _, _, _ = installed
    status = runtime.runtime_status(root)
    assert status["installed"] and status["metadata_installed"] and status["native_codex_available"]
    assert status["execution_verified"] is False
    assert "first task" in status["detail"]


def test_expected_python_versions_follow_shipped_manifest(installed, monkeypatch, tmp_path):
    root, deps, _, _ = installed
    manifest = {"dependencies": {**runtime.runtime_dependencies(), "acpx": "99.1.0"}}
    write_json(tmp_path / "assets/package.json", manifest)
    monkeypatch.setattr(runtime, "BRIDGE", tmp_path / "assets/bridge.mjs")
    assert not runtime.runtime_status(root)["metadata_installed"]
    write_json(deps / "node_modules/acpx/package.json", {"version": "99.1.0"})
    assert runtime.runtime_status(root)["metadata_installed"]


def model_server(model, trace):
    return f"""
import json,sys
from pathlib import Path
trace=Path({str(trace)!r})
for line in sys.stdin:
    request=json.loads(line)
    with trace.open('a') as stream: stream.write(json.dumps(request)+'\\n')
    if request.get('method')=='initialize':
        result={{}}
    elif request.get('method')=='model/list':
        result={{'data':[{{'id':{model!r},'displayName':'Fixture model','isDefault':True}}]}}
    else: continue
    print(json.dumps({{'id':request['id'],'result':result}}),flush=True)
"""


@pytest.mark.parametrize("bundled_env", [False, True])
def test_models_come_from_managed_cli_not_user_path(installed, monkeypatch, tmp_path, bundled_env):
    root, deps, cli, _ = installed
    managed_trace, user_trace = tmp_path / "managed.jsonl", tmp_path / "user.jsonl"
    fake_binary(cli, model_server("managed-model", managed_trace))
    user = tmp_path / "user-bin/codex"
    fake_binary(user, model_server("unrelated-user-model", user_trace))
    monkeypatch.setenv("PATH", str(user.parent) + os.pathsep + os.environ.get("PATH", ""))
    assert runtime.executable("codex") == str(user)
    if bundled_env:
        monkeypatch.setenv("AGENT_MAIL_RUNTIME_DIR", str(deps))
    models = runtime.available_models("codex", None if bundled_env else root)
    assert [model["id"] for model in models] == ["managed-model"]
    assert not user_trace.exists()
    requests = [json.loads(line) for line in managed_trace.read_text().splitlines()]
    assert requests[0]["params"]["clientInfo"]["version"] == __version__
    assert [r["method"] for r in requests] == ["initialize", "initialized", "model/list"]


def test_missing_managed_pair_never_uses_user_cli(installed, monkeypatch, tmp_path):
    root, _, _, host = installed
    host.unlink()
    trace = tmp_path / "user.jsonl"
    user = tmp_path / "user-bin/codex"
    fake_binary(user, model_server("unrelated-user-model", trace))
    monkeypatch.setenv("PATH", str(user.parent) + os.pathsep + os.environ.get("PATH", ""))
    assert runtime.available_models("codex", root) == []
    assert not trace.exists()


def test_models_follow_node_architecture_when_python_differs(installed, monkeypatch, tmp_path):
    root, deps, cli, _ = installed
    python_arch = cli.parents[3].name.rsplit("-", 1)[1]
    node_arch = "x64" if python_arch == "arm64" else "arm64"
    native = deps / "node_modules" / f"@openai/codex-{sys.platform}-{node_arch}"
    version = runtime.runtime_dependencies()["@openai/codex"]
    write_json(native / "package.json", {"version": f"{version}-{sys.platform}-{node_arch}"})
    target = {
        ("darwin", "arm64"): "aarch64-apple-darwin",
        ("darwin", "x64"): "x86_64-apple-darwin",
        ("linux", "arm64"): "aarch64-unknown-linux-musl",
        ("linux", "x64"): "x86_64-unknown-linux-musl",
    }[(sys.platform, node_arch)]
    bin_dir = native / "vendor" / target / "bin"
    trace = tmp_path / "node-arch.jsonl"
    fake_binary(bin_dir / "codex", model_server("node-arch-model", trace))
    fake_binary(bin_dir / "codex-code-mode-host", "raise SystemExit()\n")
    node = tmp_path / "alternate-node"
    fake_binary(node, f"print('{sys.platform}-{node_arch}')\n")
    monkeypatch.setenv("AGENT_MAIL_NODE_BIN", str(node))
    assert runtime.available_models("codex", root)[0]["id"] == "node-arch-model"


@pytest.fixture
def discovery(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_MAIL_RUNTIME_DIR", raising=False)
    directories = [tmp_path / "CLI with spaces", tmp_path / "another CLI directory"]
    apps = [tmp_path / "Applications", tmp_path / "User Applications"]
    monkeypatch.setattr(runtime, "discovery_directories", lambda: directories)
    monkeypatch.setattr(runtime, "application_directories", lambda: apps)
    return directories, apps


def app_bundle(path, bundle_id, name, package_type="APPL"):
    info = path / "Contents/Info.plist"
    info.parent.mkdir(parents=True)
    info.write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": bundle_id,
                "CFBundleName": name,
                "CFBundlePackageType": package_type,
            }
        )
    )


def test_discovery_preserves_missing_supported_cli_rows(discovery):
    rows = runtime.discover_employees()
    assert [row["kind"] for row in rows] == ["codex", "claude"]
    assert all(row["status"] == "unavailable" and row["entrypoint"] is None for row in rows)
    assert all(row["execution_supported"] and not row["execution_verified"] for row in rows)
    assert all(row["auth_status"] == "not_checked" for row in rows)


def test_known_clis_are_found_but_unsupported_entries_are_never_launched(discovery, tmp_path):
    if os.name == "nt":
        pytest.skip("Auth status fixtures use POSIX shebangs")
    directories, _ = discovery
    marker = tmp_path / "unsupported-was-launched"
    for kind in runtime.KNOWN_AGENTS:
        command = "wb" if kind == "workbuddy" else kind
        if kind == "codex":
            source = "print('Logged in')\n"
        elif kind == "claude":
            source = "print('{\"loggedIn\": false}')\n"
        else:
            source = f"from pathlib import Path\nPath({str(marker)!r}).write_text('launched')\n"
        fake_binary(directories[0] / command, source)
    rows = runtime.discover_employees()
    assert {r["kind"] for r in rows} == set(runtime.KNOWN_AGENTS)
    assert not marker.exists()
    assert next(r for r in rows if r["kind"] == "workbuddy")["entrypoint"].endswith("/wb")
    assert next(r for r in rows if r["kind"] == "claude")["auth_status"] == "auth_required"
    assert all(
        r["auth_status"] == "not_checked" for r in rows if r["kind"] not in runtime.SUPPORTED
    )
    assert all(r["execution_supported"] == (r["kind"] in runtime.SUPPORTED) for r in rows)


def test_discovery_keeps_multiple_physical_cli_entries_and_exposes_no_auth_output(discovery):
    if os.name == "nt":
        pytest.skip("Auth status fixtures use POSIX shebangs")
    directories, _ = discovery
    secret = "fake-auth-token-do-not-expose"
    for directory in directories:
        fake_binary(directory / "codex", f"print('Logged in as fixture@example.test {secret}')\n")
    rows = [r for r in runtime.discover_employees() if r["kind"] == "codex"]
    assert len(rows) == 2 and len({r["discovery_id"] for r in rows}) == 2
    assert {r["entrypoint"] for r in rows} == {str(d / "codex") for d in directories}
    assert all(r["auth_status"] == "authenticated" for r in rows)
    assert secret not in json.dumps(rows) and "fixture@example.test" not in json.dumps(rows)
    assert rows == [r for r in runtime.discover_employees() if r["kind"] == "codex"]


def test_mac_bundle_identity_overrides_filename_and_does_not_merge_app_and_cli(
    discovery, monkeypatch
):
    directories, apps = discovery
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    for directory in apps:
        app_bundle(directory / "ChatGPT.app", "com.openai.codex", "ChatGPT")
    fake_binary(directories[0] / "wb", "raise SystemExit()\n")
    app_bundle(apps[0] / "WorkBuddy desktop.app", "com.tencent.workbuddy.mac", "WorkBuddy")
    rows = runtime.discover_employees()
    codex_apps = [r for r in rows if r["kind"] == "codex" and r["connection_type"] == "app"]
    assert len(codex_apps) == 2 and len({r["discovery_id"] for r in codex_apps}) == 2
    assert all(r["name"] == "Codex" and r["binary"] is None for r in codex_apps)
    assert {r["entrypoint"] for r in codex_apps} == {str(d / "ChatGPT.app") for d in apps}
    workbuddy = [r for r in rows if r["kind"] == "workbuddy"]
    assert {r["connection_type"] for r in workbuddy} == {"cli", "app"}
    assert all(
        not r["execution_supported"] and not r["execution_verified"] for r in workbuddy + codex_apps
    )
    assert all(r["auth_status"] == "not_checked" for r in workbuddy + codex_apps)


def test_mac_discovery_ignores_url_handlers_nonagents_and_invalid_plists(discovery, monkeypatch):
    _, apps = discovery
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    app_bundle(apps[0] / "Claude.app", "com.anthropic.claude-code-url-handler", "Claude")
    app_bundle(apps[0] / "WorkBuddy.app", "org.example.notes", "Some Notes")
    app_bundle(apps[0] / "Cursor plugin.app", "org.example.plugin", "Cursor", "BNDL")
    app_bundle(apps[0] / "Gemini web.app", "com.google.Chrome.app.fixture", "Gemini")
    invalid = apps[0] / "Broken.app/Contents/Info.plist"
    invalid.parent.mkdir(parents=True)
    invalid.write_bytes(b"invalid plist")
    malformed = apps[0] / "Malformed.app/Contents/Info.plist"
    malformed.parent.mkdir(parents=True)
    malformed.write_bytes(b'<?xml version="1.0"?><plist><dict>')
    app_bundle(apps[1] / "Cursor.app", "org.fixture.cursor", "Cursor")
    rows = runtime.discover_applications()
    assert len(rows) == 1 and rows[0]["kind"] == "cursor"


@pytest.mark.parametrize("system", ["linux", "win32"])
def test_other_platforms_only_discover_known_cli_entries(discovery, monkeypatch, system):
    directories, _ = discovery
    # Keep the host's shutil implementation real; select only our app-scan branch.
    monkeypatch.setattr(runtime, "sys", SimpleNamespace(platform=system))
    monkeypatch.setattr(
        runtime, "application_directories", lambda: pytest.fail("App scanning is macOS-only")
    )
    fake_binary(directories[0] / "ollama", "raise SystemExit()\n")
    rows = runtime.discover_employees()
    assert all(r["connection_type"] == "cli" for r in rows)
    assert next(r for r in rows if r["kind"] == "ollama")["auth_status"] == "not_checked"


def test_discovery_searches_user_python_script_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(runtime.sys, "platform", "darwin")
    monkeypatch.setenv("PATH", "")
    directory = tmp_path / "Library/Python/3.13/bin"
    fake_binary(directory / "hermes", "raise SystemExit()\n")
    assert directory in runtime.discovery_directories()
    assert str(directory / "hermes") in runtime.cli_entries("hermes")


def test_context_prompt_names_scoped_identity_and_message_semantics():
    task = {
        "id": "task-one",
        "assignee_id": "employee-one",
        "run_id": "run-one",
        "request_message_id": "message-one",
        "prompt": "Read the project.",
    }
    context = {"messages": [{"body": "do-not-inject-entire-message-history"}]}
    prompt = runtime.context_prompt(task, context)
    encoded = prompt.split("<project_data>\n")[1].split("\n</project_data>")[0]
    identity = json.loads(encoded)["managed_identity"]
    assert identity["employee_id"] == "employee-one" and identity["task_id"] == "task-one"
    assert identity["run_id"] == "run-one" and identity["request_message_id"] == "message-one"
    assert "must not share these tools or credentials" in prompt
    assert "project_messages" in prompt and "does not trigger work" in prompt
    assert "do-not-inject-entire-message-history" not in prompt


def test_context_prompt_identity_keeps_existing_briefing_bound():
    task = {"prompt": "Read", "assignee_id": "x" * 100000}
    context = {"resources": [{"name": "x" * 2000} for _ in range(100)]}
    prompt = runtime.context_prompt(task, context)
    encoded = prompt.split("<project_data>\n")[1].split("\n</project_data>")[0]
    assert len(encoded) <= 60000
    assert len(json.loads(encoded)["managed_identity"]["employee_id"]) == 256


@pytest.mark.parametrize("operation", ["permission", "cancel"])
def test_python_control_commands_include_session_identity(tmp_path, operation):
    child = tmp_path / "bridge.py"
    trace = tmp_path / "control.json"
    child.write_text(f"""
import json,sys
from pathlib import Path
run=json.loads(sys.stdin.readline())
def send(kind,**payload):
    print(json.dumps(dict(protocol=1,run_id=run['run_id'],session_id=run['session_id'],type=kind,**payload)),flush=True)
send('started')
if {operation!r}=='permission': send('permission_required',request_id='request-one')
control=json.loads(sys.stdin.readline())
Path({str(trace)!r}).write_text(json.dumps(control))
send('result',status='cancelled' if {operation!r}=='cancel' else 'completed',output_text='fixture')
sys.stdin.readline()
""")
    task = {
        "kind": "codex",
        "run_id": "run-one",
        "session_id": "session-one",
        "prompt": "fixture",
        "permission_mode": "read-only",
    }
    events = []
    result = runtime.BridgeExecution(tmp_path, [sys.executable, str(child)]).run(
        task,
        {"path": str(tmp_path)},
        [],
        events.append,
        lambda e: "deny",
        lambda: operation == "cancel" and bool(events),
        timeout=3,
    )
    control = json.loads(trace.read_text())
    assert control["op"] == operation and control["session_id"] == "session-one"
    assert result["status"] == ("cancelled" if operation == "cancel" else "completed")


def test_python_rejects_foreign_control_error_before_any_terminal(tmp_path):
    child = tmp_path / "foreign.py"
    child.write_text("""
import json,sys
run=json.loads(sys.stdin.readline())
print(json.dumps(dict(protocol=1,run_id=run['run_id'],session_id='foreign',type='control_error',error={'code':'INVALID_REQUEST','message':'fake'})),flush=True)
sys.stdin.readline()
""")
    task = {
        "kind": "codex",
        "run_id": "run-one",
        "session_id": "session-one",
        "prompt": "fixture",
        "permission_mode": "read-only",
    }
    result = runtime.BridgeExecution(tmp_path, [sys.executable, str(child)]).run(
        task,
        {"path": str(tmp_path)},
        [],
        lambda e: pytest.fail("Foreign event was accepted"),
        lambda e: "deny",
        lambda: False,
        timeout=3,
    )
    assert result["status"] == "failed" and result["error"]["code"] == "RUNTIME_PROTOCOL_ERROR"
    assert "another employee" in result["error"]["message"]
