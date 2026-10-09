"""Regressions for the **fourth** adversarial review (2026-10-05, probes re-run live).

That round's reviewer crashed before writing its report, but its probes survived in
``/tmp/amr4``; re-running them against the live source exposed these defects, all
fixed here.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys

import pytest

from agent_mailbox import (
    workbench_artifact,
    workbench_bridge,
    workbench_enroll,
    workbench_proof,
    workbench_wall,
)
from agent_mailbox.workbench_mail_tasks import create_mail_task
from agent_mailbox.workbench_store import WorkbenchStore

REPO = __import__("pathlib").Path(__file__).resolve().parents[1]
CLI = [sys.executable, "-m", "agent_mailbox"]


@pytest.fixture
def scene(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    alice = store.create_employee("Alice", "codex", project["id"])
    bob = store.create_employee("Bob", "codex", project["id"])
    return store, project, alice, bob


def _journal(store) -> str:
    raw = sqlite3.connect(store.db_path, isolation_level=None)
    try:
        return raw.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        raw.close()


def test_rv4_1_read_paths_never_flip_the_journal_mode(scene, tmp_path):
    """第四轮：一批读路径（含 CLI 读命令）仍开写连接 ⇒ 把库从 delete 改成 WAL。"""
    store, project, alice, _bob = scene
    task = create_mail_task(store, project["id"], "活", "说明", alice["id"])
    artifact = tmp_path / "out.md"
    artifact.write_text("交付", encoding="utf-8")
    workbench_proof.build_proof(store, task["id"], artifacts=[str(artifact)])
    doc = tmp_path / "PRD.md"
    doc.write_text("批准内容", encoding="utf-8")
    resource = store.add_resource(project["id"], "PRD", "prd", doc)

    raw = sqlite3.connect(store.db_path, isolation_level=None)
    raw.execute("PRAGMA journal_mode=delete")
    raw.close()

    # 每条路径前后都必须是 delete（不得被改成 WAL）
    from agent_mailbox import (
        workbench_mail_sessions,
        workbench_policy,
        workbench_resources,
        workbench_workspaces,
    )

    workbench_proof.latest_proof(store, task["id"])
    workbench_proof.verify_proof(store, task["id"])
    workbench_artifact.list_refs(store)
    store.governance_events(limit=None)
    store.snapshot()
    workbench_mail_sessions.list_sessions(store, alice["id"], project["id"])
    workbench_workspaces.task_delivery(store, task["id"])
    workbench_policy.peer_verified(store, task["id"], alice["id"])
    workbench_resources.versions(store, project["id"], resource["id"])
    assert _journal(store) == "delete", "读路径把库改成了 WAL"

    for command in (["artifact", "list"], ["proof", task["id"]]):
        result = subprocess.run(
            [*CLI, *command, "--home", str(store.root)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert _journal(store) == "delete", f"CLI {command[0]} 把库改成了 WAL"


def test_rv4_2_store_construction_does_not_write_when_schema_is_current(scene):
    """第四轮：`WorkbenchStore(...)` 每次都走写路径（迁移 + 本地节点）⇒ 读命令也写库。"""
    store, _project, _alice, _bob = scene
    raw = sqlite3.connect(store.db_path, isolation_level=None)
    raw.execute("PRAGMA journal_mode=delete")
    raw.close()

    WorkbenchStore(store.root)  # 结构已就绪 ⇒ 不应写
    assert _journal(store) == "delete", "结构已就绪时构造 store 仍写了库"


def test_rv4_3_enrolled_and_bridge_validate_the_project(scene, tmp_path):
    """第四轮：`enrolled()` 坏 project 返回 {}；`bridge project` 0 候选时 rc=0。"""
    from agent_mailbox.workbench_store import WorkbenchError

    store, _project, _alice, _bob = scene
    with pytest.raises(WorkbenchError) as excinfo:
        workbench_enroll.enrolled(store, "project_nope")
    assert excinfo.value.code == "not_found"

    with pytest.raises(WorkbenchError) as excinfo:
        workbench_bridge.project(store, tmp_path / "mail", "project_nope", apply=True)
    assert excinfo.value.code == "not_found"

    cli = subprocess.run(
        [
            *CLI,
            "bridge",
            "project",
            "--project",
            "project_nope",
            "--mail-root",
            str(tmp_path / "mail"),
            "--home",
            str(store.root),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert cli.returncode == 2 and "Traceback" not in cli.stderr
    # 不能只断言退出码：任何用法错都会 rc=2，必须看到"项目不存在"这条语义
    assert "not_found" in cli.stderr or "找不到" in cli.stderr, cli.stderr

    # 空项目仍可用（第五轮：上一批的强校验曾把 plan("")/onboard("") 误伤成 invalid_field）
    assert workbench_enroll.plan(store, "")["summary"]["installed"] >= 0
    assert workbench_enroll.plan(store, None)["summary"]["installed"] >= 0


def test_rv4_4_wall_shows_peer_verification(scene, tmp_path):
    """第四轮：`wall` 完全不体现同行校验 ⇒ 人读一屏看不出两人规则是否满足。"""
    store, project, alice, bob = scene
    task = create_mail_task(store, project["id"], "活", "说明", alice["id"])
    with store._transaction() as db:
        db.execute("UPDATE tasks SET status='review' WHERE id=?", (task["id"],))
    artifact = tmp_path / "out.md"
    artifact.write_text("交付", encoding="utf-8")
    workbench_proof.build_proof(store, task["id"], artifacts=[str(artifact)])

    workbench_proof.verify_proof(store, task["id"], record=True)  # 系统校验
    row = next(
        r
        for r in workbench_wall.wall(store, project["id"])["waiting_on_human"]
        if r["id"] == task["id"]
    )
    assert row["peer_verified"] is False, "系统校验被当成同行校验"

    workbench_proof.verify_proof(store, task["id"], record=True, verifier_id=bob["id"])
    row = next(
        r
        for r in workbench_wall.wall(store, project["id"])["waiting_on_human"]
        if r["id"] == task["id"]
    )
    assert row["peer_verified"] is True


def test_rv4_5_candidate_fallback_is_not_blocked_by_the_guard(scene, tmp_path, monkeypatch):
    """第四轮：大小闸曾先卡住"最新候选"⇒ 明明有可读的等价副本也拒绝（我引入的回归）。"""
    from agent_mailbox.workbench_mail_tasks import create_mail_task as _new

    store, project, alice, _bob = scene
    monkeypatch.setattr(workbench_artifact, "MAX_ARTIFACT_BYTES", 8)
    first = tmp_path / "first.md"
    first.write_text("12345", encoding="utf-8")
    second = tmp_path / "second.md"
    second.write_text("12345", encoding="utf-8")  # 同内容 ⇒ 同 ref

    task_a = _new(store, project["id"], "先", "说明", alice["id"])
    workbench_proof.build_proof(store, task_a["id"], artifacts=[str(first)])
    task_b = _new(store, project["id"], "后", "说明", alice["id"])
    ref = workbench_proof.build_proof(store, task_b["id"], artifacts=[str(second)])["artifacts"][0][
        "ref"
    ]
    assert len(workbench_artifact._index_raw(store)[ref]["candidates"]) >= 2

    second.unlink()  # 最新副本不在
    result = workbench_artifact.read(store, ref, budget=100)
    assert result["refused"] is None and result["content"] == "12345"


def test_rv4_6_busy_timeout_is_clamped_and_capability_errors_are_not_disk(scene):
    """第四轮：极大的 busy_timeout 可致永久挂起；fts5 缺失被误报为"检查磁盘"。"""
    import sqlite3 as _sqlite3

    from agent_mailbox.workbench_store import _classify_operational_error

    _store, _project, _alice, _bob = scene
    assert WorkbenchStore._busy_timeout_ms() == 10000  # 未设
    import os

    os.environ["AGENT_MAIL_BUSY_TIMEOUT_MS"] = "99999999999999999999"
    try:
        assert WorkbenchStore._busy_timeout_ms() == 600_000, "极大值必须被钳到上限"
    finally:
        del os.environ["AGENT_MAIL_BUSY_TIMEOUT_MS"]
    assert (
        _classify_operational_error(_sqlite3.OperationalError("no such module: fts5")).code
        == "internal_error"
    )
    assert (
        _classify_operational_error(_sqlite3.OperationalError("out of memory")).code
        == "internal_error"
    )
