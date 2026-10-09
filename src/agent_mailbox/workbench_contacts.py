"""Contacts and handshake: who may message whom (roadmap T33, T18 的前提).

Absorbed as a *method* from the competitor read-through (contacts + a handshake
macro): a lightweight "address book" plus an explicit request/accept step, with no
separate database — here a contact is a governance event, like every other rule we
own.

Two design choices keep it safe to ship:

* **open by default.** A recipient with **no** contact list accepts mail from
  anyone (today's behaviour). Only once a recipient has at least one contact does
  the whitelist bind — strictness is opt-in, so turning this on cannot break an
  existing project.
* **a request grants nothing.** ``contact_requested`` is an intent; only
  ``contact_added`` grants access, and ``contact_removed`` revokes it. The
  handshake is therefore expressible with the ledger we already have.
"""

from __future__ import annotations

import json
from typing import Any

from . import workbench_events

REQUESTED_EVENT = "contact_requested"
ADDED_EVENT = "contact_added"
REMOVED_EVENT = "contact_removed"


def _payload(event: dict) -> dict:
    value = event.get("payload")
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _events(store, project_id: str | None = None) -> list[dict]:
    kinds = (REQUESTED_EVENT, ADDED_EVENT, REMOVED_EVENT)
    rows = [
        event
        for event in store.governance_events(limit=None)
        if event.get("type") in kinds
        and (project_id is None or event.get("project_id") in (project_id, None))
    ]
    rows.sort(key=workbench_events.event_order)  # 同秒靠插入序（uuid 不能当平局键）
    return rows


def _events_in(db, project_id: str | None = None) -> list[dict]:
    """Same as :func:`_events` but on an existing connection (no nested connection)."""
    rows = db.execute(
        "SELECT * FROM governance_events WHERE type IN (?,?,?) ORDER BY created_at, rowid",
        (REQUESTED_EVENT, ADDED_EVENT, REMOVED_EVENT),
    ).fetchall()
    out = [{k: row[k] for k in row.keys()} for row in rows]  # noqa: SIM118
    if project_id is not None:
        out = [event for event in out if event.get("project_id") in (project_id, None)]
    return out


def contacts_in(db, project_id: str, employee_id: str) -> dict[str, Any]:
    """Address book computed on an existing connection."""
    return _build_contacts(_events_in(db, project_id), employee_id)


def contacts(store, project_id: str, employee_id: str) -> dict[str, Any]:
    if project_id:
        store.ensure_project(project_id)
    """The address book of one employee: granted targets and pending requests."""
    return _build_contacts(_events(store, project_id), employee_id)


def _build_contacts(events: list[dict], employee_id: str) -> dict[str, Any]:
    granted: dict[str, dict] = {}
    pending: dict[str, dict] = {}
    for event in events:
        payload = _payload(event)
        if payload.get("owner_id") != employee_id:
            continue
        target = str(payload.get("target_id") or "")
        if not target:
            continue
        if event["type"] == ADDED_EVENT:
            granted[target] = {
                "target_id": target,
                "note": payload.get("note") or "",
                "added_at": event.get("created_at"),
                "added_by": event.get("actor"),
            }
            pending.pop(target, None)
        elif event["type"] == REMOVED_EVENT:
            granted.pop(target, None)
        elif event["type"] == REQUESTED_EVENT and target not in granted:
            pending[target] = {
                "target_id": target,
                "note": payload.get("note") or "",
                "requested_at": event.get("created_at"),
            }
    return {
        "employee_id": employee_id,
        "contacts": sorted(granted.values(), key=lambda r: r["target_id"]),
        "pending": sorted(pending.values(), key=lambda r: r["target_id"]),
    }


def may_message(
    store, project_id: str, sender_id: str | None, recipient_id: str | None
) -> dict[str, Any]:
    """Whitelist check. Open unless the **recipient** has a list (opt-in strictness)."""
    if not sender_id or not recipient_id or sender_id == recipient_id:
        return {"allowed": True, "reason": "no_restriction"}
    return _verdict(contacts(store, project_id, recipient_id), sender_id)


def may_message_in(
    db, project_id: str, sender_id: str | None, recipient_id: str | None
) -> dict[str, Any]:
    """Same verdict on an existing connection (used by the send path)."""
    if not sender_id or not recipient_id or sender_id == recipient_id:
        return {"allowed": True, "reason": "no_restriction"}
    return _verdict(contacts_in(db, project_id, recipient_id), sender_id)


def whitelisted_members_in(db, project_id: str) -> list[str]:
    """Employees of this project that have a non-empty address book (i.e. who enforce one)."""
    return _whitelisted_in(db, project_id)


def _whitelisted_in(db, project_id: str) -> list[str]:
    """Owners whose **address book is non-empty** — computed by the same rule as the view.

    原先按"该 owner 最近一个 add/remove 事件"判断：于是 Bob 维护两人名单后**正常删掉一人**，
    闸门就被整体关掉（员工群发又能到达白名单收件人）。同一份事件流必须只有一个解释。
    """
    events = _events_in(db, project_id)
    owners: set[str] = set()
    for event in events:
        owner = _payload(event).get("owner_id")
        if owner:
            owners.add(str(owner))
    return sorted(owner for owner in owners if _build_contacts(events, owner)["contacts"])


def _verdict(book: dict[str, Any], sender_id: str) -> dict[str, Any]:
    if not book["contacts"]:
        return {"allowed": True, "reason": "recipient_has_no_list"}
    targets = {row["target_id"] for row in book["contacts"]}
    if sender_id in targets:
        return {"allowed": True, "reason": "listed"}
    return {
        "allowed": False,
        "reason": "not_in_recipient_contacts",
        "recipient_has": sorted(targets),
    }


def request_contact(
    store,
    project_id: str,
    *,
    owner_id: str,
    target_id: str,
    note: str = "",
    actor: str | None = None,
) -> dict:
    """Ask to be added (intent only — grants nothing)."""
    _require_ids(("owner_id", owner_id), ("target_id", target_id))
    with store._transaction() as db:
        store._required(db, "projects", project_id)
        store._governance(
            db,
            REQUESTED_EVENT,
            employee_id=owner_id,
            project_id=project_id,
            actor=actor or f"employee:{owner_id}",
            reason=f"请求加入通讯录：{target_id}",
            payload={"owner_id": owner_id, "target_id": target_id, "note": note},
        )
    return {"requested": True, "owner_id": owner_id, "target_id": target_id}


def _require_ids(*pairs: tuple[str, Any]) -> None:
    """Guard against the common misuse of passing an employee dict instead of its id."""
    for label, value in pairs:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} 需要非空字符串（员工请传 employee['id']，不是整个对象）")


def add_contact(
    store, project_id: str, *, owner_id: str, target_id: str, note: str = "", actor: str = "human"
) -> dict:
    """Grant a sender access to ``owner_id`` (the recipient decides)."""
    _require_ids(("owner_id", owner_id), ("target_id", target_id))
    if owner_id == target_id:
        raise ValueError("不能把自己加进自己的通讯录")
    with store._transaction() as db:
        store._required(db, "projects", project_id)
        for employee_id in (owner_id, target_id):
            store._required(db, "employees", employee_id)
        store._governance(
            db,
            ADDED_EVENT,
            employee_id=owner_id,
            project_id=project_id,
            actor=actor,
            reason=f"通讯录新增：{target_id}",
            payload={"owner_id": owner_id, "target_id": target_id, "note": note},
        )
    return {"added": True, "owner_id": owner_id, "target_id": target_id}


def remove_contact(
    store, project_id: str, *, owner_id: str, target_id: str, actor: str = "human"
) -> dict:
    _require_ids(("owner_id", owner_id), ("target_id", target_id))
    with store._transaction() as db:
        store._required(db, "projects", project_id)
        store._governance(
            db,
            REMOVED_EVENT,
            employee_id=owner_id,
            project_id=project_id,
            actor=actor,
            reason=f"通讯录移除：{target_id}",
            payload={"owner_id": owner_id, "target_id": target_id},
        )
    return {"removed": True, "owner_id": owner_id, "target_id": target_id}
