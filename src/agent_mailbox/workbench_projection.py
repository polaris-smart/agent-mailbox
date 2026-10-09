"""常驻投影卡（一屏三问）：**在跑 / 等谁 / 卡住** + 今日成果（③ 的用户可见面 ✓）。

设计（评审与老板的共同要求 ✓）：
* **投影而非真源** ✓：内容全部从本地 store 现算 ✓（飞书只是通道 ✗ 本地才是权威 ✓）
* **状态哈希门控** ✓：哈希没变 ⇒ `skip` ✓ **一个字节都不发** ✗；变了且有旧卡 ⇒ `patch` ✓
  （原地更新 ✓ 用 `notifications.external_message_id` ✓）；没旧卡 ⇒ `send` ✓
* 本模块只**计算与决策** ✓ 不做 HTTP ✓（发送/更新由 `transport` 注入 ✓ 便于测试 ✓）
"""

from __future__ import annotations

import hashlib
import json

from .workbench_store import WorkbenchStore

PROJECTION_KIND = "projection"
PROJECTION_TARGET_KIND = "dm"


def projection_payload(store: WorkbenchStore, *, project_id: str | None = None) -> dict:
    """算出一屏三问的载荷 ✓（只读 ✓ 不改状态 ✓ **数据源是真实列值** ✓ 不猜键名 ✗）。

    三问来源（实测对齐 2026-10-07 ✓）：
    * **在跑** = `running` + `queued`（任务"在飞"的两种 ✓）
    * **等谁** = `review`（待验收 ✓）+ `pending_permissions()`（待授权 ✓）
    * **卡住** = `failed`（+ `cancelled` ✓）
    * 今日成果 = `done` 且 `updated_at` 为今日 ✓
    """
    tasks = store.task_counts_by_status(project_id)
    counts = tasks["counts"]
    permissions = (
        store.pending_permissions() if not project_id else store.pending_permissions(project_id)
    )
    running = counts.get("running", 0) + counts.get("queued", 0)
    reviews = counts.get("review", 0)
    stuck = counts.get("failed", 0) + counts.get("cancelled", 0)
    return {
        "running": running,
        "waiting_on_human": reviews + len(permissions),
        "permissions": len(permissions),
        "reviews": reviews,
        "stuck": stuck,
        "done_today": tasks["done_today"],
        "items": {
            "running": [],
            "waiting": [str(x.get("request_id") or x.get("task_id"))[:60] for x in permissions][:5],
            "stuck": [],
            "today": [str(x)[:60] for x in tasks["today_items"]][:5],
        },
    }


def projection_state_hash(payload: dict) -> str:
    """稳定哈希 ✓（键排序 ✓ 保证"同状态 ⇒ 同哈希" ✓ 也就保证"没变就闭嘴" ✓）。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def projection_card(payload: dict) -> dict:
    """把载荷渲染成飞书卡片（**结构化而非聊天消息** ✓）。"""
    waiting = (
        f"在跑 {payload['running']} · 等你 {payload['waiting_on_human']}"
        f"（授权 {payload['permissions']} / 验收 {payload['reviews']}） · 卡住 {payload['stuck']}"
    )
    lines = [f"**{waiting}**", f"今日成果：**{payload['done_today']}** 件"]
    for label, key in (("在跑", "running"), ("等你", "waiting"), ("卡住", "stuck")):
        items = payload["items"].get(key) or []
        if items:
            lines.append(f"{label}：" + " · ".join(items[:3]))
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "agent-mailbox · 一屏三问"},
            "template": "blue",
        },
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}],
    }


def decide_projection(
    store: WorkbenchStore, *, target_kind: str, target_id: str, payload: dict
) -> tuple[str, dict | None, str]:
    """决策：`send`（首次）/ `patch`（变了且已有卡 ✓）/ `skip`（没变 ✓）。纯读 ✓。

    返回 `(action, previous_row, state_hash)` ✓ —— 调用方据此发或改 ✓ 本函数不写库 ✗。
    """
    state_hash = projection_state_hash(payload)
    previous = store.latest_notification(
        PROJECTION_KIND, target_kind=target_kind, target_id=target_id
    )
    if previous is None:
        return "send", None, state_hash
    if previous["state_hash"] == state_hash:
        return "skip", previous, state_hash
    return "patch", previous, state_hash


def sync_projection(
    store: WorkbenchStore, *, target_config: dict, sender, patcher, project_id: str | None = None
) -> dict:
    """同步投影卡：**首次 send ✓ 变了 patch ✓ 没变一个字都不发** ✗（守卫必过 ✓）。"""
    from .workbench_feishu_target import guard_target
    from .workbench_notify import NotifySkipped, send_card

    payload = projection_payload(store, project_id=project_id)
    target_kind = str(target_config["target"]["kind"])
    target_id = str(target_config["target"]["receive_id"])
    action, previous, state_hash = decide_projection(
        store, target_kind=target_kind, target_id=target_id, payload=payload
    )
    if action == "skip":
        raise NotifySkipped("投影卡没变（没变就闭嘴 ✓）", previous)
    card = projection_card(payload)
    target = guard_target(
        target_config,
        target_config["target"]["receive_id_type"],
        target_config["target"]["receive_id"],
    )
    if action == "send":
        row = send_card(
            store,
            kind=PROJECTION_KIND,
            state_hash=state_hash,
            card=card,
            target_config=target_config,
            transport=sender,
            project_id=project_id,
        )
        return {
            "action": "send",
            "state_hash": state_hash,
            "message_id": row["external_message_id"],
        }
    message_id = patcher(previous["external_message_id"], card)  # 原地更新 ✓
    row = store.record_notification(
        PROJECTION_KIND,
        channel="ch",
        target_kind=target_kind,
        target_id=target_id,
        state_hash=state_hash,
        project_id=project_id,
    )
    sent = store.mark_notification(row["id"], status="sent", external_message_id=message_id)
    return {
        "action": "patch",
        "state_hash": state_hash,
        "message_id": sent["external_message_id"],
        "target": target.receive_id,
    }
