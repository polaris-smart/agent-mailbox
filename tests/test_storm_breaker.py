"""Storm breaker: bounded restarts for launchd units (2026-10-04 incident)."""

from __future__ import annotations

import json

from agent_mailbox import storm_breaker as sb

REAL_PRINT = """
gui/501/com.polaris-smart.agent-mailbox-workbench = {
\tactive count = 0
\tpath = /Users/x/Library/LaunchAgents/com.polaris-smart.agent-mailbox-workbench.plist
\tstate = spawn scheduled
\truns = 1086
\tlast exit code = 1
}
"""

NOT_LOADED = 'Could not find service "x" in domain for user gui: 501\n'


def test_parse_real_launchctl_output():
    sample = sb.parse_launchctl_print(REAL_PRINT, "u")
    assert sample is not None
    assert sample.runs == 1086
    assert sample.last_exit == 1
    assert sample.state == "spawn scheduled"


def test_parse_returns_none_when_not_loaded():
    assert sb.parse_launchctl_print(NOT_LOADED, "u") is None


def test_should_trip_at_threshold_and_above():
    now = 1_000.0
    assert sb.should_trip([(now - 30, 10), (now, 15)], now, 600, 5) == (True, 5)
    assert sb.should_trip([(now - 30, 10), (now, 14)], now, 600, 5) == (False, 4)


def test_should_trip_ignores_samples_outside_window():
    now = 1_000.0
    # the burst happened long ago -> a fresh window must not trip on it
    assert sb.should_trip([(now - 5000, 1), (now, 900)], now, 600, 5) == (False, 0)


def test_should_trip_needs_two_samples():
    now = 1_000.0
    assert sb.should_trip([(now, 42)], now, 600, 5) == (False, 0)
    assert sb.should_trip([], now) == (False, 0)


def test_state_roundtrip_is_atomic_and_bounded(tmp_path):
    path = tmp_path / "state.json"
    state = sb.load_state(path)
    assert state["units"] == {}

    for i in range(200):
        sb.record_sample(state, "u", i, now=1_000.0 + i, window=60)
    sb.save_state(path, state)

    reloaded = sb.load_state(path)
    samples = reloaded["units"]["u"]["samples"]
    assert len(samples) <= 50  # rule S2: state cannot grow without bound
    assert not list(tmp_path.glob("*.tmp"))  # atomic replace leaves no temp file
    assert json.loads(path.read_text())["version"] == sb.STATE_VERSION


def test_mark_tripped_is_idempotent_and_resettable(tmp_path):
    path = tmp_path / "state.json"
    state = sb.load_state(path)
    assert sb.mark_tripped(state, "u", "boom", 1.0) is True
    assert sb.mark_tripped(state, "u", "boom again", 2.0) is False  # never bounce twice
    assert state["units"]["u"]["tripped_reason"] == "boom"
    assert sb.reset_unit(state, "u") is True
    assert sb.mark_tripped(state, "u", "boom3", 3.0) is True
    assert sb.reset_unit(state, "missing") is False


def test_corrupt_state_falls_back_to_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    assert sb.load_state(path)["units"] == {}


def test_looks_suspect_catches_dead_but_retried_unit():
    dead = sb.UnitSample(label="u", runs=1086, last_exit=1, state="not running")
    assert sb.looks_suspect(dead, 50) is True
    alive = sb.UnitSample(label="u", runs=1086, last_exit=1, state="running")
    assert sb.looks_suspect(alive, 50) is False  # running units are the rate rule's job
    healthy = sb.UnitSample(label="u", runs=1086, last_exit=0, state="not running")
    assert sb.looks_suspect(healthy, 50) is False  # clean exits are fine
    few = sb.UnitSample(label="u", runs=3, last_exit=1, state="not running")
    assert sb.looks_suspect(few, 50) is False
    unknown = sb.UnitSample(label="u", runs=999, last_exit=None, state=None)
    assert sb.looks_suspect(unknown, 50) is False


def test_single_sample_never_trips_so_first_run_is_baseline():
    now = 1_000.0
    assert sb.should_trip([(now, 1086)], now, 600, 5) == (False, 0)
