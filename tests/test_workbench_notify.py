"""发送器三条约束：默认沉默 ✓ 必过守卫 ✓ 没变就闭嘴 ✓（假 transport ✓ 不联网 ✗）。"""

from __future__ import annotations

import pytest

from agent_mailbox.workbench_feishu_target import FeishuTargetError
from agent_mailbox.workbench_notify import NotifySkipped, send_card, should_send
from agent_mailbox.workbench_store import WorkbenchStore

GOOD = {"target": {"kind": "dm", "receive_id_type": "open_id", "receive_id": "ou_owner123456"}}
# 守卫校验的是**配置合法性** ✓（不是"证明是某个特定人"✗ —— 目标只能来自配置 ⇒ 发到别处在结构上不可能 ✓）
BAD_GROUP = {"target": {"kind": "group", "receive_id_type": "open_id", "receive_id": "oc_x"}}
BAD_TYPE = {"target": {"kind": "dm", "receive_id_type": "chat_id", "receive_id": "oc_x"}}
BAD_EMPTY = {"target": {"kind": "dm", "receive_id_type": "open_id", "receive_id": "  "}}
CARD = {"header": {"title": {"tag": "plain_text", "content": "x"}}}


class Recorder:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.fail = False

    def __call__(self, target, card):
        self.calls.append((target.receive_id, card))  # **先记尝试** ✓（失败也要算一次 ✓）
        if self.fail:
            raise RuntimeError("网络炸了")
        return f"om_{len(self.calls)}"


def _store(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    (tmp_path / "repo").mkdir(exist_ok=True)
    project = store.create_project("P", tmp_path / "repo")
    return store, project


def test_whitelist_defaults_to_silence(tmp_path):
    for kind in ("progress", "status_change", "letter", "digest"):
        assert should_send(kind) is False, kind
    for kind in ("decision", "review", "stuck", "quota", "security"):
        assert should_send(kind) is True, kind
    store, _project = _store(tmp_path)
    with pytest.raises(NotifySkipped):
        send_card(
            store,
            kind="progress",
            state_hash="h1",
            card=CARD,
            target_config=GOOD,
            transport=Recorder(),
        )


def test_sends_once_per_state_hash(tmp_path):
    store, project = _store(tmp_path)
    transport = Recorder()
    first = send_card(
        store,
        kind="review",
        state_hash="h1",
        card=CARD,
        target_config=GOOD,
        transport=transport,
        project_id=project["id"],
    )
    assert first["status"] == "sent" and first["external_message_id"] == "om_1"
    assert len(transport.calls) == 1
    with pytest.raises(NotifySkipped):
        send_card(
            store,
            kind="review",
            state_hash="h1",
            card=CARD,
            target_config=GOOD,
            transport=transport,
            project_id=project["id"],
        )
    assert len(transport.calls) == 1, "同一状态不得重复发 ✓（没变就闭嘴 ✓）"
    send_card(
        store,
        kind="review",
        state_hash="h2",
        card=CARD,
        target_config=GOOD,
        transport=transport,
        project_id=project["id"],
    )
    assert len(transport.calls) == 2, "状态变了 ⇒ 才发 ✓"


def test_target_guard_is_enforced_before_any_send(tmp_path):
    store, _project = _store(tmp_path)
    transport = Recorder()
    with pytest.raises(FeishuTargetError):
        send_card(
            store,
            kind="review",
            state_hash="h1",
            card=CARD,
            target_config=BAD_GROUP,
            transport=transport,
        )
    assert transport.calls == [], "守卫不通过 ⇒ **一个字节都不发** ✓（零调用点问题就此消除 ✓）"


def test_transport_failure_is_recorded_not_swallowed(tmp_path):
    store, project = _store(tmp_path)
    transport = Recorder()
    transport.fail = True
    with pytest.raises(RuntimeError):
        send_card(
            store,
            kind="stuck",
            state_hash="h1",
            card=CARD,
            target_config=GOOD,
            transport=transport,
            project_id=project["id"],
        )
    row = store.latest_notification("stuck", target_kind="dm", target_id="ou_owner123456")
    assert row["status"] == "failed" and "网络炸了" in (row["last_error"] or "")
    transport.fail = False
    with pytest.raises(NotifySkipped):
        send_card(
            store,
            kind="stuck",
            state_hash="h1",
            card=CARD,
            target_config=GOOD,
            transport=transport,
            project_id=project["id"],
        )
    assert len(transport.calls) == 1, "失败已留痕 ⇒ 同一状态不自动重发 ✓（避免风暴 ✓）"
