"""The upper drain gate refuses stale, busy or mismatched Hermes evidence."""

from datetime import datetime, timedelta, timezone

from agent_mailbox.workbench_hermes_gateway_gate import (
    drained_idle,
    first_contact_drained_idle,
    stopped_after_drain,
    stopped_after_first_contact,
)

NOW = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)


def sample():
    runtime = {
        "kind": "hermes-gateway",
        "gateway_state": "draining",
        "pid": 123,
        "start_time": 456,
        "hermes_home": "/private/tmp/hermes",
        "active_agents": 0,
        "active_work": None,
        "updated_at": "2026-10-13T14:59:30+00:00",
    }
    route = {
        "protocol": "agent-mailbox.hermes-routing.v1",
        "host_kind": "hermes",
        "pid": 123,
        "instance_id": "instance-1",
        "routing_home": "/private/tmp/hermes",
    }
    return runtime, route


def test_drained_idle_requires_independent_live_identity():
    runtime, route = sample()
    assert not drained_idle(runtime, route, now=NOW)
    assert drained_idle(runtime, route, pid_identity_confirmed=True, now=NOW)


def test_drained_idle_refuses_busy_stale_or_unmatched_state():
    for field, value in (
        ("gateway_state", "running"),
        ("active_agents", 1),
        ("active_work", [{"kind": "turn"}]),
        ("pid", 124),
        ("start_time", None),
        ("hermes_home", "/private/tmp/other"),
        ("updated_at", "2026-10-13T14:57:00+00:00"),
        ("updated_at", "2026-10-13T15:01:00+00:00"),
    ):
        runtime, route = sample()
        runtime[field] = value
        assert not drained_idle(runtime, route, pid_identity_confirmed=True, now=NOW), field
    runtime, route = sample()
    route["instance_id"] = ""
    assert not drained_idle(runtime, route, pid_identity_confirmed=True, now=NOW)


def test_first_contact_requires_fresh_status_answer_from_same_drained_gateway():
    runtime, _ = sample()
    status = {**runtime, "protocol": 1, "answering_pid": 123, "answered_at": NOW.timestamp()}
    assert not first_contact_drained_idle(runtime, status, now=NOW)
    assert first_contact_drained_idle(runtime, status, pid_identity_confirmed=True, now=NOW)
    for field, value in (
        ("protocol", 2),
        ("answering_pid", 124),
        ("pid", 124),
        ("start_time", 457),
        ("hermes_home", "/private/tmp/other"),
        ("gateway_state", "running"),
        ("active_agents", 1),
        ("active_work", [{"kind": "turn"}]),
        ("answered_at", NOW.timestamp() - 6),
        ("answered_at", NOW.timestamp() + 1),
        ("updated_at", "2026-10-13T14:57:00+00:00"),
    ):
        assert not first_contact_drained_idle(
            runtime, {**status, field: value}, pid_identity_confirmed=True, now=NOW
        ), field
    for field, value in (
        ("gateway_state", "running"),
        ("active_agents", 1),
        ("active_agents", False),
        ("active_work", [{"kind": "turn"}]),
    ):
        assert not first_contact_drained_idle(
            {**runtime, field: value}, status, pid_identity_confirmed=True, now=NOW
        ), field


def test_stopped_after_drain_requires_fresh_host_and_independent_stop_evidence():
    before, route = sample()
    after = {
        **before,
        "gateway_state": "stopped",
        "updated_at": "2026-10-13T15:00:00+00:00",
        "restart_requested": False,
    }
    evidence = {
        "pid_identity_confirmed": True,
        "original_pid_gone": True,
        "service_stopped": True,
        "receiver_socket_absent": True,
        "now": NOW,
    }
    assert stopped_after_drain(before, route, after, **evidence)
    for field in (
        "pid_identity_confirmed",
        "original_pid_gone",
        "service_stopped",
        "receiver_socket_absent",
    ):
        assert not stopped_after_drain(before, route, after, **{**evidence, field: False}), field
    for field, value in (
        ("gateway_state", "running"),
        ("pid", 124),
        ("start_time", 457),
        ("hermes_home", "/private/tmp/other"),
        ("active_agents", 1),
        ("restart_requested", True),
        ("updated_at", "2026-10-13T14:59:29+00:00"),
        ("updated_at", "2026-10-13T14:57:00+00:00"),
        ("updated_at", "2026-10-13T15:01:00+00:00"),
    ):
        assert not stopped_after_drain(before, route, {**after, field: value}, **evidence), field


def test_first_contact_stop_joins_fresh_live_answer_to_independent_shutdown():
    before, _ = sample()
    live = {**before, "protocol": 1, "answering_pid": before["pid"], "answered_at": NOW.timestamp()}
    after = {
        **before,
        "gateway_state": "stopped",
        "restart_requested": False,
        "updated_at": "2026-10-13T15:00:06+00:00",
    }
    proof = {
        "observed_at": NOW,
        "pid_identity_confirmed": True,
        "original_pid_gone": True,
        "service_stopped": True,
        "receiver_socket_absent": True,
        "now": NOW + timedelta(seconds=10),
    }
    assert stopped_after_first_contact(before, live, after, **proof)
    for field in (
        "pid_identity_confirmed",
        "original_pid_gone",
        "service_stopped",
        "receiver_socket_absent",
    ):
        assert not stopped_after_first_contact(before, live, after, **{**proof, field: False}), (
            field
        )
    for field, value in (
        ("answering_pid", 999),
        ("answered_at", NOW.timestamp() - 6),
        ("gateway_state", "running"),
        ("active_agents", 1),
    ):
        assert not stopped_after_first_contact(before, {**live, field: value}, after, **proof), (
            field
        )
    for field, value in (
        ("gateway_state", "running"),
        ("pid", 124),
        ("start_time", 457),
        ("restart_requested", True),
        ("active_agents", 1),
        ("updated_at", "2026-10-13T14:59:59+00:00"),
    ):
        assert not stopped_after_first_contact(before, live, {**after, field: value}, **proof), (
            field
        )
    assert not stopped_after_first_contact(
        before, live, after, **{**proof, "now": NOW + timedelta(seconds=121)}
    )
    assert not stopped_after_first_contact(
        before, live, after, **{**proof, "observed_at": NOW - timedelta(seconds=6)}
    )
