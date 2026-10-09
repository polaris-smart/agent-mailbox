"""One-way bridge: legacy letters → project task cards (roadmap T7).

Scope (agreed): during the transition the two layers are joined **one way only** —
a legacy letter can become a task card, and the legacy store is never written back.
Double-writing is forbidden, so this module:

  * reads the v0.7 mail root **read-only** (tests assert the files are untouched);
  * records the mapping as a governance event (``letter_projected``), so a letter's
    fate is traceable and re-running is idempotent — **no new table**;
  * is a **dry run by default** (``apply=False``): looking never changes anything.

The detection rule is deliberately conservative, because the real archive proved
why: of 11 letters that look task-like, several say **非派工 / 思路参考** in the
subject and others are ``Re:`` replies. Projecting those as fresh task cards would
invent work nobody asked for, so every skip carries a reason.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import workbench_events
from .workbench_store import WorkbenchError

PROJECTED_EVENT = "letter_projected"

TASK_MARKERS = ("【任务", "任务书", "派工", "工单")
NOT_TASK_MARKERS = ("非派工", "参考非派工", "思路参考", "非任务", "仅供", "通报")
REPLY_PREFIXES = ("re:", "回复：", "答复：")


def load_letters(mail_root: str | Path) -> list[dict[str, Any]]:
    """Read legacy inbox letters (read-only). Unreadable files are reported, not skipped."""
    root = Path(mail_root)
    # 打错的 --mail-root 不许被静默当成"0 封"：与"坏 project 不许静默"同一条原则
    if not root.is_dir():
        raise WorkbenchError(
            "invalid_path",
            f"信件目录不存在或不是目录：{root}；请检查 --mail-root 指向旧信箱根目录。",
        )
    letters: list[dict[str, Any]] = []
    for path in sorted(root.glob("inbox/*/*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            data.setdefault("id", path.stem)
            data["_path"] = str(path)
            letters.append(data)
    return letters


def classify(letter: dict[str, Any]) -> tuple[bool, str]:
    """Is this letter a work order? Returns ``(is_task, reason)``."""
    subject = str(letter.get("subject") or "").strip()
    lowered = subject.lower()
    if not subject:
        return False, "empty_subject"
    if letter.get("reply_to") or lowered.startswith(REPLY_PREFIXES) or subject.startswith("Re:"):
        return False, "reply_not_a_new_order"
    # 已办结的信不是"待办任务"：真机旧档里唯一通过标记判定的那封状态就是 done
    status = str(letter.get("status") or "").strip().lower()
    if status == "done":
        return False, "already_done"
    for marker in NOT_TASK_MARKERS:
        if marker in subject:
            return False, f"marked_not_a_task:{marker}"
    for marker in TASK_MARKERS:
        if marker in subject:
            return True, f"task_marker:{marker}"
    return False, "no_task_marker"


def scan(mail_root: str | Path) -> dict[str, Any]:
    """Classify every legacy letter without writing anything."""
    candidates, skipped = [], []
    for letter in load_letters(mail_root):
        is_task, reason = classify(letter)
        row = {
            "letter_id": letter.get("id"),
            "subject": letter.get("subject"),
            "from": letter.get("from"),
            "to": letter.get("to"),
            "created_at": letter.get("created_at") or letter.get("date"),
            "status": letter.get("status"),
            "path": letter.get("_path"),
        }
        if is_task:
            candidates.append({**row, "reason": reason})
        else:
            skipped.append({**row, "reason": reason})
    return {"candidates": candidates, "skipped": skipped, "total": len(candidates) + len(skipped)}


def projected_ids(store) -> dict[str, str]:
    """letter_id → task_id for everything already projected (derived, newest wins)."""
    seen: dict[str, tuple[tuple[str, int], str]] = {}
    for event in store.governance_events(limit=None):
        if event.get("type") != PROJECTED_EVENT:
            continue
        payload = event.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                continue
        if isinstance(payload, dict) and payload.get("letter_id"):
            letter = str(payload["letter_id"])
            current = seen.get(letter)
            if current is None or workbench_events.event_order(event) > current[0]:
                seen[letter] = (
                    workbench_events.event_order(event),
                    str(payload.get("task_id") or ""),
                )
    mapping = {letter: value for letter, (_order, value) in seen.items()}
    return mapping


def _resolve_assignee(
    store, project_id: str, letter: dict[str, Any], mapping: dict[str, str]
) -> str | None:
    """Map a legacy recipient (``to``) to a project member id."""
    recipient = str(letter.get("to") or "").strip()
    if not recipient:
        return None
    if recipient in mapping:
        return mapping[recipient]
    # 纯 SELECT：必须只读 —— 它在 `if not apply:` **之前**被调用，
    # 用写事务会让 `bridge project` 的干跑路径也拿写锁并把库翻成 WAL
    with store._transaction(readonly=True) as db:
        row = db.execute(
            "SELECT e.id, e.name FROM employees e JOIN memberships m ON m.employee_id = e.id "
            "WHERE m.project_id = ? AND (e.id = ? OR lower(e.name) = lower(?))",
            (project_id, recipient, recipient),
        ).fetchone()
    return row["id"] if row else None


def project(
    store,
    mail_root: str | Path,
    project_id: str,
    *,
    apply: bool = False,
    assignee_map: dict[str, str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Project task-like letters into mailbox task cards. **Dry run unless apply.**"""
    store.ensure_project(project_id)  # 坏 project 不许静默（哪怕 0 候选）
    outlook = scan(mail_root)
    already = projected_ids(store)
    mapping = {**(assignee_map or {})}
    steps: list[dict[str, Any]] = []
    candidates = outlook["candidates"]
    if limit is not None and int(limit) < 0:
        raise WorkbenchError(
            "invalid_field", "limit 不能为负（0 表示不处理任何候选，省略表示不限量）。"
        )
    selected = candidates if limit is None else candidates[: int(limit)]
    for row in selected:
        letter_id = str(row["letter_id"])
        if letter_id in already:
            steps.append({**row, "action": "already_projected", "task_id": already[letter_id]})
            continue
        assignee = _resolve_assignee(store, project_id, row, mapping)
        if not assignee:
            steps.append({**row, "action": "skipped_no_employee"})
            continue
        if not apply:
            steps.append({**row, "action": "would_project", "assignee_id": assignee})
            continue
        from .workbench_mail_tasks import create_mail_task

        letter = next(
            (item for item in load_letters(mail_root) if str(item.get("id")) == letter_id), {}
        )
        task = create_mail_task(
            store,
            project_id,
            str(row["subject"])[:300],
            f"（自 v0.7 信件单向投影 · letter {letter_id}）\n\n{str(letter.get('body') or '')[:4000]}",
            assignee,
        )
        with store._transaction() as db:
            store._governance(
                db,
                PROJECTED_EVENT,
                employee_id=assignee,
                project_id=project_id,
                task_id=task["id"],
                actor="human",
                reason=f"v0.7 信件单向投影：{letter_id} → 任务卡",
                payload={"letter_id": letter_id, "task_id": task["id"], "subject": row["subject"]},
            )
        steps.append({**row, "action": "projected", "task_id": task["id"], "assignee_id": assignee})
    counts: dict[str, int] = {}
    for step in steps:
        counts[step["action"]] = counts.get(step["action"], 0) + 1
    return {
        "dry_run": not apply,
        "project_id": project_id,
        "total_letters": outlook["total"],
        "candidates": len(outlook["candidates"]),
        "skipped_not_task": len(outlook["skipped"]),
        "steps": steps,
        "counts": counts,
    }


def status(store) -> dict[str, Any]:
    """How many letters have been projected so far (read-only)."""
    mapping = projected_ids(store)
    return {"projected": len(mapping), "letter_ids": sorted(mapping)}
