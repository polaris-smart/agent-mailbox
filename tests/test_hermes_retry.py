import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_mailbox import workbench_hermes_journal as journal
from agent_mailbox import workbench_hermes_recovery as recovery
from agent_mailbox.workbench_reauthorization_holds import cutoff_path, gate_path, required_path


@pytest.mark.parametrize(
    "condition,expected_state,expected_phase",
    [
        ("ready", "old_gateway_restored_mail_fenced", "restart_retry_verified"),
        (
            "start_failed",
            "gateway_restart_retry_outcome_requires_inspection",
            "restart_retry_intent",
        ),
        ("pin_changed", "gateway_restore_scope_changed", "restart_intent"),
        ("service_alive", "gateway_stop_unconfirmed", "restart_intent"),
        ("socket_present", "gateway_stop_unconfirmed", "restart_intent"),
        ("marker_present", "gateway_stop_unconfirmed", "restart_intent"),
        ("live_lock", "gateway_stop_unconfirmed", "restart_intent"),
        ("other_drain", "other_drain_in_progress", "restart_intent"),
        (
            "route_missing",
            "gateway_restart_retry_outcome_requires_inspection",
            "restart_retry_intent",
        ),
    ],
)
def test_explicit_retry_once_with_original_scope_and_fence(
    monkeypatch, tmp_path, condition, expected_state, expected_phase
):
    store = SimpleNamespace(root=tmp_path)
    (tmp_path / "wake").mkdir(mode=0o700)
    profile = tmp_path / "profile"
    profile.mkdir()
    binding = {
        "id": "binding_" + "a" * 32,
        "employee_id": "employee",
        "project_id": "project",
        "host_kind": "hermes",
    }
    plan = "plan_" + "b" * 32
    domain = f"gui/{os.getuid()}"
    gate = gate_path(tmp_path, binding["id"])
    gate.parent.mkdir(mode=0o700, parents=True)
    gate.write_text(
        json.dumps(
            {
                "version": 1,
                "plan_id": plan,
                "binding_id": binding["id"],
                "employee_id": "employee",
                "project_id": "project",
            }
        )
    )
    gate.chmod(0o600)
    required = required_path(tmp_path, binding["id"])
    required.write_text(json.dumps({"version": 1, "plan_id": plan, "binding_id": binding["id"]}))
    required.chmod(0o600)
    first = SimpleNamespace(
        profile=profile,
        domain=domain,
        plist_digest=bytes.fromhex("ab" * 32),
        command_digest=bytes.fromhex("cd" * 32),
    )
    journal.begin(store, binding, plan, "selected", first, {"pid": 42, "start_time": 100})
    scope = {
        "employee_id": "employee",
        "project_id": "project",
        "profile_id": "selected",
        "profile": profile,
    }
    journal.advance(store, binding["id"], plan, "stop_intent", "restart_intent", **scope)
    events = []

    class StoppedControl:
        def __init__(self, _python, selected, *, require_stopped=False):
            assert selected == profile and require_stopped is True
            self.plist_digest = first.plist_digest
            self.command_digest = first.command_digest

        def runtime(self):
            return {
                "kind": "hermes-gateway",
                "gateway_state": "stopped",
                "hermes_home": str(profile),
                "pid": 42,
                "start_time": 100,
                "active_agents": 0,
                "active_work": None,
                "restart_requested": False,
            }

        def original_pid_gone(self, _pid):
            return True

        def service_stopped(self):
            return condition != "service_alive"

        def receiver_socket_absent(self, _endpoint):
            return condition != "socket_present"

        def planned_stop_marker_absent(self):
            return condition != "marker_present"

        def strict_gateway_absent(self):
            return condition != "live_lock"

        def _drain_marker(self, _plan):
            body = {"principal": "some-other-operator"} if condition == "other_drain" else None
            return profile / ".drain_request.json", body

        def clear_drain(self, _plan):
            events.append("clear")

        def start_stopped_original(self, selected, **hashes):
            assert selected == domain
            assert hashes == {"plist_sha256": "ab" * 32, "command_sha256": "cd" * 32}
            events.append("start")
            if condition == "start_failed":
                raise ValueError("synthetic launchctl failure")

        def restored_original_route(self, *_args):
            events.append("verify")
            return condition != "route_missing"

    monkeypatch.setattr(recovery.catalog, "_member", lambda *_: None)
    monkeypatch.setattr(recovery.catalog, "resolve", lambda *_: (Path("/fake/python"), profile))
    monkeypatch.setattr(recovery, "_binding", lambda *_: binding)
    monkeypatch.setattr(recovery, "endpoint_for", lambda *_: tmp_path / "receiver.sock")
    monkeypatch.setattr(recovery, "_pin_preview", lambda *_: condition != "pin_changed")
    args = (store, "employee", "project", plan, "selected")
    retry = recovery.retry_restore_pre_pin(
        *args, confirmed=True, control_factory=StoppedControl, route_seconds=0
    )
    assert retry == {"state": expected_state, "delivery_verified": False}
    assert journal.read(store, binding["id"], plan, **scope)["phase"] == expected_phase
    assert gate.exists() and required.exists() and not cutoff_path(tmp_path, binding["id"]).exists()
    assert events == (
        ["clear", "start", "verify"]
        if condition == "ready"
        else ["clear", "start"]
        if condition == "start_failed"
        else ["clear", "start", "verify"]
        if condition == "route_missing"
        else []
    )
    visible = recovery.inspect(store, "employee", "project", "selected")["state"]
    assert visible == (
        "old_gateway_restored_recorded_mail_fenced"
        if condition == "ready"
        else "gateway_restart_retry_outcome_requires_inspection"
        if expected_phase == "restart_retry_intent"
        else "gateway_restart_outcome_requires_inspection"
    )
    if condition in {"ready", "start_failed", "route_missing"}:
        second = recovery.retry_restore_pre_pin(
            *args, confirmed=True, control_factory=StoppedControl, route_seconds=0
        )
        assert second["state"] == (
            "old_gateway_restored_recorded_mail_fenced"
            if condition == "ready"
            else "gateway_restart_retry_outcome_requires_inspection"
        )
        assert events == (
            ["clear", "start", "verify"]
            if condition == "ready"
            else ["clear", "start", "verify"]
            if condition == "route_missing"
            else ["clear", "start"]
        )


def test_explicit_retry_has_distinct_durable_intent(tmp_path):
    store = SimpleNamespace(root=tmp_path)
    (tmp_path / "wake").mkdir(mode=0o700)
    profile = tmp_path / "profile"
    profile.mkdir()
    binding = {
        "id": "binding_" + "a" * 32,
        "host_kind": "hermes",
        "employee_id": "employee",
        "project_id": "project",
    }
    plan = "plan_" + "b" * 32
    control = SimpleNamespace(
        profile=profile,
        domain=f"gui/{os.getuid()}",
        plist_digest=bytes.fromhex("ab" * 32),
        command_digest=bytes.fromhex("cd" * 32),
    )
    journal.begin(store, binding, plan, "selected", control, {"pid": 42, "start_time": 100})
    scope = {
        "employee_id": "employee",
        "project_id": "project",
        "profile_id": "selected",
        "profile": profile,
    }
    journal.advance(store, binding["id"], plan, "stop_intent", "restart_intent", **scope)
    retry = journal.advance(
        store, binding["id"], plan, "restart_intent", "restart_retry_intent", **scope
    )
    assert retry["phase"] == "restart_retry_intent"
    assert retry["pin_may_have_run"] is False
    with pytest.raises(ValueError, match="transition_unconfirmed"):
        journal.advance(store, binding["id"], plan, "restart_retry_intent", "pin_intent", **scope)
