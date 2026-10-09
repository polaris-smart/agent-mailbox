"""Regressions for the **third** adversarial review (2026-10-05).

That review's verdict: "0 高 / 4 中 / 8 低；这批修复可用，但有两处『声称修好实际没修全』
（收件人必填、MAX_ARTIFACT_BYTES）和一处产品级承诺漏网（未知 --project）"。
Each case below locks one finding.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys

import pytest

from agent_mailbox import (
    workbench_brief,
    workbench_contacts,
    workbench_lease,
    workbench_ledger,
    workbench_policy,
    workbench_schedule,
    workbench_search,
    workbench_wall,
)
from agent_mailbox.workbench_mail_sessions import create_session, invoke
from agent_mailbox.workbench_store import (
    WorkbenchError,
    WorkbenchStore,
    _classify_operational_error,
)

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
    session = create_session(store, alice["id"], project["id"], "probe")
    return store, project, alice, bob, session


def test_rv3_1_recipient_required_at_the_dispatch_layer(scene):
    """RV3-中1：schema 只保证"参数存在"，真派发层仍把缺参/空串当全项目群发。"""
    store, _project, _alice, bob, session = scene
    for args in (
        {"title": "缺参", "body": "b"},
        {"title": "空串", "body": "b", "recipient_id": ""},
    ):
        with pytest.raises(WorkbenchError) as excinfo:
            invoke(store, session["token"], "message", args)
        assert excinfo.value.code == "MAILBOX_ARGUMENT_DENIED"
    sent = invoke(
        store,
        session["token"],
        "message",
        {"title": "正常", "body": "b", "recipient_id": bob["id"]},
    )
    assert sent["id"] and sent["recipient_id"] == bob["id"]


def test_rv3_2_unknown_project_rejected_on_every_read_path(scene):
    """RV3-中2：policy/search/watch 曾静默按"全局/空"处理（policy 还会返回默认值）。"""
    store, _project, _alice, _bob, _session = scene
    calls = {
        "effective_policy": lambda: workbench_policy.effective_policy(store, "project_nope"),
        "search": lambda: workbench_search.search(store, "x", project_id="project_nope"),
        "due_tasks": lambda: workbench_schedule.due_tasks(store, "project_nope"),
        "wall": lambda: workbench_wall.wall(store, "project_nope"),
        "ledger": lambda: workbench_ledger.ledger(store, "project_nope"),
        "brief": lambda: workbench_brief.brief(store, project_id="project_nope"),
        "contacts": lambda: workbench_contacts.contacts(store, "project_nope", _alice["id"]),
        "lease": lambda: workbench_lease.active_claims(store, "project_nope"),
    }
    for name, call in calls.items():
        with pytest.raises(WorkbenchError) as excinfo:
            call()
        assert excinfo.value.code == "not_found", f"{name} 未校验项目存在"
    for command in (["policy"], ["search", "x"], ["watch", "--timeout", "0"]):
        result = subprocess.run(
            [*CLI, *command, "--project", "project_nope", "--home", str(store.root)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2, f"{command[0]} 对坏 project 应 rc=2"
        assert "Traceback" not in result.stderr


def test_rv3_3_error_codes_are_classified():
    """RV3-中3：非 busy 的 OperationalError 曾一律报"请检查磁盘"。"""
    cases = {
        "attempt to write a readonly database": "readonly_violation",
        "no such table: task": "internal_error",
        "no such column: nope": "internal_error",
        'near "SELEC": syntax error': "internal_error",
        "table task has 5 columns but 4 values were supplied": "internal_error",
        "NOT NULL constraint failed: messages.title": "constraint_error",
        "unable to open database file": "storage_error",
        "disk I/O error": "storage_error",
        "database disk image is malformed": "storage_error",
        "database is locked": "busy",
    }
    for message, expected in cases.items():
        assert _classify_operational_error(sqlite3.OperationalError(message)).code == expected, (
            message
        )


def test_rv3_4_readonly_paths_do_not_change_journal_mode(scene):
    """RV3-低1：只读连接曾执行 PRAGMA journal_mode=WAL ⇒ 非 WAL 库上"只读"也写库头。"""
    store, project, _alice, _bob, _session = scene
    raw = sqlite3.connect(store.db_path, isolation_level=None)
    raw.execute("PRAGMA journal_mode=delete")
    before = raw.execute("PRAGMA journal_mode").fetchone()[0]
    workbench_wall.wall(store, project["id"])
    workbench_ledger.ledger(store, project["id"])
    after = raw.execute("PRAGMA journal_mode").fetchone()[0]
    assert (before, after) == ("delete", "delete"), f"只读路径改了库模式：{before} → {after}"
    raw.close()


def test_rv3_5_readonly_connections_are_query_only(scene):
    """RV3-低7：13 处只读切换此前无一条断言"真的只读"。"""
    store, _project, _alice, _bob, _session = scene
    with store._transaction(readonly=True) as db:
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            db.execute("INSERT INTO projects VALUES('x','y','z','w')")


def test_rv3_6_ledger_and_wall_show_peer_verification_separately(scene, tmp_path):
    """RV3-低3：无 verifier_id 的系统校验不得让账本显示成"同行已验"。"""
    from agent_mailbox import workbench_proof
    from agent_mailbox.workbench_mail_tasks import create_mail_task

    store, project, alice, bob, _session = scene
    task = create_mail_task(store, project["id"], "活", "说明", alice["id"])
    with store._transaction() as db:
        db.execute("UPDATE tasks SET status='review' WHERE id=?", (task["id"],))
    artifact = tmp_path / "out.md"
    artifact.write_text("交付", encoding="utf-8")
    workbench_proof.build_proof(store, task["id"], artifacts=[str(artifact)])

    workbench_proof.verify_proof(store, task["id"], record=True)  # 系统校验（无 verifier）
    assert workbench_policy.peer_verified(store, task["id"], alice["id"]) is False
    row = next(
        r
        for r in workbench_ledger.ledger(store, project["id"])["tasks"]
        if r["task_id"] == task["id"]
    )
    assert row["proof_verdict"] == "verified"
    assert row["peer_verified"] is False, "账本应把「系统校验过」与「同行校验过」分开显示"

    workbench_proof.verify_proof(store, task["id"], record=True, verifier_id=bob["id"])
    row = next(
        r
        for r in workbench_ledger.ledger(store, project["id"])["tasks"]
        if r["task_id"] == task["id"]
    )
    assert row["peer_verified"] is True


def test_rv3_7_candidate_fallback_skips_unusable_candidates(scene, tmp_path, monkeypatch):
    """RV3-中4 + 第四轮回归：候选回退必须**跳过不可用者**，用第一个可校验副本。

    （初版这例只有 1 个候选 ⇒ 从没走回退分支，正好掩盖了我引入的回退回归。）
    """
    from agent_mailbox import workbench_artifact, workbench_proof
    from agent_mailbox.workbench_mail_tasks import create_mail_task

    store, project, alice, _bob, _session = scene
    monkeypatch.setattr(workbench_artifact, "MAX_ARTIFACT_BYTES", 8)
    old_copy = tmp_path / "old.md"
    old_copy.write_text("12345", encoding="utf-8")  # 5B 可读
    new_copy = tmp_path / "new.md"
    new_copy.write_text("12345", encoding="utf-8")  # 同内容 ⇒ 同 ref

    task_a = create_mail_task(store, project["id"], "旧单", "说明", alice["id"])
    workbench_proof.build_proof(store, task_a["id"], artifacts=[str(old_copy)])
    task_b = create_mail_task(store, project["id"], "新单", "说明", alice["id"])
    proof_b = workbench_proof.build_proof(store, task_b["id"], artifacts=[str(new_copy)])
    ref = proof_b["artifacts"][0]["ref"]
    assert len(workbench_artifact._index_raw(store)[ref]["candidates"]) >= 2, "必须先有 ≥2 个候选"

    # ① 最新副本不见了 ⇒ 应回退到旧副本（这就是第四轮抓到的回归）
    new_copy.unlink()
    fallback = workbench_artifact.read(store, ref, budget=100)
    assert fallback["refused"] is None and fallback["content"] == "12345"
    assert fallback["path"] == str(old_copy)

    # ② 最新候选**超限**、旧候选可读 ⇒ 必须跳过超限件、读旧件（原实现直接 too_large）
    #    注意：同内容 ⇒ 同 ref 且同 sha，所以"更大的同 ref 副本"必须**直接构造**证明事件
    import json as _json

    from agent_mailbox.workbench_proof import PROOF_EVENT

    oversized_path = tmp_path / "oversized_same_ref.md"
    oversized_path.write_text("12345" + "z" * 200, encoding="utf-8")  # 超 8B 上限
    new_task = create_mail_task(store, project["id"], "超限单", "说明", alice["id"])
    with store._transaction() as db:
        db.execute(
            "INSERT INTO governance_events(id,employee_id,project_id,task_id,type,actor,reason,payload,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "far_oversized",
                None,
                project["id"],
                new_task["id"],
                PROOF_EVENT,
                "system",
                "同 ref 的超限副本",
                _json.dumps(
                    {
                        "task_id": new_task["id"],
                        "artifacts": [
                            {
                                "ref": ref,
                                "sha256": workbench_artifact._index_raw(store)[ref]["sha256"],
                                "bytes": 205,
                                "path": str(oversized_path),
                                "resolved": str(oversized_path),
                            }
                        ],
                    }
                ),
                "2099-01-01T00:00:00+00:00",  # 时间戳更晚 ⇒ 成为"最新候选"
            ),
        )
    raw = workbench_artifact._index_raw(store)[ref]
    assert len(raw["candidates"]) >= 2 and raw["candidates"][0]["path"] == str(oversized_path)

    result = workbench_artifact.read(store, ref, budget=100)
    assert result["refused"] is None, "有可读旧副本时不该因最新件超限而拒绝"
    assert result["content"] == "12345"
    assert result["is_newest"] is False and result["used_candidate_index"] >= 1
    assert any(item["reason"] == "too_large" for item in result["skipped"]), result["skipped"]


def test_rv3_8_fifo_artifact_does_not_hang(tmp_path):
    """RV3-低6：把具名管道当产出物记录会让 artifact_ref 永久挂死。"""
    import os
    import subprocess
    import sys

    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    script = (
        f"import sys; sys.path.insert(0, {str(REPO / 'src')!r});"
        "from agent_mailbox.workbench_proof import artifact_ref;"
        f"print(artifact_ref({str(fifo)!r}).get('error'))"
    )
    finished = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10, check=False
    )
    assert finished.returncode == 0, finished.stderr
    assert "not_a_regular_file" in finished.stdout


def test_rv3_9_large_artifact_hashes_in_chunks(scene, tmp_path, monkeypatch):
    """RV3-中4：分块哈希必须**不依赖整份读**（仅断言 bytes/sha 的话，修前实现也能过）。"""
    import pathlib as _pathlib

    from agent_mailbox import workbench_proof

    _store, _project, _alice, _bob, _session = scene
    big = tmp_path / "big.bin"
    with big.open("wb") as handle:
        handle.truncate(70 * 1024 * 1024)  # 70MB 稀疏文件

    def forbidden(*args, **kwargs):  # 整份读就该失败
        raise AssertionError("artifact_ref 不该调用 read_bytes（应分块哈希）")

    monkeypatch.setattr(_pathlib.Path, "read_bytes", forbidden)
    info = workbench_proof.artifact_ref(str(big))
    assert info["bytes"] == 70 * 1024 * 1024 and info["sha256"]


def test_rv3_10_gate_is_rechecked_inside_the_write_transaction(scene, tmp_path, monkeypatch):
    """RV3-低4：门在写事务外算过一次 ⇒ 门后策略翻成 required 仍会合入。"""
    from agent_mailbox import workbench_policy, workbench_proof
    from agent_mailbox.workbench_mail_tasks import create_mail_task
    from agent_mailbox.workbench_workspaces import apply_delivery

    store, project, alice, _bob, _session = scene
    task = create_mail_task(store, project["id"], "写能力交付", "说明", alice["id"])
    with store._transaction() as db:
        db.execute("UPDATE tasks SET status='review' WHERE id=?", (task["id"],))
    artifact = tmp_path / "out.md"
    artifact.write_text("交付", encoding="utf-8")
    workbench_proof.build_proof(store, task["id"], artifacts=[str(artifact)])
    workbench_policy.set_policy(
        store, {"peer_review": "required_for_write"}, project_id=project["id"]
    )

    # 模拟"门读到 off 之后策略才翻转"：门被绕过 ⇒ 写事务内的复检必须拦下
    monkeypatch.setattr(
        workbench_policy, "effective_policy", lambda store, project_id=None: {"peer_review": "off"}
    )
    with pytest.raises(WorkbenchError) as excinfo:
        apply_delivery(store, task["id"])
    assert excinfo.value.code == "PEER_REVIEW_REQUIRED", "门后策略翻转未被复检拦下"


def test_rv3_11_busy_message_mentions_nested_transactions(scene):
    """RV3-低2：嵌套调用只会得到"工作数据被占用"，没提"已有事务"这条真因。"""
    from agent_mailbox.workbench_store import _classify_operational_error

    message = _classify_operational_error(sqlite3.OperationalError("database is locked")).message
    assert "事务" in message


def test_rv3_12_integrity_errors_are_constraint_errors(scene):
    """RV3-中3 补：NOT NULL/UNIQUE 违约抛的是 IntegrityError，曾落到 catch-all ⇒ storage_error。"""
    store, project, _alice, _bob, _session = scene
    with (
        pytest.raises(WorkbenchError) as excinfo,
        store._transaction() as db,
    ):
        db.execute(
            "INSERT INTO messages(id,project_id,title) VALUES('m1',?,NULL)", (project["id"],)
        )
    assert excinfo.value.code == "constraint_error", excinfo.value.code
