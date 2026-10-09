"""Delivery proof: a handoff sheet + evidence a human can judge without chat (roadmap T12).

The product already captures deliveries for **workspace** tasks (git diff + patch
sha). The **mailbox** tasks — the v0.8 main path, where a human assigns work and
an employee submits a result — had no delivery capture at all: the reader returns
``workspace: None, files: [], diff: ""``. This module fills that half.

Success criterion (agreed with HS): **不看聊天记录，只看交接单+证据即可判定**.

Design rules taken from our own incidents:
  * **reference, never inline** (waggle's lesson): an artifact is a short
    ``art:<sha256[:16]>`` token plus hash and size — never the file body;
  * **自报 ≠ 通过**: the employee's own summary is kept as *claim*, and the
    verifier recomputes hashes so a tampered artifact is caught;
  * **no new table**: the proof is a governance event (``delivery_proof``), so it
    shows up in the existing audit chain and adds no new writer subsystem;
  * **no new daemon**: verification is on demand (pull), never a background loop.
"""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import workbench_events

PROOF_EVENT = "delivery_proof"
VERIFY_EVENT = "delivery_verified"
REF_PREFIX = "art:"
MAX_SUMMARY = 4000
SELF_REPORT_NOTE = "员工报告不等于通过；系统校验结果见 verify。"


def artifact_ref(path: str | Path, *, root: str | Path | None = None) -> dict[str, Any]:
    """Describe one produced file by reference: token + sha256 + size (no content)."""
    target = Path(path)
    if root is not None and not target.is_absolute():
        target = Path(root) / target
    resolved = target.resolve()
    try:
        info = resolved.stat()
    except OSError as exc:
        return {
            "ref": None,
            "path": str(path),
            "sha256": None,
            "bytes": None,
            "error": f"{type(exc).__name__}: {exc.strerror or exc}",
        }
    if not stat.S_ISREG(info.st_mode):
        # FIFO/目录/设备：读它会永久挂住（具名管道）或读不出内容 ⇒ 直接拒绝
        return {
            "ref": None,
            "path": str(path),
            "sha256": None,
            "bytes": None,
            "error": "not_a_regular_file",
        }
    digest = hashlib.sha256()
    size = 0
    try:
        with resolved.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):  # 分块哈希：200MB 文件不再进内存
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        return {
            "ref": None,
            "path": str(path),
            "sha256": None,
            "bytes": None,
            "error": f"{type(exc).__name__}: {exc.strerror or exc}",
        }
    return {
        "ref": f"{REF_PREFIX}{digest.hexdigest()[:16]}",
        "path": str(path),
        "resolved": str(resolved),
        "sha256": digest.hexdigest(),
        "bytes": size,
    }


def build_proof(
    store,
    task_id: str,
    *,
    artifacts: Iterable[str] = (),
    evidence: Iterable[dict] = (),
    criteria: Iterable[dict] = (),
    checks: Iterable[dict] = (),
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Assemble a handoff sheet for a task and record it as a governance event.

    ``criteria`` items look like ``{"criterion": str, "self_check": "pass|fail|unknown",
    "evidence": [ref, ...]}``; ``evidence`` items look like ``{"kind": ..., "ref": ...,
    "sha256": ..., "note": ...}``; ``checks`` items look like ``{"name": ..., "status":
    "pass|fail|not_run", "detail": ...}``.
    """
    with store._transaction() as db:
        task = store._required(db, "tasks", task_id)
        workspace = db.execute(
            "SELECT * FROM task_workspaces WHERE task_id=?", (task_id,)
        ).fetchone()
        delivery = db.execute(
            "SELECT patch_sha256, payload FROM task_deliveries WHERE task_id=?", (task_id,)
        ).fetchone()
        proof = {
            "task_id": task_id,
            "title": task["title"],
            "assignee_id": task["assignee_id"],
            "execution_mode": task["execution_mode"],
            "task_status": task["status"],
            "summary": str(task["result"] or "")[:MAX_SUMMARY],
            "artifacts": [artifact_ref(p, root=root) for p in artifacts],
            "evidence": [dict(item) for item in evidence],
            "criteria": [dict(item) for item in criteria],
            "checks": [dict(item) for item in checks],
            "workspace": dict(workspace) if workspace else None,
            "patch_sha256": delivery["patch_sha256"] if delivery else None,
            "self_report": {"note": SELF_REPORT_NOTE, "summary_is_claim": True},
            "recorded_at": _now(),
        }
        proof["proof_sha256"] = _hash(proof)
        store._governance(
            db,
            PROOF_EVENT,
            employee_id=task["assignee_id"],
            project_id=task["project_id"],
            task_id=task_id,
            actor="human",
            reason=f"交付证明：{len(proof['artifacts'])} 个产出物 · {len(proof['criteria'])} 条验收项",
            payload=proof,
        )
        return proof


def latest_proof(store, task_id: str) -> dict[str, Any] | None:
    """Newest recorded proof for a task (read-only)."""
    rows = store.governance_events(limit=None)  # 全量：证明查找不能被截断
    candidates = [
        event
        for event in rows
        if event.get("type") == PROOF_EVENT and event.get("task_id") == task_id
    ]
    if not candidates:
        return None
    newest = workbench_events.latest(candidates)  # 同秒靠插入序，不靠 uuid
    payload = newest.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    return payload if isinstance(payload, dict) else None


def verify_proof(
    store, task_id: str, *, record: bool = False, verifier_id: str | None = None
) -> dict[str, Any]:
    """Recompute every artifact hash and return a verdict (pull, on demand).

    Verdicts: ``no_proof`` · ``verified`` · ``mismatch`` · ``incomplete``.
    """
    if record and verifier_id:
        # "必须由另一名员工校验"要真的强制：验者须是该项目在职成员，且不是交付人
        with store._transaction() as db:
            task = store._required(db, "tasks", task_id)
            row = db.execute(
                "SELECT e.id FROM employees e JOIN memberships m ON m.employee_id = e.id "
                "WHERE e.id = ? AND m.project_id = ? AND e.lifecycle = 'active'",
                (verifier_id, task["project_id"]),
            ).fetchone()
        if row is None:
            raise ValueError(
                f"校验者必须是本项目的在职员工（{verifier_id} 不是）；"
                "请用会话/身份把真实员工 id 传进来，不要自填字符串。"
            )
        if verifier_id == task["assignee_id"]:
            raise ValueError("同行校验必须换人：交付人不能验自己。")
    proof = latest_proof(store, task_id)
    if proof is None:
        # 键集统一（CLI 直接取 result['mismatch'] 曾因此吐裸 traceback）
        return {
            "task_id": task_id,
            "verdict": "no_proof",
            "items": [],
            "checked": 0,
            "mismatch": 0,
            "missing": 0,
        }
    items, missing, mismatch = [], 0, 0
    for artifact in proof.get("artifacts", []) or []:
        expected = artifact.get("sha256")
        path = artifact.get("resolved") or artifact.get("path")
        if not expected or not path:
            missing += 1
            items.append({**artifact, "verdict": "missing"})
            continue
        current = artifact_ref(path)
        if current.get("sha256") == expected:
            items.append({**artifact, "verdict": "ok"})
        elif current.get("sha256") is None:
            missing += 1
            items.append({**artifact, "verdict": "missing", "error": current.get("error")})
        else:
            mismatch += 1
            items.append(
                {**artifact, "verdict": "mismatch", "current_sha256": current.get("sha256")}
            )
    failed_checks = [
        check for check in (proof.get("checks") or []) if str(check.get("status")) == "fail"
    ]
    if mismatch:
        verdict = "mismatch"
    elif missing:
        verdict = "incomplete"
    elif failed_checks:
        verdict = "mismatch"
    elif not (proof.get("artifacts") or []):
        # 空证明不得判"通过"：没有任何产出物的"verified"就是橡皮图章（独立审查指出）
        verdict = "incomplete"
    else:
        verdict = "verified"
    result = {
        "task_id": task_id,
        "verdict": verdict,
        "mismatch": mismatch,
        "missing": missing,
        "checked": len(items),
        "failed_checks": failed_checks,
        "items": items,
        "proof_sha256": proof.get("proof_sha256"),
    }
    if record:
        with store._transaction() as db:
            task = store._required(db, "tasks", task_id)
            store._governance(
                db,
                VERIFY_EVENT,
                employee_id=task["assignee_id"],
                project_id=task["project_id"],
                task_id=task_id,
                actor=f"employee:{verifier_id}" if verifier_id else "system",
                reason=f"系统校验：{verdict}（产出物 {len(items)} 个，哈希不符 {mismatch}，缺失 {missing}）",
                payload={
                    "verdict": verdict,
                    "checked": len(items),
                    "mismatch": mismatch,
                    "missing": missing,
                    "proof_sha256": proof.get("proof_sha256"),
                    "verifier_id": verifier_id,
                },
            )
    return result


def render_verdict(proof: dict[str, Any], verification: dict[str, Any] | None = None) -> str:
    """One screen a human can accept/reject from — **takes only the proof**.

    This signature is the point: no store, no messages, no chat history. If the
    verdict cannot be rendered from the proof alone, the proof is incomplete.
    """
    lines = [f"交付证明 · {proof.get('title') or proof.get('task_id')}"]
    lines.append(
        f"  交付人：{proof.get('assignee_id')} · 任务态：{proof.get('task_status')} · 模式：{proof.get('execution_mode')}"
    )
    if verification:
        lines.append(
            f"  系统校验：{verification.get('verdict')}（检查 {verification.get('checked')} 个产出物）"
        )
    else:
        lines.append("  系统校验：未运行（用 verify 触发）")
    criteria = proof.get("criteria") or []
    if criteria:
        lines.append("  验收项：")
        for item in criteria:
            mark = {"pass": "✅", "fail": "❌"}.get(str(item.get("self_check")), "❔")
            refs = ", ".join(item.get("evidence") or []) or "—"
            lines.append(f"    {mark} {item.get('criterion')}  [证据 {refs}]")
    else:
        lines.append("  验收项：未提供（无法据此判定，需人工补）")
    artifacts = proof.get("artifacts") or []
    lines.append(f"  产出物（{len(artifacts)}）：")
    for artifact in artifacts:
        state = ""
        if verification:
            match = next(
                (i for i in verification.get("items", []) if i.get("path") == artifact.get("path")),
                None,
            )
            state = f" · {match.get('verdict')}" if match else ""
        size = artifact.get("bytes")
        lines.append(
            f"    · {artifact.get('ref') or '—'}  {str(artifact.get('path'))[:48]}"
            f"  {size if size is not None else '?'}B{state}"
        )
    checks = proof.get("checks") or []
    if checks:
        lines.append("  自检：")
        for check in checks:
            lines.append(
                f"    · {check.get('name')} → {check.get('status')} {check.get('detail') or ''}".rstrip()
            )
    lines.append(f"  补丁哈希：{proof.get('patch_sha256') or '—'}")
    lines.append(f"  {proof.get('self_report', {}).get('note', SELF_REPORT_NOTE)}")
    return "\n".join(lines)


def _hash(payload: dict[str, Any]) -> str:
    clone = {k: v for k, v in payload.items() if k != "proof_sha256"}
    return hashlib.sha256(
        json.dumps(clone, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
