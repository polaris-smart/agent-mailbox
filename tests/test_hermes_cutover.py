"""A stopped host, not an owner-supplied boolean, authorizes the pin write."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_mailbox import workbench_hermes_cutover as cutover
from agent_mailbox import workbench_hermes_journal as journal
from agent_mailbox.workbench_reauthorization_holds import cutoff_path, epoch_path, gate_path, required_path


class FakeControl:
    def __init__(self, python, profile):
        self.profile = profile
        self.domain = f"gui/{os.getuid()}"
        self.plist_digest = bytes.fromhex("ab" * 32)
        self.command_digest = bytes.fromhex("cd" * 32)
        self.events = []
        self.stopped = False
        self.stop_raises = False
        self.stop_after_marker_raises = False
        self.marker_consumed = True
        self.after_runtime_raises = False
        self.pid_probe_failures = 0
        self.drain_idle = True
        self.socket_present = False
        self.clear_raises = False
        now = datetime.now(timezone.utc)
        self.before = {
            "kind": "hermes-gateway",
            "gateway_state": "draining",
            "pid": 123,
            "start_time": 456,
            "hermes_home": str(profile),
            "active_agents": 0,
            "active_work": None,
            "updated_at": (now - timedelta(seconds=2)).isoformat(),
        }
        self.after = {
            **self.before,
            "gateway_state": "stopped",
            "restart_requested": False,
            "updated_at": (now - timedelta(seconds=1)).isoformat(),
        }
        self.route = {
            "protocol": "agent-mailbox.hermes-routing.v1",
            "host_kind": "hermes",
            "pid": 123,
            "instance_id": "one",
            "routing_home": str(profile),
        }

    def request_drain(self, _plan_id):
        self.events.append("drain")

    def clear_drain(self, _plan_id):
        self.events.append("clear")
        if self.clear_raises:
            raise ValueError("clear uncertain")

    def runtime(self):
        self.events.append("runtime")
        if self.stopped and self.after_runtime_raises:
            raise ValueError("retained state temporarily unreadable")
        return (
            self.after
            if self.stopped
            else (self.before if self.drain_idle else {**self.before, "gateway_state": "running"})
        )

    def routing(self, _endpoint):
        self.events.append("routing")
        return self.route

    def pid_identity(self, _runtime):
        return True

    def stop(self):
        self.events.append("stop")
        self.stopped = True
        if self.stop_after_marker_raises:
            raise cutover.HermesStopAfterMarkerUnconfirmed("marker may stop Gateway")
        if self.stop_raises:
            raise ValueError("command uncertain")

    def planned_stop_marker_absent(self):
        self.events.append("marker-consumed-check")
        return self.marker_consumed

    def original_pid_gone(self, _pid):
        if self.pid_probe_failures:
            self.pid_probe_failures -= 1
            return False
        return self.stopped

    def service_stopped(self):
        return self.stopped

    def receiver_socket_absent(self, _endpoint):
        return not self.socket_present

    def remove_stale_receiver_socket(self, _endpoint, **evidence):
        assert evidence == {"original_pid_gone": True, "service_stopped": True}
        self.events.append("remove-stale-socket")
        self.socket_present = False

    def start(self):
        self.events.append("start")


def setup(monkeypatch, tmp_path):
    store = SimpleNamespace(root=tmp_path)
    (tmp_path / "wake").mkdir(mode=0o700)
    binding = {
        "id": "binding_" + "a" * 32,
        "host_kind": "hermes",
        "employee_id": "employee",
        "project_id": "project",
    }
    profile = tmp_path / "profile"
    profile.mkdir()
    controller = FakeControl(Path("/private/fake/python"), profile)
    events = controller.events
    monkeypatch.setattr(cutover.catalog, "_member", lambda *_: None)
    monkeypatch.setattr(
        cutover.catalog,
        "_discover",
        lambda: (
            tmp_path,
            Path("/private/fake/python"),
            {"profiles": [{"profile_id": "default", "home": str(profile)}]},
        ),
    )
    monkeypatch.setattr(cutover, "_binding", lambda *_: binding)
    monkeypatch.setattr(cutover, "endpoint_for", lambda *_: tmp_path / "receiver.sock")
    monkeypatch.setattr(
        cutover,
        "arm_old_mail",
        lambda *a, **k: events.append("arm") or {"state": "old_pending_held"},
    )
    monkeypatch.setattr(
        cutover,
        "release_new_route",
        lambda *a, **k: events.append("release") or {"state": "new_route_released"},
    )
    source = tmp_path / "source"
    source.mkdir()
    (source / "bridge.py").write_bytes(b"pinned")
    monkeypatch.setattr(cutover.process, "FILES", ("bridge.py",))
    monkeypatch.setattr(cutover.process, "payload", lambda: source)
    monkeypatch.setattr(
        cutover.process,
        "run",
        lambda *a, **k: events.append("pin_apply") or {"state": "configured_runtime_unverified"},
    )
    return store, controller


def invoke(store, controller, **options):
    options.setdefault("stop_seconds", 0.6)
    options.setdefault("route_seconds", 0.6)
    return cutover.execute(
        store,
        "employee",
        "project",
        "plan_" + "b" * 32,
        "default",
        confirmed=True,
        control_factory=lambda *_: controller,
        **options,
    )


def read_journal(store, controller):
    return journal.read(
        store,
        "binding_" + "a" * 32,
        "plan_" + "b" * 32,
        employee_id="employee",
        project_id="project",
        profile_id="default",
        profile=controller.profile,
    )


def test_pin_write_only_after_drain_fence_and_independent_stop(monkeypatch, tmp_path):
    store, controller = setup(monkeypatch, tmp_path)
    controller.socket_present = True
    assert invoke(store, controller) == {
        "state": "new_route_released",
        "old_mail_released": False,
        "delivery_verified": False,
    }
    events = controller.events
    assert events.index("drain") < events.index("arm") < events.index("stop")
    assert events.index("stop") < events.index("remove-stale-socket") < events.index("pin_apply")
    assert (
        events.index("pin_apply")
        < events.index("clear")
        < events.index("start")
        < events.index("release")
    )
    record = read_journal(store, controller)
    assert record["phase"] == "complete"
    assert record["pin_may_have_run"] is True
    assert record["original_pid"] == 123 and record["original_start_time"] == 456


def test_nonidle_host_never_fences_or_stops(monkeypatch, tmp_path):
    store, controller = setup(monkeypatch, tmp_path)
    controller.drain_idle = False
    assert invoke(store, controller, drain_seconds=0)["state"] == "gateway_drain_not_idle"
    assert controller.events[-1] == "clear"
    assert not {"arm", "stop", "pin_apply", "start", "release"}.intersection(controller.events)


def test_uncertain_stop_restores_proven_stopped_host_but_keeps_fence(monkeypatch, tmp_path):
    store, controller = setup(monkeypatch, tmp_path)
    controller.stop_raises = True
    assert invoke(store, controller)["state"] == "stop_command_requires_inspection"
    before_retry = list(controller.events)
    assert invoke(store, controller)["state"] == "cutover_journal_requires_inspection"
    assert controller.events == before_retry
    assert "start" in controller.events
    assert "pin_apply" not in controller.events
    assert "release" not in controller.events


def test_stop_after_marker_failure_preserves_drain_and_fence(monkeypatch, tmp_path):
    store, controller = setup(monkeypatch, tmp_path)
    controller.stop_after_marker_raises = True
    controller.socket_present = True
    controller.pid_probe_failures = 100
    assert invoke(store, controller)["state"] == "gateway_stop_marker_requires_recovery"
    assert "arm" in controller.events and "stop" in controller.events
    assert not {"clear", "start", "pin_apply", "release", "remove-stale-socket"}.intersection(
        controller.events
    )
    record = read_journal(store, controller)
    assert record["phase"] == "stop_intent" and record["pin_may_have_run"] is False


def test_unconsumed_stop_marker_blocks_pin_and_restart(monkeypatch, tmp_path):
    store, controller = setup(monkeypatch, tmp_path)
    controller.marker_consumed = False
    assert invoke(store, controller)["state"] == "gateway_stop_marker_requires_recovery"
    assert "marker-consumed-check" in controller.events
    assert not {"clear", "start", "pin_apply", "release"}.intersection(controller.events)


def test_unreadable_post_stop_state_preserves_marker_recovery_boundary(monkeypatch, tmp_path):
    store, controller = setup(monkeypatch, tmp_path)
    controller.after_runtime_raises = True
    result = invoke(store, controller)
    assert result["state"] == "gateway_stop_marker_requires_recovery"
    assert "start" not in controller.events
    assert "clear" not in controller.events
    assert "pin_apply" not in controller.events
    assert "release" not in controller.events


def test_stop_waits_for_late_pid_exit_before_pin_apply(monkeypatch, tmp_path):
    store, controller = setup(monkeypatch, tmp_path)
    controller.pid_probe_failures = 2
    result = invoke(store, controller)
    assert result["state"] == "new_route_released"
    assert controller.events.index("stop") < controller.events.index("pin_apply")


def test_restart_waits_for_exact_receiver_route_before_releasing(monkeypatch, tmp_path):
    store, controller = setup(monkeypatch, tmp_path)
    attempts = []

    def release(*_args, **_kwargs):
        attempts.append("release")
        if len(attempts) == 1:
            return {"state": "check_unconfirmed", "delivery_verified": False}
        return {"state": "new_route_released", "delivery_verified": False}

    monkeypatch.setattr(cutover, "release_new_route", release)
    assert invoke(store, controller)["state"] == "new_route_released"
    assert len(attempts) == 2


def test_failed_drain_clear_does_not_restart_or_release(monkeypatch, tmp_path):
    store, controller = setup(monkeypatch, tmp_path)
    controller.clear_raises = True
    assert invoke(store, controller)["state"] == "gateway_recovery_required"
    assert "pin_apply" in controller.events
    assert "start" not in controller.events
    assert "release" not in controller.events


def test_failed_pin_apply_restores_host_but_keeps_old_mail_fenced(monkeypatch, tmp_path):
    store, controller = setup(monkeypatch, tmp_path)

    def fail_apply(*args, **kwargs):
        controller.events.append("pin_apply")
        raise ValueError("write outcome unknown")

    monkeypatch.setattr(cutover.process, "run", fail_apply)
    assert invoke(store, controller)["state"] == "fence_requires_recovery"
    assert controller.events.index("pin_apply") < controller.events.index("start")
    assert "release" not in controller.events
    record = read_journal(store, controller)
    assert record["phase"] == "pin_intent" and record["pin_may_have_run"] is True


def test_journal_failure_before_stop_keeps_fence_without_stopping(monkeypatch, tmp_path):
    store, controller = setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cutover.journal, "begin", lambda *_: (_ for _ in ()).throw(OSError("disk full"))
    )
    assert invoke(store, controller)["state"] == "fence_requires_recovery"
    assert "arm" in controller.events and "clear" in controller.events
    assert not {"stop", "pin_apply", "start", "release"}.intersection(controller.events)


def test_journal_failure_before_pin_never_runs_writer(monkeypatch, tmp_path):
    store, controller = setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cutover.journal, "advance", lambda *_: (_ for _ in ()).throw(OSError("disk full"))
    )
    assert invoke(store, controller)["state"] == "fence_requires_recovery"
    assert "stop" in controller.events and "start" in controller.events
    assert not {"pin_apply", "release"}.intersection(controller.events)


def test_wrong_profile_or_missing_confirmation_never_requests_drain(monkeypatch, tmp_path):
    store, controller = setup(monkeypatch, tmp_path)
    result = cutover.execute(
        store,
        "employee",
        "project",
        "plan_" + "b" * 32,
        "default",
        confirmed=False,
        control_factory=lambda *_: controller,
    )
    assert result["state"] == "confirmation_required"
    assert not controller.events
    result = cutover.execute(
        store,
        "employee",
        "project",
        "plan_" + "b" * 32,
        "another",
        confirmed=True,
        control_factory=lambda *_: controller,
    )
    assert result["state"] == "profile_unconfirmed"
    assert not controller.events
    result = cutover.execute(
        store,
        "employee",
        "project",
        "not-a-plan",
        "default",
        confirmed=True,
        control_factory=lambda *_: controller,
    )
    assert result["state"] == "plan_unconfirmed"
    assert not controller.events


def test_concurrent_owner_cutover_refuses_without_touching_gateway(monkeypatch, tmp_path):
    store, controller = setup(monkeypatch, tmp_path)
    with cutover._exclusive_cutover(store.root):
        assert invoke(store, controller)["state"] == "cutover_in_progress"
    assert not controller.events


@pytest.mark.parametrize("phase", ["restart_intent", "restart_verified"])
def test_restored_plan_cannot_reenter_cutover_or_touch_gateway(monkeypatch, tmp_path, phase):
    store, controller = setup(monkeypatch, tmp_path)
    binding = {
        "id": "binding_" + "a" * 32,
        "host_kind": "hermes",
        "employee_id": "employee",
        "project_id": "project",
    }
    plan = "plan_" + "b" * 32
    journal.begin(store, binding, plan, "default", controller, controller.before)
    journal.advance(
        store,
        binding["id"],
        plan,
        "stop_intent",
        "restart_intent",
        employee_id="employee",
        project_id="project",
        profile_id="default",
        profile=controller.profile,
    )
    if phase == "restart_verified":
        journal.advance(
            store,
            binding["id"],
            plan,
            "restart_intent",
            "restart_verified",
            employee_id="employee",
            project_id="project",
            profile_id="default",
            profile=controller.profile,
        )
    result = invoke(store, controller)
    assert result == {"state": "cutover_journal_requires_inspection", "delivery_verified": False}
    assert controller.events == []
    assert read_journal(store, controller)["phase"] == phase


def test_prior_plan_for_same_binding_blocks_new_plan_before_gateway(monkeypatch, tmp_path):
    store, controller = setup(monkeypatch, tmp_path)
    binding = {
        "id": "binding_" + "a" * 32,
        "host_kind": "hermes",
        "employee_id": "employee",
        "project_id": "project",
    }
    old_plan = "plan_" + "c" * 32
    journal.begin(store, binding, old_plan, "default", controller, controller.before)
    journal.advance(
        store,
        binding["id"],
        old_plan,
        "stop_intent",
        "restart_intent",
        employee_id="employee",
        project_id="project",
        profile_id="default",
        profile=controller.profile,
    )
    result = invoke(store, controller)
    assert result == {"state": "cutover_journal_requires_inspection", "delivery_verified": False}
    assert controller.events == []
    assert (
        journal.read(
            store,
            binding["id"],
            old_plan,
            employee_id="employee",
            project_id="project",
            profile_id="default",
            profile=controller.profile,
        )["phase"]
        == "restart_intent"
    )


def test_completed_prior_plan_needs_matching_cutoff_before_next_gateway_drain(
    monkeypatch, tmp_path
):
    store, controller = setup(monkeypatch, tmp_path)
    binding = {
        "id": "binding_" + "a" * 32,
        "host_kind": "hermes",
        "employee_id": "employee",
        "project_id": "project",
    }
    old_plan = "plan_" + "c" * 32
    journal.begin(store, binding, old_plan, "default", controller, controller.before)
    scope = {
        "employee_id": "employee",
        "project_id": "project",
        "profile_id": "default",
        "profile": controller.profile,
    }
    journal.advance(store, binding["id"], old_plan, "stop_intent", "pin_intent", **scope)
    journal.advance(store, binding["id"], old_plan, "pin_intent", "complete", **scope)

    # A completed journal alone is not proof that its delivery boundary was
    # persisted. Do not drain the same Gateway until the pair matches.
    assert invoke(store, controller) == {
        "state": "fence_requires_recovery",
        "delivery_verified": False,
    }
    assert controller.events == []

    required = required_path(store.root, binding["id"])
    cutoff = cutoff_path(store.root, binding["id"])
    required.write_text(
        json.dumps({"version": 1, "binding_id": binding["id"], "plan_id": old_plan})
    )
    cutoff.write_text(
        json.dumps(
            {
                "version": 1,
                "binding_id": binding["id"],
                "plan_id": old_plan,
                "authorized_after": datetime.now(timezone.utc).isoformat(),
            }
        )
    )
    required.chmod(0o600)
    cutoff.chmod(0o600)
    assert invoke(store, controller)["state"] == "new_route_released"
    assert "drain" in controller.events


def test_partial_binding_fence_blocks_new_plan_before_gateway(monkeypatch, tmp_path):
    store, controller = setup(monkeypatch, tmp_path)
    gate = gate_path(store.root, "binding_" + "a" * 32)
    gate.parent.mkdir(mode=0o700)
    gate.write_text("partial previous plan")
    gate.chmod(0o600)
    assert invoke(store, controller) == {
        "state": "fence_requires_recovery",
        "delivery_verified": False,
    }
    assert controller.events == []


def test_exact_prearmed_plan_can_commit_without_releasing_old_mail(monkeypatch, tmp_path):
    store, controller = setup(monkeypatch, tmp_path)
    binding_id = "binding_" + "a" * 32
    plan_id = "plan_" + "b" * 32
    claims = store.root / "wake" / "claims"
    claims.mkdir(mode=0o700)
    required = required_path(store.root, binding_id)
    gate = gate_path(store.root, binding_id)
    required.write_text(json.dumps({
        "version": 1, "binding_id": binding_id, "plan_id": plan_id,
    }))
    gate.write_text(json.dumps({
        "version": 1, "binding_id": binding_id, "plan_id": plan_id,
        "employee_id": "employee", "project_id": "project",
    }))
    required.chmod(0o600)
    gate.chmod(0o600)
    assert invoke(store, controller)["state"] == "new_route_released"
    assert controller.events.index("drain") < controller.events.index("arm")


def test_exact_prearmed_plan_after_prejournal_baseline_can_commit(monkeypatch, tmp_path):
    store, controller = setup(monkeypatch, tmp_path)
    binding_id = "binding_" + "a" * 32
    old_plan = "plan_" + "c" * 32
    plan_id = "plan_" + "b" * 32
    claims = store.root / "wake" / "claims"
    claims.mkdir(mode=0o700)
    timestamp = datetime.now(timezone.utc).isoformat()
    records = {
        required_path(store.root, binding_id): {
            "version": 1, "binding_id": binding_id, "plan_id": old_plan,
        },
        cutoff_path(store.root, binding_id): {
            "version": 1, "binding_id": binding_id, "plan_id": old_plan,
            "authorized_after": timestamp,
        },
        epoch_path(store.root, binding_id, plan_id): {
            "version": 1, "binding_id": binding_id, "plan_id": plan_id,
            "phase": "pending", "previous_cutoff": timestamp, "authorized_after": None,
        },
        gate_path(store.root, binding_id): {
            "version": 1, "binding_id": binding_id, "plan_id": plan_id,
            "employee_id": "employee", "project_id": "project",
        },
    }
    for path, value in records.items():
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    assert invoke(store, controller)["state"] == "new_route_released"
    assert controller.events.index("drain") < controller.events.index("arm")


def test_unsafe_binding_journal_directory_blocks_gateway(monkeypatch, tmp_path):
    store, controller = setup(monkeypatch, tmp_path)
    claims = store.root / "wake" / "claims"
    claims.mkdir(mode=0o755)
    claims.chmod(0o755)
    assert invoke(store, controller) == {"state": "check_unconfirmed", "delivery_verified": False}
    assert controller.events == []
