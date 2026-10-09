"""Echo guard: stop A↔B letter loops (2026-10-04 incident)."""

from __future__ import annotations

from agent_mailbox import echo_guard as eg
from agent_mailbox.workbench_store import WorkbenchStore


def _pingpong(pairs: int, *, state: str | None = None, base_hour: int = 1) -> list[dict]:
    """``pairs`` = how many full A→B→A round trips."""
    rows, minute = [], 0
    for i in range(pairs):
        for sender, recipient in (("A", "B"), ("B", "A")):
            minute += 1
            row = {
                "sender_id": sender,
                "recipient_id": recipient,
                "created_at": f"2026-10-04T{base_hour:02d}:{minute:02d}:00+00:00",
            }
            if state is not None:
                row["state"] = state
            rows.append(row)
    return rows


def test_count_exchanges_counts_only_reversals():
    rows = _pingpong(2)  # A→B B→A A→B B→A  => 3 reversals
    assert eg.count_exchanges(rows) == 3
    same_direction = [
        {"sender_id": "A", "recipient_id": "B", "created_at": "2026-10-04T01:01:00+00:00"},
        {"sender_id": "A", "recipient_id": "B", "created_at": "2026-10-04T01:02:00+00:00"},
    ]
    assert eg.count_exchanges(same_direction) == 0


def test_detect_echo_trips_at_three_round_trips():
    finding = eg.detect_echo(_pingpong(3), max_rounds=3, window_seconds=None)
    assert finding is not None
    assert finding.turns == 6  # 6 messages = 3.0 round trips
    assert finding.rounds == 3.0
    assert finding.exchanges == 5
    assert finding.participants == ("A", "B")
    assert eg.count_turns(_pingpong(3)) == 6


def test_detect_echo_below_threshold_is_quiet():
    assert eg.detect_echo(_pingpong(2), max_rounds=3, window_seconds=None) is None
    assert eg.detect_echo(_pingpong(1), max_rounds=3, window_seconds=None) is None


def test_detect_echo_ignores_loop_when_state_changed():
    rows = _pingpong(4)
    rows[2]["state"] = "queued"
    rows[5]["state"] = "running"  # progress happened -> not an echo
    assert eg.detect_echo(rows, max_rounds=3, window_seconds=None) is None


def test_detect_echo_window_excludes_old_traffic():
    rows = _pingpong(4, base_hour=1)  # 01:0x
    fresh = [m for m in rows if m["created_at"] >= "2026-10-04T09:00:00"]
    assert fresh == []
    assert (
        eg.detect_echo(rows, max_rounds=3, window_seconds=600, now="2026-10-04T12:00:00+00:00")
        is None
    )


def test_direction_supports_legacy_and_workbench_shapes():
    legacy = {"from": "HS", "to": "dsh-agent-01"}
    modern = {"sender_id": "employee_a", "recipient_id": "employee_b"}
    assert eg.direction(legacy) == ("HS", "dsh-agent-01")
    assert eg.direction(modern) == ("employee_a", "employee_b")
    assert eg.direction({"from": "HS"}) is None


def test_freeze_then_unfreeze_is_auditable(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    thread = "th-echo-1"
    assert eg.thread_frozen(store, thread) is False

    event = eg.freeze_thread(
        store, thread, finding=eg.detect_echo(_pingpong(3), window_seconds=None)
    )
    assert event["type"] == eg.FREEZE_EVENT
    assert "回声环" in event["reason"]
    assert eg.thread_frozen(store, thread) is True

    eg.unfreeze_thread(store, thread, reason="人工确认已修")
    assert eg.thread_frozen(store, thread) is False

    # the ledger keeps both decisions (store order is not chronological -> sort explicitly)
    events = [
        e for e in store.governance_events() if e["type"] in (eg.FREEZE_EVENT, eg.UNFREEZE_EVENT)
    ]
    ordered = [e["type"] for e in sorted(events, key=lambda e: str(e.get("created_at") or ""))]
    assert ordered == [eg.FREEZE_EVENT, eg.UNFREEZE_EVENT]
    first = min(events, key=lambda e: str(e.get("created_at") or ""))
    assert first["payload"]["thread_id"] == thread


def test_freeze_is_scoped_per_thread(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    eg.freeze_thread(store, "th-a")
    assert eg.thread_frozen(store, "th-a") is True
    assert eg.thread_frozen(store, "th-b") is False


def _flood(
    count: int, *, sender: str = "HS", recipient: str = "WB", step_seconds: int = 5
) -> list[dict]:
    """A one-way burst: ``count`` messages ``step_seconds`` apart (within one window)."""
    base_minute, base_second = 7, 0
    rows = []
    for i in range(count):
        total = base_second + i * step_seconds
        rows.append(
            {
                "sender_id": sender,
                "recipient_id": recipient,
                "created_at": f"2026-10-04T{base_minute:02d}:{total // 60:02d}:{total % 60:02d}+00:00",
            }
        )
    return rows


def test_detect_flood_catches_one_way_burst():
    finding = eg.detect_flood(_flood(12), max_messages=10, window_seconds=None)
    assert finding is not None
    assert (finding.sender, finding.recipient, finding.count) == ("HS", "WB", 12)


def test_detect_flood_quiet_below_threshold():
    assert eg.detect_flood(_flood(9), max_messages=10, window_seconds=None) is None


def test_detect_flood_ignores_balanced_pingpong():
    # 5 round trips = 5 messages per direction -> below a 6-message-per-direction ceiling.
    # (Echo is the echo guard's job; a balanced exchange is not a flood.)
    rows = _pingpong(5)
    assert eg.detect_flood(rows, max_messages=6, window_seconds=None) is None
    assert eg.detect_flood(rows, max_messages=5, window_seconds=None) is not None


def test_detect_flood_window_excludes_old_burst():
    burst = _flood(12, step_seconds=5)  # 12 messages inside ~55s
    assert eg.detect_flood(burst, max_messages=10, window_seconds=300) is not None
    # same messages, but the window only covers the last 20s -> 4 messages -> quiet
    assert eg.detect_flood(burst, max_messages=10, window_seconds=20) is None


def test_fold_notice_is_one_line():
    finding = eg.detect_flood(_flood(12), max_messages=10, window_seconds=300)
    assert finding is not None
    notice = eg.fold_notice(finding)
    assert notice.count("\n") == 0
    assert "12 封" in notice
