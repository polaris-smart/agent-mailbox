"""飞书传输层：把 `send_card()` 的卡片真的发出去（③ 接线最后一件 ✓）。

设计要点（评审 + 事故双重教训 ✓）：
* **只读配置**：凭据从配置读 ✓（键名 `app_id`/`app_secret` ✓）—— 本模块**不接受**调用方传目标 ✗
  （目标只能来自 `send_card()` 里那个显式配置 ✓ ⇒ 守卫是唯一入口 ✓，与"零调用点" ✗ 相反）
* **不打印凭据** ✗：日志/异常里只出现 `app_id` 前 6 位与长度 ✓（事故纪律 ✓）
* **可测**：HTTP 走 `urlopen` 注入点 ✓ ⇒ 单测用假响应 ✓ 不联网 ✓
* **失败要能被 send_card 记痕** ✓：本函数只抛异常 ✓ 不吞 ✗（`send_card` 会写 failed + last_error ✓）
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"


class FeishuTransportError(RuntimeError):
    """传输失败（网络/凭据/权限）⇒ 由 `send_card` 记入 failed + last_error ✓。"""


def _mask(value: str) -> str:
    """凭据打码（只留前 6 位与长度 ✓ 事故纪律：明文不进日志 ✗）。"""
    return f"{value[:6]}…({len(value)})" if value else "<空>"


def _post(
    url: str,
    payload: dict,
    *,
    timeout: float,
    opener=urllib.request.urlopen,
    headers: dict | None = None,
) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with opener(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except Exception as exc:
        raise FeishuTransportError(f"飞书请求失败：{type(exc).__name__}") from exc


def tenant_token(config: dict, *, opener=urllib.request.urlopen, timeout: float = 15.0) -> str:
    """取 `tenant_access_token`（凭据只在内存里用 ✓ 不外泄 ✓）。"""
    app_id, app_secret = str(config.get("app_id") or ""), str(config.get("app_secret") or "")
    if not app_id or not app_secret:
        raise FeishuTransportError("配置缺少 app_id/app_secret ⇒ 拒绝发送 ✗")
    body = _post(
        TOKEN_URL, {"app_id": app_id, "app_secret": app_secret}, timeout=timeout, opener=opener
    )
    token = body.get("tenant_access_token")
    if not token:
        raise FeishuTransportError(
            f"取 token 失败：code={body.get('code')} msg={body.get('msg')}（app_id={_mask(app_id)}）"
        )
    return str(token)


def make_transport(config: dict, *, opener=urllib.request.urlopen, timeout: float = 15.0):
    """返回一个 `transport(target, card) -> message_id` ✓（供 `send_card()` 注入 ✓）。

    注意：**目标不来自调用方** ✗ —— `target` 由 `send_card()` 从显式配置解析并交进来 ✓，
    这里只做"照着发" ✓ 不允许改目标 ✗。
    """

    def transport(target, card: dict) -> str:
        token = tenant_token(config, opener=opener, timeout=timeout)
        url = f"{MESSAGE_URL}?receive_id_type={target.receive_id_type}"
        body = _post(
            url,
            {
                "receive_id": target.receive_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
            timeout=timeout,
            opener=opener,
            headers={"Authorization": f"Bearer {token}"},
        )
        if body.get("code") != 0:
            raise FeishuTransportError(f"发送失败：code={body.get('code')} msg={body.get('msg')}")
        message_id: Any = (body.get("data") or {}).get("message_id")
        if not message_id:
            raise FeishuTransportError("飞书未返回 message_id ⇒ 视为失败 ✓")
        return str(message_id)

    return transport


def make_patcher(config: dict, *, opener=urllib.request.urlopen, timeout: float = 15.0):
    """返回 `patcher(message_id, card) -> message_id` ✓（**原地更新**既有卡 ✓ 不新增消息 ✗）。"""

    def patcher(message_id: str, card: dict) -> str:
        if not message_id:
            raise FeishuTransportError("缺少 message_id ⇒ 无法原地更新 ✗")
        token = tenant_token(config, opener=opener, timeout=timeout)
        body = _post(
            f"{MESSAGE_URL}/{message_id}",
            {"msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)},
            timeout=timeout,
            opener=opener,
            headers={"Authorization": f"Bearer {token}"},
        )
        if body.get("code") != 0:
            raise FeishuTransportError(f"更新失败：code={body.get('code')} msg={body.get('msg')}")
        return message_id

    return patcher
