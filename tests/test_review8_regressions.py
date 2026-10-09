"""Regressions for the **eighth** adversarial review (2026-10-05).

Round 8 finished (169-line report on disk) and found the sharpest bug of the run —
**mine, introduced by round 7**:

* high: ``consumed_shapes_read()`` gated the post-read re-hash *backwards*. Files
  larger than ``budget`` were **never** re-verified, so an in-place rewrite during
  the window read was served as ``hash_verified=True``. ``test_rv6_7`` used a
  48-byte file with ``budget=200`` (whole file read) so it passed under both the
  correct and the broken implementation — zero coverage.
* medium: a current ``user_version`` with a **missing table** was neither repaired
  nor reported; the store just silently flipped ``delete → wal``.
* medium: column repair was not atomic (autocommit per ``ALTER``) ⇒ half-repaired
  state persisted, with an unactionable error.
* medium: ``rv7_9`` was an empty test (never dropped a column, never reopened).
"""

from __future__ import annotations

import os
import pathlib
import sqlite3

import pytest

from agent_mailbox import workbench_artifact, workbench_proof
from agent_mailbox.workbench_mail_tasks import create_mail_task
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


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


def _journal(path) -> str:
    db = _raw(path)
    try:
        return db.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        db.close()


def _tables(path) -> set[str]:
    db = _raw(path)
    try:
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        db.close()


def test_rv8_1_partial_window_rewrite_is_refused(scene, monkeypatch):
    """RV8-高：>budget 的文件被就地改写时，曾返回**从未校验**的字节却自称 hash_verified=True。

    rv6_7 用的是"整份读完"的小文件，对新旧实现都绿 ⇒ 这条专门覆盖**部分窗口**。
    """
    store, project, alice, tmp = scene
    artifact = tmp / "big.txt"
    artifact.write_text("A" * 4000, encoding="utf-8")
    task = create_mail_task(store, project["id"], "大件", "说明", alice["id"])
    ref = workbench_proof.build_proof(store, task["id"], artifacts=[str(artifact)])["artifacts"][0][
        "ref"
    ]

    original = workbench_artifact._read_window

    def rewrite_then_read(fd, offset, length):
        pathlib.Path(artifact).write_text("B" * 4000, encoding="utf-8")  # 等长就地改写
        return original(fd, offset, length)

    monkeypatch.setattr(workbench_artifact, "_read_window", rewrite_then_read)
    result = workbench_artifact.read(store, ref, budget=100)

    assert result["refused"] in {"changed_while_reading", "size_changed_while_reading"}, result
    assert result["content"] is None, "被改写的字节一个都不能给"


def test_rv8_2_missing_table_is_repaired(scene):
    """RV8-中：版本号已最新但缺表 ⇒ 此前既不补表也不报错，只把库头翻成 WAL。"""
    store, _project, _alice, _tmp = scene
    db = _raw(store.db_path)
    db.execute("DROP TABLE permissions")
    db.close()
    assert "permissions" not in _tables(store.db_path)

    reopened = WorkbenchStore(store.root)
    assert "permissions" in _tables(reopened.db_path), "缺表必须被幂等重建"


def test_rv8_3_repair_is_all_or_nothing(scene):
    """RV8-中：补列曾是逐条 autocommit ⇒ 中途失败留下半修状态。"""
    store, _project, _alice, _tmp = scene
    db = _raw(store.db_path)
    db.execute("ALTER TABLE tasks DROP COLUMN model")
    # 把 permissions 换成**缺少必填列**的视图：补列到它时必然报 "cannot add a column to a view"
    db.execute("ALTER TABLE permissions RENAME TO permissions_backup_old")
    db.execute("CREATE VIEW permissions AS SELECT id FROM permissions_backup_old")
    db.close()

    with pytest.raises(WorkbenchError) as caught:
        WorkbenchStore(store.root)
    assert caught.value.code in {"internal_error", "storage_error"}, caught.value.code

    columns = {row[1] for row in _raw(store.db_path).execute("PRAGMA table_info(tasks)")}
    assert "model" not in columns, "失败的修复不得留下半修状态（应先补的列必须被回滚）"


def test_rv8_4_read_apis_do_not_write_after_construction_repair(scene):
    """RV8-中：rv7_9 曾是空测试 —— 这里真的造缺列、真的重开 store，再验读 API 不写库。"""
    store, project, _alice, _tmp = scene
    db = _raw(store.db_path)
    db.execute("ALTER TABLE memberships DROP COLUMN role")
    db.close()

    reopened = WorkbenchStore(store.root)  # 构造期完成修复（这一步可以写）
    db = _raw(reopened.db_path)
    db.execute("PRAGMA journal_mode=delete")
    db.close()

    reopened.snapshot()
    reopened.project_context(project["id"])
    assert _journal(reopened.db_path) == "delete", "读 API 不得切换 journal_mode"
    assert not os.path.exists(str(reopened.db_path) + "-wal")


def test_rv8_5_bridge_negative_limit_is_rejected(scene):
    """RV8-低：负 `--limit` 曾静默等价于 0（既不报错也无文档）。"""
    import json as _json

    from agent_mailbox import workbench_bridge

    store, project, _alice, tmp = scene
    mail = tmp / "mail" / "inbox" / "Codex"
    mail.mkdir(parents=True)
    (mail / "L-1.json").write_text(
        _json.dumps(
            {"id": "L-1", "subject": "【任务书】x", "from": "HS", "to": "Alice", "body": "b"}
        ),
        encoding="utf-8",
    )
    with pytest.raises(WorkbenchError) as caught:
        workbench_bridge.project(store, tmp / "mail", project["id"], apply=False, limit=-1)
    assert caught.value.code == "invalid_field"


def test_rv8_6_schema_is_splittable():
    """`_ensure_schema` 按 `;` 拆 SCHEMA：加了触发器/含分号的字符串就会静默拆坏 —— 守住它。"""
    from agent_mailbox.workbench_store import SCHEMA

    statements = [part.strip() for part in SCHEMA.split(";") if part.strip()]
    assert statements, "SCHEMA 不该为空"
    for statement in statements:
        upper = statement.upper()
        assert "TRIGGER" not in upper, "触发器含分号，会被 `;` 拆分破坏"
        assert statement.count("'") % 2 == 0, f"语句内引号不成对，可能含分号：{statement[:60]}"
        assert upper.startswith(("CREATE", "INSERT", "PRAGMA", "WITH")), statement[:60]

    # 逐条执行必须与 executescript 产出**完全一致**的对象集合
    import sqlite3 as _sqlite3
    import tempfile as _tempfile

    with _tempfile.TemporaryDirectory() as root:
        one = _sqlite3.connect(f"{root}/one.db")
        one.executescript(SCHEMA)
        by_one = {row[0] for row in one.execute("SELECT name FROM sqlite_master")}
        two = _sqlite3.connect(f"{root}/two.db")
        for statement in statements:
            two.execute(statement)
        by_two = {row[0] for row in two.execute("SELECT name FROM sqlite_master")}
    assert by_one == by_two, "逐条执行与 executescript 不等价 ⇒ 拆分不安全"
