"""Contacts whitelist: open by default, strict once a list exists (roadmap T33)."""

from __future__ import annotations

import pytest

from agent_mailbox import workbench_contacts as wc
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


@pytest.fixture
def scene(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    alice = store.create_employee("Alice", "codex", project["id"])
    bob = store.create_employee("Bob", "codex", project["id"])
    carol = store.create_employee("Carol", "codex", project["id"])
    task = store.create_task(project["id"], "Build", "work", alice["id"])
    store.claim_task(store.local_node()["id"])
    store.set_status(task["id"], "running")
    return store, project, alice, bob, carol, task


def test_open_by_default(scene):
    store, project, alice, bob, _carol, _task = scene
    assert wc.may_message(store, project["id"], alice["id"], bob["id"])["allowed"] is True
    assert wc.contacts(store, project["id"], bob["id"])["contacts"] == []


def test_whitelist_binds_once_the_recipient_has_a_list(scene):
    store, project, alice, bob, carol, _task = scene
    wc.add_contact(store, project["id"], owner_id=bob["id"], target_id=alice["id"], note="同事")
    assert wc.may_message(store, project["id"], alice["id"], bob["id"]) == {
        "allowed": True,
        "reason": "listed",
    }

    denied = wc.may_message(store, project["id"], carol["id"], bob["id"])
    assert denied["allowed"] is False and denied["reason"] == "not_in_recipient_contacts"
    assert denied["recipient_has"] == [alice["id"]]

    book = wc.contacts(store, project["id"], bob["id"])
    assert [row["target_id"] for row in book["contacts"]] == [alice["id"]]
    assert book["contacts"][0]["note"] == "同事"


def test_request_grants_nothing_and_removal_revokes(scene):
    store, project, alice, bob, _carol, _task = scene
    wc.request_contact(
        store, project["id"], owner_id=bob["id"], target_id=alice["id"], note="想联系"
    )
    assert (
        wc.may_message(store, project["id"], alice["id"], bob["id"])["allowed"] is True
    )  # 还是开放：请求不授权
    assert [
        row["target_id"] for row in wc.contacts(store, project["id"], bob["id"])["pending"]
    ] == [alice["id"]]

    wc.add_contact(store, project["id"], owner_id=bob["id"], target_id=alice["id"])
    assert wc.contacts(store, project["id"], bob["id"])["pending"] == []  # 接受后待办清空
    wc.remove_contact(store, project["id"], owner_id=bob["id"], target_id=alice["id"])
    assert (
        wc.may_message(store, project["id"], alice["id"], bob["id"])["allowed"] is True
    )  # 移回开放态
    assert wc.contacts(store, project["id"], bob["id"])["contacts"] == []


def test_self_and_human_mail_are_unrestricted(scene):
    store, project, alice, _bob, _carol, _task = scene
    assert wc.may_message(store, project["id"], alice["id"], alice["id"])["allowed"] is True
    assert wc.may_message(store, project["id"], None, alice["id"])["allowed"] is True  # 人工消息
    with pytest.raises(ValueError):
        wc.add_contact(store, project["id"], owner_id=alice["id"], target_id=alice["id"])


def test_send_path_enforces_the_whitelist(scene):
    store, project, alice, bob, carol, alice_task = scene
    # 员工发信必须绑定自己的执行来源 ⇒ 给 carol 也建一张跑着的卡
    carol_task = store.create_task(project["id"], "Carol 的活", "work", carol["id"])
    store.claim_task(store.local_node()["id"])
    store.set_status(carol_task["id"], "running")

    # bob 把 alice 加进通讯录 ⇒ carol 发给 bob 被拦
    wc.add_contact(store, project["id"], owner_id=bob["id"], target_id=alice["id"])
    with pytest.raises(WorkbenchError) as excinfo:
        store.send_message(
            project["id"],
            "打扰一下",
            "正文",
            recipient_id=bob["id"],
            sender_id=carol["id"],
            source_task_id=carol_task["id"],
        )
    assert excinfo.value.code == "CONTACT_REQUIRED"

    # 白名单内的发件人照常（复用 fixture 里 alice 已在跑的那张卡 —— 同一位员工
    # 不能同时有两张活跃的 managed 卡，重复建卡会认领不到）
    sent = store.send_message(
        project["id"],
        "正常联系",
        "正文",
        recipient_id=bob["id"],
        sender_id=alice["id"],
        source_task_id=alice_task["id"],
    )
    assert sent["id"]


def test_listing_is_read_only(scene):
    store, project, _alice, bob, _carol, _task = scene
    wc.add_contact(store, project["id"], owner_id=bob["id"], target_id=_alice["id"])
    with store._transaction() as db:
        before = db.execute("SELECT count(*) AS c FROM governance_events").fetchone()["c"]
    wc.contacts(store, project["id"], bob["id"])
    wc.may_message(store, project["id"], _alice["id"], bob["id"])
    with store._transaction() as db:
        assert db.execute("SELECT count(*) AS c FROM governance_events").fetchone()["c"] == before


def test_employee_broadcast_cannot_bypass_the_whitelist(scene):
    """独立审查指出的高危：私信被拦、群发却可达 ⇒ 白名单形同虚设。"""
    store, project, alice, bob, carol, _task = scene
    carol_task = store.create_task(project["id"], "Carol 的活", "work", carol["id"])
    store.claim_task(store.local_node()["id"])
    store.set_status(carol_task["id"], "running")

    # 无人设白名单时：员工群发是允许的（项目内通告）
    allowed = store.send_message(
        project["id"], "通告", "正文", sender_id=carol["id"], source_task_id=carol_task["id"]
    )
    assert allowed["id"] and allowed["recipient_id"] is None

    # Bob 设了通讯录 ⇒ 员工群发不得再绕过白名单
    wc.add_contact(store, project["id"], owner_id=bob["id"], target_id=alice["id"])
    with pytest.raises(WorkbenchError) as excinfo:
        store.send_message(
            project["id"],
            "绕过通告",
            "正文",
            sender_id=carol["id"],
            source_task_id=carol_task["id"],
        )
    assert excinfo.value.code == "CONTACT_REQUIRED"

    # 人工群发不受影响（人本来就不在白名单语义内）
    human = store.send_message(project["id"], "人工通告", "正文")
    assert human["id"] and human["recipient_id"] is None
