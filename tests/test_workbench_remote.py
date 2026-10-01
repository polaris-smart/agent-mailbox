"""Owner HTTP -> pinned device HTTPS -> fake bridge child -> human acceptance.

Only isolated temporary homes and loopback listeners are used. No installed
agent, user mailbox, global configuration or model provider is contacted.
"""

import json
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from agent_mailbox.workbench import WorkbenchHTTP
from agent_mailbox.workbench_engine import WorkbenchEngine
from agent_mailbox.workbench_remote import RemoteWorker
from agent_mailbox.workbench_runtime import BridgeExecution
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore

BRIDGE = r"""
import asyncio,json,os,sys,pathlib
request=json.loads(sys.stdin.readline())
def send(kind, **fields):
    print(json.dumps(dict(protocol=1,run_id=request['run_id'],
                         session_id=request['session_id'],type=kind,**fields)),flush=True)
send('started')
if request['prompt']=='wait':
    operation=json.loads(sys.stdin.readline())
    send('result',status='cancelled' if operation['op']=='cancel' else 'failed')
    sys.exit(0)
if request['prompt'].startswith('permission'):
    send('permission_required',request_id='write',
         options=[{'optionId':'once','name':'Allow once','kind':
                  'allow_always' if request['prompt']=='permission-invalid' else 'allow_once',
                  'private':'option-secret'}],
         tool_call={'title':'Write a file','rawInput':{'secret':'tool-secret'},'content':'private-content'},
         request={'credentials':'raw-request-secret'},
         timeout_ms=1000 if request['prompt']=='permission-timeout' else 120000)
    answer=json.loads(sys.stdin.readline())
    if answer.get('decision')=='allow_once':
        pathlib.Path(request['cwd'],'permission-approved').write_text('authorized')
        send('result',status='completed',output_text='Human approved this operation once')
    else:
        send('result',status='failed',error={'code':'REMOTE_PERMISSION_DENIED','message':'Denied'})
    sys.exit(0)
if request['prompt']=='context':
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    server=request['mcp_servers'][0]
    env={**os.environ,**{item['name']:item['value'] for item in server['env']}}
    async def call():
        async with stdio_client(StdioServerParameters(command=server['command'],
                 args=server['args'],env=env)) as (reader,writer),ClientSession(reader,writer) as session:
            await session.initialize()
            context=await session.call_tool('project_context',{})
            if context.is_error:
                raise RuntimeError('Shared context failed')
            note=await session.call_tool('project_note',{'title':'Remote note','body':'Context verified'})
            if note.is_error:
                raise RuntimeError('Shared note failed')
            return json.dumps(context.model_dump())
    result=asyncio.run(call())
else:
    result='Mapped directory: '+request['cwd']
send('result',status='completed',output_text=result)
sys.stdin.readline()
"""


def request(server, path, data=None):
    encoded = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request(
        server.endpoint + "/api/workbench/" + path,
        data=encoded,
        headers={"Authorization": "Bearer " + server.token},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        pytest.fail(f"HTTP {exc.code} for {path}: {exc.read().decode()}")


def await_task(store, task_id, status, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = store.get_task(task_id)
        if task["status"] == status:
            return task
        time.sleep(0.02)
    pytest.fail(f"Expected {status}, got {task['status']}: {task.get('error')}")


@pytest.fixture
def remote(tmp_path, monkeypatch):
    bridge = tmp_path / "fake_bridge.py"
    bridge.write_text(BRIDGE)
    command = [sys.executable, str(bridge)]
    # Discovery must never invoke a real user's Codex/Claude installation.
    monkeypatch.setattr(
        "agent_mailbox.workbench.discover_employees",
        lambda: [
            {"kind": "codex", "name": "Fake Codex", "status": "installed", "detail": "Fixture"},
        ],
    )
    owner = WorkbenchStore(tmp_path / "owner")
    remote_store = WorkbenchStore(tmp_path / "device")
    owner_folder = tmp_path / "owner-project"
    remote_folder = tmp_path / "device-project"
    owner_folder.mkdir()
    remote_folder.mkdir()
    project = owner.create_project("Shared", owner_folder)
    private = owner.create_project("Private", tmp_path)
    owner.add_memory(project["id"], "Shared marker", "remote-shared-context")
    owner.add_memory(private["id"], "Private marker", "must-not-leak-to-remote")
    owner_http = WorkbenchHTTP(owner, engine=WorkbenchEngine(owner, command), token="owner-fixture")
    device_http = WorkbenchHTTP(
        remote_store, engine=WorkbenchEngine(remote_store, command), token="device-fixture"
    )
    threads = []
    for server in (owner_http, device_http):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        threads.append(thread)
        server.engine.start()
    request(owner_http, "fleet/start", {"address": "127.0.0.1"})
    invite = request(owner_http, "fleet/invite", {"project_ids": [project["id"]]})
    request(device_http, "fleet/join", {"invite": invite})
    device_http.remote_worker.execution = BridgeExecution(remote_store.root, command)
    employee = request(
        device_http,
        "fleet/employee",
        {
            "project_id": project["id"],
            "name": "Remote fixture",
            "kind": "codex",
        },
    )
    request(device_http, "fleet/map", {"project_id": project["id"], "path": str(remote_folder)})
    try:
        yield owner_http, device_http, project, employee, remote_folder
    finally:
        for server in (device_http, owner_http):
            server.shutdown()
            server.close()
        for thread in threads:
            thread.join(timeout=2)


def dispatch(remote, prompt):
    owner, _, project, employee, _ = remote
    return request(
        owner,
        "tasks",
        {
            "project_id": project["id"],
            "title": prompt,
            "prompt": prompt,
            "assignee_id": employee["id"],
        },
    )


def test_remote_dispatch_real_https_bridge_result_then_human_accept(remote):
    owner, device, _, employee, folder = remote
    task = dispatch(remote, "complete")
    result = await_task(owner.store, task["id"], "review")
    assert result["node_id"] == device.store.local_node()["id"]
    assert result["assignee_id"] == employee["id"]
    assert result["result"] == "Mapped directory: " + str(folder)
    types = [event["type"] for event in owner.store.task_detail(task["id"])["events"]]
    assert "starting" in types and "started" in types and "review" in types
    assert request(owner, f"tasks/{task['id']}/review", {"decision": "accept"})["status"] == "done"
    assert task["run_id"] == owner.store.get_task(task["id"])["run_id"]
    assert task["session_id"] == owner.store.get_task(task["id"])["session_id"]


def test_remote_control_cancel_stops_child_and_releases_same_employee_queue(remote):
    owner, _, _, _, _ = remote
    first = dispatch(remote, "wait")
    await_task(owner.store, first["id"], "running")
    second = dispatch(remote, "complete")
    assert owner.store.get_task(second["id"])["status"] == "queued"
    cancelled = request(owner, f"tasks/{first['id']}/cancel", {})
    assert cancelled["cancel_requested"] is True
    assert await_task(owner.store, first["id"], "cancelled")["cancel_requested"] is True
    assert await_task(owner.store, second["id"], "review")["result"]


def test_remote_permission_requests_follow_owner_human_deny(remote):
    owner, _, _, _, _ = remote
    task = dispatch(remote, "permission")
    await_task(owner.store, task["id"], "waiting_approval")
    request(owner, f"tasks/{task['id']}/permissions/write", {"decision": "deny"})
    result = await_task(owner.store, task["id"], "failed")
    assert result["error"]["code"] == "REMOTE_PERMISSION_DENIED"
    detail = owner.store.task_detail(task["id"])
    assert detail["permissions"][0]["decision"] == "deny"
    assert any(event["type"] == "permission_resolved" for event in detail["events"])


def test_remote_permission_owner_allow_once_uses_only_sanitized_summary(remote):
    owner, _, _, _, folder = remote
    task = dispatch(remote, "permission")
    await_task(owner.store, task["id"], "waiting_approval")
    detail = owner.store.task_detail(task["id"])
    encoded = json.dumps(detail)
    for secret in ("option-secret", "tool-secret", "private-content", "raw-request-secret"):
        assert secret not in encoded
    assert detail["permissions"][0]["tool_call"] == {"title": "Write a file"}
    request(owner, f"tasks/{task['id']}/permissions/write", {"decision": "allow_once"})
    assert (
        await_task(owner.store, task["id"], "review")["result"]
        == "Human approved this operation once"
    )
    assert (folder / "permission-approved").read_text() == "authorized"


def test_remote_permission_timeout_expires_and_late_approval_fails(remote):
    owner, _, _, _, _ = remote
    task = dispatch(remote, "permission-timeout")
    result = await_task(owner.store, task["id"], "failed")
    assert result["error"]["code"] == "REMOTE_PERMISSION_DENIED"
    assert owner.store.get_permission(task["id"], "write")["status"] == "expired"
    with pytest.raises(WorkbenchError):
        owner.store.resolve_permission(task["id"], "write", "allow_once")


def test_remote_permission_cancel_never_allows_operation(remote):
    owner, _, _, _, _ = remote
    task = dispatch(remote, "permission")
    await_task(owner.store, task["id"], "waiting_approval")
    request(owner, f"tasks/{task['id']}/cancel", {})
    assert await_task(owner.store, task["id"], "cancelled")["cancel_requested"]
    assert owner.store.get_permission(task["id"], "write")["status"] == "expired"


def test_remote_permission_without_allow_once_option_cannot_be_approved(remote):
    owner, _, _, _, _ = remote
    task = dispatch(remote, "permission-invalid")
    await_task(owner.store, task["id"], "waiting_approval")
    with pytest.raises(WorkbenchError):
        owner.store.resolve_permission(task["id"], "write", "allow_once")
    request(owner, f"tasks/{task['id']}/permissions/write", {"decision": "deny"})
    assert (
        await_task(owner.store, task["id"], "failed")["error"]["code"] == "REMOTE_PERMISSION_DENIED"
    )


def test_remote_permission_revoke_stops_child_without_authorized_operation(remote):
    owner, device, _, _, folder = remote
    task = dispatch(remote, "permission")
    await_task(owner.store, task["id"], "waiting_approval")
    request(owner, "fleet/revoke", {"device_id": device.store.local_node()["id"]})
    deadline = time.monotonic() + 5
    while device.remote_worker.status()["active"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert device.remote_worker.status()["active"] == 0
    assert not (folder / "permission-approved").exists()
    assert owner.store.get_permission(task["id"], "write")["status"] == "expired"
    with pytest.raises(WorkbenchError):
        owner.store.resolve_permission(task["id"], "write", "allow_once")


def test_remote_permission_disconnect_never_runs_unapproved_operation(remote, monkeypatch):
    owner, device, _, _, folder = remote

    def disconnected(*args, **kwargs):
        raise WorkbenchError("network_error", "Coordinator disconnected")

    monkeypatch.setattr(device.remote_client, "permission_decision", disconnected)
    task = dispatch(remote, "permission")
    assert (
        await_task(owner.store, task["id"], "failed")["error"]["code"] == "REMOTE_PERMISSION_DENIED"
    )
    assert not (folder / "permission-approved").exists()
    assert owner.store.get_permission(task["id"], "write")["status"] == "expired"


def test_remote_permission_worker_stop_expires_pending_without_allow(remote):
    owner, device, _, _, folder = remote
    task = dispatch(remote, "permission")
    await_task(owner.store, task["id"], "waiting_approval")
    device.remote_worker.close()
    assert await_task(owner.store, task["id"], "cancelled")
    assert owner.store.get_permission(task["id"], "write")["status"] == "expired"
    assert not (folder / "permission-approved").exists()


def test_remote_child_uses_injected_mcp_shared_context_and_proposes_note(remote):
    owner, device, project, employee, _ = remote
    task = dispatch(remote, "context")
    result = await_task(owner.store, task["id"], "review")
    assert "remote-shared-context" in result["result"]
    assert "must-not-leak-to-remote" not in result["result"]
    assert project["path"] not in result["result"]
    assert device.remote_client.credentials["token"] not in result["result"]
    notes = owner.store.search_memory(project["id"], "Context verified")
    assert notes[0]["source"] == "employee:" + employee["id"]


def test_remote_recovery_keeps_journal_if_coordinator_does_not_acknowledge(remote, monkeypatch):
    owner, device, project, employee, _ = remote
    device.remote_worker.close()
    task = owner.store.create_task(project["id"], "Recover", "complete", employee["id"])
    client = device.remote_client
    assert client.claim(project["id"], wait=0)["id"] == task["id"]
    active_path = client.directory / "active-runs.json"
    active_path.write_text(json.dumps({task["id"]: task["run_id"]}))

    def unacknowledged(*args, **kwargs):
        raise WorkbenchError("network_error", "Coordinator offline")

    monkeypatch.setattr(client, "receipt", unacknowledged)
    restarted = RemoteWorker(client, bridge_command=device.remote_worker.execution.command)
    try:
        restarted.start_project(project["id"])
        assert json.loads(active_path.read_text())[task["id"]] == task["run_id"]
        assert owner.store.get_task(task["id"])["status"] == "starting"
        assert "recovery" in restarted.errors
    finally:
        restarted.close()


def test_stopped_worker_cannot_execute_a_late_claim_response(remote, monkeypatch):
    owner, device, project, employee, _ = remote
    device.remote_worker.close()
    task = owner.store.create_task(project["id"], "Late claim", "complete", employee["id"])
    client = device.remote_client
    claimed, release, executed = threading.Event(), threading.Event(), threading.Event()
    real_claim = client.claim

    def delayed_claim(project_id, wait=30):
        result = real_claim(project_id, wait=0)
        claimed.set()
        assert release.wait(timeout=3)
        return result

    monkeypatch.setattr(client, "claim", delayed_claim)
    worker = RemoteWorker(client, bridge_command=device.remote_worker.execution.command)
    monkeypatch.setattr(worker.execution, "run", lambda *args, **kwargs: executed.set())
    try:
        worker.start_project(project["id"])
        assert claimed.wait(timeout=2)
        worker.close()
        release.set()
        worker.threads[0].join(timeout=2)
        assert not worker.threads[0].is_alive()
        assert not executed.is_set()
        assert owner.store.get_task(task["id"])["status"] == "interrupted"
    finally:
        release.set()
        worker.close()


def test_remote_restore_reconnects_when_coordinator_starts_later(remote):
    owner, device, project, _, _ = remote
    coordinator = owner.fleet
    port = coordinator.server.server_port
    credentials = dict(device.remote_client.credentials)
    coordinator.stop()
    device.shutdown()
    device.close()
    restored = WorkbenchHTTP(device.store, token="restored-device")
    thread = threading.Thread(target=restored.serve_forever, daemon=True)
    thread.start()
    try:
        # Restoration performs a real FleetClient request with a 10-second
        # connection budget. Winsock need not refuse a closed port immediately;
        # wait for that bounded attempt, not the unrelated 2-second stop budget.
        deadline = time.monotonic() + 12
        while restored.fleet_error is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert restored.fleet_error is not None, (
            "Offline coordinator did not report restore failure"
        )
        assert restored.fleet_error["code"] == "DEVICE_RESTORE_FAILED"
        coordinator.start(port=port)
        deadline = time.monotonic() + 7
        while restored.remote_client is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert restored.remote_client is not None
        assert restored.remote_client.credentials == credentials
        assert project["id"] in restored.remote_client.mappings
    finally:
        restored.shutdown()
        restored.close()
        thread.join(timeout=2)


def test_orphan_starting_run_without_device_journal_is_interrupted_not_replayed(
    remote, monkeypatch
):
    owner, device, project, employee, _ = remote
    device.remote_worker.close()
    task = owner.store.create_task(project["id"], "Lost claim", "complete", employee["id"])
    client = device.remote_client
    # Simulate a coordinator committed claim whose response was not journaled.
    assert client.claim(project["id"], wait=0)["id"] == task["id"]
    active_path = client.directory / "active-runs.json"
    active_path.unlink(missing_ok=True)
    executed = threading.Event()
    recovered = RemoteWorker(client, bridge_command=device.remote_worker.execution.command)
    monkeypatch.setattr(recovered.execution, "run", lambda *args, **kwargs: executed.set())
    try:
        recovered.start_project(project["id"])
        assert owner.store.get_task(task["id"])["status"] == "interrupted"
        assert not executed.is_set()
        assert client.active_runs() == []
    finally:
        recovered.close()


@pytest.mark.parametrize("committed", [False, True])
def test_durable_terminal_outbox_survives_lost_reply_and_restart_without_execution(
    remote, monkeypatch, committed
):
    owner, device, project, _, _ = remote
    worker = device.remote_worker
    original_receipt = device.remote_client.receipt
    original_execution = worker.execution.run
    executed = []
    lost = threading.Event()

    def execution(*args, **kwargs):
        executed.append(args[0]["id"])
        return original_execution(*args, **kwargs)

    def disconnected(*args, **kwargs):
        if committed:
            original_receipt(*args, **kwargs)
        lost.set()
        raise WorkbenchError("network_error", "Final reply was lost")

    monkeypatch.setattr(worker.execution, "run", execution)
    monkeypatch.setattr(device.remote_client, "receipt", disconnected)
    task = dispatch(remote, "complete")
    assert lost.wait(8)
    worker.close()
    pending = json.loads(worker.outbox_path.read_text())
    assert pending[task["id"]]["status"] == "review"
    assert "Mapped directory:" in pending[task["id"]]["result"]
    assert executed == [task["id"]]
    assert owner.store.get_task(task["id"])["status"] == ("review" if committed else "running")
    if committed:
        owner.store.review_task(task["id"], "accept")
    monkeypatch.setattr(device.remote_client, "receipt", original_receipt)
    restarted = RemoteWorker(device.remote_client, bridge_command=worker.execution.command)
    monkeypatch.setattr(restarted.execution, "run", lambda *a, **k: pytest.fail("Model replayed"))
    try:
        restarted.start_project(project["id"])
        expected = "done" if committed else "review"
        assert owner.store.get_task(task["id"])["status"] == expected
        assert json.loads(restarted.outbox_path.read_text()) == {}
        assert json.loads(restarted.active_path.read_text()) == {}
        assert executed == [task["id"]]
    finally:
        restarted.close()


def test_final_receipt_retries_on_live_reconnect_without_model_replay(remote, monkeypatch):
    owner, device, _, _, _ = remote
    worker = device.remote_worker
    real_receipt = device.remote_client.receipt
    real_execution = worker.execution.run
    offline = threading.Event()
    offline.set()
    lost = threading.Event()
    executed = []

    def receipt(*args, **kwargs):
        if offline.is_set():
            lost.set()
            raise WorkbenchError("network_error", "Offline fixture")
        return real_receipt(*args, **kwargs)

    def execute(*args, **kwargs):
        executed.append(args[0]["id"])
        return real_execution(*args, **kwargs)

    monkeypatch.setattr(device.remote_client, "receipt", receipt)
    monkeypatch.setattr(worker.execution, "run", execute)
    task = dispatch(remote, "complete")
    assert lost.wait(8)
    assert json.loads(worker.outbox_path.read_text())[task["id"]]["status"] == "review"
    offline.clear()
    worker.receipt_ready.set()
    delivered = await_task(owner.store, task["id"], "review", timeout=12)
    assert "Mapped directory:" in delivered["result"]
    assert executed == [task["id"]]


@pytest.mark.parametrize("failed_path", ["active-runs.json", "terminal-receipts.json"])
def test_acknowledged_output_survives_local_cleanup_write_failure(remote, monkeypatch, failed_path):
    owner, device, _, _, _ = remote
    worker = device.remote_worker
    real_write = __import__(
        "agent_mailbox.workbench_fleet", fromlist=["_write_private"]
    )._write_private
    fail_cleanup = threading.Event()
    fail_cleanup.set()
    failed = threading.Event()
    executions = []
    real_execution = worker.execution.run

    def write(path, data):
        if path.name == failed_path and json.loads(data) == {} and fail_cleanup.is_set():
            failed.set()
            raise WorkbenchError("storage_error", "Cleanup disk failure fixture")
        return real_write(path, data)

    def execution(*args, **kwargs):
        executions.append(args[0]["id"])
        return real_execution(*args, **kwargs)

    monkeypatch.setattr("agent_mailbox.workbench_remote._write_private", write)
    monkeypatch.setattr(worker.execution, "run", execution)
    task = dispatch(remote, "complete")
    assert failed.wait(8)
    result = await_task(owner.store, task["id"], "review")
    assert "Mapped directory:" in result["result"]
    # Receipt retry and cleanup replace this journal under the same lock.
    # Windows may deny reads racing the replacement; inspect a coherent state.
    with worker.lock:
        assert worker.outbox[task["id"]]["status"] == "review"
        assert worker.outbox[task["id"]]["result"] == result["result"]
        assert json.loads(worker.outbox_path.read_text())[task["id"]]["status"] == "review"
    owner.store.review_task(task["id"], "accept")
    fail_cleanup.clear()
    worker._flush_receipts()
    with worker.lock:
        assert worker.outbox == {}
        assert json.loads(worker.outbox_path.read_text()) == {}
    assert owner.store.get_task(task["id"])["status"] == "done"
    assert executions == [task["id"]]
