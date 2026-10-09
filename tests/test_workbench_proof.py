"""Delivery proof: handoff sheet + evidence, judgeable without chat (roadmap T12)."""

from __future__ import annotations

import pytest

from agent_mailbox import workbench_proof as wp
from agent_mailbox import workbench_wall as ww
from agent_mailbox.workbench_store import WorkbenchStore


@pytest.fixture
def scene(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    alice = store.create_employee("Alice", "codex", project["id"])
    request = store.send_message(
        project["id"],
        "写一封体检单",
        "把 11 项体检做完",
        recipient_id=alice["id"],
        request_work=True,
    )
    with store._transaction() as db:
        task_id = db.execute(
            "SELECT id FROM tasks WHERE request_message_id = ?", (request["id"],)
        ).fetchone()["id"]
    store.claim_task(store.local_node()["id"])
    store.set_status(task_id, "running")
    store.finish_task(task_id, "review", "体检单已产出，见 out/report.md")
    (tmp_path / "out").mkdir()
    artifact = tmp_path / "out" / "report.md"
    artifact.write_text("# 体检单\n11/11 PASS\n", encoding="utf-8")
    return store, project, alice, task_id, request, tmp_path, artifact


def test_artifact_ref_is_a_token_not_content(scene):
    _store, _project, _alice, _task, _request, _tmp, artifact = scene
    ref = wp.artifact_ref("out/report.md", root=artifact.parents[1])
    assert ref["ref"].startswith("art:") and len(ref["ref"]) == 4 + 16
    assert ref["bytes"] == artifact.stat().st_size
    assert "content" not in ref and "body" not in ref  # 只给引用，不给正文
    missing = wp.artifact_ref("out/nope.md", root=artifact.parents[1])
    assert missing["sha256"] is None and "error" in missing


def test_build_proof_is_recorded_and_visible_in_the_audit_chain(scene):
    store, _project, _alice, task_id, request, tmp, _artifact = scene
    proof = wp.build_proof(
        store,
        task_id,
        artifacts=["out/report.md"],
        root=tmp,
        criteria=[{"criterion": "11 项体检全列", "self_check": "pass", "evidence": ["art:abc123"]}],
        checks=[{"name": "pytest", "status": "pass", "detail": "全绿"}],
    )
    assert proof["task_id"] == task_id
    assert proof["proof_sha256"]
    assert wp.latest_proof(store, task_id)["proof_sha256"] == proof["proof_sha256"]

    chain = ww.audit_chain(store, request["id"])
    assert ww.FOLD_EVENT not in [e["type"] for e in chain["governance"]]  # 无关事件不混入
    assert wp.PROOF_EVENT in [e["type"] for e in chain["governance"]]  # 交付证明进了审计链


def test_verify_detects_tampering_and_absence(scene):
    store, _project, _alice, task_id, _request, tmp, artifact = scene
    wp.build_proof(store, task_id, artifacts=["out/report.md"], root=tmp)

    ok = wp.verify_proof(store, task_id)
    assert ok["verdict"] == "verified" and ok["checked"] == 1

    artifact.write_text("被改过了", encoding="utf-8")
    tampered = wp.verify_proof(store, task_id, record=True)
    assert tampered["verdict"] == "mismatch" and tampered["mismatch"] == 1
    events = [e for e in store.governance_events() if e["type"] == wp.VERIFY_EVENT]
    assert events and events[-1]["payload"]["verdict"] == "mismatch"

    artifact.unlink()
    gone = wp.verify_proof(store, task_id)
    assert gone["verdict"] == "incomplete" and gone["missing"] == 1


def test_verify_reports_no_proof_without_crashing(scene):
    store, _project, _alice, task_id, _request, _tmp, _artifact = scene
    result = wp.verify_proof(store, task_id)
    assert result == {
        "task_id": task_id,
        "verdict": "no_proof",
        "items": [],
        "checked": 0,
        "mismatch": 0,
        "missing": 0,
    }


def test_failed_self_check_makes_the_verdict_mismatch(scene):
    store, _project, _alice, task_id, _request, tmp, _artifact = scene
    wp.build_proof(
        store,
        task_id,
        artifacts=["out/report.md"],
        root=tmp,
        checks=[{"name": "pytest", "status": "fail", "detail": "1 failed"}],
    )
    assert wp.verify_proof(store, task_id)["verdict"] == "mismatch"


def test_verdict_renders_from_the_proof_alone(scene):
    """判据形状：只看交接单+证据即可判定 —— 渲染函数**不接 store、不接聊天**。"""
    store, _project, _alice, task_id, _request, tmp, _artifact = scene
    proof = wp.build_proof(
        store,
        task_id,
        artifacts=["out/report.md"],
        root=tmp,
        criteria=[{"criterion": "11/11 通过", "self_check": "pass", "evidence": ["art:deadbeef"]}],
        checks=[{"name": "pytest", "status": "pass"}],
    )
    verification = wp.verify_proof(store, task_id)
    screen = wp.render_verdict(proof, verification)  # 只传 proof + verification
    assert "交付证明" in screen
    assert "11/11 通过" in screen and "art:deadbeef" in screen
    assert "verified" in screen
    assert "员工报告不等于通过" in screen
    assert "11/11 PASS" not in screen  # 正文没被搬进来（引用而非内联）


def test_proof_never_changes_task_status(scene):
    store, _project, _alice, task_id, _request, tmp, _artifact = scene
    with store._transaction() as db:
        before = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()["status"]
    wp.build_proof(store, task_id, artifacts=["out/report.md"], root=tmp)
    wp.verify_proof(store, task_id, record=True)
    with store._transaction() as db:
        after = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()["status"]
    assert before == after == "review"  # 校验不推进终态：人验收仍是唯一终态
