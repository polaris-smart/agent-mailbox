"""传输层：打码 ✓ 不泄凭据 ✓ 目标不可被调用方改 ✗ 失败抛错交 send_card 记痕 ✓。"""

from __future__ import annotations

import json

import pytest

from agent_mailbox.workbench_feishu_target import Target
from agent_mailbox.workbench_feishu_transport import (
    FeishuTransportError,
    make_transport,
    tenant_token,
)

CFG = {"app_id": "cli_abcdefgh", "app_secret": "s3cr3t-value-1234"}


class FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def _opener(*responses):
    queue = list(responses)

    def open_(request, timeout=None):
        if not queue:
            raise AssertionError("多余的 HTTP 调用 ✗")
        payload = queue.pop(0)
        open_.calls.append(
            (request.full_url, json.loads(request.data or b"{}"), dict(request.headers))
        )
        return FakeResponse(payload)

    open_.calls = []
    return open_


def test_token_is_fetched_with_config_credentials_only():
    opener = _opener({"code": 0, "tenant_access_token": "t-123"})
    assert tenant_token(CFG, opener=opener) == "t-123"
    url, body, _headers = opener.calls[0]
    assert url.endswith("tenant_access_token/internal") and body == {
        "app_id": CFG["app_id"],
        "app_secret": CFG["app_secret"],
    }


def test_missing_credentials_refuse_without_http():
    opener = _opener()
    with pytest.raises(FeishuTransportError):
        tenant_token({}, opener=opener)
    assert opener.calls == [], "缺凭据 ⇒ 一个请求都不发 ✓"


def test_error_message_masks_the_secret():
    opener = _opener({"code": 99, "msg": "bad"})
    with pytest.raises(FeishuTransportError) as caught:
        tenant_token(CFG, opener=opener)
    message = str(caught.value)
    assert CFG["app_secret"] not in message, "异常里**不得**出现密钥明文 ✗"
    assert "cli_ab…" in message, "只留打码后的 app_id ✓"


def test_transport_sends_to_the_target_it_is_given_and_returns_message_id():
    opener = _opener(
        {"code": 0, "tenant_access_token": "t-1"}, {"code": 0, "data": {"message_id": "om_9"}}
    )
    transport = make_transport(CFG, opener=opener)
    target = Target(kind="dm", receive_id_type="open_id", receive_id="ou_owner123456")
    assert transport(target, {"header": {"title": {"tag": "plain_text", "content": "x"}}}) == "om_9"
    url, body, headers = opener.calls[1]
    assert "receive_id_type=open_id" in url and body["receive_id"] == "ou_owner123456"
    assert headers.get("Authorization") == "Bearer t-1"


def test_send_failure_raises_so_send_card_can_record_it():
    opener = _opener(
        {"code": 0, "tenant_access_token": "t-1"}, {"code": 230002, "msg": "no permission"}
    )
    transport = make_transport(CFG, opener=opener)
    with pytest.raises(FeishuTransportError) as caught:
        transport(Target("dm", "open_id", "ou_x"), {"a": 1})
    assert "230002" in str(caught.value)
