"""Real Git isolation, immutable deliveries and guarded owner application."""

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore
from agent_mailbox.workbench_workspaces import (
    WORKSPACE_SCHEMA,
    apply_delivery,
    capture_delivery,
    prepare_workspace,
    task_delivery,
)


def git(path, *args):
    return (
        subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.DEVNULL)
        .decode()
        .strip()
    )


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    (root / "main.txt").write_text("original\n")
    (root / ".gitignore").write_text(".env\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "baseline")
    (root / ".env").write_text("API_KEY=secret")
    store = WorkbenchStore(tmp_path / "home")
    with store._transaction() as db:
        for sql in WORKSPACE_SCHEMA:
            db.execute(sql)
        db.execute(
            "CREATE TABLE IF NOT EXISTS task_links(task_id TEXT PRIMARY KEY,parent_task_id TEXT,relation TEXT)"
        )
    project = store.create_project("Git project", root)
    employee = store.create_employee("Codex", "codex", project["id"])

    def task(mode="workspace-write"):
        return store.create_task(project["id"], "change", "change safely", employee["id"], mode)

    return store, project, task, root


def accept(store, task):
    with store._transaction() as db:
        db.execute("UPDATE tasks SET status='done' WHERE id=?", (task["id"],))


def test_isolation_commits_untracked_and_explicit_apply(setup):
    store, project, create, source = setup
    first, second = create(), create()
    a = Path(prepare_workspace(store, first, project)["path"])
    b = Path(prepare_workspace(store, second, project)["path"])
    assert a != b and not (a / ".env").exists()
    (a / "main.txt").write_text("committed\n")
    git(a, "add", ".")
    git(a, "commit", "-m", "employee work")
    (a / "extra.txt").write_text("untracked\n")
    captured = capture_delivery(
        store, first, project, {"status": "completed", "output_text": "tests pass"}
    )
    assert captured["capture_error"] is None
    assert "committed" in captured["diff"] and "untracked" in captured["diff"]
    assert captured["verification"]["status"] == "not_verified"
    assert (source / "main.txt").read_text() == "original\n"
    assert (b / "main.txt").read_text() == "original\n"
    assert not task_delivery(store, first["id"])["can_apply"]
    accept(store, first)
    assert task_delivery(store, first["id"])["can_apply"]
    applied = apply_delivery(store, first["id"])
    assert applied["applied"] and not applied["can_apply"]
    assert (source / "main.txt").read_text() == "committed\n"
    assert (source / "extra.txt").read_text() == "untracked\n"
    assert git(source, "rev-parse", "HEAD") == captured["workspace"]["base_commit"]
    with pytest.raises(WorkbenchError):
        apply_delivery(store, first["id"])


def test_followup_inherits_patch_and_baseline_guard(setup):
    store, project, create, source = setup
    original = create()
    isolated = Path(prepare_workspace(store, original, project)["path"])
    (isolated / "main.txt").write_text("proposal\n")
    capture_delivery(store, original, project, {"output_text": "draft"})
    child = create()
    with store._transaction() as db:
        db.execute(
            "INSERT INTO task_links VALUES(?,?,?)", (child["id"], original["id"], "follow_up")
        )
    followup = Path(prepare_workspace(store, child, project)["path"])
    assert (followup / "main.txt").read_text() == "proposal\n"
    assert (source / "main.txt").read_text() == "original\n"
    (source / "main.txt").write_text("human change\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "advanced")
    accept(store, original)
    assert task_delivery(store, original["id"])["apply_blocked_reason"] == "workspace_stale"
    with pytest.raises(WorkbenchError):
        apply_delivery(store, original["id"])


def test_dirty_source_and_nonroot_refused(setup):
    store, project, create, source = setup
    (source / "unknown.txt").write_text("not committed")
    with pytest.raises(WorkbenchError, match="未提交"):
        prepare_workspace(store, create(), project)
    (source / "unknown.txt").unlink()
    subdir = source / "nested"
    subdir.mkdir()
    with pytest.raises(WorkbenchError, match="根目录"):
        prepare_workspace(store, create(), {**project, "path": str(subdir)})


@pytest.mark.parametrize("kind", ["symlink", "binary", "secret"])
def test_unsafe_patch_not_applied_or_secret_displayed(setup, kind):
    store, project, create, source = setup
    task = create()
    isolated = Path(prepare_workspace(store, task, project)["path"])
    if kind == "symlink":
        (isolated / "link").symlink_to(source / "main.txt")
    elif kind == "binary":
        (isolated / "binary.bin").write_bytes(b"\0\xff")
    else:
        (isolated / "main.txt").write_text("API_KEY=do-not-display-secret\n")
    delivery = capture_delivery(
        store, task, project, {"output_text": "API_KEY=do-not-display-secret"}
    )
    assert delivery["capture_error"]
    assert "do-not-display-secret" not in str(delivery)
    accept(store, task)
    assert not task_delivery(store, task["id"])["can_apply"]
    with pytest.raises(WorkbenchError):
        apply_delivery(store, task["id"])
    assert (source / "main.txt").read_text() == "original\n"


def test_readonly_and_capture_immutable(setup):
    store, project, create, _source = setup
    task = create("read-only")
    assert prepare_workspace(store, task, project) == project
    one = capture_delivery(store, task, project, {"status": "completed", "output_text": "reported"})
    two = capture_delivery(store, task, project, {"output_text": "different"})
    assert one == two and one["workspace"] is None
    assert not task_delivery(store, task["id"])["can_apply"]


def test_payload_patch_integrity_and_dirty_guard(setup):
    store, project, create, source = setup
    task = create()
    isolated = Path(prepare_workspace(store, task, project)["path"])
    (isolated / "main.txt").write_text("changed\n")
    capture_delivery(store, task, project, {})
    accept(store, task)
    (source / "main.txt").write_text("human\n")
    assert task_delivery(store, task["id"])["apply_blocked_reason"] == "workspace_dirty"
    (source / "main.txt").write_text("original\n")
    with store._transaction() as db:
        db.execute(
            "UPDATE task_deliveries SET patch=patch||'corrupt' WHERE task_id=?", (task["id"],)
        )
    assert task_delivery(store, task["id"])["apply_blocked_reason"] == "delivery_integrity_error"


def test_concurrent_worktrees_and_apply_uses_frozen_patch(setup):
    store, project, create, source = setup
    tasks = [create(), create()]
    with ThreadPoolExecutor(max_workers=2) as pool:
        prepared = list(pool.map(lambda task: prepare_workspace(store, task, project), tasks))
    assert prepared[0]["path"] != prepared[1]["path"]
    workspace = Path(prepared[0]["path"])
    (workspace / "main.txt").write_text("frozen\n")
    capture_delivery(store, tasks[0], project, {})
    (workspace / "main.txt").write_text("later mutation\n")
    accept(store, tasks[0])
    apply_delivery(store, tasks[0]["id"])
    assert (source / "main.txt").read_text() == "frozen\n"


def test_path_traversal_and_git_metadata_are_refused(setup):
    from agent_mailbox.workbench_workspaces import _safe_path

    store, project, create, source = setup
    for path in ("../outside", ".git/config", "/tmp/outside", "child/../outside"):
        with pytest.raises(WorkbenchError):
            _safe_path(source, path)
    task = create()
    prepared = Path(prepare_workspace(store, task, project)["path"])
    original_git = (prepared / ".git").read_text()
    (prepared / ".git").unlink()
    (prepared / ".git").symlink_to(source / ".git")
    assert capture_delivery(store, task, project, {})["capture_error"]["code"] == "delivery_unsafe"
    (prepared / ".git").unlink()
    (prepared / ".git").write_text(original_git)
