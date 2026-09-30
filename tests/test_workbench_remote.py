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
import asyncio,json,os,sys
request=json.loads(sys.stdin.readline())
def send(kind, **fields):
    print(json.dumps(dict(protocol=1,run_id=request['run_id'],
                         session_id=request['session_id'],type=kind,**fields)),flush=True)
send('started')
if request['prompt']=='wait':
    operation=json.loads(sys.stdin.readline())
    send('result',status='cancelled' if operation['op']=='cancel' else 'failed')
    sys.exit(0)
if request['prompt']=='permission':
    send('permission_required',request_id='write',
         options=[{'optionId':'once','name':'Allow once','kind':'allow_once'}],
         tool_call={'title':'Write a file'})
    answer=json.loads(sys.stdin.readline())
    if answer.get('decision')!='deny':
        send('result',status='failed',error={'code':'UNEXPECTED_APPROVAL','message':'Must deny'})
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


def test_remote_permission_requests_default_to_deny_without_human_approval(remote):
    owner, _, _, _, _ = remote
    task = dispatch(remote, "permission")
    result = await_task(owner.store, task["id"], "failed")
    assert result["error"]["code"] == "REMOTE_PERMISSION_DENIED"
    detail = owner.store.task_detail(task["id"])
    assert detail["permissions"] == []
    assert any(event["type"] == "permission_denied" for event in detail["events"])


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
        deadline = time.monotonic() + 2
        while restored.fleet_error is None and time.monotonic() < deadline:
            time.sleep(0.02)
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
