"""Wake decision core: watermark + cold start + gates (the 2026-10-04 storm lesson).

Docstrings ASCII-only; Chinese quoting uses 「」 per this session's lesson.
"""

from __future__ import annotations

from agent_mailbox.workbench_wake import (
    DEFAULT_COOLDOWN_SECONDS,
    MAX_WAKES_PER_HOUR,
    cold_start,
    decide,
    empty_state,
    record_wake,
)

STEP = DEFAULT_COOLDOWN_SECONDS + 1


def _wake(state, employee_id, inbox, now):
    decision = decide(state, employee_id, inbox, now=now)
    if decision:
        record_wake(state, employee_id, decision["fresh"], now=now)
    return decision


def test_same_letter_never_wakes_twice():
    state = empty_state()
    cold_start(state, "e1", [])
    first = _wake(state, "e1", ["m1"], 1000.0)
    assert first and first["fresh"] == ["m1"], "first new letter must wake"
    assert _wake(state, "e1", ["m1"], 1000.0 + STEP) is None, (
        "watermark must prevent a second wake for the same letter"
    )


def test_cold_start_marks_history_seen_and_does_not_wake():
    state = empty_state()
    assert cold_start(state, "e1", ["old1", "old2", "old3"]) == 3
    assert decide(state, "e1", ["old1", "old2", "old3"], now=1000.0) is None, (
        "cold start must not wake for historical mail"
    )
    fresh = _wake(state, "e1", ["old1", "new1"], 1000.0 + STEP)
    assert fresh and fresh["fresh"] == ["new1"], "only genuinely new mail wakes"


def test_cooldown_then_cap():
    state = empty_state()
    cold_start(state, "e1", [])
    now = 10_000.0
    assert _wake(state, "e1", ["m0"], now) is not None
    assert decide(state, "e1", ["m1"], now=now + 60) is None, "cooldown must hold"
    assert _wake(state, "e1", ["m1"], now + STEP) is not None, "after cooldown it must wake again"
    for index in range(MAX_WAKES_PER_HOUR + 2):
        now += STEP
        _wake(state, "e1", [f"m{index + 10}"], now)
    assert decide(state, "e1", ["m999"], now=now + 1) is None, "hourly cap must hold"


def test_disabled_means_silence():
    state = empty_state()
    cold_start(state, "e1", [])
    state["enabled"] = False
    assert decide(state, "e1", ["m1"], now=5000.0) is None


def test_fifty_letter_burst_produces_bounded_wakes():
    """Storm regression: 50 letters in 200s must not become 50 wakes."""
    state = empty_state()
    cold_start(state, "e1", [])
    wakes = 0
    now = 100_000.0
    inbox: list[str] = []
    for step in range(200):
        now += 4.0
        if step % 4 == 0:
            inbox.append(f"m{step}")
        if _wake(state, "e1", inbox, now):
            wakes += 1
    assert wakes <= 2, f"50 letters in 200s should wake at most twice, got {wakes}"
