"""外发通知的**发送器**：白名单（默认沉默）+ 目标守卫 + 状态哈希门控（③ 接线 ✓）。

三条设计约束都来自评审与实测 ✓：
1. **默认沉默** ✓：只有白名单里的 kind 才会外发 ✗（进度、每次状态变化、每条信件一律不发 ✓）
2. **必须过 `guard_target()`** ✓：飞书守卫此前**零调用点** ✗（评审实测 ✓）⇒ 发送器是它的**唯一入口** ✓

   守卫语义（精确 ✓ 不含糊 ✗）：**目标只能来自显式配置** —— 本函数**不接受**调用方传目标 ✗
   （所以"发到别处"在结构上不可能 ✓）；`guard_target()` 校验的是**配置本身合法**：
   kind 必须 dm ✓ receive_id_type 必须 open_id ✓ receive_id 非空 ✓。
3. **没变就闭嘴** ✓：同 `(kind, 目标, state_hash)` 已在 `notifications` 表里 ⇒ **不再发送** ✓
   （幂等由唯一索引保证 ✓ 不是靠本模块自觉 ✗）

传输层用**可注入的 `transport`** ✓（真实实现打飞书 API ✓ 测试用假函数 ✓ 不联网 ✗）。
"""

from __future__ import annotations

from collections.abc import Callable

from .workbench_feishu_target import Target, guard_target
from .workbench_store import WorkbenchStore

# 白名单（**默认沉默** ✓）：打断级 5 类 —— 其余一律不发 ✗
SEND_KINDS: frozenset[str] = frozenset(
    {
        "decision",  # 需你决策（授权请求 ✓ 阻塞性）
        "review",  # 等你验收
        "stuck",  # 卡住 / 失败
        "quota",  # 额度耗尽 / 配额（不占 routine 预算 ✓ 直达人 ✓）
        "security",  # 安全类（无论额度 ✓）
        "projection",  # 常驻投影卡（一屏三问 ✓）：首次 send / 之后 patch ✓ 变了才动 ✗
    }
)
CHANNEL = "feishu_dm"
Transport = Callable[[Target, dict], str]


class NotifySkipped(Exception):
    """不该发的（不在白名单 ✓ 或状态没变 ✓）——**不是错误** ✗ 调用方按"已处理"对待 ✓。"""

    def __init__(self, reason: str, notification: dict | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.notification = notification


def should_send(kind: str) -> bool:
    """白名单判定 ✓（未知 kind ⇒ **不发** ✗ 默认沉默 ✓）。"""
    return kind in SEND_KINDS


def send_card(
    store: WorkbenchStore,
    *,
    kind: str,
    state_hash: str,
    card: dict,
    target_config: dict,
    transport: Transport,
    project_id: str | None = None,
    task_id: str | None = None,
) -> dict:
    """按三条约束发一条卡片 ✓；不该发时抛 `NotifySkipped` ✗（调用方无需当异常处理 ✓）。

    顺序即防线 ✓：白名单 ⇒ 守卫 ⇒ 幂等 ⇒ 发送 ⇒ 记录结果 ✓
    """
    if not should_send(kind):
        raise NotifySkipped(f"kind={kind!r} 不在发送白名单内（默认沉默 ✓）")
    target = guard_target(
        target_config,
        target_config["target"]["receive_id_type"],
        target_config["target"]["receive_id"],
    )  # ① 守卫：目标必须显式且一致 ✓
    existing = store.record_notification(
        kind,
        channel=CHANNEL,
        target_kind=target.kind,
        target_id=target.receive_id,
        state_hash=state_hash,
        project_id=project_id,
        task_id=task_id,
    )  # ② 幂等登记 ✓
    if existing["status"] != "pending" or existing["attempts"]:
        raise NotifySkipped("该状态已发送过（没变就闭嘴 ✓）", existing)  # ③ 已发过 ⇒ 不发 ✓
    try:
        message_id = transport(target, card)
    except Exception as exc:  # ④ 发送失败要**留痕** ✓
        store.mark_notification(existing["id"], status="failed", last_error=str(exc)[:200])
        raise
    return store.mark_notification(
        existing["id"], status="sent", external_message_id=message_id
    )  # ⑤
