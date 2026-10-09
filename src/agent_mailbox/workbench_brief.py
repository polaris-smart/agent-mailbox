"""Session-start brief: the cheap half of the wake ladder (roadmap T31).

Our wake history is the reason this module exists: v0.7 woke a process **per
letter** (WatchPaths on the inbox), and on 2026-10-04 that became a storm. The
roadmap therefore asks for two channels — a **hook** (cheap, zero daemon) and an
optional single kernel (for an idle agent that must be woken). This module is the
hook half: when a host starts a session it calls :func:`brief` once and pastes the
result into context.

Rules:
  * **bounded output** — the brief obeys a byte budget (the T6 discipline); a
    brief that can grow without limit is just the storm in a new suit;
  * **read-only** — no daemon, no writes, nothing to crash at 3am;
  * **denoised** — counts and titles, never a message firehose.
"""

from __future__ import annotations

from typing import Any

from . import workbench_contacts, workbench_policy
from .workbench_artifact import MAX_BUDGET

DEFAULT_BUDGET = 1200
ACTIVE = ("starting", "running", "waiting_approval")


def brief(
    store,
    *,
    employee_id: str | None = None,
    project_id: str | None = None,
    budget: int = DEFAULT_BUDGET,
) -> dict[str, Any]:
    if project_id:
        store.ensure_project(project_id)
    """What a session should know before it acts. Never writes, never exceeds budget."""
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        raise ValueError("budget 需要正整数（字节）")
    if budget > MAX_BUDGET:
        raise ValueError(f"budget 超过上限 {MAX_BUDGET} 字节")
    scope_t = " AND t.project_id = ?" if project_id else ""
    scope_m = " AND project_id = ?" if project_id else ""
    params = (project_id,) if project_id else ()
    with store._transaction(readonly=True) as db:
        unread = []
        if employee_id:
            rows = db.execute(
                "SELECT title, sender_id, created_at FROM messages WHERE recipient_id = ?"
                + (" AND project_id = ?" if project_id else "")
                + " ORDER BY created_at DESC LIMIT 20",
                (employee_id, *params),
            ).fetchall()
            unread = [{k: row[k] for k in row.keys()} for row in rows]  # noqa: SIM118
        # 参数化：绝不能把 employee_id 拼进 SQL（撇号会崩，`x' OR '1'='1` 会读到别人的活）
        waiting_clause = " AND t.assignee_id = ?" if employee_id else ""
        waiting_params = ((employee_id,) if employee_id else ()) + params
        waiting = [
            {k: row[k] for k in row.keys()}  # noqa: SIM118
            for row in db.execute(
                "SELECT t.id, t.title, t.updated_at FROM tasks t WHERE t.status = 'review'"
                + waiting_clause
                + (scope_t or "")
                + " ORDER BY t.updated_at DESC LIMIT 10",
                waiting_params,
            ).fetchall()
        ]
        running = [
            {k: row[k] for k in row.keys()}  # noqa: SIM118
            for row in db.execute(
                f"SELECT t.id, t.title, t.updated_at FROM tasks t WHERE t.status IN "
                f"({','.join('?' * len(ACTIVE))})"
                + (scope_t or "")
                + " ORDER BY t.updated_at DESC LIMIT 10",
                (*ACTIVE, *params),
            ).fetchall()
        ]
        signals = {
            "folded": db.execute(
                "SELECT count(*) AS c FROM governance_events WHERE type='message_folded'"
                + (scope_m or ""),
                params,
            ).fetchone()["c"],
            "escalations_suppressed": db.execute(
                "SELECT count(*) AS c FROM governance_events WHERE type='escalation_suppressed'"
                + (scope_m or ""),
                params,
            ).fetchone()["c"],
        }
        policy = workbench_policy.effective_policy_in(db, project_id)
        contacts_count = (
            len(workbench_contacts.contacts_in(db, project_id, employee_id)["contacts"])
            if employee_id and project_id
            else None
        )
    return {
        "employee_id": employee_id,
        "project_id": project_id,
        "unread": {"count": len(unread), "recent": unread[:5]},
        "waiting_on_human": waiting,
        "running": running,
        "signals": signals,
        "policy": {
            "notification_mode": policy.get("notification_mode"),
            "peer_review": policy.get("peer_review"),
            "contacts": contacts_count,
        },
        "budget": budget,
    }


def render(data: dict[str, Any], *, budget: int = DEFAULT_BUDGET) -> str:
    """Compact text a host can paste; truncated to the byte budget (last line wins)."""
    lines = [
        f"【交接简报】项目 {data.get('project_id') or '（全部）'}"
        + (f" · 员工 {data['employee_id']}" if data.get("employee_id") else ""),
        f"  待我处理：未读 {data['unread']['count']} · 等人验收 {len(data['waiting_on_human'])} · 在跑 {len(data['running'])}",
    ]
    for row in data["unread"]["recent"][:3]:
        lines.append(f"  · 未读：{str(row.get('title') or '')[:40]}（{row.get('sender_id')}）")
    for row in data["waiting_on_human"][:3]:
        lines.append(f"  · 等人验收：{str(row.get('title') or '')[:40]}")
    signals = data["signals"]
    if signals.get("folded") or signals.get("escalations_suppressed"):
        lines.append(
            f"  · 已折叠 {signals['folded']} 条 · 升级被抑制 {signals['escalations_suppressed']} 次"
            "（安静不等于无事，必要时查团队墙）"
        )
    policy = data["policy"]
    lines.append(
        f"  策略：通知={policy.get('notification_mode')} · 同行校验={policy.get('peer_review')}"
        + (f" · 通讯录 {policy['contacts']} 人" if policy.get("contacts") is not None else "")
    )
    text = "\n".join(lines)
    encoded = text.encode("utf-8")
    if len(encoded) > budget:
        # 预算必须**真**守住：先给提示语留位置，再截内容（否则截断后反而超预算）
        marker = "\n…（已按预算截断）"
        marker_bytes = len(marker.encode("utf-8"))
        keep = max(0, budget - marker_bytes)
        text = encoded[:keep].decode("utf-8", errors="ignore") + marker
    return text
