"""飞书发送目标守卫（HS 裁定 2026-10-07：目标必须显式配置 + 发送前校验）。

**状态：尚未接线（2026-10-07 对抗评审实测）** —— 本仓目前**没有飞书发送实现**，
全仓 grep 无生产调用者 ⇒ 本模块**当前拦不住任何真实发送** ✗。
它现在的作用是：把 HS 的裁定固化成**可测的契约**，等发送器接入时**必须**经由 `guard_target()` ✓。
在接入之前，不得对外宣称"发送前校验已生效" ✗（宣称与事实分离 ✓）。

背景（真实事故 ✗）：因从别的配置文件猜 chat_id，把测试卡误发到了老板的 Home 群。
HS 裁定：open_id 写进**显式配置**，发送前校验目标非空且与配置一致才发 —— **落成代码校验**
（不是口头规矩；他验收时会核对本条是否已实现）。

* `target_from_config()` —— 从本机配置读目标（形状 `{"target": {"kind": "dm", ...}}`）
* `guard_target()` —— **只允许发往配置里的那一个目标**：任何不一致一律拒发
* `card_payload()` —— 生成发送请求体（纯函数、不含凭据，便于 dry-run 与单测）
"""

from __future__ import annotations

from dataclasses import dataclass

ALLOWED_KINDS = ("dm",)
ALLOWED_RECEIVE_ID_TYPES = ("open_id",)


class FeishuTargetError(RuntimeError):
    """目标非法 ⇒ **绝不发送**（宁可报错，也不误发到群里）。"""


@dataclass(frozen=True)
class Target:
    kind: str
    receive_id_type: str
    receive_id: str


def target_from_config(config: dict) -> Target:
    """读显式配置；缺字段就报错（**不猜**，不从别处兜底）。"""
    raw = (config or {}).get("target")
    if not isinstance(raw, dict):
        raise FeishuTargetError("配置里没有 target：飞书目标必须显式配置（不许从别的配置文件猜）")
    kind = str(raw.get("kind") or "").strip()
    receive_id_type = str(raw.get("receive_id_type") or "").strip()
    receive_id = str(raw.get("receive_id") or "").strip()
    if kind not in ALLOWED_KINDS:
        raise FeishuTargetError(
            f"kind 必须是 {'/'.join(ALLOWED_KINDS)}（当前 {kind!r}）—— 一屏三问是老板私人决策卡，发群等于逼他公开表态"
        )
    if receive_id_type not in ALLOWED_RECEIVE_ID_TYPES:
        raise FeishuTargetError(
            f"receive_id_type 必须是 {'/'.join(ALLOWED_RECEIVE_ID_TYPES)}（当前 {receive_id_type!r}）"
        )
    if not receive_id:
        raise FeishuTargetError("receive_id 为空 ⇒ 拒绝发送")
    return Target(kind=kind, receive_id_type=receive_id_type, receive_id=receive_id)


def guard_target(config: dict, receive_id_type: str, receive_id: str) -> Target:
    """发送前校验：**只允许发往配置里的那一个目标**；别的 chat/用户一律拒。"""
    target = target_from_config(config)
    if (receive_id_type or "").strip() != target.receive_id_type:
        raise FeishuTargetError(
            f"接收类型不一致：调用方 {receive_id_type!r} ≠ 配置 {target.receive_id_type!r} ⇒ 拒绝发送"
        )
    if (receive_id or "").strip() != target.receive_id:
        raise FeishuTargetError("接收方与配置不一致 ⇒ 拒绝发送（根因：目标可被调用方指定）")
    return target


def card_payload(target: Target, card: dict) -> dict:
    """生成发送请求体（纯函数、不含凭据，可直接单测/dry-run）。"""
    if not isinstance(card, dict) or not card:
        raise FeishuTargetError("卡片内容为空 ⇒ 拒绝发送")
    return {"receive_id": target.receive_id, "msg_type": "interactive", "content": card}
