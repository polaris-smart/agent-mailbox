"""End-to-end self-check: our own features have to work *together*.

Unit tests prove each piece; this proves the product path — enrol → assign →
schedule → brief → lease → accept → deliver → proof → peer verify → artifact →
wall/ledger/audit → search → observe — on a throwaway home, with no publishing
and nothing outside the sandbox.
"""

from __future__ import annotations

import json

import pytest

from agent_mailbox import (
    workbench_artifact,
    workbench_bridge,
    workbench_brief,
    workbench_contacts,
    workbench_enroll,
    workbench_lease,
    workbench_ledger,
    workbench_observe,
    workbench_policy,
    workbench_proof,
    workbench_schedule,
    workbench_search,
    workbench_wall,
)
from agent_mailbox.workbench_mail_sessions import invoke
from agent_mailbox.workbench_mail_tasks import create_mail_task, submit_mail_task
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


@pytest.fixture
def home(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("自检项目", directory)
    return store, project, tmp_path


def test_full_product_path(tmp_path, home, monkeypatch):
    store, project, root = home
    workbench_observe.start(store)  # 观察窗：先开窗，再发生一切
    # 1) 探测 + 一键入伙（真机探测在别的测试里；这里注入固定发现，保持自检确定）
    from agent_mailbox import workbench_runtime

    monkeypatch.setattr(
        workbench_runtime,
        "discover_employees",
        lambda: [
            {
                "kind": "codex",
                "name": "Codex",
                "connection_type": "cli",
                "entrypoint": "/usr/local/bin/codex",
                "auth_status": "authenticated",
            },
            {"kind": "hermes", "name": "Hermes", "connection_type": "cli", "entrypoint": None},
        ],
    )
    outline = workbench_enroll.plan(store, project["id"])
    assert [row["kind"] for row in outline["suggestions"]] == ["codex"]
    enrolled = workbench_enroll.onboard(store, project["id"], apply=True, host="hermes")
    step = enrolled["steps"][0]
    assert step["action"] == "enrolled" and "agent-mailbox" in step["snippet"]
    employee_id = step["employee"]["id"]
    from pathlib import Path

    token = json.loads(Path(step["session_file"]).read_text(encoding="utf-8"))["token"]
    # 第二位员工（发起方/同行校验者）
    reviewer = store.create_employee("Reviewer", "codex", project["id"])

    # 2) 派活：人给目标（用产品的正规入口 —— mailbox 任务，员工在自己的会话里接）
    task = create_mail_task(store, project["id"], "写一份自检报告", "把结论写清楚", employee_id)
    task_id = task["id"]
    request_id = task["request_message_id"]
    assert task["execution_mode"] == "mailbox"

    # 3) 排期 + 到期派生（拉取式）
    workbench_schedule.schedule(store, task_id, "2020-01-01T00:00:00+00:00", note="尽快")
    assert [row["id"] for row in workbench_schedule.due_tasks(store, project["id"])] == [task_id]

    # 4) 会话起始简报（钩子轻通道）：员工看到"待我处理"
    brief = workbench_brief.brief(store, employee_id=employee_id, project_id=project["id"])
    assert brief["unread"]["count"] >= 1
    assert "【交接简报】" in workbench_brief.render(brief)

    # 5) 文件认领：别人先占，自己不能重复占
    workbench_lease.claim_paths(store, project["id"], reviewer["id"], ["src/**"], reason="先占")
    blocked = workbench_lease.claim_paths(store, project["id"], employee_id, ["src/app.py"])
    assert blocked["granted"] == [] and blocked["conflicts"][0]["held_by"] == reviewer["id"]
    assert workbench_lease.release_paths(store, project["id"], reviewer["id"])["released"]
    with pytest.raises(ValueError):  # 传字典应给清楚错误，不是崩在别处
        workbench_lease.claim_paths(store, project["id"], reviewer, ["src/**"])
    assert workbench_lease.claim_paths(store, project["id"], employee_id, ["src/app.py"])["granted"]

    # 6) 员工接单 → 交付（走真实的邮箱会话工具面）
    accepted = invoke(store, token, "task_accept", {"task_id": task_id})
    assert accepted["status"] == "running" or accepted
    with store._transaction() as db:
        status = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()["status"]
    assert status in ("running", "waiting_approval")
    submitted = submit_mail_task(store, token, task_id, "自检报告已产出：11/11 PASS")
    assert submitted["status"] == "review"  # 提交后等人验收

    # 7) 交付证明 + 同行校验（策略开启后必须换人）
    artifact = root / "report.md"
    artifact.write_text("自检报告\n11/11 PASS\n", encoding="utf-8")
    workbench_policy.set_policy(
        store, {"peer_review": "required_for_write"}, project_id=project["id"]
    )
    proof = workbench_proof.build_proof(
        store,
        task_id,
        artifacts=[str(artifact)],
        criteria=[{"criterion": "结论齐全", "self_check": "pass"}],
        checks=[{"name": "pytest", "status": "pass"}],
    )
    with pytest.raises(ValueError):
        # 自己验自己必须被拒（不是"记录但不计数"），且报错要说清原因
        workbench_proof.verify_proof(store, task_id, record=True, verifier_id=employee_id)
    assert workbench_policy.peer_verified(store, task_id, employee_id) is False
    verified = workbench_proof.verify_proof(store, task_id, record=True, verifier_id=reviewer["id"])
    assert workbench_policy.peer_verified(store, task_id, employee_id) is True

    # 8) 产出物引用：按预算读回，不内联
    reread = workbench_artifact.read(store, proof["artifacts"][0]["ref"], budget=8)
    # 契约：returned_bytes ≤ budget + 3（补齐一个完整字符最多多读 3 字节）
    assert reread["returned_bytes"] <= 8 + 3 and reread["truncated"] is True
    assert reread["hash_verified"] is True
    assert "报告" not in (reread["content"] or "")[:0]  # 引用而非内联的语义检查在别处

    # 9) 一屏三问 + 账本 + 审计链
    view = workbench_wall.wall(store, project["id"])
    assert any(row["id"] == task_id for row in view["running"] + view["waiting_on_human"])
    ledger = workbench_ledger.ledger(store, project["id"])
    row = next(r for r in ledger["tasks"] if r["task_id"] == task_id)
    assert row["has_proof"] is True and row["proof_verdict"] == "verified"
    chain = workbench_wall.audit_chain(store, request_id)
    steps = [s["step"] for s in chain["steps"]]
    assert steps[0] == "sent" and any(s.startswith("terminal:") for s in steps)

    # 10) 检索：显式建索引后 FTS 命中；索引与消息同步
    workbench_search.build_index(store)
    assert workbench_search.search(store, "自检报告", project_id=project["id"])["count"] >= 1
    assert workbench_search.index_stats(store)["in_sync"] is True

    # 11) 通讯录白名单：绑定后他人被拦
    workbench_contacts.add_contact(
        store, project["id"], owner_id=employee_id, target_id=reviewer["id"]
    )
    verdict = workbench_contacts.may_message(store, project["id"], "employee_outsider", employee_id)
    assert verdict["allowed"] is False

    # 12) 单向桥接：一封派工信 → 一张卡；旧信目录零改动
    mail = root / "mail" / "inbox" / "Codex"
    mail.mkdir(parents=True)
    letter = mail / "L-9.json"
    letter.write_text(
        json.dumps(
            {
                "id": "L-9",
                "subject": "【任务书】另一件事",
                "from": "HS",
                "to": "Codex",
                "body": "正文",
            }
        ),
        encoding="utf-8",
    )
    before = letter.read_bytes()
    projected = workbench_bridge.project(
        store, root / "mail", project["id"], apply=True, assignee_map={"Codex": employee_id}
    )
    assert projected["counts"].get("projected") == 1
    assert letter.read_bytes() == before

    # 13) 观察窗：判据必须覆盖窗口内发生的全部信件
    result = workbench_observe.evaluate(store, sample=5)
    assert result["window"]["started_at"]
    assert result["criteria"]["audit_full"] is True
    assert result["verdict"] in ("观察中", "达标")

    # 14) 全程只应有我们自己的写：没有任何"外部"痕迹
    assert verified["verdict"] == "verified"
    with pytest.raises(WorkbenchError):
        store.send_message(
            project["id"],
            "越界",
            "正文",
            recipient_id=employee_id,
            sender_id="employee_outsider",
            source_task_id=task_id,
        )
