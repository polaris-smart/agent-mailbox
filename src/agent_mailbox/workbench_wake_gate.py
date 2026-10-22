"""Read-only, project-scoped native wake component check.

This checks configuration, current receiver health and durable queue state. A
historical host receipt is useful evidence but cannot replace a fresh letter
opened and accepted in the intended original session.
"""

from __future__ import annotations

import errno
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path

from .workbench_host_bindings import load_bindings
from .workbench_host_transport import configured_for_binding, endpoint_for, receiver_online
from .workbench_receiver_status import _probe


def _hermes_health(home: Path, binding: dict) -> dict | None:
    """A valid listener response is distinct from an available exact route."""
    endpoint = endpoint_for(home, binding)
    if not endpoint:
        return None
    try:
        return _probe(endpoint, "hermes", timeout=1.0, binding=binding, details=True)
    except OSError as exc:
        if isinstance(exc, TimeoutError) or exc.errno in {
            errno.ENOENT,
            errno.ECONNREFUSED,
            errno.ETIMEDOUT,
        }:
            return None
        raise
    except ValueError:
        return None


def evaluate(
    home: Path, employee_id: str, project_id: str, *, max_pending_minutes: float = 30.0
) -> dict:
    """Never construct WorkbenchStore: its constructor may create or migrate a DB."""
    if not isfinite(max_pending_minutes) or max_pending_minutes < 0:
        raise ValueError("invalid_pending_age")
    home = Path(home).expanduser().resolve()
    db_path = home / "workbench" / "state.db"
    claims = home / "wake" / "claims"
    if not db_path.is_file():
        raise ValueError("workbench_db_missing")
    with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        member = db.execute(
            "SELECT e.kind FROM employees e "
            "JOIN memberships m ON m.employee_id=e.id AND m.project_id=? "
            "JOIN devices d ON d.id=e.node_id AND d.is_local=1 "
            "WHERE e.id=? AND e.lifecycle='active'",
            (project_id, employee_id),
        ).fetchone()
        counts = {
            row["status"]: row["total"]
            for row in db.execute(
                "SELECT status,count(*) AS total FROM notifications "
                "WHERE kind='mailbox_wake' AND target_id=? AND project_id=? "
                "GROUP BY status",
                (employee_id, project_id),
            )
        }
        pending_rows = [
            row
            for row in db.execute(
                "SELECT state_hash,created_at FROM notifications "
                "WHERE kind='mailbox_wake' AND target_id=? AND project_id=? AND status='pending'",
                (employee_id, project_id),
            )
        ]
        legacy_untracked_pending = sum(
            db.execute(
                "SELECT 1 FROM messages m WHERE m.id=? AND m.recipient_id=? "
                "AND m.project_id=? AND NOT EXISTS ("
                "SELECT 1 FROM notifications n WHERE n.kind='mailbox_wake' "
                "AND n.state_hash=m.id AND n.target_id=m.recipient_id "
                "AND n.project_id=m.project_id)",
                (marker.name[: -len(".pending")], employee_id, project_id),
            ).fetchone()
            is not None
            for marker in claims.glob("*.pending")
        )
    pending = counts.get("pending", 0)
    held = counts.get("reauthorization_held", 0)
    receipts = counts.get("host_received", 0)
    unknown = sum((claims / f"{row['state_hash']}.unknown").is_file() for row in pending_rows)
    oldest_pending_minutes = max(
        (
            (datetime.now(timezone.utc) - datetime.fromisoformat(row["created_at"])).total_seconds()
            / 60
            for row in pending_rows
        ),
        default=0.0,
    )
    binding = next(
        (
            row
            for row in load_bindings(home)
            if member is not None
            and row["employee_id"] == employee_id
            and row["project_id"] == project_id
            and row["host_kind"] == member["kind"]
        ),
        None,
    )
    declared = bool(binding and configured_for_binding(home, binding))
    if declared and member["kind"] == "hermes":
        health = _hermes_health(home, binding)
        receiver = health is not None
        route_ready = bool(health and health["route_available"] and health["gateway_available"])
    else:
        receiver = bool(declared and receiver_online(home, binding))
        route_ready = receiver
    checks = {
        "G1_declared": {"passed": declared},
        "G2_listener": {"passed": receiver},
        "G3_consistent": {"passed": declared and route_ready},
        "G4_backlog": {
            "passed": held == unknown == legacy_untracked_pending == 0
            and oldest_pending_minutes <= max_pending_minutes
        },
        "G5_receipt": {"passed": receipts > 0},
    }
    return {
        "verdict": (
            "pass"
            if member is not None and all(item["passed"] for item in checks.values())
            else "fail"
        ),
        "scope": "component_readiness_only",
        "human_acceptance": False,
        "fresh_original_session_probe_verified": False,
        "receipt_scope": "historical_employee_project",
        "active_local_member": member is not None,
        "checks": checks,
        "pending": pending,
        "held": held,
        "unknown": unknown,
        "legacy_untracked_pending": legacy_untracked_pending,
        "oldest_pending_minutes": round(oldest_pending_minutes, 1),
        "host_receipts": receipts,
    }
