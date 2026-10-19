"""Update preparation preserves identities, blocks claims and fails visibly."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

import pytest

from agent_mailbox import __version__
from agent_mailbox.workbench import WorkbenchHTTP
from agent_mailbox.workbench_engine import WorkbenchEngine
from agent_mailbox.workbench_mail_sessions import create_session, invoke
from agent_mailbox.workbench_mail_tasks import accept_mail_task, create_mail_task, submit_mail_task
from agent_mailbox.workbench_private import private_access
from agent_mailbox.workbench_store import SCHEMA_VERSION, WorkbenchError, WorkbenchStore
from agent_mailbox.workbench_updates import (
    UpdateService,
    private_backup,
    select_release,
    version_key,
)
from agent_mailbox.workbench_wake import empty_state, load_state, run_once, save_state, state_lock


@pytest.fixture
def setup(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Project", tmp_path)
    employee = store.create_employee("Employee", "codex", project["id"])
    server = WorkbenchHTTP(store)
    yield store, project, employee, server
    server.close()


def release(version, **kw):
    return {
        "tag_name": "v" + version,
        "draft": False,
        "prerelease": False,
        "body": "Release notes",
        **kw,
    }


def test_channels_ordering_and_untrusted_feed_links():
    feed = [
        release("0.8.0b9", prerelease=True),
        release("0.8.0b10", prerelease=True),
        release("0.7.9"),
        release("0.8.0a99", prerelease=True),
        release("9.0.0", draft=True),
        release("bad"),
        release("0.7.10", html_url="https://evil.test"),
    ]
    assert select_release(feed, "stable")["version"] == "0.7.10"
    latest = select_release(feed, "beta")
    assert latest["version"] == "0.8.0b10"
    assert latest["url"].startswith("https://github.com/polaris-smart/agent-mailbox/releases/tag/")
    assert version_key("0.8.0b10") < version_key("0.8.0rc1") < version_key("0.8.0")
    assert version_key("0.8.0+evil") is None
    assert select_release([release("0.8.0b9")], "stable") is None
    with pytest.raises(WorkbenchError):
        select_release({}, "stable")


def test_check_does_not_reuse_success_after_failure(setup, monkeypatch):
    *_, server = setup
    monkeypatch.setattr(
        "agent_mailbox.workbench_updates.fetch_releases", lambda: [release("0.9.0")]
    )
    assert server.updates.check_releases()["check"]["status"] == "update_available"

    def fail():
        raise urllib.error.HTTPError("https://api.github.com", 429, "rate limit", {}, None)

    monkeypatch.setattr("agent_mailbox.workbench_updates.fetch_releases", fail)
    check = server.updates.check_releases()["check"]
    assert check["status"] == "error" and check["latest"] is None
    assert check["error"]["code"] == "UPDATE_RATE_LIMIT"
    assert server.updates.channel("beta")["check"] is None
    assert WorkbenchStore(server.store.root).update_channel() == "beta"
    with pytest.raises(WorkbenchError):
        server.updates.channel("invalid")


def test_preparation_pauses_queue_and_durable_resume(setup):
    store, project, employee, server = setup
    task = store.create_task(project["id"], "Queue", "No model", employee["id"])
    result = server.updates.prepare()
    assert result["status"] == "ready" and result["maintenance"]["queued_count"] == 1
    assert store.claim_task(store.local_node()["id"]) is None
    reopened = WorkbenchStore(store.root)
    assert reopened.update_maintenance()["paused"]
    server.updates.resume()
    assert reopened.claim_task(store.local_node()["id"])["id"] == task["id"]


def test_active_task_finishes_before_backup_without_cancelling(setup):
    store, project, employee, server = setup
    store.create_task(project["id"], "Active", "No model", employee["id"])
    task = store.claim_task(store.local_node()["id"])
    result = server.updates.prepare()
    assert result["status"] == "waiting" and result["backup"] is None
    assert store.get_task(task["id"])["status"] == "starting"
    assert store.get_task(task["id"])["cancel_requested"] == 0
    store.finish_task(task["id"], "review", result="Complete")
    assert server.updates.prepare()["status"] == "ready"


def test_active_managed_permission_can_drain_while_update_waits(setup):
    store, project, employee, server = setup
    store.create_task(project["id"], "Approval", "No model", employee["id"])
    task = store.claim_task(store.local_node()["id"])
    store.set_status(task["id"], "running")
    store.request_permission(task["id"], "write", [{"kind": "allow_once"}], {"title": "Write"})
    assert server.updates.prepare()["status"] == "waiting"
    assert store.permission_decision(task["id"], "write", task["run_id"])["status"] == "pending"
    assert store.resolve_permission(task["id"], "write", "deny")["decision"] == "deny"
    assert store.permission_decision(task["id"], "write", task["run_id"])["decision"] == "deny"
    store.finish_task(task["id"], "review", result="Stopped after denial")
    assert server.updates.prepare()["status"] == "ready"


def test_owner_can_decide_existing_managed_permission_during_pause(setup):
    store, project, employee, server = setup
    store.create_task(project["id"], "Owner approval", "No model", employee["id"])
    task = store.claim_task(store.local_node()["id"])
    store.set_status(task["id"], "running")
    store.request_permission(task["id"], "write", [{"kind": "allow_once"}], {"title": "Write"})
    assert server.updates.prepare()["status"] == "waiting"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            server.endpoint + f"/api/workbench/tasks/{task['id']}/permissions/write",
            data=json.dumps({"decision": "deny"}).encode(),
            headers={"Authorization": "Bearer " + server.token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=4) as response:
            assert json.load(response)["decision"] == "deny"
    finally:
        server.shutdown()
        thread.join(timeout=4)
    store.finish_task(task["id"], "review", result="Stopped after denial")
    assert server.updates.prepare()["status"] == "ready"


def test_active_managed_late_permission_and_delivery_survive_pause(setup):
    from agent_mailbox.workbench_workspaces import capture_delivery

    store, project, employee, server = setup
    store.create_task(project["id"], "Late approval", "No model", employee["id"])
    task = store.claim_task(store.local_node()["id"])
    store.set_status(task["id"], "running")
    assert server.updates.prepare()["status"] == "waiting"
    store.request_permission(task["id"], "late", [{"kind": "allow_once"}], {"title": "Write"})
    store.resolve_permission(task["id"], "late", "deny")
    delivery = capture_delivery(
        store, task, project, {"status": "completed", "output_text": "After pause"}
    )
    assert delivery["task_id"] == task["id"]
    store.finish_task(task["id"], "review", result="After pause")
    assert server.updates.prepare()["status"] == "ready"


def test_managed_engine_completes_after_update_pause_without_losing_delivery(setup, tmp_path):
    from agent_mailbox.workbench_proof import latest_proof

    store, project, employee, server = setup
    store.create_task(project["id"], "Finish", "No model", employee["id"])
    task = store.claim_task(store.local_node()["id"])
    engine = server.engine
    engine.execution.command = ["synthetic"]
    deliverable = tmp_path / "synthetic-result.md"

    def fake_run(_task, _project, _mcp_servers, event, _permission, _cancelled, *, timeout):
        event({"type": "started"})
        assert server.updates.prepare()["status"] == "waiting"
        deliverable.write_text("Synthetic result")
        event(
            {
                "type": "event",
                "event": {"tag": "tool_call", "rawInput": {"file_path": str(deliverable)}},
            }
        )
        return {"status": "completed", "output_text": "Synthetic result"}

    engine.execution.run = fake_run
    engine.execute(task)
    assert store.get_task(task["id"])["status"] == "review"
    assert store.snapshot()["employees"][0]["status"] == "available"
    with store._transaction(readonly=True) as db:
        assert db.execute("SELECT 1 FROM task_deliveries WHERE task_id=?", (task["id"],)).fetchone()
    assert latest_proof(store, task["id"]) is not None
    assert server.updates.prepare()["status"] == "ready"


def test_completed_task_id_cannot_bypass_paused_employee_updates(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Project", tmp_path)
    employee = store.create_employee("Employee", "codex", project["id"])
    store.create_task(project["id"], "Old", "No model", employee["id"])
    old = store.claim_task(store.local_node()["id"])
    store.finish_task(old["id"], "review", result="Old result")
    store.pause_updates(True)
    with pytest.raises(WorkbenchError) as blocked:
        store.update_employee(employee["id"], "unknown", "Bypass")
    assert blocked.value.code == "UPDATE_PAUSED"
    with pytest.raises(WorkbenchError) as replay:
        store.finish_task(old["id"], "review", result="Replay", employee_status="unknown")
    assert replay.value.code == "invalid_state"


def test_managed_completion_updates_employee_atomically_while_paused(tmp_path):
    from agent_mailbox.workbench_proof import latest_proof

    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Project", tmp_path)
    employee = store.create_employee("Employee", "codex", project["id"])
    store.create_task(project["id"], "Active", "No model", employee["id"])
    task = store.claim_task(store.local_node()["id"])
    engine = WorkbenchEngine(store)
    engine.execution.command = ["synthetic"]
    artifact = tmp_path / "result.md"

    def fake_run(_task, _project, _mcp_servers, event, _permission, _cancelled, *, timeout):
        event({"type": "started"})
        artifact.write_text("Synthetic result")
        event(
            {
                "type": "event",
                "event": {"tag": "tool_call", "rawInput": {"file_path": str(artifact)}},
            }
        )
        store.pause_updates(True)
        return {"status": "completed", "output_text": "Synthetic result"}

    engine.execution.run = fake_run
    engine.execute(task)
    assert store.get_task(task["id"])["status"] == "review"
    assert store.snapshot()["employees"][0]["status"] == "available"
    with store._transaction(readonly=True) as db:
        assert db.execute("SELECT 1 FROM task_deliveries WHERE task_id=?", (task["id"],)).fetchone()
    assert latest_proof(store, task["id"]) is not None


def test_other_process_can_drain_managed_task_but_cannot_reuse_pause_exception(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Project", tmp_path)
    employee = store.create_employee("Employee", "codex", project["id"])
    store.create_task(project["id"], "Active", "No model", employee["id"])
    task = store.claim_task(store.local_node()["id"])
    store.pause_updates(True)
    child_code = """\
import json
import sys
from pathlib import Path
from agent_mailbox.workbench_store import WorkbenchStore, WorkbenchError
s = WorkbenchStore(Path(sys.argv[1]))
task_id, employee_id = sys.argv[2:]
finished = s.finish_task(task_id, 'review', result='Done', employee_status='available')
try:
    s.update_employee(employee_id, 'unknown', 'Bypass')
    blocked = 'WRONG'
except WorkbenchError as exc:
    blocked = exc.code
try:
    s.finish_task(task_id, 'review', result='Replay', employee_status='unknown')
    replay = 'WRONG'
except WorkbenchError as exc:
    replay = exc.code
print(json.dumps({'status': finished['status'], 'blocked': blocked, 'replay': replay}))
"""
    child = subprocess.run(
        [sys.executable, "-c", child_code, str(store.root), task["id"], employee["id"]],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    assert json.loads(child.stdout) == {
        "status": "review",
        "blocked": "UPDATE_PAUSED",
        "replay": "invalid_state",
    }
    assert WorkbenchStore(store.root).snapshot()["employees"][0]["status"] == "available"


def test_paused_update_rejects_new_event_on_finished_managed_task(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Project", tmp_path)
    employee = store.create_employee("Employee", "codex", project["id"])
    store.create_task(project["id"], "Completed", "No model", employee["id"])
    task = store.claim_task(store.local_node()["id"])
    store.finish_task(task["id"], "review", result="Done")
    store.pause_updates(True)
    with pytest.raises(WorkbenchError) as blocked:
        WorkbenchStore(store.root).add_event(task["id"], "late_event", "After backup")
    assert blocked.value.code == "UPDATE_PAUSED"


def test_verified_backup_freezes_late_proof_until_explicit_resume(tmp_path):
    from agent_mailbox.workbench_proof import build_proof, latest_proof

    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Project", tmp_path)
    employee = store.create_employee("Employee", "codex", project["id"])
    store.create_task(project["id"], "Completed", "No model", employee["id"])
    task = store.claim_task(store.local_node()["id"])
    store.finish_task(task["id"], "review", result="Done")
    store.pause_updates(True)
    backup = private_backup(store)
    assert backup["verified"]
    with sqlite3.connect(Path(backup["path"]) / "workbench/state.db") as backed_up:
        assert (
            backed_up.execute(
                "SELECT count(*) FROM update_claims WHERE id=?",
                ("__agent_mailbox_update_frozen__",),
            ).fetchone()[0]
            == 0
        )
    assert store.update_maintenance()["frozen"]
    assert store.update_maintenance()["pending_claims"] == []
    reopened = WorkbenchStore(store.root)
    assert reopened.claim_task(reopened.local_node()["id"]) is None
    assert reopened.begin_update_claim("late-claim") is False
    engine = WorkbenchEngine(reopened)
    engine.start()
    engine.close()
    assert private_backup(reopened)["verified"]
    with pytest.raises(WorkbenchError) as blocked:
        build_proof(reopened, task["id"], managed_completion=True)
    assert blocked.value.code == "UPDATE_PAUSED"
    assert latest_proof(store, task["id"]) is None
    store.pause_updates(False)
    assert not store.update_maintenance()["frozen"]
    assert build_proof(store, task["id"])["task_id"] == task["id"]


def test_failed_backup_keeps_freeze_until_retry_or_resume(tmp_path, monkeypatch):
    from agent_mailbox import workbench_updates

    store = WorkbenchStore(tmp_path / "home")
    store.pause_updates(True)
    original_hash = workbench_updates._hash

    def fail_hash(_path):
        raise OSError("synthetic manifest failure")

    monkeypatch.setattr(workbench_updates, "_hash", fail_hash)
    with pytest.raises(OSError, match="synthetic manifest failure"):
        private_backup(store)
    assert store.update_maintenance()["frozen"]
    assert not list((store.root / "update-backups").iterdir())
    monkeypatch.setattr(workbench_updates, "_hash", original_hash)
    assert private_backup(store)["verified"]
    store.pause_updates(False)
    assert not store.update_maintenance()["frozen"]


def test_update_service_can_reprepare_and_resume_frozen_home_without_socket(tmp_path):
    store = WorkbenchStore(tmp_path / "home")

    class Server:
        def __init__(self):
            self.store = store
            self.engine = WorkbenchEngine(store)
            self.fleet = None

        def ui_notify(self):
            pass

        def notify(self):
            pass

    updates = UpdateService(Server())
    assert updates.prepare()["status"] == "ready"
    repeated = updates.prepare()
    assert repeated["status"] == "ready" and repeated["maintenance"]["frozen"]
    updates.assert_can_quit()
    assert updates.resume()["maintenance"]["paused"] is False
    assert not store.update_maintenance()["frozen"]


def test_pause_waits_for_inflight_wake_and_next_round_sends_nothing(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    delivered = []
    with ThreadPoolExecutor(1) as pool:
        with state_lock(store.root):
            future = pool.submit(store.pause_updates, True)
            with pytest.raises(FutureTimeoutError):
                future.result(timeout=0.25)
            assert not store.update_maintenance()["paused"]
        assert future.result(timeout=5)["paused"]
    result = run_once(
        store,
        store.root,
        unread_provider=lambda _store, _employee: ["message-synthetic"],
        deliver=lambda _employee, _decision: delivered.append(1) or True,
        employee_ids=["employee-synthetic"],
        pending_provider=lambda _store, _employee: ["message-synthetic"],
        paused_provider=lambda: store.update_maintenance()["paused"],
        event_driven=True,
    )
    assert result["deferred"] == "update_paused"
    assert delivered == []
    assert not (store.root / "wake/wake-state.json").exists()


def test_pause_cannot_split_host_delivery_from_receipt(tmp_path, monkeypatch):
    store = WorkbenchStore(tmp_path / "home")
    entered_delivery = threading.Event()
    release_delivery = threading.Event()
    receipt_paused_states = []
    monkeypatch.setattr("agent_mailbox.workbench_wake.delivery_available", lambda *_: True)

    def deliver(_employee, _decision):
        entered_delivery.set()
        assert release_delivery.wait(5)
        return True

    def acknowledge(_store, _employee, _message_ids):
        receipt_paused_states.append(store.update_maintenance()["paused"])

    with ThreadPoolExecutor(2) as pool:
        wake = pool.submit(
            run_once,
            store,
            store.root,
            unread_provider=lambda *_: ["message-synthetic"],
            deliver=deliver,
            employee_ids=["employee-synthetic"],
            cold_started=True,
            pending_provider=lambda *_: ["message-synthetic"],
            acknowledge=acknowledge,
            paused_provider=lambda: store.update_maintenance()["paused"],
            event_driven=True,
        )
        try:
            assert entered_delivery.wait(5)
            pause = pool.submit(store.pause_updates, True)
            with pytest.raises(FutureTimeoutError):
                pause.result(timeout=0.15)
            assert not store.update_maintenance()["paused"]
        finally:
            release_delivery.set()
        assert wake.result(timeout=5)["woke"] == ["employee-synthetic"]
        assert pause.result(timeout=5)["paused"]
    assert receipt_paused_states == [False]


def test_native_wake_loop_uses_the_persisted_pause_gate(tmp_path, monkeypatch):
    from agent_mailbox.workbench import wake_redeem_loop

    store = WorkbenchStore(tmp_path / "home")
    store.pause_updates(True)
    stop = threading.Event()
    observed = []

    def inspect_round(*_args, **kwargs):
        observed.append(kwargs["paused_provider"]())
        stop.set()
        return {"woke": []}

    monkeypatch.setattr("agent_mailbox.workbench_wake.run_once", inspect_round)
    wake_redeem_loop(store, store.root, interval=0, stop=stop)
    assert observed == [True]


def test_wake_cli_cannot_toggle_backed_up_state_during_pause(tmp_path):
    from agent_mailbox.workbench_wake import cli_main

    store = WorkbenchStore(tmp_path / "home")
    store.pause_updates(True)
    assert cli_main(["--disable"], home=store.root, store=store) == 2
    assert not (store.root / "wake/wake-state.json").exists()


def test_update_backup_waits_for_managed_postprocessing_worker(setup, monkeypatch):
    store, project, employee, server = setup
    store.create_task(project["id"], "Postprocess", "No model", employee["id"])
    task = store.claim_task(store.local_node()["id"])
    engine = server.engine
    engine.execution.command = ["synthetic"]
    engine.execution.run = lambda *_args, **_kwargs: {
        "status": "completed",
        "output_text": "Synthetic result",
    }
    entered = threading.Event()
    release = threading.Event()
    original = engine._record_delivery_proof

    def held_postprocessing(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "_record_delivery_proof", held_postprocessing)

    def worker():
        engine.execute(task)
        with engine.lock:
            engine.workers.discard(threading.current_thread())

    thread = threading.Thread(target=worker)
    with engine.lock:
        engine.workers.add(thread)
    thread.start()
    try:
        assert entered.wait(5)
        assert store.get_task(task["id"])["status"] == "review"
        prepared = server.updates.prepare()
        assert prepared["status"] == "waiting" and prepared["backup"] is None
        assert prepared["maintenance"]["finishing_workers"] == 1
        with pytest.raises(WorkbenchError) as busy:
            server.updates.assert_can_quit()
        assert busy.value.code == "UPDATE_BUSY"
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert server.updates.prepare()["status"] == "ready"


def test_update_pause_fences_existing_mailbox_session_writes(setup, tmp_path):
    from agent_mailbox.workbench_workspaces import capture_delivery

    store, project, employee, _server = setup
    peer = store.create_employee("Peer", "codex", project["id"])
    session = create_session(store, employee["id"], project["id"], "Synthetic session")
    queued = create_mail_task(store, project["id"], "Queued", "Read", employee["id"])
    running = create_mail_task(store, project["id"], "Running", "Read", employee["id"])
    accept_mail_task(store, session["token"], running["id"])
    incoming = store.send_message(project["id"], "Notice", "Read me", recipient_id=employee["id"])
    source = tmp_path / "prd.md"
    source.write_text("Synthetic resource")
    resource = store.add_resource(project["id"], "PRD", "prd", source)
    store.pause_updates(True)

    # Existing sessions retain read access, but cannot mutate the snapshot
    # boundary by accepting, submitting, acknowledging or sending.
    assert invoke(store, session["token"], "identity", {})["session_id"] == session["id"]
    assert invoke(store, session["token"], "tasks", {})["reading_accepts"] is False
    for tool, args in (
        ("message_open", {"message_id": incoming["id"]}),
        ("message", {"title": "New", "body": "Body", "recipient_id": peer["id"]}),
        ("note", {"title": "Log", "body": "Body"}),
        ("resource_propose", {"resource_id": resource["id"], "content": "New text"}),
        ("task_accept", {"task_id": queued["id"]}),
        ("task_submit", {"task_id": running["id"], "result": "Done"}),
        ("aoci_status", {}),
    ):
        with pytest.raises(WorkbenchError) as error:
            invoke(store, session["token"], tool, args)
        assert error.value.code == "UPDATE_PAUSED", tool
    for action in (
        lambda: accept_mail_task(store, session["token"], queued["id"]),
        lambda: submit_mail_task(store, session["token"], running["id"], "Done"),
        lambda: store.finish_task(running["id"], "review", result="Bypass"),
        lambda: store.request_permission(
            running["id"], "bypass", [{"kind": "allow_once"}], {"title": "Write"}
        ),
        lambda: capture_delivery(store, running, project, {"status": "completed"}),
    ):
        with pytest.raises(WorkbenchError) as error:
            action()
        assert error.value.code == "UPDATE_PAUSED"
    assert store.get_task(queued["id"])["status"] == "queued"
    assert store.get_task(running["id"])["status"] == "running"
    assert not [message for message in store.snapshot()["messages"] if message["title"] == "New"]
    store.pause_updates(False)
    assert accept_mail_task(store, session["token"], queued["id"])["status"] == "running"


def test_running_mailbox_task_survives_update_backup_and_resume(setup):
    store, project, employee, server = setup
    session = create_session(store, employee["id"], project["id"], "Synthetic session")
    task = create_mail_task(
        store, project["id"], "Long handoff", "Pinned instructions", employee["id"]
    )
    accepted = accept_mail_task(store, session["token"], task["id"])
    assert accepted["resource_manifest"] is not None

    prepared = server.updates.prepare()
    assert prepared["status"] == "ready"
    assert prepared["maintenance"]["durable_mail_tasks"][0]["id"] == task["id"]
    server.updates.assert_can_quit()
    backup = Path(prepared["backup"]["path"]) / "workbench/state.db"
    with sqlite3.connect(backup) as db:
        assert (
            db.execute("SELECT status FROM tasks WHERE id=?", (task["id"],)).fetchone()[0]
            == "running"
        )
        assert (
            db.execute(
                "SELECT count(*) FROM task_resource_manifests WHERE task_id=?", (task["id"],)
            ).fetchone()[0]
            == 1
        )
    with pytest.raises(WorkbenchError) as paused:
        submit_mail_task(store, session["token"], task["id"], "Finished after upgrade")
    assert paused.value.code == "UPDATE_PAUSED"

    reopened = WorkbenchStore(store.root)
    assert reopened.get_task(task["id"])["status"] == "running"
    assert reopened.update_maintenance()["paused"]
    engine = WorkbenchEngine(reopened)
    engine.start()
    engine.close()
    server.updates.resume()
    assert (
        submit_mail_task(reopened, session["token"], task["id"], "Finished after upgrade")["status"]
        == "review"
    )


def test_other_process_store_cannot_write_after_update_backup(setup, tmp_path):
    store, _project, _employee, server = setup
    assert server.updates.prepare()["status"] == "ready"
    independent = WorkbenchStore(store.root)
    with pytest.raises(WorkbenchError) as blocked:
        independent.create_project("After backup", tmp_path)
    assert blocked.value.code == "UPDATE_PAUSED"
    child_code = """\
import sys
from pathlib import Path
from agent_mailbox.workbench_store import WorkbenchStore, WorkbenchError
s = WorkbenchStore(Path(sys.argv[1]))
try:
    s.create_project('Child write', Path(sys.argv[2]))
    print('WRONG')
except WorkbenchError as exc:
    print(exc.code)
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            child_code,
            str(store.root),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    assert child.stdout.strip() == "UPDATE_PAUSED"
    assert len(store.snapshot()["projects"]) == 1
    server.updates.resume()
    assert independent.create_project("After resume", tmp_path)["name"] == "After resume"


def test_owner_http_writes_wait_during_prepared_update(setup, tmp_path):
    store, _project, _employee, server = setup
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(route, body=None):
        req = urllib.request.Request(
            server.endpoint + "/api/workbench/" + route,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + server.token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=4) as response:
            return json.load(response)

    try:
        assert request("updates/prepare", {})["status"] == "ready"
        with pytest.raises(urllib.error.HTTPError) as blocked:
            request("projects", {"name": "During upgrade", "path": str(tmp_path)})
        assert blocked.value.code == 400
        assert json.load(blocked.value)["error"]["code"] == "UPDATE_PAUSED"
        assert len(store.snapshot()["projects"]) == 1
        assert request("bootstrap")["update_maintenance"]["paused"]
        assert request("updates/resume", {})["maintenance"]["paused"] is False
        assert (
            request("projects", {"name": "After resume", "path": str(tmp_path)})["name"]
            == "After resume"
        )
    finally:
        server.shutdown()
        thread.join(2)


def test_update_backup_cannot_overtake_inflight_owner_write(setup, tmp_path, monkeypatch):
    store, _project, _employee, server = setup
    entered = threading.Event()
    release = threading.Event()
    original = store.create_project

    def delayed_create(*args):
        entered.set()
        assert release.wait(4)
        return original(*args)

    monkeypatch.setattr(store, "create_project", delayed_create)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def create_http():
        req = urllib.request.Request(
            server.endpoint + "/api/workbench/projects",
            data=json.dumps({"name": "Before backup", "path": str(tmp_path)}).encode(),
            headers={"Authorization": "Bearer " + server.token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)

    try:
        with ThreadPoolExecutor(2) as pool:
            write = pool.submit(create_http)
            assert entered.wait(2)
            prepare = pool.submit(server.updates.prepare)
            with pytest.raises(FutureTimeoutError):
                prepare.result(timeout=0.2)
            release.set()
            assert write.result(timeout=5)["name"] == "Before backup"
            result = prepare.result(timeout=5)
        assert result["status"] == "ready"
        with sqlite3.connect(Path(result["backup"]["path"]) / "workbench/state.db") as db:
            assert (
                db.execute("SELECT count(*) FROM projects WHERE name='Before backup'").fetchone()[0]
                == 1
            )
    finally:
        release.set()
        server.shutdown()
        thread.join(2)


def test_remote_unconfirmed_claim_and_receipt_blocks_prepare(setup):
    store, *_, server = setup
    assert store.begin_update_claim("claim-1")
    assert server.updates.prepare()["status"] == "waiting"
    assert not store.begin_update_claim("claim-2")
    store.end_update_claim("claim-1")
    directory = store.directory / "fleet"
    directory.mkdir()
    path = directory / "terminal-receipts.json"
    path.write_text(json.dumps({"task-1": {"status": "review"}}))
    result = server.updates.prepare()
    assert result["remote_pending"] == ["terminal-receipts.json"]
    path.write_text("{}")
    assert server.updates.prepare()["status"] == "ready"


def test_claim_pause_race_serializes_without_lost_work(setup):
    store, project, employee, server = setup
    task = store.create_task(project["id"], "Race", "No model", employee["id"])
    barrier = threading.Barrier(2)

    def claim():
        barrier.wait()
        return store.claim_task(store.local_node()["id"])

    def prepare():
        barrier.wait()
        return server.updates.prepare()

    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(claim), pool.submit(prepare)
        claimed, prepared = a.result(), b.result()
    assert store.update_maintenance()["paused"]
    if claimed:
        assert prepared["status"] == "waiting"
        assert claimed["id"] == task["id"]
    else:
        assert prepared["status"] == "ready"
        assert store.get_task(task["id"])["status"] == "queued"


def test_backup_restores_private_identity_and_excludes_runtime(setup, tmp_path):
    store, project, employee, server = setup
    identity = store.employee_credentials(employee["id"], project["id"])["token"]
    fleet = store.directory / "fleet"
    fleet.mkdir()
    (fleet / "device.key").write_bytes(b"private device material")
    runtime = store.directory / "runtime"
    runtime.mkdir()
    (runtime / "rebuildable").write_text("do not copy")
    backup = Path(server.updates.prepare()["backup"]["path"])
    manifest = json.loads((backup / "manifest.json").read_text())
    paths = [row["path"] for row in manifest["files"]]
    assert "workbench/state.db" in paths
    assert "workbench/fleet/device.key" in paths
    assert (backup / "workbench/fleet/device.key").read_bytes() == b"private device material"
    assert not (backup / "workbench/runtime").exists()
    assert not (backup / "workbench/instance.json").exists()
    assert private_access(backup / "manifest.json", 0o600)
    assert not list((backup / "workbench").glob("state.db-*"))
    restored_root = tmp_path / "restored"
    shutil.copytree(backup / "workbench", restored_root / "workbench")
    restored = WorkbenchStore(restored_root)
    assert restored.employee_credentials(employee["id"], project["id"])["token"] == identity
    assert restored.update_maintenance()["paused"]
    with sqlite3.connect(restored.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert private_access(backup, 0o700)
    unsafe = [
        p.relative_to(backup).as_posix()
        for p in backup.rglob("*")
        if p.is_file() and not private_access(p, 0o600)
    ]
    assert unsafe == []


def test_backup_preserves_wake_claims_receipts_and_executable_hook(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    wake = store.root / "wake"
    claims = wake / "claims"
    receipts = wake / "receipts"
    claims.mkdir(parents=True)
    receipts.mkdir()
    wake_state = empty_state()
    wake_state["cold_done"] = True
    save_state(store.root, wake_state)
    (wake / "host-bindings.json").write_text('{"version":1,"bindings":[]}')
    (claims / "message-synthetic.unknown").write_text("synthetic-delivery")
    (receipts / "synthetic-delivery").write_text('{"status":"received"}')
    hook = wake / "wake-hook.sh"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o700)

    store.pause_updates(True)
    backup = Path(private_backup(store)["path"])
    manifest = json.loads((backup / "manifest.json").read_text())
    paths = {item["path"] for item in manifest["files"]}
    for relative in (
        "wake/wake-state.json",
        "wake/host-bindings.json",
        "wake/claims/message-synthetic.unknown",
        "wake/receipts/synthetic-delivery",
        "wake/wake-hook.sh",
    ):
        assert relative in paths
        assert (backup / relative).read_bytes() == (store.root / relative).read_bytes()
    assert private_access(backup / "wake/wake-hook.sh", 0o700)
    restored = tmp_path / "restored"
    shutil.copytree(backup / "workbench", restored / "workbench")
    shutil.copytree(backup / "wake", restored / "wake")
    assert load_state(restored)["cold_done"] is True
    assert (restored / "wake/claims/message-synthetic.unknown").read_text() == "synthetic-delivery"


@pytest.mark.skipif(os.name == "nt", reason="Symlink semantics vary on Windows")
def test_symlink_in_wake_rejects_complete_backup(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    wake = store.root / "wake"
    wake.mkdir()
    (wake / "foreign").symlink_to(tmp_path / "outside")
    store.pause_updates(True)
    with pytest.raises(WorkbenchError, match="符号链接"):
        private_backup(store)
    assert store.update_maintenance()["paused"]
    assert not list((store.root / "update-backups").iterdir())


@pytest.mark.skipif(os.name == "nt", reason="Symlink semantics vary on Windows")
def test_symlinked_wake_root_is_rejected_before_backup(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    external = tmp_path / "external"
    external.mkdir()
    store.pause_updates(True)
    (store.root / "wake").rmdir()
    (store.root / "wake").symlink_to(external, target_is_directory=True)
    with pytest.raises(WorkbenchError, match="符号链接"):
        private_backup(store)
    assert not (store.root / "update-backups").exists()


def test_backup_waits_for_wake_claim_transaction(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    store.pause_updates(True)
    started = threading.Event()

    def snapshot():
        started.set()
        return private_backup(store)

    with ThreadPoolExecutor(1) as pool:
        with state_lock(store.root):
            future = pool.submit(snapshot)
            assert started.wait(2)
            with pytest.raises(FutureTimeoutError):
                future.result(timeout=0.25)
            claims = store.root / "wake/claims"
            claims.mkdir()
            (claims / "message-synthetic.unknown").write_text("synthetic-delivery")
        backup = Path(future.result(timeout=5)["path"])
    assert (backup / "wake/claims/message-synthetic.unknown").read_text() == "synthetic-delivery"


def test_backup_failure_keeps_pause_no_false_ready(setup, monkeypatch):
    store, *_, server = setup

    def fail(*args):
        raise OSError("private file content must not leak")

    monkeypatch.setattr("agent_mailbox.workbench_updates.private_backup", fail)
    with pytest.raises(WorkbenchError) as exc:
        server.updates.prepare()
    assert exc.value.code == "UPDATE_BACKUP_FAILED"
    assert "private file content" not in exc.value.message
    assert store.update_maintenance()["paused"] and server.updates.status()["last_backup"] is None
    assert not server.updates.resume()["maintenance"]["paused"]


@pytest.mark.skipif(os.name == "nt", reason="Symlink semantics vary on Windows")
def test_symlink_backup_rejected_and_partial_removed(setup, tmp_path):
    store, *_, server = setup
    (store.directory / "outside").symlink_to(tmp_path / "target")
    with pytest.raises(WorkbenchError, match="符号链接"):
        server.updates.prepare()
    assert store.update_maintenance()["paused"]
    assert not list((store.root / "update-backups").iterdir())


def test_status_no_network_and_installation_guidance(setup, monkeypatch):
    *_, server = setup
    monkeypatch.setattr(
        "agent_mailbox.workbench_updates.fetch_releases",
        lambda: pytest.fail("GET cannot contact GitHub"),
    )
    status = server.updates.status()
    assert status["current_version"] == __version__
    assert status["channel"] == "stable" and status["check"] is None
    assert status["installation"]["home"] == str(server.store.root)
    assert len(status["installation"]["instructions"]) == 4
    assert status["nodes"][0]["status"] == "matched"
    monkeypatch.setattr("agent_mailbox.workbench_updates.sys.frozen", True, raising=False)
    assert server.updates.status()["installation"]["kind"] == "app"


def test_owner_http_boundary_and_bootstrap(setup):
    *_, server = setup
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(route, body=None, token=None):
        req = urllib.request.Request(
            server.endpoint + "/api/workbench/" + route,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + (token or "wrong")},
        )
        with urllib.request.urlopen(req, timeout=4) as response:
            return json.load(response)

    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            request("updates/prepare", {})
        assert exc.value.code == 401 and not server.store.update_maintenance()["paused"]
        assert request("updates/prepare", {}, server.token)["status"] == "ready"
        assert request("bootstrap", token=server.token)["update_maintenance"]["paused"]
        assert request("updates/resume", {}, server.token)["maintenance"]["paused"] is False
    finally:
        server.shutdown()
        thread.join(2)


def test_waiting_or_failed_preparation_cannot_quit_for_update(setup, monkeypatch):
    store, project, employee, server = setup
    store.create_task(project["id"], "Running", "No model", employee["id"])
    task = store.claim_task(store.local_node()["id"])
    server.updates.prepare()
    with pytest.raises(WorkbenchError) as exc:
        server.updates.assert_can_quit()
    assert exc.value.code == "UPDATE_BUSY"
    store.finish_task(task["id"], "review")
    with pytest.raises(WorkbenchError) as exc:
        server.updates.assert_can_quit()
    assert exc.value.code == "UPDATE_BACKUP_REQUIRED"
    server.updates.prepare()
    server.updates.assert_can_quit()

    def fail(*args):
        raise OSError()

    monkeypatch.setattr("agent_mailbox.workbench_updates.private_backup", fail)
    with pytest.raises(WorkbenchError):
        server.updates.prepare()
    with pytest.raises(WorkbenchError):
        server.updates.assert_can_quit()
    server.updates.resume()
    server.updates.assert_can_quit()


def test_schema7_upgrade_backup_and_identity_preservation(setup):
    store, project, employee, _server = setup
    token = store.employee_credentials(employee["id"], project["id"])["token"]
    with sqlite3.connect(store.db_path) as db:
        db.execute("DROP TABLE update_settings")
        db.execute("DROP TABLE update_claims")
        db.execute("PRAGMA user_version=7")
    migrated = WorkbenchStore(store.root)
    assert migrated.employee_credentials(employee["id"], project["id"])["token"] == token
    assert migrated.update_channel() == "stable"
    assert not migrated.update_maintenance()["paused"]
    backup = migrated.migration_backup_path
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 7
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name='update_settings'"
        ).fetchone()
        assert db.execute("SELECT id FROM employees").fetchone()[0] == employee["id"]
