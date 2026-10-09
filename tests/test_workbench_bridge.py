"""One-way bridge: legacy letters → task cards (roadmap T7)."""

from __future__ import annotations

import json

import pytest

from agent_mailbox import workbench_bridge as wb
from agent_mailbox.workbench_store import WorkbenchStore


@pytest.fixture
def scene(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    alice = store.create_employee("Alice", "codex", project["id"])

    mail = tmp_path / "mail"
    (mail / "inbox" / "Alice").mkdir(parents=True)
    letters = {
        "L-1": {
            "subject": "【任务书】把日志统计做完",
            "from": "HS",
            "to": "Alice",
            "body": "正文一",
            "status": "pending",
        },
        "L-2": {
            "subject": "【思路参考·非派工】竞品情报",
            "from": "HS",
            "to": "Alice",
            "body": "正文二",
            "status": "pending",
        },
        "L-3": {
            "subject": "Re: 【任务书】回执",
            "from": "HS",
            "to": "Alice",
            "body": "回声",
            "reply_to": "L-1",
        },
        "L-4": {"subject": "【派工】清理临时目录", "from": "HS", "to": "Nobody", "body": "正文四"},
        "L-5": {"subject": "闲聊", "from": "WB", "to": "Alice", "body": "无标记"},
    }
    for letter_id, payload in letters.items():
        (mail / "inbox" / "Alice" / f"{letter_id}.json").write_text(
            json.dumps({"id": letter_id, **payload}, ensure_ascii=False), encoding="utf-8"
        )
    return store, project, alice, mail


def _snapshot(store):
    with store._transaction() as db:
        return {
            "tasks": db.execute("SELECT count(*) AS c FROM tasks").fetchone()["c"],
            "governance": db.execute("SELECT count(*) AS c FROM governance_events").fetchone()["c"],
        }


def _mail_fingerprint(mail):
    return {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(mail.glob("inbox/*/*.json"))
    }


def test_classify_is_conservative(scene):
    _store, _project, _alice, _mail = scene
    assert wb.classify({"subject": "【任务书】做某事"})[0] is True
    assert wb.classify({"subject": "【派工】清理"})[0] is True
    ok, reason = wb.classify({"subject": "【思路参考·非派工】情报"})
    assert ok is False and reason.startswith("marked_not_a_task")
    assert wb.classify({"subject": "Re: 【任务书】回执"})[1] == "reply_not_a_new_order"
    assert wb.classify({"subject": "闲聊"})[1] == "no_task_marker"
    assert wb.classify({"subject": "【任务书】已做完", "status": "done"})[1] == "already_done"
    assert wb.classify({})[1] == "empty_subject"


def test_scan_separates_candidates_and_reasons(scene):
    _store, _project, _alice, mail = scene
    outlook = wb.scan(mail)
    assert outlook["total"] == 5
    assert [row["letter_id"] for row in outlook["candidates"]] == ["L-1", "L-4"]
    reasons = {row["letter_id"]: row["reason"] for row in outlook["skipped"]}
    assert reasons["L-2"].startswith("marked_not_a_task")
    assert reasons["L-3"] == "reply_not_a_new_order"
    assert reasons["L-5"] == "no_task_marker"


def test_projection_is_dry_by_default(scene):
    store, project, _alice, mail = scene
    before = _snapshot(store)
    outline = wb.project(store, mail, project["id"])
    assert outline["dry_run"] is True
    assert [step["action"] for step in outline["steps"]] == ["would_project", "skipped_no_employee"]
    assert _snapshot(store) == before, "演练阶段不允许写入"


def test_projection_creates_a_mailbox_task_and_is_idempotent(scene):
    store, project, alice, mail = scene
    applied = wb.project(store, mail, project["id"], apply=True)
    steps = {step["letter_id"]: step for step in applied["steps"]}
    assert steps["L-1"]["action"] == "projected"
    assert steps["L-4"]["action"] == "skipped_no_employee"  # 收件人不是项目成员

    task_id = steps["L-1"]["task_id"]
    with store._transaction() as db:
        task = dict(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
    assert task["execution_mode"] == "mailbox" and task["assignee_id"] == alice["id"]
    assert "L-1" in task["prompt"]  # 可追溯回原信

    again = wb.project(store, mail, project["id"], apply=True)
    assert {step["letter_id"]: step["action"] for step in again["steps"]}[
        "L-1"
    ] == "already_projected"
    with store._transaction() as db:
        assert db.execute("SELECT count(*) AS c FROM tasks").fetchone()["c"] == 1  # 不重复建卡


def test_bridge_is_one_way_and_never_touches_the_legacy_store(scene):
    """'禁止双写'的机械保证：投影前后，旧信文件内容与修改时间都不变。"""
    store, project, _alice, mail = scene
    before = _mail_fingerprint(mail)
    wb.project(store, mail, project["id"], apply=True)
    assert _mail_fingerprint(mail) == before, "桥接写回了 v0.7 信件（双写）"
    assert wb.status(store)["projected"] == 1


def test_assignee_map_makes_a_legacy_name_enrollable(scene):
    store, project, alice, mail = scene
    applied = wb.project(
        store, mail, project["id"], apply=True, assignee_map={"Nobody": alice["id"]}
    )
    steps = {step["letter_id"]: step["action"] for step in applied["steps"]}
    assert steps["L-4"] == "projected"  # 显式映射后才可投
    assert applied["counts"]["projected"] == 2
