"""投影卡：载荷来自只读快照 ✓ 哈希稳定 ✓ 三态决策（send/patch/skip）✓。"""

from __future__ import annotations

from agent_mailbox.workbench_projection import (
    decide_projection,
    projection_card,
    projection_payload,
    projection_state_hash,
)
from agent_mailbox.workbench_store import WorkbenchStore


def _store(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    (tmp_path / "repo").mkdir(exist_ok=True)
    project = store.create_project("P", tmp_path / "repo")
    return store, project


def test_payload_is_read_only_and_shaped(tmp_path):
    store, project = _store(tmp_path)
    payload = projection_payload(store, project_id=project["id"])
    assert set(payload) >= {
        "running",
        "waiting_on_human",
        "permissions",
        "reviews",
        "stuck",
        "done_today",
        "items",
    }
    assert all(
        isinstance(payload[k], int)
        for k in ("running", "waiting_on_human", "permissions", "reviews", "stuck", "done_today")
    )


def test_hash_is_stable_and_order_independent():
    a = {"running": 1, "waiting_on_human": 2, "items": {"x": ["a", "b"]}}
    b = {"items": {"x": ["a", "b"]}, "waiting_on_human": 2, "running": 1}
    assert projection_state_hash(a) == projection_state_hash(b), "键序不同不得改变哈希 ✓"
    assert projection_state_hash(a) != projection_state_hash({**a, "running": 2})


def test_card_is_structured_not_a_chat_message(tmp_path):
    store, project = _store(tmp_path)
    card = projection_card(projection_payload(store, project_id=project["id"]))
    assert card["header"]["title"]["content"] == "agent-mailbox · 一屏三问"
    text = card["elements"][0]["text"]["content"]
    for token in ("在跑", "等你", "卡住", "今日成果"):
        assert token in text, token


def test_first_time_sends_changed_hash_patches_same_hash_skips(tmp_path):
    store, project = _store(tmp_path)
    payload = projection_payload(store, project_id=project["id"])
    action, previous, state = decide_projection(
        store, target_kind="dm", target_id="ou_x", payload=payload
    )
    assert (action, previous) == ("send", None)
    # 先登记一条"已发"（模拟首发送达 ✓）
    row = store.record_notification(
        "projection",
        channel="feishu_dm",
        target_kind="dm",
        target_id="ou_x",
        state_hash=state,
        project_id=project["id"],
    )
    store.mark_notification(row["id"], status="sent", external_message_id="om_1")
    again, prev2, state2 = decide_projection(
        store, target_kind="dm", target_id="ou_x", payload=payload
    )
    assert (again, state2) == ("skip", state) and prev2["external_message_id"] == "om_1"
    changed = {**payload, "running": payload["running"] + 1}
    action3, prev3, state3 = decide_projection(
        store, target_kind="dm", target_id="ou_x", payload=changed
    )
    assert action3 == "patch" and state3 != state and prev3["external_message_id"] == "om_1"


def test_sync_sends_then_patches_then_skips(tmp_path):
    """三态端到端（假 sender/patcher ✓ 不联网 ✗）。

    夹具照仓库既有 `scene` 模式 ✓（`tests/test_backpressure.py:12-22` ✓）：
    **`kind="codex"` 才有可执行适配器** ✓（我先前用 deepseek ⇒ create_task 直接拒 ✗ 连撞两轮 ✓）。
    """
    import pytest

    from agent_mailbox.workbench_notify import NotifySkipped
    from agent_mailbox.workbench_projection import sync_projection

    store, project = _store(tmp_path)
    alice = store.create_employee("Alice", "codex", project["id"])  # ← 有适配器的 kind ✓
    config = {"target": {"kind": "dm", "receive_id_type": "open_id", "receive_id": "ou_owner1"}}
    sent, patched = [], []

    def sender(_target, _card):
        sent.append(1)
        return f"om_{len(sent)}"

    def patcher(message_id, _card):
        patched.append(message_id)
        return message_id

    first = sync_projection(
        store, target_config=config, sender=sender, patcher=patcher, project_id=project["id"]
    )
    assert first["action"] == "send", first
    assert sent == [1] and patched == [], (sent, patched)

    with pytest.raises(NotifySkipped):
        sync_projection(
            store, target_config=config, sender=sender, patcher=patcher, project_id=project["id"]
        )
    assert sent == [1] and patched == [], "没变 ⇒ 不发不改 ✓"

    store.create_task(project["id"], "新任务", "p", alice["id"])  # 产品 API ✓ 不裸 SQL ✗
    third = sync_projection(
        store, target_config=config, sender=sender, patcher=patcher, project_id=project["id"]
    )
    assert third["action"] == "patch", third
    assert patched == ["om_1"] and sent == [1], (patched, sent)
    assert third["message_id"] == "om_1", "原地更新必须复用同一 message_id ✓"
