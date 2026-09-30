"""Verified HTTPS identity reports and update claim safety, without model calls."""

import json

import pytest

from agent_mailbox import __version__
from agent_mailbox.workbench_compatibility import compatibility_nodes, compatibility_status
from agent_mailbox.workbench_fleet import FleetClient, FleetCoordinator
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


@pytest.fixture
def setup(tmp_path):
    owner = WorkbenchStore(tmp_path / "owner")
    project = owner.create_project("Shared", tmp_path)
    coordinator = FleetCoordinator(owner)
    coordinator.start()
    try:
        client = FleetClient(tmp_path / "remote", coordinator.issue_invite([project["id"]]))
        client.map_project(project["id"], tmp_path)
        employee = client.register_employee(project["id"], "Worker", "codex")
        task = owner.create_task(project["id"], "Task", "Report", employee["id"])
        yield owner, project, coordinator, client, task
    finally:
        coordinator.stop()


def test_authenticated_version_report_is_private_and_legacy_unknown(setup):
    owner, _, coordinator, client, _ = setup
    nodes = compatibility_nodes(owner, coordinator)
    remote = next(n for n in nodes if not n["is_local"])
    assert remote["status"] == "matched"
    assert remote["version"] == __version__ and remote["protocol"] == "1"
    assert compatibility_nodes(owner) == nodes
    assert "token" not in json.dumps(nodes)
    device_id = client.credentials["device_id"]
    # Only authenticated identity may replace its self-report.
    with pytest.raises(WorkbenchError):
        coordinator._authenticate(
            {
                "X-Device-ID": device_id,
                "Authorization": "Bearer wrong",
                "X-Agent-Mail-Protocol": "2",
            }
        )
    assert coordinator.public_devices(include_reports=True)[0]["protocol"] == "1"
    coordinator.state["devices"][device_id].pop("protocol")
    coordinator.state["devices"][device_id].pop("version")
    assert (
        next(n for n in compatibility_nodes(owner, coordinator) if not n["is_local"])["status"]
        == "unknown"
    )


@pytest.mark.parametrize(
    ("version", "protocol", "expected"),
    [
        (None, None, "unknown"),
        ("0.7.6", "1", "compatible"),
        ("0.8.0b1", "2", "incompatible"),
        (None, "invalid", "incompatible"),
    ],
)
def test_compatibility_does_not_require_equal_versions(version, protocol, expected):
    assert compatibility_status(version, protocol) == expected


def test_incompatible_claim_rejected_but_existing_receipt_allowed(setup):
    owner, project, coordinator, client, task = setup
    claimed = client.claim(project["id"], wait=0)
    assert claimed["id"] == task["id"]
    client.event(task["id"], task["run_id"], "started")
    headers = {
        "Authorization": "Bearer " + client.credentials["token"],
        "X-Device-ID": client.credentials["device_id"],
        "X-Agent-Mail-Version": __version__,
        "X-Agent-Mail-Protocol": "2",
    }
    with pytest.raises(WorkbenchError, match="协议不兼容"):
        coordinator._route(
            "POST", "/v1/tasks/claim", headers, {"project_id": project["id"], "wait": 0}
        )
    result = coordinator._route(
        "POST",
        f"/v1/tasks/{task['id']}/receipt",
        headers,
        {"run_id": task["run_id"], "status": "review", "result": "Delivered"},
    )
    assert result["task"]["status"] == "review"
    assert owner.get_task(task["id"])["result"] == "Delivered"


def test_local_pause_no_network_then_resume_preserves_queue(setup, monkeypatch):
    owner, project, _, client, task = setup
    client.store.pause_updates(True)
    original = client._request
    monkeypatch.setattr(
        client, "_request", lambda *a, **k: pytest.fail("paused claim accessed network")
    )
    assert client.claim(project["id"], wait=0) is None
    assert owner.get_task(task["id"])["status"] == "queued"
    monkeypatch.setattr(client, "_request", original)
    client.store.pause_updates(False)
    assert client.claim(project["id"], wait=0)["id"] == task["id"]
    assert (
        json.loads((client.directory / "active-runs.json").read_text())[task["id"]]
        == task["run_id"]
    )
    assert not client.store.update_maintenance()["pending_claims"]


def test_lost_claim_response_marker_survives_restart_until_confirmed(setup, monkeypatch):
    owner, project, _, client, task = setup
    original = client._request

    def lose(method, path, *args, **kwargs):
        reply = original(method, path, *args, **kwargs)
        if path == "/v1/tasks/claim":
            raise WorkbenchError("network_error", "Reply lost")
        return reply

    monkeypatch.setattr(client, "_request", lose)
    with pytest.raises(WorkbenchError):
        client.claim(project["id"], wait=0)
    assert owner.get_task(task["id"])["status"] == "starting"
    assert client.store.update_maintenance()["pending_claims"]
    restarted = FleetClient(client.store.root, client.credentials)
    assert restarted.store.update_maintenance()["pending_claims"]
    assert restarted.active_runs()[0]["id"] == task["id"]
    assert not restarted.store.update_maintenance()["pending_claims"]
    assert (
        json.loads((restarted.directory / "active-runs.json").read_text())[task["id"]]
        == task["run_id"]
    )
    assert restarted.receipt(task["id"], task["run_id"], "interrupted")["status"] == "interrupted"


def test_unknown_legacy_protocol_still_claims_but_future_coordinator_does_not(setup, monkeypatch):
    owner, project, _, client, task = setup
    original = client.device
    monkeypatch.setattr(
        client, "device", lambda: {"coordinator": {"version": "9.0.0", "protocol": "2"}}
    )
    with pytest.raises(WorkbenchError, match="主控协议不兼容"):
        client.claim(project["id"], wait=0)
    assert owner.get_task(task["id"])["status"] == "queued"
    assert not client.store.update_maintenance()["pending_claims"]
    monkeypatch.setattr(client, "device", lambda: {"coordinator": {}})
    assert client.claim(project["id"], wait=0)["id"] == task["id"]
    monkeypatch.setattr(client, "device", original)


def test_pause_during_inflight_claim_retains_blocker_and_received_run(setup, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    _, project, _, client, task = setup
    received, release = threading.Event(), threading.Event()
    original = client._request

    def delayed(method, path, *args, **kwargs):
        reply = original(method, path, *args, **kwargs)
        if path == "/v1/tasks/claim":
            received.set()
            assert release.wait(5)
        return reply

    monkeypatch.setattr(client, "_request", delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.claim, project["id"], wait=0)
        try:
            assert received.wait(5)
            client.store.pause_updates(True)
            assert client.store.update_maintenance()["pending_claims"]
        finally:
            release.set()
        assert future.result()["id"] == task["id"]
    assert client.store.update_maintenance()["paused"]
    assert not client.store.update_maintenance()["pending_claims"]
    assert (
        json.loads((client.directory / "active-runs.json").read_text())[task["id"]]
        == task["run_id"]
    )
    assert client.claim(project["id"], wait=0) is None


def test_standalone_ack_clears_exact_journal_run(setup):
    _, project, _, client, task = setup
    client.claim(project["id"], wait=0)
    path = client.directory / "active-runs.json"
    assert task["id"] in json.loads(path.read_text())
    client.receipt(task["id"], task["run_id"], "interrupted")
    assert task["id"] not in json.loads(path.read_text())
    # Repeated acknowledgement must not erase a newer execution journal.
    path.write_text(json.dumps({task["id"]: "new-run"}))
    client.receipt(task["id"], task["run_id"], "interrupted")
    assert json.loads(path.read_text())[task["id"]] == "new-run"


def test_pair_pause_rejects_new_identity_without_consuming_invite(setup, tmp_path):
    owner, project, coordinator, client, _ = setup
    invite = coordinator.issue_invite([project["id"]])
    owner.pause_updates(True)
    before = len(coordinator.public_devices())
    with pytest.raises(WorkbenchError) as failure:
        FleetClient(tmp_path / "new-node", invite)
    assert failure.value.code == "UPDATE_PAUSED"
    assert len(coordinator.public_devices()) == before
    assert not coordinator.state["invites"][invite["invite_id"]]["used"]
    owner.pause_updates(False)
    assert FleetClient(tmp_path / "new-node", invite).projects()
    assert client.device()["device"]["device_id"] == client.credentials["device_id"]


def test_paused_worker_reconciles_uncertain_claim_without_new_claim(setup, monkeypatch):
    from agent_mailbox.workbench_remote import RemoteWorker

    _, project, _, client, _ = setup
    client.store.begin_update_claim("lost-before-prepare")
    client.store.pause_updates(True)
    worker = RemoteWorker(client)
    original = client.active_runs
    calls = []

    def recover():
        result = original()
        calls.append(result)
        worker.stopping.set()
        return result

    monkeypatch.setattr(client, "active_runs", recover)
    worker._loop(project["id"])
    assert calls == [[]]
    assert not client.store.update_maintenance()["pending_claims"]
    assert client.store.update_maintenance()["paused"]
