"""Regressions for the **seventh** adversarial review (2026-10-05).

That reviewer also crashed, but again wrote findings to ``/tmp/amr7/report.md``
first (369 lines, surfaces 1–6) — the crash-safe rule keeps paying off. Its
findings, all fixed here:

* high: ``PRAGMA journal_mode=WAL`` ran *before* migration validation, so a failed
  migration permanently flipped the DB to WAL (rollback cannot undo the header);
* medium: ``_ready()`` ignored missing columns, so repair never ran and public APIs
  raised misleading errors (``snapshot()`` raised a bare ``IndexError``);
* medium: ``wall`` rows lacked proof fields — "no proof" and "system-verified, peer
  pending" rendered identically, though they need chasing different people;
* medium: ``onboard(None/"")`` promised ``would_enroll`` while ``apply=True`` failed,
  and ``plan(None)`` emitted a copy-paste command that always fails;
* medium: a typo'd ``--mail-root`` silently reported "0 letters" with rc=0;
* medium: ``test_rv6_3`` guarded only the reported number, not ``content`` (still 3× budget);
* low: env ``busy_timeout`` reached only ``_connection()``; lossy content; debug-list
  dead entries; detector blind spots; ``offset_ignored`` one-sided assertion.
"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import subprocess
import sys

import pytest

from agent_mailbox import workbench_artifact, workbench_bridge, workbench_proof
from agent_mailbox.workbench_mail_tasks import create_mail_task
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore

REPO = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def scene(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    alice = store.create_employee("Alice", "codex", project["id"])
    bob = store.create_employee("Bob", "codex", project["id"])
    return store, project, alice, bob, tmp_path


def _raw(path) -> sqlite3.Connection:
    return sqlite3.connect(path, isolation_level=None)


def _journal(path) -> str:
    db = _raw(path)
    try:
        return db.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        db.close()


def test_rv7_1_failed_migration_leaves_the_journal_mode_alone(scene):
    """RV7-高：迁移失败曾被永久改成 WAL（rollback 撤不回库头）。"""
    store, _project, _alice, _bob, _tmp = scene
    db = _raw(store.db_path)
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute(
        "INSERT INTO messages(id,project_id,title,body,thread_id,request_work,created_at) "
        "VALUES('m_bad','project_missing','t','b','th',0,'2026-01-01T00:00:00+00:00')"
    )
    db.execute("PRAGMA user_version=3")  # 触发迁移
    db.execute("PRAGMA journal_mode=delete")
    db.close()
    assert _journal(store.db_path) == "delete"

    with pytest.raises(WorkbenchError) as caught:
        WorkbenchStore(store.root)
    assert caught.value.code == "migration_failed"
    # 关键不变量：失败路径不得改库头
    assert _journal(store.db_path) == "delete"
    header = pathlib.Path(store.db_path).read_bytes()[18:20]
    assert header == b"\x01\x01", f"库头被改成 WAL：{header!r}"


def test_rv7_2_missing_columns_are_repaired(scene):
    """RV7-中：缺列的库此前既不迁移也不报错，之后 snapshot() 抛裸 IndexError。"""
    store, project, _alice, _bob, _tmp = scene
    db = _raw(store.db_path)
    db.execute("ALTER TABLE memberships DROP COLUMN role")  # 制造「缺列但版本号已最新」
    db.close()

    reopened = WorkbenchStore(store.root)  # 应补回 role 列
    columns = {
        row[1]
        for row in _raw(reopened.db_path).execute("PRAGMA table_info(memberships)").fetchall()
    }
    assert "role" in columns, columns
    assert reopened.snapshot()["employees"]  # 不再抛裸 IndexError
    assert reopened.project_context(project["id"])["project"]["id"] == project["id"]


def test_rv7_3_mail_root_must_exist(scene):
    """RV7-中：打错的 --mail-root 曾静默报"0 封"并 rc=0（--apply 也一样）。"""
    store, _project, _alice, _bob, tmp = scene
    with pytest.raises(WorkbenchError) as caught:
        workbench_bridge.scan(tmp / "no-such-root")
    assert caught.value.code == "invalid_path"

    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_mailbox",
            "bridge",
            "scan",
            "--mail-root",
            str(tmp / "no-such-root"),
            "--home",
            str(store.root),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert cli.returncode != 0, cli.stdout
    assert "invalid_path" in cli.stderr or "信件目录" in cli.stderr, cli.stderr


def test_rv7_4_onboard_requires_a_project(scene):
    """RV7-中：干跑曾承诺 would_enroll，apply=True 立刻 invalid_field。"""
    from agent_mailbox import workbench_enroll

    store, _project, _alice, _bob, _tmp = scene
    for empty in (None, ""):
        with pytest.raises(WorkbenchError) as caught:
            workbench_enroll.onboard(store, empty)
        assert caught.value.code == "invalid_field"

    plan = workbench_enroll.plan(store, None)
    # 无 agent CLI 的机器上 suggestions 为空 ⇒ 该用例要 hermetic（别用索引断言）
    for suggestion in plan["suggestions"]:
        assert suggestion["command"] is None, "无项目时不该给出必然失败的命令"
        assert suggestion["needs_project"] is True


def test_rv7_5_wall_separates_no_proof_from_peer_pending(scene):
    """RV7-中：墙把"等交付人交证明"与"等同行复验"显示成同一句，该催的人不同。"""
    from agent_mailbox import workbench_wall

    store, project, alice, bob, tmp = scene
    no_proof = create_mail_task(store, project["id"], "无证明", "说明", alice["id"])
    proof_only = create_mail_task(store, project["id"], "只有证明", "说明", alice["id"])
    verified = create_mail_task(store, project["id"], "同行已验", "说明", alice["id"])
    for task in (no_proof, proof_only, verified):
        with store._transaction() as db:
            db.execute("UPDATE tasks SET status='review' WHERE id=?", (task["id"],))

    artifact = tmp / "out.md"
    artifact.write_text("交付", encoding="utf-8")
    for task in (proof_only, verified):
        workbench_proof.build_proof(store, task["id"], artifacts=[str(artifact)])
    # 同行复核要显式给复核人（成员、且非交付人）——这是既定契约
    workbench_proof.verify_proof(store, verified["id"], record=True, verifier_id=bob["id"])

    rows = {row["id"]: row for row in workbench_wall.wall(store, project["id"])["waiting_on_human"]}
    assert rows[no_proof["id"]]["has_proof"] is False
    assert rows[proof_only["id"]]["has_proof"] is True
    assert rows[proof_only["id"]]["peer_verified"] is False
    assert rows[verified["id"]]["peer_verified"] is True
    # 三者必须能彼此区分（人读据此判断该催谁）
    states = {
        (rows[t["id"]]["has_proof"], bool(rows[t["id"]]["peer_verified"]))
        for t in (no_proof, proof_only, verified)
    }
    assert states == {(False, False), (True, False), (True, True)}, states


def test_rv7_6_lossy_content_really_respects_the_budget(scene):
    """RV7-中：只有回报字段守预算，content 仍可 3× 预算（替换符膨胀）。"""
    store, project, alice, _bob, tmp = scene
    artifact = tmp / "bad.bin"
    artifact.write_bytes(b"A" + b"\xff" * 20 + b"B")
    task = create_mail_task(store, project["id"], "坏字节", "说明", alice["id"])
    ref = workbench_proof.build_proof(store, task["id"], artifacts=[str(artifact)])["artifacts"][0][
        "ref"
    ]

    result = workbench_artifact.read(store, ref, budget=22)
    assert result["lossy"] is True
    assert len(result["content"].encode("utf-8")) <= 22, result["content_bytes"]
    assert result["returned_bytes"] <= 22


def test_rv7_7_ledger_limit_is_consistent_between_json_and_human(scene):
    """RV7-低：`--limit 0` 人读 0 行、--json 1 行（内部钳到 1，打印端又按原值切）。"""
    store, project, alice, _bob, _tmp = scene
    for index in range(3):
        create_mail_task(store, project["id"], f"活{index}", "说明", alice["id"])
    binary = [sys.executable, "-m", "agent_mailbox"]

    as_json = subprocess.run(
        [*binary, "ledger", "--json", "--limit", "0", "--home", str(store.root)],
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(as_json.stdout)
    human = subprocess.run(
        [*binary, "ledger", "--limit", "0", "--home", str(store.root)],
        capture_output=True,
        text=True,
        check=False,
    )
    human_rows = [line for line in human.stdout.splitlines() if line.strip().startswith("[")]
    assert len(human_rows) == len(payload["tasks"]), (human_rows, payload["tasks"])


def test_rv7_8_bridge_limit_zero_means_no_candidates(scene):
    """RV7-低：`limit=0` 曾是"不限量"，负值还会截掉最后一条。"""
    store, project, _alice, _bob, tmp = scene
    mail = tmp / "mail" / "inbox" / "Codex"
    mail.mkdir(parents=True)
    (mail / "L-1.json").write_text(
        json.dumps(
            {"id": "L-1", "subject": "【任务书】x", "from": "HS", "to": "Alice", "body": "b"}
        ),
        encoding="utf-8",
    )
    outline = workbench_bridge.project(store, tmp / "mail", project["id"], apply=False)
    assert outline["counts"]["would_project"] >= 1

    limited = workbench_bridge.project(store, tmp / "mail", project["id"], apply=False, limit=0)
    assert limited["counts"].get("would_project", 0) == 0, limited["counts"]


def test_rv7_9_read_apis_do_not_write_even_when_a_column_was_missing(scene):
    """RV7 初版是空测试（从不造缺列、也不重开 store）；这里按声称的语义真造一遍。"""
    store, project, _alice, _bob, _tmp = scene
    db = _raw(store.db_path)
    db.execute("ALTER TABLE memberships DROP COLUMN role")  # 真的制造"缺列"
    db.close()

    reopened = WorkbenchStore(store.root)  # 真的重新构造（构造期会修复）
    db = _raw(reopened.db_path)
    db.execute("PRAGMA journal_mode=delete")
    db.close()

    reopened.snapshot()
    reopened.project_context(project["id"])
    assert _journal(reopened.db_path) == "delete"
    assert not os.path.exists(str(reopened.db_path) + "-wal")
