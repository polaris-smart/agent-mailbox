"""Artifact reads honour a byte budget and refuse tampered evidence (roadmap T6)."""

from __future__ import annotations

import pytest

from agent_mailbox import workbench_artifact as wa
from agent_mailbox import workbench_proof as wp
from agent_mailbox.workbench_store import WorkbenchStore


@pytest.fixture
def scene(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    alice = store.create_employee("Alice", "codex", project["id"])
    request = store.send_message(
        project["id"], "出一份报告", "把结论写清楚", recipient_id=alice["id"], request_work=True
    )
    with store._transaction() as db:
        task_id = db.execute(
            "SELECT id FROM tasks WHERE request_message_id = ?", (request["id"],)
        ).fetchone()["id"]
    store.claim_task(store.local_node()["id"])
    store.set_status(task_id, "running")
    store.finish_task(task_id, "review", "报告已产出")
    body = "\n".join(f"第 {i} 行：结论 {i}" for i in range(1, 41))
    artifact = tmp_path / "report.md"
    artifact.write_text(body, encoding="utf-8")
    proof = wp.build_proof(store, task_id, artifacts=[str(artifact)])
    ref = proof["artifacts"][0]["ref"]
    return store, task_id, artifact, ref, body


def test_index_and_resolve(scene):
    store, task_id, artifact, ref, _body = scene
    assert ref.startswith("art:") and len(ref) == 20
    entry = wa.resolve(store, ref)
    assert entry["task_id"] == task_id and entry["bytes"] == artifact.stat().st_size
    assert [row["ref"] for row in wa.list_refs(store, task_id=task_id)] == [ref]
    with pytest.raises(ValueError):
        wa.resolve(store, "art:deadbeefdeadbeef")
    with pytest.raises(ValueError):
        wa.resolve(store, "not-a-ref")


def test_budget_is_enforced_and_truncation_is_reported(scene):
    store, _task, _artifact, ref, _body = scene
    small = wa.read(store, ref, budget=50)
    # 契约：returned_bytes ≤ budget + 3（为补齐一个完整字符最多多读 3 字节），
    # 但**不允许**替换符导致的成倍膨胀（那条由 budget_extended_bytes / lossy 反映）
    assert small["returned_bytes"] <= 53, small["returned_bytes"]
    assert small.get("budget_extended_bytes", 0) <= 3
    assert small["truncated"] is True and small["next_offset"] >= small["returned_bytes"]
    assert small["hash_verified"] is True

    rest = wa.read(store, ref, budget=50, offset=small["next_offset"])
    assert rest["content"] and rest["content"] != small["content"]  # 可续读

    whole = wa.read(store, ref, budget=4096)
    assert whole["truncated"] is False and whole["returned_bytes"] == whole["total_bytes"]


def test_hard_cap_and_validation(scene):
    store, _task, _artifact, ref, _body = scene
    with pytest.raises(ValueError):
        wa.read(store, ref, budget=wa.MAX_BUDGET + 1)  # 防灌爆上下文
    with pytest.raises(ValueError):
        wa.read(store, ref, budget=0)
    with pytest.raises(ValueError):
        wa.read(store, ref, projection="everything")
    with pytest.raises(ValueError):
        wa.read(store, ref, offset=10**9)


def test_tampered_artifact_returns_no_content(scene):
    store, _task, artifact, ref, _body = scene
    artifact.write_text("被改过了", encoding="utf-8")
    result = wa.read(store, ref, budget=1000)
    assert result["hash_verified"] is False and result["refused"] == "hash_mismatch"
    assert result["content"] is None and result["returned_bytes"] == 0  # 不给内容


def test_projection_head_and_json_keys(scene):
    store, _task, artifact, ref, _body = scene
    head = wa.read(store, ref, budget=500, projection="head")
    assert head["content"] == "第 1 行：结论 1"  # 只给首行

    artifact.write_text('{"a": 1, "b": {"c": [1, 2, 3]}}', encoding="utf-8")
    wp.build_proof(store, _task, artifacts=[str(artifact)])  # 重新登记新哈希
    import hashlib

    new_ref = "art:" + hashlib.sha256(artifact.read_bytes()).hexdigest()[:16]  # 由内容算，确定性
    keys = wa.read(store, new_ref, budget=500, projection="json_keys")
    assert keys["content"] == {"a": "int", "b": {"c": "list[3]"}}  # 只给结构不给值
    assert "1" not in str(keys["content"])


def test_reading_is_read_only(scene):
    store, _task, _artifact, ref, _body = scene
    with store._transaction() as db:
        before = (
            db.execute("SELECT count(*) AS c FROM governance_events").fetchone()["c"],
            db.execute("SELECT count(*) AS c FROM tasks").fetchone()["c"],
        )
    wa.read(store, ref, budget=100)
    wa.list_refs(store)
    with store._transaction() as db:
        after = (
            db.execute("SELECT count(*) AS c FROM governance_events").fetchone()["c"],
            db.execute("SELECT count(*) AS c FROM tasks").fetchone()["c"],
        )
    assert before == after
