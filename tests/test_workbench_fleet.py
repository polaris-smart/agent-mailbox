"""Two isolated data roots communicate over real pinned HTTPS on loopback."""

import json
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_mailbox.workbench_fleet import FleetClient, FleetCoordinator
from agent_mailbox.workbench_private import private_access
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


def test_pair_response_loss_recovers_same_home_proof_without_plaintext_server_secret(
    fleet, tmp_path, monkeypatch
):
    _, project, _, coordinator = fleet
    invite = coordinator.issue_invite([project["id"]])
    root = tmp_path / "recover"
    original = FleetClient._request
    lost = []

    def lose_once(self, method, path, *args, **kwargs):
        reply = original(self, method, path, *args, **kwargs)
        if path == "/v1/pair" and not lost:
            lost.append(reply)
            raise WorkbenchError("network_error", "Pair reply was lost after commit")
        return reply

    monkeypatch.setattr(FleetClient, "_request", lose_once)
    with pytest.raises(WorkbenchError):
        FleetClient(root, invite)
    attempt_path = root / "workbench/fleet/pair-attempt.json"
    attempt = json.loads(attempt_path.read_text())
    assert private_access(attempt_path, 0o600)
    assert not (root / "workbench/fleet/client.json").exists()
    assert coordinator.state["invites"][invite["invite_id"]]["used"]
    coordinator.state["invites"][invite["invite_id"]]["expires_at"] = 0
    client = FleetClient(
        root, {**invite, "base_url": invite["base_url"].replace("127.0.0.1", "localhost")}
    )
    assert client.credentials["token"] == lost[0]["token"] == attempt["pair_token"]
    assert client.credentials["device_id"] == lost[0]["device_id"]
    assert client.projects() == [{"id": project["id"], "name": "Shared"}]
    assert not attempt_path.exists()
    assert attempt["pair_token"] not in coordinator.state_path.read_text()


def test_pair_proof_survives_client_credential_save_failure(fleet, tmp_path, monkeypatch):
    import agent_mailbox.workbench_fleet as module

    _, project, _, coordinator = fleet
    invite = coordinator.issue_invite([project["id"]])
    root = tmp_path / "disk-failure"
    original = module._write_private

    def disk_failure(path, data):
        if path.name == "client.json":
            raise WorkbenchError("storage_error", "Client credential save failed")
        return original(path, data)

    monkeypatch.setattr(module, "_write_private", disk_failure)
    with pytest.raises(WorkbenchError):
        FleetClient(root, invite)
    attempt_path = root / "workbench/fleet/pair-attempt.json"
    proof = json.loads(attempt_path.read_text())["pair_token"]
    monkeypatch.setattr(module, "_write_private", original)
    recovered = FleetClient(root, invite)
    assert recovered.credentials["token"] == proof
    assert not attempt_path.exists()


def test_pair_recovery_rejects_wrong_proof_device_and_revocation(fleet, tmp_path):
    _, project, _, coordinator = fleet
    invite = coordinator.issue_invite([project["id"]])
    client = FleetClient(tmp_path / "device", invite)
    body = {
        "invite_id": invite["invite_id"],
        "invite_secret": invite["invite_secret"],
        "device_id": client.credentials["device_id"],
        "name": "Retry",
        "pair_token": client.credentials["token"],
    }
    for changes in ({"pair_token": secrets.token_urlsafe(32)}, {"device_id": "other-node"}):
        with pytest.raises(WorkbenchError):
            client._request("POST", "/v1/pair", {**body, **changes}, authenticated=False)
    assert client.device()["device"]["device_id"] == body["device_id"]
    coordinator.revoke_device(body["device_id"])
    with pytest.raises(WorkbenchError):
        client._request("POST", "/v1/pair", body, authenticated=False)


@pytest.mark.parametrize("proof", ["short", "A" * 42, "A" * 44, "x" * 43, "!" * 43, 123])
def test_pair_rejects_noncanonical_256_bit_proof(fleet, tmp_path, proof):
    _, project, _, coordinator = fleet
    invite = coordinator.issue_invite([project["id"]])
    client = paired(fleet, tmp_path)
    with pytest.raises(WorkbenchError):
        client._request(
            "POST",
            "/v1/pair",
            {
                "invite_id": invite["invite_id"],
                "invite_secret": invite["invite_secret"],
                "device_id": "new-node",
                "name": "Invalid",
                "pair_token": proof,
            },
            authenticated=False,
        )
    assert not coordinator.state["invites"][invite["invite_id"]]["used"]


PERMISSION_OPTIONS = [{"optionId": "once", "name": "Allow once", "kind": "allow_once"}]


def awaiting_permission(fleet, tmp_path):
    owner, project, _, coordinator = fleet
    client = mapped(fleet, tmp_path)
    employee = client.register_employee(project["id"], "Remote", "codex")
    task = owner.create_task(project["id"], "Write", "Report", employee["id"])
    client.claim(project["id"], wait=0)
    client.event(task["id"], task["run_id"], "started")
    reply = client.request_permission(
        task["id"],
        task["run_id"],
        employee["id"],
        "write",
        PERMISSION_OPTIONS,
        {"title": "Write file", "rawInput": {"token": "secret"}},
    )
    assert reply["status"] == "pending"
    return owner, coordinator, client, task, employee


@pytest.mark.parametrize("decision", ["allow_once", "deny"])
def test_device_permission_wait_is_event_driven_and_human_only(
    fleet, tmp_path, monkeypatch, decision
):
    owner, coordinator, client, task, employee = awaiting_permission(fleet, tmp_path)
    calls = []
    real = owner.permission_decision

    def observed(*args):
        calls.append(time.monotonic())
        return real(*args)

    monkeypatch.setattr(owner, "permission_decision", observed)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(
            client.permission_decision, task["id"], task["run_id"], employee["id"], "write", 5
        )
        time.sleep(0.2)
        assert not future.done() and len(calls) == 1
        owner.resolve_permission(task["id"], "write", decision)
        coordinator.notify()
        assert future.result(timeout=1) == {"status": "resolved", "decision": decision}
    assert len(calls) == 2
    with pytest.raises(WorkbenchError):
        client._request(
            "POST", f"/v1/tasks/{task['id']}/permissions/write/resolve", {"decision": "allow_once"}
        )


@pytest.mark.parametrize("stop", ["cancel", "revoke"])
def test_permission_wait_cancel_or_revoke_defaults_deny(fleet, tmp_path, stop):
    owner, coordinator, client, task, employee = awaiting_permission(fleet, tmp_path)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(
            client.permission_decision, task["id"], task["run_id"], employee["id"], "write", 5
        )
        time.sleep(0.1)
        if stop == "cancel":
            owner.cancel_task(task["id"])
            coordinator.notify()
            assert future.result(timeout=1)["decision"] == "deny"
        else:
            coordinator.revoke_device(client.credentials["device_id"])
            with pytest.raises(WorkbenchError):
                future.result(timeout=1)
    assert owner.get_permission(task["id"], "write")["status"] == "expired"
    with pytest.raises(WorkbenchError):
        owner.resolve_permission(task["id"], "write", "allow_once")


def test_permission_endpoint_binds_device_project_employee_run_and_request(fleet, tmp_path):
    owner, _, client, task, employee = awaiting_permission(fleet, tmp_path)
    peer = mapped(fleet, tmp_path, "peer")
    for wrong_client, run_id, employee_id, request_id in (
        (peer, task["run_id"], employee["id"], "write"),
        (client, "stale-run", employee["id"], "write"),
        (client, task["run_id"], "other-employee", "write"),
        (client, task["run_id"], employee["id"], "unknown"),
    ):
        with pytest.raises(WorkbenchError):
            wrong_client.permission_decision(task["id"], run_id, employee_id, request_id, wait=0)
    assert owner.get_permission(task["id"], "write")["status"] == "pending"
    assert client.permission_decision(
        task["id"], task["run_id"], employee["id"], "write", wait=0
    ) == {"status": "pending", "decision": None}
    assert owner.get_permission(task["id"], "write")["status"] == "pending"
    assert client.expire_permission(task["id"], task["run_id"], employee["id"], "write") == {
        "status": "expired",
        "decision": "deny",
    }


def test_permission_timeout_deadline_is_persisted_not_renewed(fleet, tmp_path, monkeypatch):
    monkeypatch.setattr("agent_mailbox.workbench_store.PERMISSION_TIMEOUT", 1)
    owner, _, client, task, employee = awaiting_permission(fleet, tmp_path)
    deadline = owner.get_permission(task["id"], "write")["expires_at"]
    duplicate = client.request_permission(
        task["id"],
        task["run_id"],
        employee["id"],
        "write",
        PERMISSION_OPTIONS,
        {"title": "Write file"},
    )
    assert duplicate["expires_at"] == deadline
    assert client.permission_decision(
        task["id"], task["run_id"], employee["id"], "write", wait=5
    ) == {"status": "expired", "decision": "deny"}
    with pytest.raises(WorkbenchError):
        owner.resolve_permission(task["id"], "write", "allow_once")


def test_permission_rejects_outside_project_and_retired_employee(fleet, tmp_path):
    owner, project, other, coordinator = fleet
    client = mapped(fleet, tmp_path)
    foreign = owner.create_employee(
        "Private", "codex", other["id"], node_id=client.credentials["device_id"]
    )
    task = owner.create_task(other["id"], "Private", "Secret", foreign["id"])
    owner.claim_task(client.credentials["device_id"], project_ids=[other["id"]])
    owner.set_status(task["id"], "running")
    with pytest.raises(WorkbenchError):
        client.request_permission(
            task["id"],
            task["run_id"],
            foreign["id"],
            "private",
            PERMISSION_OPTIONS,
            {"title": "Private"},
        )
    assert owner.task_detail(task["id"])["permissions"] == []
    employee = client.register_employee(project["id"], "Active", "codex")
    own = owner.create_task(project["id"], "Own", "Report", employee["id"])
    client.claim(project["id"], wait=0)
    client.event(own["id"], own["run_id"], "started")
    client.request_permission(
        own["id"], own["run_id"], employee["id"], "write", PERMISSION_OPTIONS, {"title": "Write"}
    )
    owner.set_employee_lifecycle(employee["id"], "retired", "Retired in isolated fixture")
    coordinator.notify()
    with pytest.raises(WorkbenchError):
        client.permission_decision(own["id"], own["run_id"], employee["id"], "write", wait=0)
    with pytest.raises(WorkbenchError):
        client.project_tool(project["id"], employee["id"], "context", {})
    assert owner.get_permission(own["id"], "write")["status"] == "expired"


def test_device_revoked_during_blocking_claim_cannot_receive_new_task(fleet, tmp_path):
    owner, project, _, coordinator = fleet
    client = mapped(fleet, tmp_path)
    employee = client.register_employee(project["id"], "Remote", "codex")
    with ThreadPoolExecutor() as pool:
        future = pool.submit(client.claim, project["id"], 5)
        time.sleep(0.1)
        task = owner.create_task(project["id"], "Next", "Report", employee["id"])
        coordinator.revoke_device(client.credentials["device_id"])
        with pytest.raises(WorkbenchError):
            future.result(timeout=1)
    assert owner.get_task(task["id"])["status"] == "queued"


@pytest.fixture
def fleet(tmp_path):
    owner = WorkbenchStore(tmp_path / "owner")
    project_folder = tmp_path / "owner-project"
    project_folder.mkdir()
    project = owner.create_project("Shared", project_folder)
    other = owner.create_project("Private", tmp_path)
    coordinator = FleetCoordinator(owner)
    coordinator.start()
    try:
        yield owner, project, other, coordinator
    finally:
        coordinator.stop()


def paired(fleet, tmp_path, name="device"):
    _, project, _, coordinator = fleet
    client = FleetClient(tmp_path / name, coordinator.issue_invite([project["id"]]))
    return client


def mapped(fleet, tmp_path, name="device"):
    client = paired(fleet, tmp_path, name)
    directory = tmp_path / (name + "-project")
    directory.mkdir()
    client.map_project(fleet[1]["id"], directory)
    return client


def test_pair_employee_registration_and_revoke_notify_owner(fleet, tmp_path):
    _, project, _, coordinator = fleet
    changes = []
    coordinator.on_change = lambda: changes.append("changed")
    client = paired(fleet, tmp_path)
    assert len(changes) == 1
    client.register_employee(project["id"], "Remote", "codex")
    assert len(changes) == 2
    coordinator.revoke_device(client.credentials["device_id"])
    assert len(changes) == 3


def test_pairing_real_tls_private_credential_and_project_context(fleet, tmp_path):
    owner, project, _, coordinator = fleet
    owner.add_memory(project["id"], "Rule", "Use local workspace")
    source = tmp_path / "brief.md"
    source.write_text("Shared document")
    owner.add_resource(project["id"], "Brief", "document", source)
    invite = coordinator.issue_invite([project["id"]])
    client = FleetClient(tmp_path / "remote", invite)
    assert client.credentials["device_id"] == client.store.local_node()["id"]
    assert client.credentials["coordinator_id"] == owner.local_node()["id"]
    assert client.credentials["device_id"] != owner.local_node()["id"]
    assert private_access(client.credentials_path, 0o600)
    assert private_access(client.directory, 0o700)
    assert private_access(coordinator.state_path, 0o600)
    assert private_access(coordinator.directory / "tls-key.pem", 0o600)
    context = client.context(project["id"])
    assert context["project"]["id"] == project["id"]
    assert "path" not in context["project"]
    assert "path" not in context["resources"][0]
    assert context["memories"][0]["body"] == "Use local workspace"
    metadata = client.device()
    assert metadata["device"]["device_id"] == client.store.local_node()["id"]
    assert "token" not in json.dumps(metadata)
    assert client.credentials["token"] not in json.dumps(owner.snapshot())
    assert client.credentials["token"] not in coordinator.state_path.read_text()
    assert invite["invite_secret"] not in coordinator.state_path.read_text()
    assert client.projects() == [{"id": project["id"], "name": "Shared"}]


def test_wrong_fingerprint_stops_before_invite_is_sent(fleet, tmp_path):
    _, project, _, coordinator = fleet
    invite = coordinator.issue_invite([project["id"]])
    altered = {**invite, "fingerprint": "sha256:" + "0" * 64}
    with pytest.raises(WorkbenchError) as error:
        FleetClient(tmp_path / "remote", altered)
    assert error.value.code == "tls_pin_mismatch"
    assert coordinator.state["invites"][invite["invite_id"]]["used"] is False
    assert coordinator.state["devices"] == {}
    valid = FleetClient(tmp_path / "remote", invite)
    assert valid.device()["coordinator"]["id"] == coordinator.store.local_node()["id"]


def test_wrong_invite_secret_rejected(fleet, tmp_path):
    _, project, _, coordinator = fleet
    invite = coordinator.issue_invite([project["id"]])
    with pytest.raises(WorkbenchError) as error:
        FleetClient(tmp_path / "remote", {**invite, "invite_secret": "wrong"})
    assert error.value.code == "permission_denied"
    assert not coordinator.state["invites"][invite["invite_id"]]["used"]
    assert coordinator.state["devices"] == {}


def test_expired_invite_rejected(fleet, tmp_path):
    _, project, _, coordinator = fleet
    invite = coordinator.issue_invite([project["id"]])
    with coordinator.lock:
        coordinator.state["invites"][invite["invite_id"]]["expires_at"] = 0
        coordinator._save()
    with pytest.raises(WorkbenchError) as error:
        FleetClient(tmp_path / "remote", invite)
    assert error.value.code == "invite_expired"
    assert coordinator.state["devices"] == {}


def test_invite_exactly_one_winner_under_race(fleet, tmp_path):
    _, project, _, coordinator = fleet
    invite = coordinator.issue_invite([project["id"]])

    def join(name):
        try:
            return FleetClient(tmp_path / name, invite)
        except WorkbenchError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(join, ["one", "two"]))
    assert sum(isinstance(value, FleetClient) for value in results) == 1
    assert results.count("invite_used") == 1
    assert len(coordinator.state["devices"]) == 1


def test_client_reuses_stable_identity_without_replaying_invite(fleet, tmp_path):
    _, project, _, coordinator = fleet
    invite = coordinator.issue_invite([project["id"]])
    first = FleetClient(tmp_path / "remote", invite)
    restarted = FleetClient(tmp_path / "remote", invite)
    assert restarted.credentials == first.credentials
    assert restarted.device()["device"]["device_id"] == first.store.local_node()["id"]
    assert len(coordinator.state["devices"]) == 1
    with pytest.raises(WorkbenchError) as error:
        FleetClient(tmp_path / "other", invite)
    assert error.value.code == "invite_used"


def test_scope_denies_other_project_registration_and_context(fleet, tmp_path):
    owner, project, other, _ = fleet
    client = paired(fleet, tmp_path)
    assert client.context(project["id"])["project"]["id"] == project["id"]
    for action in (
        lambda: client.context(other["id"]),
        lambda: client.register_employee(other["id"], "Hacker", "codex"),
        lambda: client._request("POST", "/v1/tasks/claim", {"project_id": other["id"]}),
    ):
        with pytest.raises(WorkbenchError) as error:
            action()
        assert error.value.code == "permission_denied"
    assert owner.snapshot()["employees"] == []


def test_device_credentials_cannot_access_human_or_arbitrary_command_api(fleet, tmp_path):
    client = paired(fleet, tmp_path)
    for path in ("/api/workbench/snapshot", "/v1/shell", "/v1/permissions/approve", "/v1/invites"):
        with pytest.raises(WorkbenchError) as error:
            client._request("POST", path, {"command": "echo hacked"})
        assert error.value.code == "not_found"


def test_credentials_invalid_and_revoked_rejected(fleet, tmp_path):
    _, project, _, coordinator = fleet
    client = paired(fleet, tmp_path)
    token = client.credentials["token"]
    client.credentials["token"] = "wrong"
    with pytest.raises(WorkbenchError) as error:
        client.context(project["id"])
    assert error.value.code == "permission_denied"
    client.credentials["token"] = token
    assert client.context(project["id"])["project"]["id"] == project["id"]
    coordinator.revoke_device(client.credentials["device_id"])
    with pytest.raises(WorkbenchError):
        client.context(project["id"])


def test_registration_forces_own_node_and_local_mapping_required(fleet, tmp_path):
    owner, project, _, _ = fleet
    client = paired(fleet, tmp_path)
    employee = client._request(
        "POST",
        "/v1/employees",
        {
            "project_id": project["id"],
            "name": "Remote Codex",
            "kind": "codex",
            "node_id": owner.local_node()["id"],
        },
    )["employee"]
    assert employee["node_id"] == client.credentials["device_id"]
    task = owner.create_task(project["id"], "Work", "Use local project", employee["id"])
    with pytest.raises(WorkbenchError) as error:
        client.claim(project["id"])
    assert error.value.code == "project_unmapped"
    assert owner.get_task(task["id"])["status"] == "queued"
    directory = tmp_path / "local-project"
    directory.mkdir()
    mapping = client.map_project(project["id"], directory)
    assert mapping["path"] != project["path"]
    assert client.local_project(project["id"])["path"] == str(directory)
    claimed = client.claim(project["id"])
    assert claimed["id"] == task["id"]
    assert claimed["kind"] == "codex"
    assert claimed["status"] == "starting"


def test_scope_filtered_claim_does_not_lose_unauthorized_queued_task(fleet, tmp_path):
    owner, project, other, _ = fleet
    client = mapped(fleet, tmp_path)
    employee = client.register_employee(project["id"], "Remote", "codex")
    # The owner can assign other projects to the same known device, but the
    # device's pairing scope must still gate which work it can receive.
    owner.create_employee("Remote", "codex", other["id"], node_id=employee["node_id"])
    private = owner.create_task(other["id"], "Private", "Do not dispatch", employee["id"])
    shared = owner.create_task(project["id"], "Shared", "Dispatch", employee["id"])
    assert client.claim(project["id"])["id"] == shared["id"]
    assert owner.get_task(private["id"])["status"] == "queued"
    assert client.claim(project["id"], wait=0) is None


def test_real_remote_claim_progress_receipt_review_then_human_accept(fleet, tmp_path):
    owner, project, _, _ = fleet
    client = mapped(fleet, tmp_path)
    employee = client.register_employee(project["id"], "Remote", "codex")
    task = owner.create_task(project["id"], "Work", "Report result", employee["id"])
    claimed = client.claim(project["id"])
    assert claimed["run_id"] == task["run_id"]
    with pytest.raises(WorkbenchError):
        client.receipt(task["id"], task["run_id"], "review", "No started confirmation")
    client.event(task["id"], task["run_id"], "started", "Worker started")
    assert owner.get_task(task["id"])["status"] == "running"
    client.event(task["id"], task["run_id"], "output", "Reported", {"text": "Done"})
    receipt = client.receipt(task["id"], task["run_id"], "review", "Remote result")
    assert receipt["status"] == "review"
    assert owner.task_detail(task["id"])["events"][-1]["type"] == "review"
    with pytest.raises(WorkbenchError):
        client.receipt(task["id"], task["run_id"], "done", "Cannot self-approve")
    assert owner.review_task(task["id"], "accept")["status"] == "done"


def test_other_device_and_stale_run_cannot_submit_receipt(fleet, tmp_path):
    owner, project, _, _ = fleet
    first = mapped(fleet, tmp_path, "first")
    second = mapped(fleet, tmp_path, "second")
    employee = first.register_employee(project["id"], "Remote", "codex")
    task = owner.create_task(project["id"], "Work", "Report", employee["id"])
    first.claim(project["id"])
    for client, run_id in ((second, task["run_id"]), (first, "run-stale")):
        with pytest.raises(WorkbenchError) as error:
            client.receipt(task["id"], run_id, "review", "Forged")
        assert error.value.code == "permission_denied"
    assert owner.get_task(task["id"])["status"] == "starting"


def test_remote_can_report_denied_permission_failure_without_approval_api(fleet, tmp_path):
    owner, project, _, _ = fleet
    client = mapped(fleet, tmp_path)
    employee = client.register_employee(project["id"], "Remote", "codex")
    task = owner.create_task(project["id"], "Work", "Needs approval", employee["id"])
    client.claim(project["id"])
    client.event(task["id"], task["run_id"], "started", "Running")
    result = client.receipt(
        task["id"],
        task["run_id"],
        "failed",
        error={
            "code": "REMOTE_PERMISSION_DENIED",
            "message": "Remote permissions default to deny.",
        },
    )
    assert result["error"]["code"] == "REMOTE_PERMISSION_DENIED"
    assert result["status"] == "failed"


def test_coordinator_restart_preserves_tls_and_device_trust(fleet, tmp_path):
    owner, project, _, old = fleet
    client = paired(fleet, tmp_path)
    port = old.server.server_address[1]
    fingerprint = old.fingerprint
    old.stop()
    restarted = FleetCoordinator(owner)
    try:
        restarted.start(port=port)
        assert restarted.fingerprint == fingerprint
        assert client.context(project["id"])["project"]["id"] == project["id"]
    finally:
        restarted.stop()


def test_listener_not_implicit_and_invite_validations(tmp_path):
    owner = WorkbenchStore(tmp_path / "owner")
    project = owner.create_project("Project", tmp_path)
    coordinator = FleetCoordinator(owner)
    assert coordinator.server is None
    with pytest.raises(WorkbenchError):
        coordinator.issue_invite([project["id"]])
    for args in (("127.0.0.1", -1), ("127.0.0.1", True), ([], 0)):
        with pytest.raises(WorkbenchError):
            coordinator.start(*args)
    try:
        coordinator.start()
        for projects, ttl in (
            ([], 300),
            (["missing"], 300),
            ([project["id"]], 0),
            ([project["id"]], 3601),
            ([project["id"]], True),
        ):
            with pytest.raises(WorkbenchError):
                coordinator.issue_invite(projects, ttl)
    finally:
        coordinator.stop()


def test_blocking_claim_wakes_immediately_on_new_task_notification(fleet, tmp_path, monkeypatch):
    owner, project, _, coordinator = fleet
    client = mapped(fleet, tmp_path)
    employee = client.register_employee(project["id"], "Remote", "codex")
    observed_empty = threading.Event()
    original = owner.claim_task

    def claim(node_id, project_ids=None):
        task = original(node_id, project_ids)
        if task is None:
            observed_empty.set()
        return task

    monkeypatch.setattr(owner, "claim_task", claim)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.claim, project["id"], 5)
        assert observed_empty.wait(timeout=2)
        assert not future.done()
        task = owner.create_task(project["id"], "New", "Do work", employee["id"])
        started = time.monotonic()
        coordinator.notify()
        assert future.result(timeout=2)["id"] == task["id"]
        assert time.monotonic() - started < 2


def test_remote_cannot_forge_approval_event_or_persist_device_token(fleet, tmp_path):
    owner, project, _, _ = fleet
    client = mapped(fleet, tmp_path)
    employee = client.register_employee(project["id"], "Remote", "codex")
    task = owner.create_task(project["id"], "Work", "Report", employee["id"])
    client.claim(project["id"])
    with pytest.raises(WorkbenchError):
        client.event(task["id"], task["run_id"], "permission_resolved", "I approved myself")
    token = client.credentials["token"]
    event = client.event(
        task["id"], task["run_id"], "output", "Token: " + token, {"text": token, "token": token}
    )
    assert token not in json.dumps(event)
    client.receipt(
        task["id"], task["run_id"], "failed", token, {"code": "FAILED", "message": token}
    )
    assert token not in json.dumps(owner.task_detail(task["id"]))
    assert token not in json.dumps(owner.snapshot())


def test_blocking_control_wakes_on_owner_cancel_and_returns_terminal(fleet, tmp_path, monkeypatch):
    owner, project, _, coordinator = fleet
    client = mapped(fleet, tmp_path)
    employee = client.register_employee(project["id"], "Remote", "codex")
    task = owner.create_task(project["id"], "Work", "Run", employee["id"])
    client.claim(project["id"])
    client.event(task["id"], task["run_id"], "started")
    entered = threading.Event()
    original = coordinator._task_scope

    def task_scope(device, task_id, run_id):
        result = original(device, task_id, run_id)
        entered.set()
        return result

    monkeypatch.setattr(coordinator, "_task_scope", task_scope)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.control, task["id"], task["run_id"], 5)
        assert entered.wait(timeout=2)
        assert not future.done()
        owner.cancel_task(task["id"])
        coordinator.notify()
        control = future.result(timeout=2)
    assert control == {"status": "running", "cancel_requested": True}
    client.receipt(task["id"], task["run_id"], "cancelled")
    assert client.control(task["id"], task["run_id"], wait=0)["status"] == "cancelled"
    with pytest.raises(WorkbenchError):
        client.control(task["id"], "run-stale", wait=0)


def test_remote_project_tools_context_notes_resources_and_team_dispatch(fleet, tmp_path):
    owner, project, _, coordinator = fleet
    client = mapped(fleet, tmp_path)
    employee = client.register_employee(project["id"], "Remote", "codex")
    colleague = owner.create_employee("Local reviewer", "claude", project["id"])
    changes = threading.Event()
    coordinator.on_change = changes.set
    context = client.project_tool(project["id"], employee["id"], "context", {})
    assert {row["id"] for row in context["employees"]} == {employee["id"], colleague["id"]}
    assert "path" not in context["project"]
    incoming = owner.send_message(
        project["id"], "For remote", "personal", recipient_id=employee["id"]
    )
    owner.send_message(project["id"], "For local", "other mail", recipient_id=colleague["id"])
    shared = owner.send_message(project["id"], "Group", "shared")
    inbox = client.project_tool(project["id"], employee["id"], "messages", {})
    assert inbox["folder"] == "inbox" and [m["id"] for m in inbox["messages"]] == [incoming["id"]]
    group = client.project_tool(project["id"], employee["id"], "messages", {"folder": "group"})
    assert [m["id"] for m in group["messages"]] == [shared["id"]]
    context = client.project_tool(project["id"], employee["id"], "context", {})
    assert {m["id"] for m in context["messages"]} == {incoming["id"], shared["id"]}
    source_task = owner.create_task(project["id"], "Source", "Request review", employee["id"])
    client.claim(project["id"], wait=0)
    note = client.project_tool(
        project["id"],
        employee["id"],
        "note",
        {"title": "Observation", "body": "Prefer clear tests"},
        task_id=source_task["id"],
        run_id=source_task["run_id"],
    )
    assert note["source"] == "employee:" + employee["id"]
    search = client.project_tool(project["id"], employee["id"], "memory_search", {"query": "tests"})
    assert search["memories"][0]["id"] == note["id"]
    source = tmp_path / "resource.md"
    source.write_text("Shared data")
    resource = owner.add_resource(project["id"], "Brief", "document", source)
    read = client.project_tool(
        project["id"], employee["id"], "resource_read", {"resource_id": resource["id"]}
    )
    assert read["content"] == "Shared data"
    assert "path" not in read["resource"]
    assert read["source"].startswith("resource:")
    receipt = client.project_tool(
        project["id"],
        employee["id"],
        "team_message",
        {
            "recipient_id": colleague["id"],
            "title": "Review",
            "message": "Please review",
            "permission_mode": "all-access",
        },
        task_id=source_task["id"],
        run_id=source_task["run_id"],
    )
    assert receipt["status"] == "queued"
    assert changes.is_set()
    task = owner.get_task(receipt["task_id"])
    assert task["assignee_id"] == colleague["id"]
    assert task["permission_mode"] == "read-only"
    assert owner.task_detail(task["id"])["events"][-1]["payload"]["from_id"] == employee["id"]


def test_project_tools_deny_wrong_device_employee_project_self_and_tool(fleet, tmp_path):
    owner, project, other, _ = fleet
    client = paired(fleet, tmp_path)
    employee = client.register_employee(project["id"], "Remote", "codex")
    local = owner.create_employee("Local", "codex", project["id"])
    outsider = owner.create_employee("Outsider", "claude", other["id"], node_id=employee["node_id"])
    for employee_id, tool, args in (
        (local["id"], "context", {}),
        (outsider["id"], "context", {}),
        (employee["id"], "shell", {"command": "echo nope"}),
        (employee["id"], "context", {"project_id": other["id"]}),
        (
            employee["id"],
            "team_message",
            {"recipient_id": employee["id"], "title": "Self", "message": "Nope"},
        ),
        (
            employee["id"],
            "team_message",
            {"recipient_id": outsider["id"], "title": "Other project", "message": "Nope"},
        ),
    ):
        with pytest.raises(WorkbenchError) as error:
            client.project_tool(project["id"], employee_id, tool, args)
        assert error.value.code == "permission_denied"
    assert owner.snapshot()["tasks"] == []


def test_active_reconciliation_lists_only_authorized_device_runs(fleet, tmp_path):
    owner, project, other, _ = fleet
    first = mapped(fleet, tmp_path, "first")
    second = mapped(fleet, tmp_path, "second")
    own = first.register_employee(project["id"], "Own", "codex")
    peer = second.register_employee(project["id"], "Peer", "codex")
    private = owner.create_employee("Private", "codex", other["id"], node_id=own["node_id"])
    own_task = owner.create_task(project["id"], "Own task", "Report", own["id"])
    peer_task = owner.create_task(project["id"], "Peer task", "Report", peer["id"])
    private_task = owner.create_task(other["id"], "Private task", "Secret", private["id"])
    first.claim(project["id"])
    second.claim(project["id"])
    owner.claim_task(own["node_id"], project_ids=[other["id"]])
    rows = first.active_runs()
    assert rows == [
        {
            "id": own_task["id"],
            "run_id": own_task["run_id"],
            "project_id": project["id"],
            "status": "starting",
            "cancel_requested": False,
        }
    ]
    assert peer_task["id"] not in json.dumps(rows)
    assert private_task["id"] not in json.dumps(rows)
    assert set(rows[0]) == {"id", "run_id", "project_id", "status", "cancel_requested"}
    first.credentials["device_id"] = second.credentials["device_id"]
    with pytest.raises(WorkbenchError) as error:
        first.active_runs()
    assert error.value.code == "permission_denied"


def test_revoked_device_can_explicitly_repair_with_rotated_token_and_new_scope(fleet, tmp_path):
    _, project, other, coordinator = fleet
    old = paired(fleet, tmp_path)
    device_id = old.credentials["device_id"]
    old_token = old.credentials["token"]
    coordinator.revoke_device(device_id)
    assert coordinator.public_devices() == [{"id": device_id, "revoked": True}]
    with pytest.raises(WorkbenchError):
        old.context(project["id"])
    # The human explicitly leaves before applying a fresh invite. Constructor
    # reuse must never silently overwrite an existing device credential.
    invite = coordinator.issue_invite([other["id"]])
    old.credentials_path.unlink()
    repaired = FleetClient(old.store.root, invite)
    assert repaired.credentials["device_id"] == device_id
    assert repaired.credentials["token"] != old_token
    assert repaired.context(other["id"])["project"]["id"] == other["id"]
    with pytest.raises(WorkbenchError):
        repaired.context(project["id"])
    with pytest.raises(WorkbenchError):
        old.context(other["id"])
    assert coordinator.public_devices() == [{"id": device_id, "revoked": False}]
    metadata = json.dumps(coordinator.public_devices())
    assert old_token not in metadata and repaired.credentials["token"] not in metadata
    assert "token_hash" not in metadata and "secret_hash" not in metadata
    unused = coordinator.issue_invite([other["id"]])
    with pytest.raises(WorkbenchError):
        repaired._request(
            "POST",
            "/v1/pair",
            {
                "invite_id": unused["invite_id"],
                "invite_secret": unused["invite_secret"],
                "device_id": device_id,
                "name": "Do not overwrite active identity",
            },
            authenticated=False,
        )
    assert coordinator.state["invites"][unused["invite_id"]]["used"] is False


def test_identical_final_receipt_replays_without_rewriting_human_review(fleet, tmp_path):
    owner, project, _, coordinator = fleet
    client = mapped(fleet, tmp_path)
    employee = client.register_employee(project["id"], "Remote", "codex")
    task = owner.create_task(project["id"], "Report", "Work", employee["id"])
    client.claim(project["id"])
    notifications = []
    coordinator.on_change = lambda: notifications.append("changed")
    client.event(task["id"], task["run_id"], "started")
    assert notifications
    client.receipt(task["id"], task["run_id"], "review", "Deliverable", {"b": 2, "a": 1})
    event_count = len(owner.task_detail(task["id"])["events"])
    owner.review_task(task["id"], "accept")
    reviewed_count = len(owner.task_detail(task["id"])["events"])
    replay = client.receipt(task["id"], task["run_id"], "review", "Deliverable", {"a": 1, "b": 2})
    assert replay["status"] == "done"
    assert reviewed_count == event_count + 1
    assert len(owner.task_detail(task["id"])["events"]) == reviewed_count
    with pytest.raises(WorkbenchError) as conflict:
        client.receipt(task["id"], task["run_id"], "review", "Different deliverable")
    assert conflict.value.code == "invalid_state"
    assert owner.get_task(task["id"])["status"] == "done"


def test_repeated_remote_receipt_respects_owner_cancel(fleet, tmp_path):
    owner, project, _, _ = fleet
    client = mapped(fleet, tmp_path)
    employee = client.register_employee(project["id"], "Remote", "codex")
    task = owner.create_task(project["id"], "Report", "Work", employee["id"])
    client.claim(project["id"])
    client.event(task["id"], task["run_id"], "started")
    owner.cancel_task(task["id"])
    assert client.receipt(task["id"], task["run_id"], "review", "Output")["status"] == "cancelled"
    assert client.receipt(task["id"], task["run_id"], "review", "Output")["status"] == "cancelled"


def test_remote_text_proposal_stays_unapproved_and_bound_to_active_run(fleet, tmp_path):
    owner, project, other, _ = fleet
    client = mapped(fleet, tmp_path)
    employee = client.register_employee(project["id"], "Remote writer", "codex")
    source = tmp_path / "PRD.md"
    source.write_text("Original coordinator brief")
    resource = owner.add_resource(project["id"], "PRD", "prd", source)
    approved = owner.capture_resource_version(project["id"], resource["id"])
    task = owner.create_task(project["id"], "Propose", "Propose text", employee["id"])
    client.claim(project["id"], wait=0)
    run = {"task_id": task["id"], "run_id": task["run_id"]}
    proposal = client.project_tool(
        project["id"],
        employee["id"],
        "resource_propose",
        {"resource_id": resource["id"], "content": "Remote text proposal", "summary": "Review"},
        **run,
    )
    assert proposal["status"] == "proposed" and proposal["approved_by"] is None
    assert source.read_text() == "Original coordinator brief"
    assert owner.resource_manifest(project["id"])["resources"][0]["version_id"] == approved["id"]
    assert (
        owner.read_resource_version(project["id"], resource["id"], proposal["id"])["content"]
        == "Remote text proposal"
    )
    assert "source" not in proposal
    with pytest.raises(WorkbenchError):
        client.project_tool(
            project["id"], employee["id"], "resource_approve", {"version_id": proposal["id"]}, **run
        )
    with pytest.raises(WorkbenchError):
        client.project_tool(
            other["id"],
            employee["id"],
            "resource_propose",
            {"resource_id": resource["id"], "content": "leak"},
            **run,
        )
    owner.approve_resource_version(project["id"], resource["id"], proposal["id"])
    pinned = client.project_tool(
        project["id"], employee["id"], "resource_read", {"resource_id": resource["id"]}, **run
    )
    assert pinned["content"] == "Original coordinator brief"
    owner.cancel_task(task["id"])
    for tool, args in [
        ("context", {}),
        ("resource_read", {"resource_id": resource["id"]}),
        ("code_search", {"query": "Example"}),
        ("messages", {}),
    ]:
        with pytest.raises(WorkbenchError) as error:
            client.project_tool(project["id"], employee["id"], tool, args, **run)
        assert error.value.code == "permission_denied"


def test_tls_listener_does_not_depend_on_reverse_dns(tmp_path, monkeypatch):
    import socket

    def unavailable(*args):
        raise AssertionError("Reverse DNS must not run during TLS bind")

    monkeypatch.setattr(socket, "getfqdn", unavailable)
    owner = WorkbenchStore(tmp_path / "owner")
    project = owner.create_project("Shared", tmp_path)
    coordinator = FleetCoordinator(owner)
    coordinator.start()
    try:
        client = FleetClient(tmp_path / "client", coordinator.issue_invite([project["id"]]))
        assert client.projects() == [{"id": project["id"], "name": "Shared"}]
    finally:
        coordinator.stop()


def test_stop_interrupts_claim_response_after_http_connection_releases_socket():
    import socket

    client = object.__new__(FleetClient)
    client.connection_lock = threading.Lock()
    client.claim_connection = None  # HTTP/1.0 response owns the socket now.
    reading, peer = socket.socketpair()
    client.claim_socket = reading
    finished = threading.Event()
    received = []

    def read_response():
        with reading.makefile("rb") as stream:
            received.append(stream.readline())
        finished.set()

    worker = threading.Thread(target=read_response)
    worker.start()
    try:
        client.interrupt_claim()
        assert finished.wait(2)
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert received == [b""]
    finally:
        reading.close()
        peer.close()
