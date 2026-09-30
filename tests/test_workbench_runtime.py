"""Runtime readiness and model metadata use isolated native process fixtures."""

import json
import os
import platform
import sys

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
