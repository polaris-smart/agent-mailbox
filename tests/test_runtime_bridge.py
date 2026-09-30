"""Real Node bridge + pinned ACPX + a separate JSON-RPC fake agent process.

No model calls, user credentials, daemons, or global installation. Provision the
pinned dependencies explicitly and set AGENT_MAILBOX_TEST_RUNTIME_DIR in CI.
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_mailbox.runtime_bridge import bridge_path

FAKE_ACP = r"""
import json, sys, uuid
from pathlib import Path

store, trace, flavor = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
known = json.loads(store.read_text()) if store.exists() else []
pending = {}
permissions = {}
model = "gpt-6-luna"
effort = "low"
def configs():
    values = [{"id":"model", "name":"Model", "category":"model", "type":"select",
        "currentValue":model, "options":[{"value":"gpt-6-luna", "name":"Luna"},
        {"value":"gpt-6-sol", "name":"Sol"}]}]
    if flavor != "no_reasoning":
        values.append({"id":"reasoning_effort", "name":"Effort", "type":"select",
            "currentValue":effort, "options":[{"value":"low", "name":"Low"},
            {"value":"medium", "name":"Medium"}]})
    return values
def out(payload):
    print(json.dumps(payload, ensure_ascii=False), flush=True)
def result(request, value):
    out({"jsonrpc":"2.0", "id":request["id"], "result":value})
def error(request, code, message):
    out({"jsonrpc":"2.0", "id":request["id"], "error":{"code":code, "message":message}})
def chunk(session, text, kind="agent_message_chunk"):
    out({"jsonrpc":"2.0", "method":"session/update", "params":{
        "sessionId":session, "update":{"sessionUpdate":kind,
        "content":{"type":"text", "text":text}}}})
def finish(request, text):
    chunk(request["params"]["sessionId"], text)
    result(request, {"stopReason":"end_turn", "_meta":{"fake":"complete"}})
for line in sys.stdin:
    request = json.loads(line)
    with trace.open("a") as f:
        f.write(json.dumps(request) + "\n")
    method = request.get("method")
    params = request.get("params", {})
    if method == "initialize":
        if flavor == "init_hang":
            continue
        result(request, {"protocolVersion":1, "agentInfo":{"name":"fake", "version":"1"},
            "agentCapabilities":{"loadSession":True, "mcpCapabilities":{"http":True},
            "sessionCapabilities":{"resume":{}, "list":{}}}, "authMethods":[]})
    elif method == "session/new":
        if flavor == "new_hang":
            continue
        if flavor == "auth":
            error(request, -32000, "Authentication required")
            continue
        sid = "fake-" + uuid.uuid4().hex
        known.append(sid)
        store.write_text(json.dumps(known))
        result(request, {"sessionId":sid, "configOptions":configs(), "modes":{"currentModeId":"read-only",
            "availableModes":[{"id":"read-only", "name":"Read only"},
            {"id":"workspace-write", "name":"Workspace write"}]}})
    elif method in ("session/resume", "session/load"):
        if params["sessionId"] not in known:
            error(request, -32002, "Session not found")
        else:
            result(request, {})
    elif method == "session/set_mode":
        result(request, {})
    elif method == "session/set_config_option":
        key, value = params["configId"], params["value"]
        options = next((option for option in configs() if option["id"] == key), None)
        if not options or value not in [o["value"] for o in options["options"]]:
            error(request, -32602, "Unsupported option value")
        else:
            if key == "model": model = value
            else: effort = value
            result(request, {"configOptions":configs()})
    elif method == "session/prompt":
        text = "".join(p.get("text", "") for p in params["prompt"])
        if text in ("permission_mcp", "permission_other", "permission_no_event"):
            if text != "permission_no_event":
                out({"jsonrpc":"2.0", "method":"session/update", "params":{
                    "sessionId":params["sessionId"], "update":{"sessionUpdate":"tool_call",
                    "toolCallId":"mcp-one", "title":"MCP tool", "kind":"execute", "status":"in_progress",
                    "rawInput":{"server":"agent-mailbox-project", "tool":"project_context", "arguments":{}}}}})
            reqid = "ask-" + uuid.uuid4().hex
            permissions[reqid] = request
            pending[params["sessionId"]] = request
            out({"jsonrpc":"2.0", "id":reqid, "method":"session/request_permission",
                "params":{"sessionId":params["sessionId"],
                    "toolCall":{"toolCallId":"mcp-other" if text == "permission_other" else "mcp-one",
                        "kind":"execute", "status":"pending"},
                    "options":[{"optionId":"allow", "name":"Allow", "kind":"allow_once"},
                        {"optionId":"deny", "name":"Deny", "kind":"reject_once"}],
                    "_meta":{"is_mcp_tool_approval":True}}})
        elif text in ("permission", "permission_cancel"):
            reqid = "ask-" + uuid.uuid4().hex
            permissions[reqid] = request
            pending[params["sessionId"]] = request
            out({"jsonrpc":"2.0", "id":reqid, "method":"session/request_permission",
                 "params":{"sessionId":params["sessionId"],
                    "toolCall":{"toolCallId":"write-1", "title":"Write file", "kind":"edit"},
                    "options":[{"optionId":"allow", "name":"Allow", "kind":"allow_once"},
                               {"optionId":"deny", "name":"Deny", "kind":"reject_once"}]}})
        elif text == "hang":
            pending[params["sessionId"]] = request
        elif text == "error":
            error(request, -32603, "Synthetic failure sk-fakecredential123")
        elif text == "crash":
            sys.exit(7)
        elif text == "empty":
            result(request, {"stopReason":"end_turn"})
        elif text == "long":
            finish(request, "字" * 50000)
        elif text in ("provider_model_error", "provider_fragmented"):
            chunk(params["sessionId"], "Warning: Model metadata was not found.\n\n")
            envelope = json.dumps({"type":"error", "status":400,
                "error":{"type":"invalid_request_error", "message":
                    "The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account."}})
            if text == "provider_fragmented":
                chunk(params["sessionId"], envelope[:40])
                finish(request, envelope[40:])
            else: finish(request, envelope + "\n\n")
        elif text == "provider_service_error":
            finish(request, json.dumps({"type":"error", "status":503,
                "error":{"type":"server_error", "message":"Provider temporarily unavailable"}}))
        elif text == "provider_prose":
            finish(request, "An unsupported model can cause an error; this is an explanation, not a provider error.")
        elif text == "provider_quoted_example":
            finish(request, '```json\n{"type":"error","status":503,"error":{"type":"server_error","message":"example"}}\n```')
        elif text == "provider_native_failure":
            result(request, {"stopReason":"end_turn", "_meta":{"jetbrains":{"air":{
                "sessionFailure":{"severity":"error", "title":"Provider denied the request"}}}}})
        else:
            chunk(params["sessionId"], "hidden thought", "agent_thought_chunk")
            chunk(params["sessionId"], "Hello ")
            finish(request, "world")
    elif method == "session/cancel":
        prior = pending.pop(params["sessionId"], None)
        if prior:
            result(prior, {"stopReason":"cancelled"})
    elif method == "session/close":
        result(request, {})
    elif method is None and request.get("id") in permissions:
        prior = permissions.pop(request["id"])
        outcome = request.get("result", {}).get("outcome", {})
        if pending.pop(prior["params"]["sessionId"], None):
            finish(prior, "allowed" if outcome.get("optionId") == "allow" else "denied")
    elif "id" in request:
        error(request, -32601, "Method not found")
"""


class Bridge:
    def __init__(
        self, directory, runtime_dir, flavor="normal", project=None, fake_agent=True, **extra
    ):
        self.directory = directory
        directory.mkdir(exist_ok=True)
        self.project = project or directory / "project"
        self.project.mkdir(exist_ok=True)
        self.trace = directory / "trace.jsonl"
        fake = directory / "fake_agent.py"
        fake.write_text(FAKE_ACP)
        args = [
            shutil.which("node"),
            str(bridge_path()),
            "--state-dir",
            str(directory / "state"),
            "--project-root",
            str(self.project),
            "--runtime-dir",
            str(runtime_dir),
            "--startup-timeout-ms",
            "1000",
            "--permission-timeout-ms",
            "1000",
        ]
        if fake_agent:
            args.extend(
                [
                    "--test-mode",
                    "1",
                    "--test-agent-command-json",
                    json.dumps(
                        [
                            sys.executable,
                            "-u",
                            str(fake),
                            str(directory / "fake_store.json"),
                            str(self.trace),
                            flavor,
                        ]
                    ),
                ]
            )
        for key, value in extra.items():
            args.extend(["--" + key.replace("_", "-"), str(value)])
        self.process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.messages = queue.Queue()
        self.seen = []
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                self.messages.put({"non_protocol_stdout": line})

    def send(self, **value):
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()

    def run(self, run_id="r1", session_id="s1", prompt="hello", **value):
        self.send(
            op="run",
            run_id=run_id,
            session_id=session_id,
            agent="codex",
            prompt=prompt,
            cwd=str(self.project),
            **value,
        )

    def until(self, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            assert "non_protocol_stdout" not in message, message
            assert message["protocol"] == 1
            self.seen.append(message)
            if predicate(message):
                return message
        raise AssertionError("No expected bridge event")

    def result(self, run_id="r1"):
        return self.until(lambda m: m["type"] == "result" and m["run_id"] == run_id)

    def requests(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()]

    def close(self):
        if self.process.poll() is None:
            self.send(op="shutdown")
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)
            raise AssertionError("Bridge shutdown did not stop its fake ACP child")
        stderr = self.process.stderr.read()
        assert "sk-fakecredential123" not in stderr
        self.reader.join(timeout=1)


@pytest.fixture
def runtime_dir():
    runtime = Path(os.environ.get("AGENT_MAILBOX_TEST_RUNTIME_DIR", str(bridge_path().parent)))
    if not shutil.which("node") or not (runtime / "node_modules/acpx/dist/runtime.js").exists():
        pytest.skip("Explicitly provision acpx@0.19.3 and set AGENT_MAILBOX_TEST_RUNTIME_DIR")
    return runtime


@pytest.fixture
def launch(tmp_path, runtime_dir):
    processes = []

    def create(flavor="normal", directory=None, **extra):
        dependencies = extra.pop("runtime_dir", runtime_dir)
        process = Bridge(
            directory or tmp_path / f"bridge{len(processes)}", dependencies, flavor, **extra
        )
        processes.append(process)
        return process

    yield create
    for process in processes:
        process.close()


def test_injects_mcp_and_returns_complete_output(launch):
    bridge = launch()
    server = {
        "name": "project",
        "command": sys.executable,
        "args": ["-m", "fake_mcp"],
        "env": [{"name": "PROJECT_TOKEN", "value": "synthetic-only"}],
    }
    bridge.run(mcp_servers=[server])
    result = bridge.result()
    assert result["status"] == "completed"
    assert result["output_text"] == "Hello world"
    assert result["runtime_result"]["_meta"] == {"fake": "complete"}
    assert any(m["type"] == "started" for m in bridge.seen)
    requests = bridge.requests()
    new = next(r for r in requests if r.get("method") == "session/new")
    assert new["params"]["mcpServers"] == [server]
    mode = next(r for r in requests if r.get("method") == "session/set_mode")
    assert mode["params"]["modeId"] == "read-only"
    initialize = next(r for r in requests if r.get("method") == "initialize")
    assert initialize["params"]["clientCapabilities"]["fs"] == {
        "readTextFile": False,
        "writeTextFile": False,
    }
    record = json.loads((bridge.directory / "state/sessions/s1.json").read_text())
    assert record["event_log"]["active_path"].startswith(str(bridge.directory / "state"))
    assert "synthetic-only" not in json.dumps(record)


@pytest.mark.parametrize("decision,expected", [("allow_once", "allowed"), ("deny", "denied")])
def test_permission_round_trip(launch, decision, expected):
    bridge = launch()
    bridge.run(prompt="permission", sandbox="workspace-write")
    request = bridge.until(lambda m: m["type"] == "permission_required")
    assert request["options"] == request["request"]["options"]
    assert request["tool_call"] == request["request"]["toolCall"]
    bridge.send(op="permission", run_id="r1", request_id=request["request_id"], decision=decision)
    assert bridge.result()["output_text"] == expected


def test_permission_defaults_to_deny_and_late_answer_is_rejected(launch):
    bridge = launch()
    bridge.run(prompt="permission", permission_timeout_ms=50)
    request = bridge.until(lambda m: m["type"] == "permission_required")
    assert bridge.result()["output_text"] == "denied"
    bridge.send(
        op="permission", run_id="r1", request_id=request["request_id"], decision="allow_once"
    )
    assert bridge.until(lambda m: m["type"] == "control_error")
    assert sum(m["type"] == "result" for m in bridge.seen) == 1


def test_cancel_during_permission_rejects_late_answer(launch):
    bridge = launch()
    bridge.run(prompt="permission_cancel")
    request = bridge.until(lambda m: m["type"] == "permission_required")
    bridge.send(op="cancel", run_id="r1")
    assert bridge.result()["status"] == "cancelled"
    bridge.send(
        op="permission", run_id="r1", request_id=request["request_id"], decision="allow_once"
    )
    bridge.until(lambda m: m["type"] == "control_error")
    assert sum(m["type"] == "result" for m in bridge.seen) == 1


def test_queued_cancel_does_not_cancel_other_run(launch):
    bridge = launch()
    bridge.run(prompt="hang")
    bridge.until(lambda m: m["type"] == "started")
    bridge.run(run_id="r2", prompt="hello")
    bridge.send(op="cancel", run_id="r2")
    assert bridge.result("r2")["status"] == "cancelled"
    assert not any(m["type"] == "result" and m["run_id"] == "r1" for m in bridge.seen)
    bridge.send(op="cancel", run_id="r1")
    assert bridge.result()["status"] == "cancelled"


@pytest.mark.parametrize(
    "flavor,prompt,expected",
    [
        ("auth", "hello", "AUTH_REQUIRED"),
        ("init_hang", "hello", "TIMEOUT"),
        ("new_hang", "hello", "TIMEOUT"),
        ("normal", "hang", "TIMEOUT"),
        ("normal", "error", "AGENT_FAILED"),
        ("normal", "crash", "AGENT_FAILED"),
    ],
)
def test_explicit_errors_and_timeouts(launch, flavor, prompt, expected):
    bridge = launch(flavor)
    bridge.run(prompt=prompt, timeout_ms=100)
    result = bridge.result()
    assert result["status"] == "failed"
    assert result["error"]["code"] == expected
    assert "sk-fakecredential123" not in json.dumps(result)


def test_empty_and_large_output_are_not_confused_or_truncated(launch):
    bridge = launch()
    bridge.run(prompt="empty")
    assert bridge.result()["output_text"] == ""
    bridge.run(run_id="r2", prompt="long")
    assert bridge.result("r2")["output_text"] == "字" * 50000


def test_resume_after_bridge_restart_and_no_fallback_on_failure(launch):
    first = launch()
    first.run()
    first.result()
    sid = next(m["backend_session_id"] for m in first.seen if m["type"] == "session")
    first.close()
    second = launch(directory=first.directory, project=first.project)
    second.run(run_id="r2", resume_session_id=sid)
    assert second.result("r2")["status"] == "completed"
    requests = second.requests()
    assert any(r.get("method") == "session/resume" for r in requests)
    assert sum(r.get("method") == "session/new" for r in requests) == 1
    third = launch()
    third.run(resume_session_id="missing-native-session")
    failed = third.result()
    assert failed["error"]["code"] == "SESSION_RESUME_FAILED"
    assert not any(r.get("method") == "session/new" for r in third.requests())


def test_realpath_boundary_and_context_change_fail_closed(launch, tmp_path):
    bridge = launch()
    outside = tmp_path / "outside"
    outside.mkdir()
    (bridge.project / "escape").symlink_to(outside, target_is_directory=True)
    bridge.send(
        op="run",
        run_id="escape",
        session_id="bad",
        agent="codex",
        prompt="hello",
        cwd=str(bridge.project / "escape"),
    )
    assert bridge.result("escape")["error"]["code"] == "PROJECT_BOUNDARY"
    bridge.run()
    bridge.result()
    subdirectory = bridge.project / "subdirectory"
    subdirectory.mkdir()
    bridge.send(
        op="run", run_id="r2", session_id="s1", agent="codex", prompt="hello", cwd=str(subdirectory)
    )
    assert bridge.result("r2")["error"]["code"] == "SESSION_CONTEXT_MISMATCH"
    assert sum(r.get("method") == "session/new" for r in bridge.requests()) == 1


def test_missing_runtime_and_duplicate_admission(launch, tmp_path):
    bridge = launch(runtime_dir=tmp_path / "not-installed")
    bridge.run()
    assert bridge.result()["error"]["code"] == "RUNTIME_MISSING"
    bridge.run()
    bridge.until(lambda m: m["type"] == "control_error")
    assert sum(m["type"] == "result" for m in bridge.seen) == 1


def test_queued_sandbox_upgrade_applies_only_after_active_turn_ends(launch):
    bridge = launch()
    bridge.run(prompt="hang")
    bridge.until(lambda m: m["type"] == "started")
    bridge.run(run_id="r2", sandbox="workspace-write")
    bridge.send(op="cancel", run_id="r1")
    assert bridge.result()["status"] == "cancelled"
    assert bridge.result("r2")["status"] == "completed"
    trace = bridge.requests()
    modes = [
        (i, r["params"]["modeId"])
        for i, r in enumerate(trace)
        if r.get("method") == "session/set_mode"
    ]
    cancel = next(i for i, r in enumerate(trace) if r.get("method") == "session/cancel")
    assert [mode for _, mode in modes] == ["read-only", "workspace-write"]
    assert modes[0][0] < cancel < modes[1][0]
    assert sum(r.get("method") == "session/new" for r in trace) == 1


def test_missing_local_adapter_does_not_fall_back_to_npx(launch, runtime_dir):
    if (runtime_dir / "node_modules/@agentclientprotocol/codex-acp").exists():
        pytest.skip("This negative test uses an ACPX-only dependency installation")
    bridge = launch(fake_agent=False)
    bridge.run()
    assert bridge.result()["error"]["code"] == "AGENT_UNAVAILABLE"
    assert not bridge.trace.exists()


def test_corrupt_session_state_cannot_silently_start_new_thread(launch):
    first = launch()
    first.run()
    first.result()
    first.close()
    (first.directory / "state/sessions/s1.json").write_text("invalid-session-json")
    second = launch(directory=first.directory, project=first.project)
    second.run(run_id="r2")
    assert second.result("r2")["error"]["code"] == "SESSION_RESUME_FAILED"
    assert sum(r.get("method") == "session/new" for r in second.requests()) == 1


@pytest.mark.parametrize(
    "prompt,code",
    [
        ("provider_model_error", "MODEL_UNSUPPORTED"),
        ("provider_fragmented", "MODEL_UNSUPPORTED"),
        ("provider_service_error", "PROVIDER_ERROR"),
        ("provider_native_failure", "PROVIDER_ERROR"),
    ],
)
def test_provider_error_cannot_become_a_successful_deliverable(launch, prompt, code):
    bridge = launch()
    bridge.run(prompt=prompt)
    result = bridge.result()
    assert result["status"] == "failed"
    assert result["error"]["code"] == code
    assert result["runtime_result"]["status"] == "completed"


@pytest.mark.parametrize("prompt", ["provider_prose", "provider_quoted_example"])
def test_error_explanations_do_not_trigger_provider_failure(launch, prompt):
    bridge = launch()
    bridge.run(prompt=prompt)
    assert bridge.result()["status"] == "completed"


def test_explicit_model_and_reasoning_are_applied_before_prompt(launch):
    bridge = launch()
    bridge.run(model="gpt-6-luna", reasoning_effort="low")
    assert bridge.result()["status"] == "completed"
    trace = bridge.requests()
    controls = [
        (i, r["params"])
        for i, r in enumerate(trace)
        if r.get("method") == "session/set_config_option"
    ]
    prompt = next(i for i, r in enumerate(trace) if r.get("method") == "session/prompt")
    assert {c["configId"]: c["value"] for _, c in controls} == {
        "model": "gpt-6-luna",
        "reasoning_effort": "low",
    }
    assert all(i < prompt for i, _ in controls)
    session = next(m for m in bridge.seen if m["type"] == "session")
    assert session["model"] == "gpt-6-luna" and session["reasoning_effort"] == "low"


@pytest.mark.parametrize(
    "flavor,profile,code",
    [
        ("normal", {"model": "invented-model"}, "MODEL_UNSUPPORTED"),
        ("no_reasoning", {"reasoning_effort": "low"}, "CONFIG_UNSUPPORTED"),
        ("normal", {"reasoning_effort": "invented-effort"}, "CONFIG_UNSUPPORTED"),
    ],
)
def test_unknown_or_unadvertised_profile_is_rejected_before_execution(
    launch, flavor, profile, code
):
    bridge = launch(flavor)
    bridge.run(**profile)
    assert bridge.result()["error"]["code"] == code
    assert not any(r.get("method") == "session/prompt" for r in bridge.requests())


FAKE_CODE_MODE_HOST = r"""
import json, struct, sys

def send(value):
    data = json.dumps(value).encode()
    sys.stdout.buffer.write(struct.pack('<I', len(data)) + data)
    sys.stdout.buffer.flush()
while True:
    prefix = sys.stdin.buffer.read(4)
    if len(prefix) != 4: break
    value = json.loads(sys.stdin.buffer.read(struct.unpack('<I', prefix)[0]))
    if value['type'] == 'connection/hello':
        send({'type':'connection/ready','selectedVersion':1,'capabilities':[]})
    elif value['request']['method'] == 'session/open':
        send({'type':'operation/response','id':value['id'],'result':{'status':'ok',
            'value':{'type':'session/ready','sessionId':value['request']['sessionId']}}})
    else:
        result = {'cell_id':'1','content_items':[{'type':'input_text','text':'4'}],
            'error_text':None}
        if not LEGACY:
            result['code_mode_host_duration_ns'] = 12345
        send({'type':'execute/initialResponse','id':value['id'],
            'result':{'status':'ok','value':{'Result':result}}})
"""


def pair_runtime(directory, legacy=False, version="0.158.0"):
    if os.name == "nt":
        pytest.skip("Fixture executables use POSIX shebangs")
    platform = subprocess.check_output(
        [shutil.which("node"), "-p", "process.platform+'-'+process.arch"], text=True
    ).strip()
    targets = {
        "darwin-arm64": "aarch64-apple-darwin",
        "darwin-x64": "x86_64-apple-darwin",
        "linux-arm64": "aarch64-unknown-linux-musl",
        "linux-x64": "x86_64-unknown-linux-musl",
    }
    root = directory / "node_modules/@openai"
    package = root / f"codex-{platform}"
    bin_dir = package / "vendor" / targets[platform] / "bin"
    bin_dir.mkdir(parents=True)
    (root / "codex").mkdir()
    (root / "codex/package.json").write_text(json.dumps({"version": "0.158.0"}))
    (package / "package.json").write_text(json.dumps({"version": f"0.158.0-{platform}"}))
    cli, host = bin_dir / "codex", bin_dir / "codex-code-mode-host"
    cli.write_text(f"#!{sys.executable}\nprint('codex-cli {version}')\n")
    host.write_text(f"#!{sys.executable}\nLEGACY={legacy!r}\n" + FAKE_CODE_MODE_HOST)
    cli.chmod(0o755)
    host.chmod(0o755)
    return cli, host


def resolve_pair(directory):
    script = """
const {resolveCodexPair}=await import(process.argv[1]);
try { console.log(JSON.stringify({ok:true,pair:await resolveCodexPair(process.argv[2])})); }
catch(error) { console.log(JSON.stringify({ok:false,code:error.code})); }
"""
    result = subprocess.run(
        [
            shutil.which("node"),
            "--input-type=module",
            "-e",
            script,
            (bridge_path().parent / "codex_pair.mjs").as_uri(),
            str(directory),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return json.loads(result.stdout)


def test_managed_codex_uses_locked_cli_and_real_host_frames(tmp_path, runtime_dir, monkeypatch):
    cli, host = pair_runtime(tmp_path)
    monkeypatch.setenv("CODEX_PATH", "/incompatible/user/codex")
    result = resolve_pair(tmp_path)
    assert result == {
        "ok": True,
        "pair": {
            "source": "managed-runtime",
            "version": "0.158.0",
            "path": str(cli),
            "host_path": str(host),
        },
    }


@pytest.mark.parametrize("fault", ["missing", "wrong_version", "legacy_host", "wrong_metadata"])
def test_managed_codex_incomplete_or_old_v1_pair_fails_closed(tmp_path, runtime_dir, fault):
    cli, host = pair_runtime(
        tmp_path,
        legacy=fault == "legacy_host",
        version="0.157.0" if fault == "wrong_version" else "0.158.0",
    )
    if fault == "missing":
        host.unlink()
    if fault == "wrong_metadata":
        (tmp_path / "node_modules/@openai/codex/package.json").write_text('{"version":"0.157.0"}')
    assert resolve_pair(tmp_path) == {"ok": False, "code": "RUNTIME_INCOMPATIBLE"}
    assert cli.exists()


def test_mcp_permission_details_join_only_the_same_run_and_tool_id(launch):
    bridge = launch()
    bridge.run(prompt="permission_mcp")
    request = bridge.until(lambda m: m["type"] == "permission_required")
    assert "title" not in request["request"]["toolCall"]
    assert "rawInput" not in request["request"]["toolCall"]
    assert request["tool_call"]["title"] == "agent-mailbox-project / project_context"
    assert request["tool_call"]["rawInput"] == {
        "server": "agent-mailbox-project",
        "tool": "project_context",
        "arguments": {},
    }
    bridge.send(
        op="permission", run_id="r1", request_id=request["request_id"], decision="allow_once"
    )
    assert bridge.result()["output_text"] == "allowed"
    # Even the same session and same tool ID cannot reuse another run's details.
    bridge.run(run_id="r2", prompt="permission_no_event")
    next_request = bridge.until(lambda m: m["type"] == "permission_required")
    assert "title" not in next_request["tool_call"]
    assert "rawInput" not in next_request["tool_call"]
    bridge.send(
        op="permission", run_id="r2", request_id=next_request["request_id"], decision="deny"
    )
    assert bridge.result("r2")["output_text"] == "denied"


def test_mcp_permission_does_not_join_a_different_tool_id(launch):
    bridge = launch()
    bridge.run(prompt="permission_other")
    request = bridge.until(lambda m: m["type"] == "permission_required")
    assert request["tool_call"]["toolCallId"] == "mcp-other"
    assert "title" not in request["tool_call"] and "rawInput" not in request["tool_call"]
    bridge.send(op="permission", run_id="r1", request_id=request["request_id"], decision="deny")
    assert bridge.result()["output_text"] == "denied"
