"""Regressions for the independent review of 2026-10-05 (each case was a real defect).

The reviewer reproduced all of these; the fixes are asserted here so they cannot
come back. Naming: RV-n matches the review item.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from agent_mailbox import (
    workbench_artifact,
    workbench_bridge,
    workbench_contacts,
    workbench_observe,
    workbench_policy,
    workbench_proof,
    workbench_schedule,
)
from agent_mailbox.workbench_brief import brief
from agent_mailbox.workbench_mail_tasks import create_mail_task
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


@pytest.fixture
def scene(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    alice = store.create_employee("Alice", "codex", project["id"])
    bob = store.create_employee("Bob", "codex", project["id"])
    return store, project, alice, bob


def _noise(store, project_id, count):
    """Insert plain ledger events (the substance does not matter; the volume does)."""
    with store._transaction() as db:
        for index in range(count):
            db.execute(
                "INSERT INTO governance_events(id,employee_id,project_id,task_id,type,actor,reason,payload,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    f"noise_{index}",
                    None,
                    project_id,
                    None,
                    "noise",
                    "system",
                    "灌量",
                    "{}",
                    f"2026-10-05T00:00:{index % 60:02d}+00:00",
                ),
            )


def test_rv1_derived_views_survive_a_ledger_beyond_the_ui_cap(scene, tmp_path):
    """RV-1: governance_events() 曾固定 LIMIT 200，派生视图当成全量 ⇒ 静默失真。"""
    store, project, alice, bob = scene
    task = create_mail_task(store, project["id"], "活", "说明", alice["id"])
    artifact = tmp_path / "out.md"
    artifact.write_text("交付物", encoding="utf-8")
    proof = workbench_proof.build_proof(store, task["id"], artifacts=[str(artifact)])
    ref = proof["artifacts"][0]["ref"]
    workbench_schedule.schedule(store, task["id"], "2020-01-01T00:00:00+00:00")
    workbench_contacts.add_contact(store, project["id"], owner_id=alice["id"], target_id=bob["id"])
    started = workbench_observe.start(store)["started_at"]

    _noise(store, project["id"], 250)  # 越过 200 行上限

    # ① 白名单判定必须与执行点一致（原先视图说"开放"，执行点说"拦"）
    assert (
        workbench_contacts.may_message(store, project["id"], "employee_outsider", alice["id"])[
            "allowed"
        ]
        is False
    )
    # ② 观察窗起点不许被"重新开始"
    assert workbench_observe.window(store)["started_at"] == started
    assert workbench_observe.start(store)["started_at"] == started
    # ③ 产出物引用仍可解析
    assert workbench_artifact.resolve(store, ref)["path"].endswith("out.md")
    # ④ 证明仍找得到
    assert workbench_proof.latest_proof(store, task["id"])["proof_sha256"]
    # ⑤ 排期到期仍算得出
    assert [row["id"] for row in workbench_schedule.due_tasks(store, project["id"])] == [task["id"]]


def test_rv1_frozen_state_survives_a_large_ledger(scene):
    """冻结状态同样不能被 200 行上限"遗忘"（工具交叉核对时抓到的漏网）。"""
    from agent_mailbox import echo_guard

    store, project, _alice, _bob = scene
    echo_guard.freeze_thread(store, "thread_x", reason="回声过深")
    assert echo_guard.thread_frozen(store, "thread_x") is True
    _noise(store, project["id"], 250)
    assert echo_guard.thread_frozen(store, "thread_x") is True, "账本变长后冻结状态丢失"


def test_rv1_bridge_stays_idempotent_beyond_the_cap(scene, tmp_path):
    store, project, alice, _bob = scene
    mail = tmp_path / "mail" / "inbox" / "Codex"
    mail.mkdir(parents=True)
    letter = mail / "L-1.json"
    letter.write_text(
        json.dumps(
            {"id": "L-1", "subject": "【任务书】x", "from": "HS", "to": "Codex", "body": "b"}
        ),
        encoding="utf-8",
    )
    first = workbench_bridge.project(
        store, tmp_path / "mail", project["id"], apply=True, assignee_map={"Codex": alice["id"]}
    )
    assert first["counts"].get("projected") == 1
    _noise(store, project["id"], 250)
    again = workbench_bridge.project(
        store, tmp_path / "mail", project["id"], apply=True, assignee_map={"Codex": alice["id"]}
    )
    assert again["counts"].get("projected") is None, "越过上限后重复投影（凭空多出任务卡）"
    with store._transaction() as db:
        assert db.execute("SELECT count(*) AS c FROM tasks").fetchone()["c"] == 1


def test_rv3_peer_review_can_be_turned_back_off(scene):
    """RV-3: peer_review 曾一旦打开就关不掉（'off' 被归一成 False 后被字符串校验拒绝）。"""
    store, project, _alice, _bob = scene
    workbench_policy.set_policy(
        store, {"peer_review": "required_for_write"}, project_id=project["id"]
    )
    assert (
        workbench_policy.effective_policy(store, project["id"])["peer_review"]
        == "required_for_write"
    )
    restored = workbench_policy.set_policy(store, {"peer_review": "off"}, project_id=project["id"])
    assert restored["peer_review"] == "off"
    assert (
        workbench_policy.set_policy(store, {"peer_review": "Off"}, project_id=project["id"])[
            "peer_review"
        ]
        == "off"
    )
    for value in ("true", False, "不是选项"):
        with pytest.raises(ValueError):
            workbench_policy.set_policy(store, {"peer_review": value}, project_id=project["id"])


def test_rv5_brief_is_parameterised(scene):
    """RV-5: waiting_on_human 曾把 employee_id 拼进 SQL（撇号崩、注入读别人活）。"""
    store, project, alice, bob = scene
    a_task = create_mail_task(store, project["id"], "Alice 的活", "说明", alice["id"])
    b_task = create_mail_task(store, project["id"], "Bob 的活", "说明", bob["id"])
    with store._transaction() as db:  # 新单是 queued；"等人验收"看的是 review
        db.execute(
            "UPDATE tasks SET status='review' WHERE id IN (?,?)", (a_task["id"], b_task["id"])
        )

    own = brief(store, employee_id=alice["id"], project_id=project["id"])
    assert [row["title"] for row in own["waiting_on_human"]] == ["Alice 的活"]
    injected = brief(store, employee_id="x' OR '1'='1", project_id=project["id"])
    assert [row["title"] for row in injected["waiting_on_human"]] == []
    assert brief(store, employee_id="O'Brien", project_id=project["id"])["unread"]["count"] == 0


def test_rv7_json_keys_projection_respects_the_budget(scene, tmp_path):
    """RV-7: json_keys 曾无视预算把整份结构吐出去，还谎报 returned_bytes=0。"""
    store, project, alice, _bob = scene
    task = create_mail_task(store, project["id"], "活", "说明", alice["id"])
    big = tmp_path / "big.json"
    big.write_text(json.dumps({f"key_{i}": {"nested": i} for i in range(20000)}), encoding="utf-8")
    ref = "art:" + hashlib.sha256(big.read_bytes()).hexdigest()[:16]
    workbench_proof.build_proof(store, task["id"], artifacts=[str(big)])
    result = workbench_artifact.read(store, ref, budget=100, projection="json_keys")
    assert result["returned_bytes"] <= 100, f"结构投影超预算：{result['returned_bytes']}B"
    assert len(json.dumps(result["content"], ensure_ascii=False).encode("utf-8")) <= 100


def _same_timestamp_events(store, project_id, rows):
    """Insert ledger rows with an **identical** created_at (only insertion order differs)."""
    with store._transaction() as db:
        for index, (event_type, payload) in enumerate(rows):
            db.execute(
                "INSERT INTO governance_events(id,employee_id,project_id,task_id,type,actor,reason,payload,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    f"tie_{event_type}_{index}",
                    None,
                    project_id,
                    None,
                    event_type,
                    "system",
                    "同秒",
                    json.dumps(payload),
                    "2026-10-05T12:00:00+00:00",
                ),
            )


def test_rv8_same_second_ties_are_decided_by_insertion_order(scene):
    """RV-8：同秒事件原先靠 uuid 或扫描顺序决胜负 ⇒ "最新一条"不确定。"""
    store, project, alice, _bob = scene
    task = create_mail_task(store, project["id"], "活", "说明", alice["id"])

    # 排期：先"已到期"后"未来" ⇒ 最新是未来 ⇒ 不该 due
    _same_timestamp_events(
        store,
        project["id"],
        [
            (workbench_schedule.SCHEDULED_EVENT, {"due_at": "2020-01-01T00:00:00+00:00"}),
            (workbench_schedule.SCHEDULED_EVENT, {"due_at": "2099-01-01T00:00:00+00:00"}),
        ],
    )
    with store._transaction() as db:
        db.execute(
            "UPDATE governance_events SET task_id=? WHERE type=?",
            (task["id"], workbench_schedule.SCHEDULED_EVENT),
        )
    assert workbench_schedule.due_tasks(store, project["id"]) == []

    # 顺序反过来（先未来后到期）⇒ 最新是到期 ⇒ 必须 due
    store2 = WorkbenchStore(scene[0].root.parent / "data2")
    directory = scene[0].root.parent / "p2"
    directory.mkdir()
    project2 = store2.create_project("Demo2", directory)
    alice2 = store2.create_employee("Alice2", "codex", project2["id"])
    task2 = create_mail_task(store2, project2["id"], "活", "说明", alice2["id"])
    _same_timestamp_events(
        store2,
        project2["id"],
        [
            (workbench_schedule.SCHEDULED_EVENT, {"due_at": "2099-01-01T00:00:00+00:00"}),
            (workbench_schedule.SCHEDULED_EVENT, {"due_at": "2020-01-01T00:00:00+00:00"}),
        ],
    )
    with store2._transaction() as db:
        db.execute(
            "UPDATE governance_events SET task_id=? WHERE type=?",
            (task2["id"], workbench_schedule.SCHEDULED_EVENT),
        )
    assert [row["id"] for row in workbench_schedule.due_tasks(store2, project2["id"])] == [
        task2["id"]
    ]


def test_rv8_contacts_tie_break_by_insertion_order(scene):
    store, project, alice, bob = scene
    _same_timestamp_events(
        store,
        project["id"],
        [
            (workbench_contacts.ADDED_EVENT, {"owner_id": bob["id"], "target_id": alice["id"]}),
            (workbench_contacts.REMOVED_EVENT, {"owner_id": bob["id"], "target_id": alice["id"]}),
        ],
    )
    # 先加后删（同秒）⇒ 删除胜 ⇒ 回到"无名单"开放态
    assert workbench_contacts.contacts(store, project["id"], bob["id"])["contacts"] == []
    assert (
        workbench_contacts.may_message(store, project["id"], "employee_x", bob["id"])["allowed"]
        is True
    )


def test_rv4_peer_review_is_bound_to_the_current_proof(scene, tmp_path):
    """RV-4：同行校验必须绑"被验的那一版"；空证明不得算通过（橡皮图章）。"""
    store, project, alice, bob = scene
    task = create_mail_task(store, project["id"], "活", "说明", alice["id"])
    with store._transaction() as db:
        db.execute("UPDATE tasks SET status='review' WHERE id=?", (task["id"],))

    # 空证明（没有任何产出物）⇒ 不得判 verified
    empty = workbench_proof.build_proof(store, task["id"])
    assert workbench_proof.verify_proof(store, task["id"])["verdict"] == "incomplete"
    assert empty["artifacts"] == []

    # V1：同伴验过
    v1 = tmp_path / "v1.md"
    v1.write_text("第一版", encoding="utf-8")
    workbench_proof.build_proof(store, task["id"], artifacts=[str(v1)])
    workbench_proof.verify_proof(store, task["id"], record=True, verifier_id=bob["id"])
    assert workbench_policy.peer_verified(store, task["id"], alice["id"]) is True

    # V2：内容整批换新 ⇒ 旧校验不再有效
    v2 = tmp_path / "v2.md"
    v2.write_text("第二版（完全不同）", encoding="utf-8")
    workbench_proof.build_proof(store, task["id"], artifacts=[str(v2)])
    assert workbench_policy.peer_verified(store, task["id"], alice["id"]) is False, (
        "换了交付内容旧校验仍放行"
    )

    # 重新验这一版 ⇒ 恢复
    workbench_proof.verify_proof(store, task["id"], record=True, verifier_id=bob["id"])
    assert workbench_policy.peer_verified(store, task["id"], alice["id"]) is True


def test_rv9_broadcast_gate_survives_a_remove(scene):
    """RV-9（复查打回的高危）：删掉名单里的**任何一人**不得把"员工不能群发"闸门整体关掉。"""
    store, project, alice, bob = scene
    carol = store.create_employee("Carol", "codex", project["id"])
    dave = store.create_employee("Dave", "codex", project["id"])
    carol_task = store.create_task(project["id"], "Carol 的活", "work", carol["id"])
    store.claim_task(store.local_node()["id"])
    store.set_status(carol_task["id"], "running")

    workbench_contacts.add_contact(store, project["id"], owner_id=bob["id"], target_id=alice["id"])
    workbench_contacts.add_contact(store, project["id"], owner_id=bob["id"], target_id=dave["id"])
    with pytest.raises(WorkbenchError):
        store.send_message(
            project["id"], "群发", "正文", sender_id=carol["id"], source_task_id=carol_task["id"]
        )

    # 删掉名单里的**一人**（Bob 的通讯录仍然非空）
    workbench_contacts.remove_contact(
        store, project["id"], owner_id=bob["id"], target_id=dave["id"]
    )
    assert workbench_contacts.contacts(store, project["id"], bob["id"])["contacts"]  # 仍有 1 人
    with pytest.raises(WorkbenchError) as excinfo:
        store.send_message(
            project["id"], "再群发", "正文", sender_id=carol["id"], source_task_id=carol_task["id"]
        )
    assert excinfo.value.code == "CONTACT_REQUIRED", "删掉一人后闸门被整体关掉"

    # 删空之后才恢复开放
    workbench_contacts.remove_contact(
        store, project["id"], owner_id=bob["id"], target_id=alice["id"]
    )
    assert workbench_contacts.contacts(store, project["id"], bob["id"])["contacts"] == []
    sent = store.send_message(
        project["id"], "合法群发", "正文", sender_id=carol["id"], source_task_id=carol_task["id"]
    )
    assert sent["id"]
