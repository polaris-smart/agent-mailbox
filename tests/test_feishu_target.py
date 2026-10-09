"""飞书目标守卫：目标必须显式配置，不一致一律拒发（HS 裁定 2026-10-07）。"""

from __future__ import annotations

import pytest

from agent_mailbox import workbench_feishu_target as ft

GOOD = {"target": {"kind": "dm", "receive_id_type": "open_id", "receive_id": "ou_owner123456"}}


def test_valid_config_resolves():
    target = ft.target_from_config(GOOD)
    assert target.kind == "dm" and target.receive_id == "ou_owner123456"


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"target": None},
        {"target": {}},
        {"target": {"kind": "group", "receive_id_type": "open_id", "receive_id": "oc_x"}},
        {"target": {"kind": "dm", "receive_id_type": "chat_id", "receive_id": "oc_x"}},
        {"target": {"kind": "dm", "receive_id_type": "open_id", "receive_id": "  "}},
    ],
)
def test_invalid_configs_are_refused(bad):
    with pytest.raises(ft.FeishuTargetError):
        ft.target_from_config(bad)


def test_sending_to_a_different_chat_is_refused():
    with pytest.raises(ft.FeishuTargetError) as caught:
        ft.guard_target(GOOD, "open_id", "oc_home_group")
    assert "不一致" in str(caught.value)


def test_sending_with_wrong_type_is_refused():
    with pytest.raises(ft.FeishuTargetError):
        ft.guard_target(GOOD, "chat_id", "ou_owner123456")


def test_matching_target_passes_and_builds_payload():
    target = ft.guard_target(GOOD, "open_id", "ou_owner123456")
    payload = ft.card_payload(target, {"header": {"title": {"tag": "plain_text", "content": "x"}}})
    assert payload["receive_id"] == "ou_owner123456" and payload["msg_type"] == "interactive"


def test_empty_card_is_refused():
    with pytest.raises(ft.FeishuTargetError):
        ft.card_payload(ft.target_from_config(GOOD), {})


def test_real_local_config_matches_the_guard():
    import json
    import pathlib

    path = pathlib.Path.home() / ".agent-mailbox-v08" / "secrets" / "feishu.json"
    if not path.exists():
        pytest.skip("本机没有飞书配置（CI 环境）")
    config = json.loads(path.read_text())
    target = ft.target_from_config(config)
    assert target.kind == "dm" and target.receive_id.startswith("ou_")
