"""Review policy: notify policy, escalation budget, peer review (roadmap T30/审核机制).

Three questions the product could not answer before this module:

1. **notify_policy** — when may the platform bother a human? (default ``manual_check``:
   the human looks when they choose to; nothing is pushed).
2. **escalation_budget** — how many automated escalations may be spent per hour?
   Without a budget, every failure escalates and the human channel becomes the storm
   (measured on 2026-10-04: 157 pushes, 118 suppressed, one thread produced 15
   "processing failed" letters).
3. **peer_review** — must a *different* employee verify a write-capable delivery
   before it is applied? (two-person rule; default ``off`` so existing behaviour
   does not change).

Storage follows the ledger discipline: **no new table**. A policy change is a
``policy_updated`` governance event, and the effective policy is *derived*
(defaults ← global override ← project override, newest wins). That keeps policy
auditable, revertible, and impossible to drift from the record.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

POLICY_EVENT = "policy_updated"
VERIFY_EVENT = "delivery_verified"  # 与 workbench_proof 同一事件类型（同行校验的来源）
PROOF_EVENT = "delivery_proof"  # 交付证明事件（同行校验要绑到最新一版）
ESCALATION_EVENT = "escalation"
ESCALATION_SUPPRESSED_EVENT = "escalation_suppressed"

DEFAULTS: dict[str, Any] = {
    "ordinary_mail_starts_work": False,
    "reading_acknowledges": False,
    "notification_mode": "manual_check",
    "managed_execution": False,
    "escalation_budget": 3,
    # 派单特权三闸：只有人能派 · 每小时上限（防风暴主力）· 同目标去重（默认关，按项目开）
    "dispatch": {"employees_may_dispatch": [], "max_tasks_per_hour": 60, "dedupe_window_hours": 0},
    "peer_review": "off",
}

_STRINGS = {
    "notification_mode": {"manual_check", "folded_digest", "immediate"},
    "peer_review": {"off", "required_for_write"},
}
_BOOLS = {"ordinary_mail_starts_work", "reading_acknowledges", "managed_execution"}
_INTS = {"escalation_budget"}
ALLOWED = set(_STRINGS) | _BOOLS | _INTS | {"dispatch"}


def _payload(event: dict) -> dict:
    value = event.get("payload")
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        import json

        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _parse(key: str, value: Any) -> Any:
    """Coerce **per key type** (CLI/env give strings).

    原先对所有键统一归一，把字符串键的 "off" 变成了 False，于是
    ``peer_review=required_for_write`` 之后**再也回不到 off**（一切可逆被破）。
    现在：布尔键才做布尔归一，整数键才转整数，字符串键原样交给校验。
    """
    if key in _BOOLS:
        if isinstance(value, bool):
            return value
        low = str(value).strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
        return value
    if key in _INTS:
        if isinstance(value, (bool, int)):
            return value
        text = str(value).strip()
        return int(text) if text.lstrip("-").isdigit() else value
    if key in _STRINGS and isinstance(value, str):
        return value.strip().lower()  # 容错大小写：可逆性不该被 "Off" 绊倒
    return value  # 其余字符串键原样


def _clean_dispatch(raw: Any) -> dict[str, Any]:
    """派单策略：只有人能派 · 每小时上限（防风暴主力）· 同目标去重窗口（0 = 关）。"""
    if not isinstance(raw, dict):
        raise TypeError(
            "dispatch 需要是一个对象：employees_may_dispatch / max_tasks_per_hour / dedupe_window_hours"
        )
    clean: dict[str, Any] = {}
    if "employees_may_dispatch" in raw:
        names = raw["employees_may_dispatch"]
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ValueError("dispatch.employees_may_dispatch 需要是员工名列表")
        clean["employees_may_dispatch"] = [n.strip() for n in names if n.strip()]
    for key in ("max_tasks_per_hour", "dedupe_window_hours"):
        if key in raw:
            value = raw[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"dispatch.{key} 需要是非负整数")
            clean[key] = value
    return clean


def validate(updates: dict[str, Any]) -> dict[str, Any]:
    """Normalise and reject unknown keys or out-of-range values."""
    clean: dict[str, Any] = {}
    for key, raw in updates.items():
        if key == "dispatch":
            clean[key] = _clean_dispatch(raw)
            continue
        if key not in ALLOWED:
            raise ValueError(f"未知策略键：{key}（可选：{', '.join(sorted(ALLOWED))}）")
        value = _parse(key, raw)
        if key in _STRINGS and value not in _STRINGS[key]:
            raise ValueError(f"{key} 只能是：{', '.join(sorted(_STRINGS[key]))}")
        if key in _BOOLS and not isinstance(value, bool):
            raise ValueError(f"{key} 需要 true/false")
        if key in _INTS and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError(f"{key} 需要 ≥0 的整数")
        clean[key] = value
    if not clean:
        raise ValueError("没有需要修改的策略项")
    return clean


def effective_policy_in(db, project_id: str | None = None) -> dict[str, Any]:
    """Same as :func:`effective_policy` but on an existing connection.

    Callers that already hold a write transaction must use this: opening a second
    connection while writing is exactly the pattern we removed elsewhere.
    """
    policy = dict(DEFAULTS)
    rows = db.execute(
        "SELECT project_id, payload, created_at FROM governance_events "
        "WHERE type = ? ORDER BY created_at, rowid",
        (POLICY_EVENT,),
    ).fetchall()
    overrides = [{k: row[k] for k in row.keys()} for row in rows]  # noqa: SIM118
    owners = [row["project_id"] for row in overrides]
    payloads = [_payload(row) for row in overrides]
    for owner, payload in zip(owners, payloads):  # ① 全局覆盖
        if owner is None:
            policy.update({k: v for k, v in payload.items() if k in ALLOWED})
    if project_id is not None:  # ② 项目覆盖（后于全局）
        for owner, payload in zip(owners, payloads):
            if owner == project_id:
                policy.update({k: v for k, v in payload.items() if k in ALLOWED})
    return policy


def effective_policy(store, project_id: str | None = None) -> dict[str, Any]:
    """Defaults, then global overrides, then project overrides (newest wins)."""
    with store._transaction(readonly=True) as db:
        if project_id:
            # 打错的项目不许被静默当成"全局"：否则查"这个项目开没开同行校验"会拿到默认值
            store._required(db, "projects", project_id)
        return effective_policy_in(db, project_id)


def peer_verified_in(db, task_id: str, assignee_id: str | None) -> bool:
    """Has a *different* employee verified the task's **current** delivery proof?

    绑定的意义：验过 V1 不代表 V2 也验过。原先只看"存在一条 verified"，于是
    交付内容整批换新（新 proof_sha256）后门禁依旧放行 —— 两人规则形同虚设。
    """

    def rows_for(event_type: str) -> list[dict[str, Any]]:
        return [
            {k: row[k] for k in row.keys()}  # noqa: SIM118
            for row in db.execute(
                "SELECT payload FROM governance_events WHERE type=? AND task_id=? "
                "ORDER BY created_at, rowid",
                (event_type, task_id),
            ).fetchall()
        ]

    current = None
    for row in rows_for(PROOF_EVENT):  # 最新一版证明（按插入序取胜）
        current = _payload(row).get("proof_sha256") or current
    if not current:
        return False
    for row in rows_for(VERIFY_EVENT):
        payload = _payload(row)
        verifier = payload.get("verifier_id")
        if (
            payload.get("verdict") == "verified"
            and payload.get("proof_sha256") == current  # 必须是这一版
            and verifier
            and verifier != assignee_id
        ):
            return True
    return False


def peer_verified(store, task_id: str, assignee_id: str | None) -> bool:
    with store._transaction(readonly=True) as db:
        return peer_verified_in(db, task_id, assignee_id)


def set_policy(
    store,
    updates: dict[str, Any],
    *,
    project_id: str | None = None,
    actor: str = "human",
) -> dict[str, Any]:
    """Record a policy change as a governance event and return the effective policy."""
    clean = validate(updates)
    with store._transaction() as db:
        if project_id is not None:
            store._required(db, "projects", project_id)
        store._governance(
            db,
            POLICY_EVENT,
            project_id=project_id,
            actor=actor,
            reason="策略更新：" + ", ".join(f"{k}={v}" for k, v in sorted(clean.items())),
            payload=clean,
        )
    return effective_policy(store, project_id)


def escalation_allowed(store, project_id: str | None = None, *, now: str | None = None) -> bool:
    """May another escalation be spent this hour? (anti-storm: budget, not infinity)."""
    policy = effective_policy(store, project_id)
    budget = int(policy.get("escalation_budget") or 0)
    anchor = _parse_time(now) or datetime.now(timezone.utc)
    cutoff = (anchor - timedelta(hours=1)).isoformat()
    with store._transaction(readonly=True) as db:  # 纯查询：只读，不拿写锁
        row = db.execute(
            "SELECT count(*) AS c FROM governance_events WHERE type=? AND (project_id=? OR project_id IS NULL) "
            "AND created_at >= ?",
            (ESCALATION_EVENT, project_id, cutoff),
        ).fetchone()
    return row["c"] < budget


def record_escalation(
    store, project_id: str | None, *, task_id: str | None, reason: str, actor: str = "system"
) -> dict[str, Any]:
    """Spend one escalation, or record that it was suppressed by the budget.

    Returns ``{"escalated": bool, "reason": str}``. Suppression is itself recorded,
    so a quiet channel is never an invisible one.
    """
    if escalation_allowed(store, project_id):
        with store._transaction() as db:
            store._governance(
                db,
                ESCALATION_EVENT,
                project_id=project_id,
                task_id=task_id,
                actor=actor,
                reason=reason,
                payload={"task_id": task_id},
            )
        return {"escalated": True, "reason": reason}
    with store._transaction() as db:
        store._governance(
            db,
            ESCALATION_SUPPRESSED_EVENT,
            project_id=project_id,
            task_id=task_id,
            actor=actor,
            reason=f"升级预算已用完，抑制升级：{reason}",
            payload={"task_id": task_id, "suppressed_reason": reason},
        )
    return {"escalated": False, "reason": "escalation_budget_exhausted"}


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
