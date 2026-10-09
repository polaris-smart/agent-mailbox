"""Storm breaker: stop crash-looping launchd units before they storm.

Why this exists (2026-10-04 incident): the workbench autostart unit ran with
``KeepAlive=true`` against a broken build and restarted **1086 times** without
anyone being notified. A plain ``KeepAlive`` is an unbounded retry loop; this
module adds the missing upper bound: bounded history, a trip decision, and a
one-time record so the same unit is not bounced twice.

Design rules (see wiki: 产品纪律 · 轻量执行 + 反风暴 + 上手丝滑):
  S1  unbounded restarts are forbidden -> trip and stop, notify once
  S2  bounded state -> samples are pruned, history cannot grow without limit
  S7  observable -> a single JSON state file is the source of truth
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATE_VERSION = 1
DEFAULT_WINDOW_SECONDS = 600
DEFAULT_MAX_RESTARTS = 5
DEFAULT_HARD_CAP = 50  # absolute run count: catches fast loops on the first check
SAMPLE_KEEP_FACTOR = 3  # keep samples for window * factor


@dataclass(frozen=True)
class UnitSample:
    """One observation of a launchd unit."""

    label: str
    runs: int
    last_exit: int | None
    state: str | None


_RUNS_RE = re.compile(r"^\s*runs\s*=\s*(\d+)\s*$", re.MULTILINE)
_EXIT_RE = re.compile(r"^\s*last exit code\s*=\s*(-?\d+)\s*$", re.MULTILINE)
_STATE_RE = re.compile(r"^\s*state\s*=\s*(.+?)\s*$", re.MULTILINE)


def parse_launchctl_print(text: str, label: str) -> UnitSample | None:
    """Parse ``launchctl print gui/<uid>/<label>`` output.

    Returns None when the output does not look like a loaded unit (e.g. the
    command failed because the unit is not loaded).
    """
    runs = _RUNS_RE.search(text)
    if not runs:
        return None
    exit_match = _EXIT_RE.search(text)
    state_match = _STATE_RE.search(text)
    return UnitSample(
        label=label,
        runs=int(runs.group(1)),
        last_exit=int(exit_match.group(1)) if exit_match else None,
        state=state_match.group(1) if state_match else None,
    )


def should_trip(
    samples: Iterable[tuple[float, int]],
    now: float,
    window: float = DEFAULT_WINDOW_SECONDS,
    max_restarts: int = DEFAULT_MAX_RESTARTS,
) -> tuple[bool, int]:
    """Decide whether a unit restarted too often inside ``window``.

    Rate-only by design: the first check establishes a baseline and can never
    trip (one sample has no rate), so the checker must run at least twice —
    hence the periodic unit uses a 60s interval (detection lag ≲2 min).

    Deliberately **no** absolute run-count rule: interval units (StartInterval)
    grow ``runs`` legitimately, so a bare ceiling would kill healthy units. The
    "died after many retries" case is reported separately by ``looks_suspect``
    instead of being killed.

    ``samples`` is ``(timestamp, runs)`` in any order. Returns ``(trip, delta)``
    where ``delta`` is how many restarts were observed inside the window.
    """
    fresh = sorted((ts, runs) for ts, runs in samples if now - ts <= window)
    if len(fresh) < 2:
        return False, 0
    delta = fresh[-1][1] - fresh[0][1]
    return delta >= max_restarts, max(delta, 0)


def looks_suspect(sample: UnitSample, hard_cap: int = DEFAULT_HARD_CAP) -> bool:
    """A unit that retried a lot and is *not* running now: report, never kill.

    Covers the case a rate rule cannot see: the loop already stopped (launchd
    gave up) but the counter shows hundreds of failed attempts — exactly the
    1086-restart unit that nobody was told about on 2026-10-04.
    """
    if sample.runs < hard_cap or sample.last_exit in (0, None):
        return False
    return (sample.state or "") != "running"


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": STATE_VERSION, "units": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": STATE_VERSION, "units": {}}
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return {"version": STATE_VERSION, "units": {}}
    data.setdefault("units", {})
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)  # atomic: a truncated state file must never lose the trip record


def record_sample(
    state: dict[str, Any],
    label: str,
    runs: int,
    now: float,
    window: float = DEFAULT_WINDOW_SECONDS,
) -> None:
    """Append a sample and prune history so state stays bounded (rule S2)."""
    unit = state["units"].setdefault(label, {"samples": []})
    unit["samples"].append([now, runs])
    cutoff = now - window * SAMPLE_KEEP_FACTOR
    unit["samples"] = [s for s in unit["samples"] if float(s[0]) >= cutoff][-50:]
    unit["last_seen"] = now
    unit["last_runs"] = runs


def mark_tripped(state: dict[str, Any], label: str, reason: str, now: float) -> bool:
    """Record a trip. Returns False when this unit was already tripped."""
    unit = state["units"].setdefault(label, {"samples": []})
    if unit.get("tripped_at"):
        return False
    unit["tripped_at"] = now
    unit["tripped_reason"] = reason
    return True


def reset_unit(state: dict[str, Any], label: str) -> bool:
    unit = state["units"].get(label)
    if not unit or not unit.get("tripped_at"):
        return False
    unit.pop("tripped_at", None)
    unit.pop("tripped_reason", None)
    unit["samples"] = []
    return True


def list_our_labels(prefix: str = "com.polaris-smart.") -> list[str]:
    """Discover loaded units under ``prefix`` (zero-config: new units are covered)."""
    try:
        out = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return []
    labels = []
    for line in (out.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2].startswith(prefix):
            labels.append(parts[2])
    return sorted(labels)


def print_unit(label: str) -> str:
    try:
        out = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout or ""
