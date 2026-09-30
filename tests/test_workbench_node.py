"""Headless nodes use isolated homes, loopback TLS and owned fake children only."""

import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from agent_mailbox.workbench import WorkbenchHTTP
from agent_mailbox.workbench_engine import WorkbenchEngine
from agent_mailbox.workbench_fleet import FleetCoordinator
from agent_mailbox.workbench_node import _client, main, run_node
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


def test_product_cli_lazily_dispatches_headless_node_and_propagates_failure(monkeypatch):
    from agent_mailbox.cli import main as product_main

    calls = []
    monkeypatch.setattr("agent_mailbox.workbench_node.main", lambda args: calls.append(args) or 1)
    with pytest.raises(SystemExit) as exc:
        product_main(["node", "join", "--invite", "private.json"])
    assert exc.value.code == 1
    assert calls == [["join", "--invite", "private.json"]]


@pytest.fixture
def node(tmp_path):
    owner = WorkbenchStore(tmp_path / "owner")
    project = owner.create_project("Shared", tmp_path)
    private = owner.create_project("Private", tmp_path)
    coordinator = FleetCoordinator(owner)
    coordinator.start()
    home = tmp_path / "node"
    invitation = tmp_path / "invite.json"
    invitation.write_text(json.dumps(coordinator.issue_invite([project["id"]])))
    invitation.chmod(0o600)
    try:
        yield owner, coordinator, home, project, private, invitation
    finally:
        coordinator.stop()


def provision(node):
    _, _, home, project, _, invitation = node
    assert main(["--home", str(home), "join", "--invite", str(invitation)]) == 0
    assert main(["map", "--home", str(home), "--project", project["id"], "--path", str(home)]) == 0
    assert (
        main(
            [
                "--home",
                str(home),
                "employee",
                "--project",
                project["id"],
                "--name",
                "Node employee",
                "--kind",
                "codex",
            ]
        )
        == 0
    )
    return _client(home)


def await_status(store, task_id, expected, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = store.get_task(task_id)
        if task["status"] == expected:
            return task
        time.sleep(0.02)
    pytest.fail(f"Expected {expected}; got {task['status']}")


def test_private_join_explicit_mapping_registration_and_saved_resume(node, capsys):
    owner, _, home, project, private, invitation = node
    client = provision(node)
    credentials = dict(client.credentials)
    output = capsys.readouterr().out
    assert credentials["token"] not in output
    assert json.loads(invitation.read_text())["invite_secret"] not in output
    assert private["id"] not in output
    invitation.unlink()  # Saved credentials, not the consumed invitation, drive restarts.
    resumed = _client(home)
    assert resumed.credentials == credentials
    assert resumed.local_project(project["id"])["path"] == str(home)
    employee = owner.snapshot()["employees"][0]
    assert employee["node_id"] == credentials["device_id"]
    assert employee["project_ids"] == [project["id"]]
    assert client.credentials_path.stat().st_mode & 0o777 == 0o600
    assert main(["--home", str(home), "map", "--project", private["id"], "--path", str(home)]) == 1
    assert private["id"] not in _client(home).mappings


def test_actual_product_module_cli_join_and_failure_exit_code(node):
    _, _, home, project, private, invitation = node
    command = [sys.executable, "-m", "agent_mailbox", "node"]
    joined = subprocess.run(
        command + ["join", "--home", str(home), "--invite", str(invitation)],
        capture_output=True,
        text=True,
        timeout=12,
        check=False,
    )
    assert joined.returncode == 0, joined.stderr
    assert json.loads(joined.stdout)["projects"] == [{"id": project["id"], "name": "Shared"}]
    token = _client(home).credentials["token"]
    assert token not in joined.stdout + joined.stderr
    rejected = subprocess.run(
        command + ["map", "--home", str(home), "--project", private["id"], "--path", str(home)],
        capture_output=True,
        text=True,
        timeout=12,
        check=False,
    )
    assert rejected.returncode == 1
    assert json.loads(rejected.stderr)["error"]["code"] == "permission_denied"


@pytest.mark.skipif(os.name == "nt", reason="POSIX private invitation mode")
def test_public_invitation_file_rejected_without_pairing(node, capsys):
    _, coordinator, home, _, _, invitation = node
    invitation.chmod(0o644)
    assert main(["--home", str(home), "join", "--invite", str(invitation)]) == 1
    assert "private_file_required" in capsys.readouterr().err
    assert coordinator.public_devices() == []


def test_ssh_dial_override_preserves_pin_scope_and_mcp_saved_address(node):
    _, coordinator, home, project, _, invitation = node
    invite = json.loads(invitation.read_text())
    invite["base_url"] = "https://unreachable.example:8443"
    invitation.write_text(json.dumps(invite))
    assert (
        main(
            [
                "--home",
                str(home),
                "join",
                "--invite",
                str(invitation),
                "--coordinator-url",
                coordinator.base_url,
            ]
        )
        == 0
    )
    saved = json.loads((home / "workbench/fleet/client.json").read_text())
    assert saved["base_url"] == coordinator.base_url
    assert saved["fingerprint"] == invite["fingerprint"]
    assert saved["project_ids"] == [project["id"]]
    assert _client(home).projects() == [{"id": project["id"], "name": "Shared"}]


def test_url_change_verified_before_credentials_are_replaced(node):
    _, coordinator, home, _, _, _ = node
    client = provision(node)
    before = client.credentials_path.read_bytes()
    foreign = FleetCoordinator(WorkbenchStore(home.parent / "foreign"))
    foreign.start()
    try:
        with pytest.raises(WorkbenchError, match="TLS"):
            _client(home, coordinator_url=foreign.base_url)
        assert client.credentials_path.read_bytes() == before
    finally:
        foreign.stop()
    # A second dial spelling that reaches the same pinned owner can be saved.
    dial = coordinator.base_url.replace("127.0.0.1", "localhost")
    migrated = _client(home, coordinator_url=dial)
    assert migrated.credentials["base_url"] == dial
    assert _client(home).credentials["base_url"] == dial


def test_headless_commands_respect_existing_same_home_owner(node, capsys):
    _, _, home, project, _, _ = node
    provision(node)
    local = WorkbenchHTTP(WorkbenchStore(home), engine=WorkbenchEngine(WorkbenchStore(home)))
    try:
        assert (
            main(
                [
                    "--home",
                    str(home),
                    "employee",
                    "--project",
                    project["id"],
                    "--name",
                    "Duplicate",
                    "--kind",
                    "codex",
                ]
            )
            == 1
        )
        assert "ALREADY_RUNNING" in capsys.readouterr().err
    finally:
        local.close()


def test_run_requires_explicit_authorized_mapping(node):
    _, _, home, project, private, invitation = node
    assert main(["--home", str(home), "join", "--invite", str(invitation)]) == 0
    client = _client(home)
    with pytest.raises(WorkbenchError, match="Map"):
        run_node(client)
    with pytest.raises(WorkbenchError):
        run_node(client, [project["id"]])
    client.mappings[private["id"]] = str(home)
    with pytest.raises(WorkbenchError, match="authorized"):
        run_node(client)


FAKE = r"""
import asyncio,json,sys,pathlib,os
r=json.loads(sys.stdin.readline())
def send(kind,**fields):
 print(json.dumps(dict(protocol=1,run_id=r['run_id'],session_id=r['session_id'],type=kind,**fields)),flush=True)
send('started')
pathlib.Path(r['cwd'],'child-started').write_text(str(__import__('os').getpid()))
if r['prompt']=='wait':
 json.loads(sys.stdin.readline())
 send('result',status='cancelled')
elif r['prompt']=='context':
 from mcp import ClientSession,StdioServerParameters
 from mcp.client.stdio import stdio_client
 server=r['mcp_servers'][0]
 async def context():
  env={**os.environ,**{item['name']:item['value'] for item in server['env']}}
  async with stdio_client(StdioServerParameters(command=server['command'],args=server['args'],env=env)) as (reader,writer),ClientSession(reader,writer) as session:
   await session.initialize()
   value=await session.call_tool('project_context',{})
   if value.is_error: raise RuntimeError('Scoped context failed')
   return json.dumps(value.model_dump())
 send('result',status='completed',output_text=asyncio.run(context()))
else:
 send('result',status='completed',output_text='Headless mapped directory: '+r['cwd'])
sys.stdin.readline()
"""


def test_node_ssh_dial_configuration_is_used_by_actual_scoped_mcp_child(node):
    owner, coordinator, home, project, private, invitation = node
    invite = json.loads(invitation.read_text())
    invite["base_url"] = "https://not-contacted.example:8443"
    invitation.write_text(json.dumps(invite))
    assert (
        main(
            [
                "--home",
                str(home),
                "join",
                "--invite",
                str(invitation),
                "--coordinator-url",
                coordinator.base_url,
            ]
        )
        == 0
    )
    assert main(["--home", str(home), "map", "--project", project["id"], "--path", str(home)]) == 0
    assert (
        main(
            [
                "--home",
                str(home),
                "employee",
                "--project",
                project["id"],
                "--name",
                "Node",
                "--kind",
                "codex",
            ]
        )
        == 0
    )
    owner.add_memory(project["id"], "Shared", "headless-shared-marker")
    owner.add_memory(private["id"], "Secret", "private-never-shared-marker")
    bridge = home.parent / "fake.py"
    bridge.write_text(FAKE)
    client = _client(home)
    stopping = threading.Event()
    thread = threading.Thread(
        target=run_node,
        args=(client,),
        kwargs={"stopping": stopping, "bridge_command": [sys.executable, str(bridge)]},
    )
    thread.start()
    try:
        employee = owner.snapshot()["employees"][0]
        task = owner.create_task(project["id"], "Context", "context", employee["id"])
        coordinator.notify()
        result = await_status(owner, task["id"], "review")
        assert "headless-shared-marker" in result["result"]
        assert "private-never-shared-marker" not in result["result"]
        assert client.credentials["token"] not in result["result"]
    finally:
        stopping.set()
        thread.join(timeout=10)


def test_real_https_headless_worker_result_and_restart_without_replay(node):
    owner, coordinator, home, project, _, _ = node
    client = provision(node)
    employee = owner.snapshot()["employees"][0]
    bridge = home.parent / "fake.py"
    bridge.write_text(FAKE)
    stopping = threading.Event()
    thread = threading.Thread(
        target=run_node,
        args=(client,),
        kwargs={"stopping": stopping, "bridge_command": [sys.executable, str(bridge)]},
    )
    thread.start()
    try:
        task = owner.create_task(project["id"], "Report", "complete", employee["id"])
        coordinator.notify()
        result = await_status(owner, task["id"], "review")
        assert result["result"] == "Headless mapped directory: " + str(home)
        owner.review_task(task["id"], "accept")
    finally:
        stopping.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    # Simulate committed delivery without a journal before this node restarts.
    orphan = owner.create_task(project["id"], "Orphan", "complete", employee["id"])
    assert client.claim(project["id"], wait=0)["id"] == orphan["id"]
    marker = home / "child-started"
    marker.unlink()
    stopping = threading.Event()
    thread = threading.Thread(
        target=run_node,
        args=(_client(home),),
        kwargs={"stopping": stopping, "bridge_command": [sys.executable, str(bridge)]},
    )
    thread.start()
    try:
        await_status(owner, orphan["id"], "interrupted")
        assert not marker.exists()
    finally:
        stopping.set()
        thread.join(timeout=10)


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM process-group cleanup")
def test_node_run_sigterm_stops_owned_child_and_releases_home_lock(node):
    owner, coordinator, home, project, _, _ = node
    provision(node)
    employee = owner.snapshot()["employees"][0]
    bridge = home.parent / "fake.py"
    bridge.write_text(FAKE)
    shim = home.parent / "node_process.py"
    shim.write_text(
        "import sys\nfrom unittest.mock import patch\n"
        "from agent_mailbox.workbench_remote import RemoteWorker\n"
        "from agent_mailbox.workbench_node import main\n"
        f"command={[sys.executable, str(bridge)]!r}\n"
        "def worker(client, bridge_command=None):\n return RemoteWorker(client,bridge_command=command)\n"
        "with patch('agent_mailbox.workbench_node.RemoteWorker',worker):\n"
        " raise SystemExit(main(sys.argv[1:]))\n"
    )
    process = subprocess.Popen(
        [sys.executable, str(shim), "--home", str(home), "run"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        task = owner.create_task(project["id"], "Wait", "wait", employee["id"])
        coordinator.notify()
        await_status(owner, task["id"], "running")
        marker = home / "child-started"
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        child_pid = int(marker.read_text())
        assert (
            main(["--home", str(home), "map", "--project", project["id"], "--path", str(home)]) == 1
        )
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=12)
        assert process.returncode == 0, stderr
        assert "token" not in stdout
        await_status(owner, task["id"], "cancelled")
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        assert (
            main(["--home", str(home), "map", "--project", project["id"], "--path", str(home)]) == 0
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)


@pytest.mark.parametrize(
    "address", ["127.0.0.2", "10.1.2.3", "172.16.1.2", "192.168.1.2", "100.100.1.2"]
)
def test_optional_listener_binds_only_selected_private_interface(tmp_path, monkeypatch, address):
    calls = []

    class Coordinator:
        def __init__(self, store):
            self.server = type("Server", (), {"server_port": 8443})()

        def start(self, **kwargs):
            calls.append(kwargs)
            return kwargs

        def stop(self):
            pass

    monkeypatch.setattr("agent_mailbox.workbench_fleet.FleetCoordinator", Coordinator)
    server = WorkbenchHTTP(WorkbenchStore(tmp_path / "home"))
    try:
        server.start_fleet(address, port=8443)
        assert calls == [{"host": address, "port": 8443, "advertised_host": address}]
    finally:
        server.close()


@pytest.mark.parametrize(
    "address",
    ["0.0.0.0", "8.8.8.8", "203.0.113.2", "169.254.1.2", "::1", "100.128.0.1", "host.example"],
)
def test_listener_rejects_public_unspecified_and_unapproved_address(tmp_path, address):
    server = WorkbenchHTTP(WorkbenchStore(tmp_path / "home"))
    try:
        with pytest.raises(WorkbenchError):
            server.start_fleet(address)
        assert server.fleet is None
    finally:
        server.close()
