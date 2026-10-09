"""Regressions for the **ninth** review round (R9b) plus one self-audit finding.

R9b's reviewer finished its main item, confirmed by mutation that three round-8
tests turn red, then crashed while probing — but its report was on disk.

Findings:
* medium: ``budget=1`` on multibyte text silently dropped **every** character
  (nine continuation reads, all ``content=''``, no signal);
* low: a refusal reported ``hash_verified=True`` — claiming verification for bytes
  that were never served;
* self-audit: ``_ready()`` only checked six tables' columns, so a missing
  *auxiliary* table (``task_links``) was neither detected nor repaired and the
  **read** path blew up with "structure mismatch".
"""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from agent_mailbox import workbench_artifact, workbench_proof
from agent_mailbox.workbench_mail_tasks import create_mail_task
from agent_mailbox.workbench_store import (
    SCHEMA_TABLES,
    WorkbenchStore,
    _auxiliary_tables,
)


@pytest.fixture
def scene(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    alice = store.create_employee("Alice", "codex", project["id"])
    return store, project, alice, tmp_path


def _raw(path) -> sqlite3.Connection:
    return sqlite3.connect(path, isolation_level=None)


def _tables(path) -> set[str]:
    db = _raw(path)
    try:
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        db.close()


def _ref_for(store, project, alice, path) -> str:
    task = create_mail_task(store, project["id"], "活", "说明", alice["id"])
    return workbench_proof.build_proof(store, task["id"], artifacts=[str(path)])["artifacts"][0][
        "ref"
    ]


def test_rv9_1_tiny_budget_never_silently_drops_characters(scene):
    """RV9-中：`budget=1` 遇 CJK 时每轮都返回空文本 ⇒ 逐字丢光且无任何信号。"""
    store, project, alice, tmp = scene
    artifact = tmp / "cjk.txt"
    artifact.write_text("中中中", encoding="utf-8")  # 9 字节 / 3 个字符
    ref = _ref_for(store, project, alice, artifact)

    joined, offset, guard = "", 0, 0
    while True:
        result = workbench_artifact.read(store, ref, budget=1, offset=offset)
        joined += result["content"] or ""
        if not result["truncated"]:
            break
        assert result["next_offset"] > offset, "必须前进（否则调用方死循环）"
        offset = result["next_offset"]
        guard += 1
        assert guard < 100, "续读未收敛"
    assert joined == "中中中", f"budget=1 静默丢字：{joined!r}"


def test_rv9_2_refusal_does_not_claim_the_bytes_were_verified(scene, monkeypatch):
    """RV9-低：拒绝时曾报 `hash_verified=True`（对从未交出的字节声称已校验）。"""
    store, project, alice, tmp = scene
    artifact = tmp / "big.txt"
    artifact.write_text("A" * 4000, encoding="utf-8")
    ref = _ref_for(store, project, alice, artifact)

    original = workbench_artifact._read_window

    def rewrite_then_read(fd, offset, length):
        pathlib.Path(artifact).write_text("B" * 4000, encoding="utf-8")
        return original(fd, offset, length)

    monkeypatch.setattr(workbench_artifact, "_read_window", rewrite_then_read)
    result = workbench_artifact.read(store, ref, budget=100)

    assert result["refused"] == "changed_while_reading"
    assert result["content"] is None
    assert result["hash_verified"] is False, "拒绝时不得自称已校验"
    assert result["hash_checked"] is True, "但要说清「查过、且没通过」"


def test_rv9_3_auxiliary_table_is_restored_before_read_paths_use_it(scene):
    """R9 自查：辅助表（模块自建）缺失时，读路径才会炸 —— 应在开库时就修复。"""
    store, project, alice, _tmp = scene
    task = create_mail_task(store, project["id"], "有链接的活", "说明", alice["id"])

    db = _raw(store.db_path)
    db.execute("DROP TABLE task_links")
    db.close()
    assert "task_links" not in _tables(store.db_path)

    assert store._ready() is False, "缺辅助表也必须判定为未就绪"
    reopened = WorkbenchStore(store.root)
    assert "task_links" in _tables(reopened.db_path), "开库时应幂等重建"
    assert reopened.task_detail(task["id"])["task"]["id"] == task["id"]


def test_rv9_4_fresh_store_declares_every_table_it_needs(scene):
    """性质：全新库必须一次性具备全部核心表 + 辅助表（否则升级路径会带着洞）。"""
    store, _project, _alice, _tmp = scene
    required = SCHEMA_TABLES | _auxiliary_tables()
    assert required, "至少要声明一些表"
    present = _tables(store.db_path)
    assert required <= present, f"缺表：{sorted(required - present)}"


def test_rv9_5_bad_recipient_id_is_self_explaining(scene):
    """实测踩坑：收件人 id 少一位时只报「找不到这条记录」，调用方无从下手。

    铁律 2（错误自解释）：必须说清是谁的 id 错了，并给出下一步。
    """
    from agent_mailbox.workbench_mail_sessions import invoke

    store, project, alice, _tmp = scene
    artifactless = create_mail_task(store, project["id"], "活", "说明", alice["id"])
    assert artifactless["id"]

    from agent_mailbox.workbench_mail_sessions import create_session

    session = create_session(store, alice["id"], project["id"], "测试会话")
    with pytest.raises(Exception) as caught:
        invoke(
            store,
            session["token"],
            "message",
            {
                "title": "t",
                "body": "b",
                "recipient_id": "employee_short",
                "reply_to": "",
                "request_id": "",
            },
        )
    message = str(caught.value)
    assert "不存在" in message or "核对" in message, message
    assert "employee_short" in message, message
