"""Path leases: declare "I am editing this" and refuse conflicting commits (T4).

Absorbed (method, not code) from the competitor read-through: a reservation has
``exclusive`` vs ``shared``, a TTL with automatic expiry, explicit release, and —
the part with teeth — a **pre-commit guard** that refuses a commit touching a path
another agent holds. Ours differs in two ways that matter here:

* **no new table**: a claim is a governance event (``path_claimed``) and a release
  is another event; the active set is *derived* (newest wins, expired excluded).
  Auditability and revertibility come for free, and a lease cannot drift from the
  record.
* **no new dependency**: we implement the minimal gitwildmatch subset we need
  (``**``, ``*``, ``?``) as a small, tested converter. If ``pathspec`` is ever
  vendored we swap the matcher and keep the tests.

Conflict rule is deliberately **conservative**: two patterns conflict when one's
static prefix is a path-prefix of the other's. Over-reporting a conflict costs a
human one line of clarification; under-reporting costs two agents overwriting each
other (the pain this feature exists for).
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from datetime import datetime, timedelta, timezone
from typing import Any

CLAIM_EVENT = "path_claimed"
RELEASE_EVENT = "path_released"
DEFAULT_TTL_SECONDS = 3600
MAX_TTL_SECONDS = 24 * 3600
MODES = ("exclusive", "shared")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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


# ── 最小 gitwildmatch 子集 ────────────────────────────────────────────────
def _seg_regex(text: str) -> str:
    """Translate a gitignore-ish pattern body to a regex body (``**``/``*``/``?``)."""
    out: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "*":
            if i + 1 < len(text) and text[i + 1] == "*":
                i += 2
                if i < len(text) and text[i] == "/":
                    out.append("(?:.*/)?")  # **/ = 零个或多个目录
                    i += 1
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        i += 1
    return "".join(out)


def pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile one pattern, **anchored** (gitignore: a slash anywhere anchors to the root).

    Use :func:`matches` for the gitignore rule that a pattern *without* a slash matches
    at any depth — that rule is applied there, not here.
    """
    text = str(pattern or "").strip().lstrip("/")
    if not text:
        return re.compile(r"(?!.*)")
    body = text.rstrip("/")
    if not body:
        return re.compile(r"(?!.*)")
    regex = _seg_regex(body)
    if text.endswith("/"):
        regex += "(?:/.*)?"  # 尾斜杠 = 该目录及其下
    return re.compile("^" + regex + "$")


def matches(pattern: str, path: str) -> bool:
    """gitignore semantics: no slash in the pattern ⇒ match at any depth."""
    text = str(pattern or "").strip().lstrip("/")
    if not text or not str(path or "").strip():
        return False
    body = text.rstrip("/")
    anchored = "/" in body
    regex = pattern_to_regex(text)
    # 规范化：`src/../etc/passwd` 必须等同 `etc/passwd`（否则 --path 可绕过认领）
    candidate = posixpath.normpath(str(path).strip().lstrip("/"))
    if anchored:
        return bool(regex.match(candidate))
    # 未锚定：对每一级后缀做全匹配（等价于"任意深度"）
    suffixes = [candidate] + [candidate[i + 1 :] for i, ch in enumerate(candidate) if ch == "/"]
    return any(regex.match(suffix) for suffix in suffixes)


def static_prefix(pattern: str) -> str:
    """The literal directory part before the first wildcard (used for conflicts)."""
    text = str(pattern or "").strip().lstrip("/")
    cut = len(text)
    for marker in ("*", "?", "["):
        found = text.find(marker)
        if found != -1:
            cut = min(cut, found)
    prefix = text[:cut]
    return prefix.rsplit("/", 1)[0] if "/" in prefix else ""


def _prefix_conflict(left: str, right: str) -> bool:
    if not left or not right:
        return True  # 通配过宽 ⇒ 保守判冲突
    a, b = left.strip("/"), right.strip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def conflicts_with(left: str, right: str) -> bool:
    """Do two claim patterns overlap? Conservative: prefix rule + direct match."""
    if matches(left, right) or matches(right, left):
        return True
    return _prefix_conflict(static_prefix(left), static_prefix(right))


# ── 认领/释放/查询 ────────────────────────────────────────────────────────
def active_claims(
    store, project_id: str | None = None, *, now: datetime | None = None
) -> list[dict]:
    if project_id:
        store.ensure_project(project_id)
    """Derived active leases: claims without a later release and not expired."""
    moment = now or _now()
    with store._transaction(readonly=True) as db:
        rows = db.execute(
            "SELECT id, type, project_id, employee_id, payload, created_at FROM governance_events "
            "WHERE type IN (?, ?) AND (project_id=? OR project_id IS NULL) ORDER BY created_at, rowid",
            (CLAIM_EVENT, RELEASE_EVENT, project_id),
        ).fetchall()
    events = [{k: row[k] for k in row.keys()} for row in rows]  # noqa: SIM118
    claims: dict[str, dict] = {}
    for event in events:
        payload = _payload(event)
        claim_id = str(payload.get("claim_id") or event["id"])
        if event["type"] == CLAIM_EVENT:
            claims[claim_id] = {
                "claim_id": claim_id,
                "project_id": event["project_id"],
                "holder": event["employee_id"],
                "patterns": list(payload.get("patterns") or []),
                "mode": payload.get("mode") or "exclusive",
                "reason": payload.get("reason") or "",
                "claimed_at": event["created_at"],
                "expires_at": payload.get("expires_at"),
            }
        else:
            claims.pop(claim_id, None)
    active = []
    for claim in claims.values():
        expiry = _parse_time(claim.get("expires_at"))
        if expiry is not None and expiry <= moment:
            continue  # TTL 自动过期，无需后台回收
        active.append(claim)
    return active


def claim_paths(
    store,
    project_id: str,
    employee_id: str,
    patterns: list[str],
    *,
    mode: str = "exclusive",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    reason: str = "",
) -> dict[str, Any]:
    """Claim paths; returns ``{"granted": [...], "conflicts": [...]}`` (never partial-silent)."""
    if mode not in MODES:
        raise ValueError(f"mode 只能是：{', '.join(MODES)}")
    # 参数类型校验：传进来员工"字典"是常见误用（e2e 自检当场踩到）
    for label, value in (("project_id", project_id), ("employee_id", employee_id)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} 需要非空字符串（员工请传 employee['id']，不是整个对象）")
    cleaned = [str(p).strip() for p in patterns if str(p).strip()]
    if not cleaned:
        raise ValueError("至少要声明一个路径模式")
    ttl = max(1, min(MAX_TTL_SECONDS, int(ttl_seconds)))
    conflicts = check_conflicts(store, project_id, employee_id, cleaned)
    if conflicts:
        return {"granted": [], "conflicts": conflicts}
    expires_at = (_now() + timedelta(seconds=ttl)).isoformat()
    # 确定性 id（不要用 hash()：跨进程不稳定）
    seed = "|".join([project_id, employee_id, ",".join(cleaned), expires_at])
    claim_id = "lease_" + hashlib.sha256(seed.encode()).hexdigest()[:16]
    with store._transaction() as db:
        store._required(db, "projects", project_id)
        store._governance(
            db,
            CLAIM_EVENT,
            employee_id=employee_id,
            project_id=project_id,
            actor=f"employee:{employee_id}",
            reason=reason or f"认领 {', '.join(cleaned)}（{mode}，{ttl}s）",
            payload={
                "claim_id": claim_id,
                "patterns": cleaned,
                "mode": mode,
                "ttl_seconds": ttl,
                "expires_at": expires_at,
                "reason": reason,
            },
        )
    return {
        "granted": [
            {
                "claim_id": claim_id,
                "holder": employee_id,
                "patterns": cleaned,
                "mode": mode,
                "expires_at": expires_at,
            }
        ],
        "conflicts": [],
    }


def release_paths(
    store,
    project_id: str,
    employee_id: str,
    *,
    claim_id: str | None = None,
    patterns: list[str] | None = None,
) -> dict[str, Any]:
    """Release a lease (by id, or everything this holder has). Recorded, never silent."""
    released = []
    for claim in active_claims(store, project_id):
        if claim["holder"] != employee_id:
            continue
        if claim_id is not None and claim["claim_id"] != claim_id:
            continue
        if patterns is not None and not any(
            conflicts_with(p, held) for p in patterns for held in claim["patterns"]
        ):
            continue
        released.append(claim["claim_id"])
    if not released:
        return {"released": []}
    with store._transaction() as db:
        for item in released:
            store._governance(
                db,
                RELEASE_EVENT,
                employee_id=employee_id,
                project_id=project_id,
                actor=f"employee:{employee_id}",
                reason=f"释放认领 {item}",
                payload={"claim_id": item},
            )
    return {"released": released}


def check_conflicts(
    store, project_id: str | None, employee_id: str | None, paths: list[str]
) -> list[dict[str, Any]]:
    """Which active claims (by *others*) overlap these paths?"""
    out = []
    for claim in active_claims(store, project_id):
        if claim["holder"] == employee_id:
            continue
        for held in claim["patterns"]:
            for path in paths:
                if claim["mode"] == "shared" and matches(held, path):
                    continue  # 共享认领不拦，但下面仍记录在 grants 里
                if matches(held, path) or conflicts_with(held, path):
                    out.append(
                        {
                            "path": path,
                            "held_by": claim["holder"],
                            "pattern": held,
                            "mode": claim["mode"],
                            "expires_at": claim["expires_at"],
                            "reason": claim["reason"],
                        }
                    )
                    break
    return out
