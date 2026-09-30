"""Update preparation preserves identities, blocks claims and fails visibly."""

import json
import os
import shutil
import sqlite3
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_mailbox import __version__
from agent_mailbox.workbench import WorkbenchHTTP
from agent_mailbox.workbench_store import SCHEMA_VERSION, WorkbenchError, WorkbenchStore
from agent_mailbox.workbench_updates import (
    select_release,
    version_key,
)


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
    assert "workbench/state.db" in [row["path"] for row in manifest["files"]]
    assert (backup / "workbench/fleet/device.key").read_bytes() == b"private device material"
    assert not (backup / "workbench/runtime").exists()
    assert not (backup / "workbench/instance.json").exists()
    restored_root = tmp_path / "restored"
    shutil.copytree(backup / "workbench", restored_root / "workbench")
    restored = WorkbenchStore(restored_root)
    assert restored.employee_credentials(employee["id"], project["id"])["token"] == identity
    assert restored.update_maintenance()["paused"]
    with sqlite3.connect(restored.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    if os.name != "nt":
        assert backup.stat().st_mode & 0o777 == 0o700
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in backup.rglob("*") if p.is_file())


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
