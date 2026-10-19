"""Synthetic Owner admission boundary; no Hermes Gateway or model is started."""

import hashlib
import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

import pytest

from agent_mailbox import workbench_hermes_admission as admission
from agent_mailbox import workbench_hermes_admission_cutover as admission_cutover
from agent_mailbox import workbench_hermes_admission_journal as admission_journal
from agent_mailbox import workbench_hermes_catalog as catalog
from agent_mailbox import workbench_hermes_installer as installer
from agent_mailbox.workbench import WorkbenchHTTP
from agent_mailbox.workbench_hermes_gateway_control import HermesStopAfterMarkerUnconfirmed
from agent_mailbox.workbench_mail_sessions import create_session
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


@pytest.fixture
def case(tmp_path, monkeypatch):
    FirstContactGateway.events = []
    FirstContactGateway.busy = False
    FirstContactGateway.restored = False
    home = tmp_path.resolve()
    user = home / "user"
    user.mkdir(mode=0o700)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user))
    monkeypatch.setattr(installer, "SOCKET_ROOT", user)
    profile = user / ".hermes"
    profile.mkdir(mode=0o700)
    python = profile / "hermes-agent/.venv/bin/python"
    python.parent.mkdir(parents=True, mode=0o700)
    python.write_text("synthetic")
    store = WorkbenchStore(home / "data")
    project = store.create_project("Synthetic", home)
    employee = store.create_employee("Synthetic", "hermes", project["id"])
    monkeypatch.setattr(
        catalog,
        "_discover",
        lambda: (
            profile,
            python,
            {
                "state": "profile_candidates",
                "profiles": [{"home": str(profile), "profile_id": "default"}],
            },
        ),
    )
    profile_id = catalog.choices(store, employee["id"], project["id"])["profiles"][0]["id"]
    calls = []

    def child(runtime, selected, request, **kwargs):
        calls.append((request, kwargs))
        assert runtime == python and selected == profile
        operation = request["operation"]
        if operation in {"preview_discovery", "apply"}:
            return {
                "state": "preview"
                if operation == "preview_discovery"
                else "prepared_not_activated",
                "plan_id": "plan_" + "a" * 32,
                "requires_confirmation": True,
                "will_activate": False,
                "will_write_injection_grant": False,
                "session_verified": False,
                "delivery_verified": False,
            }
        assert operation.startswith("diagnostic_admission_")
        return {
            "state": {
                "diagnostic_admission_preview": "preview",
                "diagnostic_admission_apply": "diagnostic_enabled_restart_required",
                "diagnostic_admission_status": "diagnostic_enabled_restart_required",
                "diagnostic_admission_rollback": "rolled_back",
            }[operation],
            "plan_id": "plan_" + "b" * 32,
            "requires_confirmation": True,
            "requires_stopped_gateway": True,
            "runtime_verified": False,
            "session_verified": False,
            "delivery_verified": False,
        }

    monkeypatch.setattr(installer.process, "run", child)
    prepared = installer.installation(
        store, employee["id"], project["id"], "preview", profile_id=profile_id
    )
    installer.installation(
        store, employee["id"], project["id"], "apply", plan_id=prepared["plan_id"], confirmed=True
    )
    calls.clear()
    return store, project, employee, profile, python, prepared, calls


class Stopped:
    permitted = True

    def __init__(self, python, profile, *, require_stopped=False):
        assert require_stopped is True
        self.profile = profile

    def runtime(self):
        return {
            "kind": "hermes-gateway",
            "gateway_state": "stopped",
            "hermes_home": str(self.profile),
            "pid": 123,
        }

    def service_stopped(self):
        return self.permitted

    def original_pid_gone(self, pid):
        return pid == 123

    def receiver_socket_absent(self, endpoint):
        return endpoint.name == "hermes-diag.sock"


class RunningDiagnostic:
    available = True

    def __init__(self, python, profile):
        self.profile = profile

    def observe_diagnostic(self, endpoint):
        assert endpoint.name == "hermes-diag.sock"
        if not self.available:
            raise ValueError("diagnostic_not_loaded")
        return {
            "state": "diagnostic_runtime_observed",
            "runtime_verified": True,
            "session_verified": False,
            "delivery_verified": False,
        }


class FirstContactGateway:
    events: ClassVar[list[tuple[str, str | None]]] = []
    busy = False
    restored = False

    def __init__(self, python, profile, *, require_stopped=False):
        self.profile = profile
        self.domain = "gui/" + str(os.getuid())
        self.plist_digest = bytes.fromhex("a" * 64)
        self.command_digest = bytes.fromhex("b" * 64)
        self.stopped = False

    def receiver_socket_absent(self, _endpoint):
        return True

    def request_drain(self, plan_id):
        self.events.append(("drain", plan_id))

    def clear_drain(self, plan_id):
        self.events.append(("clear", plan_id))

    def live_status(self):
        now = datetime.now(timezone.utc)
        runtime = (
            self.runtime()
            if self.restored
            else {
                "kind": "hermes-gateway",
                "gateway_state": "draining",
                "pid": 123,
                "start_time": 456,
                "hermes_home": str(self.profile),
                "active_agents": 1 if self.busy else 0,
                "active_work": None,
                "updated_at": now.isoformat(),
            }
        )
        return runtime, {
            **runtime,
            "protocol": 1,
            "answered_at": now.timestamp(),
            "answering_pid": runtime["pid"],
        }

    def pid_identity(self, _runtime):
        return True

    def stop(self):
        self.events.append(("stop", None))
        self.stopped = True

    def runtime(self):
        now = datetime.now(timezone.utc)
        if self.restored:
            return {
                "kind": "hermes-gateway",
                "gateway_state": "running",
                "pid": 789,
                "start_time": 987,
                "hermes_home": str(self.profile),
                "active_agents": 0,
                "active_work": None,
                "updated_at": now.isoformat(),
            }
        return {
            "kind": "hermes-gateway",
            "gateway_state": "stopped",
            "pid": 123,
            "start_time": 456,
            "hermes_home": str(self.profile),
            "active_agents": 0,
            "active_work": None,
            "restart_requested": False,
            "updated_at": now.isoformat(),
        }

    def original_pid_gone(self, _pid):
        return self.stopped

    def service_stopped(self):
        return self.stopped

    def planned_stop_marker_absent(self):
        return True

    def strict_gateway_absent(self):
        return self.stopped

    @classmethod
    def restore_after_pm(cls, profile, domain, *, original_plist_sha256, original_command_sha256):
        assert domain.startswith("gui/") and profile
        assert original_plist_sha256 == "a" * 64
        assert original_command_sha256 == "b" * 64
        cls.events.append(("restore", None))
        cls.restored = True

    def observe_diagnostic(self, endpoint):
        assert self.restored and endpoint.name == "hermes-diag.sock"
        return {
            "state": "diagnostic_runtime_observed",
            "runtime_verified": True,
            "session_verified": False,
            "delivery_verified": False,
        }


def synthetic_admission_status(monkeypatch):
    original = admission_cutover.process.run
    state = {"applied": False}

    def child(python, selected, request, **kwargs):
        result = original(python, selected, request, **kwargs)
        if request["operation"] == "diagnostic_admission_apply":
            state["applied"] = True
        if request["operation"] == "diagnostic_admission_status" and not state["applied"]:
            return {**result, "state": "preview"}
        return result

    monkeypatch.setattr(admission_cutover.process, "run", child)
    return child


def test_exact_preparation_and_independent_stop_gate(case, monkeypatch):
    store, project, employee, _profile, _python, prepared, calls = case
    actor, scope = employee["id"], project["id"]
    preview = admission.admission(
        store, actor, scope, "preview", preparation_id=prepared["plan_id"]
    )
    assert preview["state"] == "preview"
    assert len(preview["plan_id"]) == 32
    assert calls[-1][0]["preparation_id"] == "plan_" + "a" * 32
    assert (
        admission.admission(store, actor, scope, "apply", plan_id=preview["plan_id"])["state"]
        == "confirmation_required"
    )
    assert len(calls) == 1
    monkeypatch.setattr(Stopped, "permitted", False)
    assert (
        admission.admission(
            store,
            actor,
            scope,
            "apply",
            plan_id=preview["plan_id"],
            confirmed=True,
            control_factory=Stopped,
        )["state"]
        == "gateway_stop_unconfirmed"
    )
    assert not any(
        request["operation"] == "diagnostic_admission_apply" for request, _kwargs in calls
    )
    monkeypatch.setattr(Stopped, "permitted", True)
    enabled = admission.admission(
        store,
        actor,
        scope,
        "apply",
        plan_id=preview["plan_id"],
        confirmed=True,
        control_factory=Stopped,
    )
    assert enabled["state"] == "diagnostic_enabled_restart_required"
    assert enabled["runtime_verified"] is False and enabled["delivery_verified"] is False
    assert calls[-1][0]["gateway_stopped"] is True and calls[-1][1]["timeout"] == 120
    assert (
        admission.admission(store, actor, scope, "status", plan_id=preview["plan_id"])["state"]
        == enabled["state"]
    )
    assert (
        admission.admission(
            store,
            actor,
            scope,
            "rollback",
            plan_id=preview["plan_id"],
            confirmed=True,
            control_factory=Stopped,
        )["state"]
        == "rolled_back"
    )


def test_first_contact_requires_no_existing_route_then_restores_and_verifies(case, monkeypatch):
    store, project, employee, profile, _python, prepared, calls = case
    actor, scope = employee["id"], project["id"]
    planned = admission.admission(
        store, actor, scope, "preview", preparation_id=prepared["plan_id"]
    )
    assert (
        admission_cutover.inspect_first_contact(store, actor, scope, planned["plan_id"])["state"]
        == "first_contact_not_started"
    )
    synthetic_admission_status(monkeypatch)
    FirstContactGateway.events = []
    FirstContactGateway.busy = False
    FirstContactGateway.restored = False
    monkeypatch.setattr(admission_cutover, "load_bindings", lambda _home: [{"host_kind": "hermes"}])
    rejected = admission_cutover.execute_first_contact(
        store,
        actor,
        scope,
        planned["plan_id"],
        confirmed=True,
        control_factory=FirstContactGateway,
        drain_seconds=0,
        stop_seconds=0,
        route_seconds=0,
    )
    assert rejected["state"] == "existing_hermes_route_requires_cutover"
    assert not FirstContactGateway.events
    monkeypatch.setattr(admission_cutover, "load_bindings", lambda _home: [])
    FirstContactGateway.busy = True
    busy = admission_cutover.execute_first_contact(
        store,
        actor,
        scope,
        planned["plan_id"],
        confirmed=True,
        control_factory=FirstContactGateway,
        drain_seconds=0,
        stop_seconds=0,
        route_seconds=0,
    )
    assert busy["state"] == "gateway_drain_not_idle"
    assert [event[0] for event in FirstContactGateway.events] == ["drain", "clear"]
    assert not admission_journal.unfinished_exists(store.root)
    FirstContactGateway.events = []
    FirstContactGateway.busy = False
    checks = 0

    def newly_bound(_home):
        nonlocal checks
        checks += 1
        return [] if checks == 1 else [{"host_kind": "hermes"}]

    monkeypatch.setattr(admission_cutover, "load_bindings", newly_bound)
    changed = admission_cutover.execute_first_contact(
        store,
        actor,
        scope,
        planned["plan_id"],
        confirmed=True,
        control_factory=FirstContactGateway,
        drain_seconds=0,
        stop_seconds=0,
        route_seconds=0,
    )
    assert changed["state"] == "existing_hermes_route_requires_cutover"
    assert [event[0] for event in FirstContactGateway.events] == ["drain", "clear"]
    assert not admission_journal.unfinished_exists(store.root)
    monkeypatch.setattr(admission_cutover, "load_bindings", lambda _home: [])
    FirstContactGateway.events = []
    done = admission_cutover.execute_first_contact(
        store,
        actor,
        scope,
        planned["plan_id"],
        confirmed=True,
        control_factory=FirstContactGateway,
        drain_seconds=0,
        stop_seconds=0,
        route_seconds=0,
    )
    assert done == {
        "state": "diagnostic_runtime_observed",
        "runtime_verified": True,
        "session_verified": False,
        "delivery_verified": False,
    }
    assert [event[0] for event in FirstContactGateway.events] == [
        "drain",
        "stop",
        "clear",
        "restore",
    ]
    assert (
        admission_journal.read(
            store,
            planned["plan_id"],
            employee_id=actor,
            project_id=scope,
            profile_id=prepared["profile_id"],
            profile=profile,
        )["phase"]
        == "complete"
    )
    assert admission_cutover.inspect_first_contact(store, actor, scope, planned["plan_id"]) == {
        "state": "recorded_complete_runtime_unconfirmed",
        "phase": "complete",
        "plan_id": planned["plan_id"],
        "runtime_verified": False,
        "session_verified": False,
        "delivery_verified": False,
    }
    assert (
        sum(request["operation"] == "diagnostic_admission_apply" for request, _kwargs in calls) == 1
    )
    with pytest.raises(WorkbenchError, match="needs inspection"):
        admission.admission(
            store,
            actor,
            scope,
            "apply",
            plan_id=planned["plan_id"],
            confirmed=True,
            control_factory=Stopped,
        )


def test_first_contact_unknown_pm_does_not_restore_or_replay(case, monkeypatch):
    store, project, employee, profile, _python, prepared, _calls = case
    actor, scope = employee["id"], project["id"]
    planned = admission.admission(
        store, actor, scope, "preview", preparation_id=prepared["plan_id"]
    )
    original = synthetic_admission_status(monkeypatch)
    monkeypatch.setattr(admission_cutover, "load_bindings", lambda _home: [])
    FirstContactGateway.events = []
    FirstContactGateway.busy = False
    FirstContactGateway.restored = False

    def interrupted(python, selected, request, **kwargs):
        if request["operation"] == "diagnostic_admission_apply":
            raise ValueError("synthetic_pm_unknown")
        return original(python, selected, request, **kwargs)

    monkeypatch.setattr(admission_cutover.process, "run", interrupted)
    first = admission_cutover.execute_first_contact(
        store,
        actor,
        scope,
        planned["plan_id"],
        confirmed=True,
        control_factory=FirstContactGateway,
        drain_seconds=0,
        stop_seconds=0,
        route_seconds=0,
    )
    assert first["state"] == "admission_requires_inspection"
    assert [event[0] for event in FirstContactGateway.events] == ["drain", "stop", "clear"]
    assert (
        admission_journal.read(
            store,
            planned["plan_id"],
            employee_id=actor,
            project_id=scope,
            profile_id=prepared["profile_id"],
            profile=profile,
        )["phase"]
        == "admission_intent"
    )
    assert admission_cutover.inspect_first_contact(store, actor, scope, planned["plan_id"])[
        "state"
    ] == ("plugin_outcome_requires_inspection")
    with pytest.raises(WorkbenchError, match="needs inspection"):
        admission.admission(
            store,
            actor,
            scope,
            "apply",
            plan_id=planned["plan_id"],
            confirmed=True,
            control_factory=Stopped,
        )
    calls_before = len(FirstContactGateway.events)
    second = admission_cutover.execute_first_contact(
        store,
        actor,
        scope,
        planned["plan_id"],
        confirmed=True,
        control_factory=FirstContactGateway,
        drain_seconds=0,
        stop_seconds=0,
        route_seconds=0,
    )
    assert second["state"] == "admission_journal_requires_inspection"
    assert len(FirstContactGateway.events) == calls_before
    unresolved = admission_cutover.resume_committed_admission(
        store,
        actor,
        scope,
        planned["plan_id"],
        confirmed=True,
        control_factory=FirstContactGateway,
        route_seconds=0,
    )
    assert unresolved["state"] == "plugin_outcome_requires_inspection"
    assert len(FirstContactGateway.events) == calls_before


def test_lost_pm_reply_with_committed_status_restores_once_without_pm_replay(case, monkeypatch):
    store, project, employee, profile, _python, prepared, calls = case
    actor, scope = employee["id"], project["id"]
    planned = admission.admission(
        store, actor, scope, "preview", preparation_id=prepared["plan_id"]
    )
    monkeypatch.setattr(admission_cutover, "load_bindings", lambda _home: [])
    original = admission_cutover.process.run
    applied = False

    def lost_reply(python, selected, request, **kwargs):
        nonlocal applied
        result = original(python, selected, request, **kwargs)
        if request["operation"] == "diagnostic_admission_apply":
            applied = True
            raise ValueError("synthetic_pm_reply_lost_after_commit")
        if request["operation"] == "diagnostic_admission_status":
            return {
                **result,
                "state": ("diagnostic_enabled_restart_required" if applied else "preview"),
            }
        return result

    monkeypatch.setattr(admission_cutover.process, "run", lost_reply)
    first = admission_cutover.execute_first_contact(
        store,
        actor,
        scope,
        planned["plan_id"],
        confirmed=True,
        control_factory=FirstContactGateway,
        drain_seconds=0,
        stop_seconds=0,
        route_seconds=0,
    )
    assert first["state"] == "admission_requires_inspection"
    assert (
        admission_journal.read(
            store,
            planned["plan_id"],
            employee_id=actor,
            project_id=scope,
            profile_id=prepared["profile_id"],
            profile=profile,
        )["phase"]
        == "admission_intent"
    )
    original_init = FirstContactGateway.__init__

    def verified_stopped(self, python, selected, *, require_stopped=False):
        original_init(self, python, selected, require_stopped=require_stopped)
        self.stopped = True

    monkeypatch.setattr(FirstContactGateway, "__init__", verified_stopped)
    result = admission_cutover.resume_committed_admission(
        store,
        actor,
        scope,
        planned["plan_id"],
        confirmed=True,
        control_factory=FirstContactGateway,
        route_seconds=0,
    )
    assert result == {
        "state": "diagnostic_runtime_observed",
        "runtime_verified": True,
        "session_verified": False,
        "delivery_verified": False,
    }
    assert [event[0] for event in FirstContactGateway.events] == [
        "drain",
        "stop",
        "clear",
        "restore",
    ]
    assert (
        sum(request["operation"] == "diagnostic_admission_apply" for request, _kwargs in calls) == 1
    )
    assert (
        admission_journal.read(
            store,
            planned["plan_id"],
            employee_id=actor,
            project_id=scope,
            profile_id=prepared["profile_id"],
            profile=profile,
        )["phase"]
        == "complete"
    )


def test_first_contact_uncertain_stop_retains_intent_without_pm_or_restart(case, monkeypatch):
    store, project, employee, profile, _python, prepared, calls = case
    actor, scope = employee["id"], project["id"]
    planned = admission.admission(
        store, actor, scope, "preview", preparation_id=prepared["plan_id"]
    )
    synthetic_admission_status(monkeypatch)
    monkeypatch.setattr(admission_cutover, "load_bindings", lambda _home: [])
    FirstContactGateway.events = []
    FirstContactGateway.busy = False
    FirstContactGateway.restored = False

    def uncertain_stop(self):
        self.events.append(("stop", None))
        raise HermesStopAfterMarkerUnconfirmed("synthetic_marker_unknown")

    monkeypatch.setattr(FirstContactGateway, "stop", uncertain_stop)
    result = admission_cutover.execute_first_contact(
        store,
        actor,
        scope,
        planned["plan_id"],
        confirmed=True,
        control_factory=FirstContactGateway,
        drain_seconds=0,
        stop_seconds=0,
        route_seconds=0,
    )
    assert result["state"] == "gateway_stop_marker_requires_recovery"
    assert [event[0] for event in FirstContactGateway.events] == ["drain", "stop"]
    assert not any(
        request["operation"] == "diagnostic_admission_apply" for request, _kwargs in calls
    )
    assert (
        admission_journal.read(
            store,
            planned["plan_id"],
            employee_id=actor,
            project_id=scope,
            profile_id=prepared["profile_id"],
            profile=profile,
        )["phase"]
        == "stop_intent"
    )
    assert admission_cutover.inspect_first_contact(store, actor, scope, planned["plan_id"])[
        "state"
    ] == ("stop_outcome_requires_inspection")
    assert (
        admission_cutover.execute_first_contact(
            store,
            actor,
            scope,
            planned["plan_id"],
            confirmed=True,
            control_factory=FirstContactGateway,
            drain_seconds=0,
            stop_seconds=0,
            route_seconds=0,
        )["state"]
        == "admission_journal_requires_inspection"
    )
    unproven = admission_cutover.restore_before_admission(
        store,
        actor,
        scope,
        planned["plan_id"],
        confirmed=True,
        control_factory=FirstContactGateway,
        route_seconds=0,
    )
    assert unproven["state"] == "gateway_stop_requires_inspection"
    assert [event[0] for event in FirstContactGateway.events] == ["drain", "stop"]

    # Only an independently proven stopped service can take the explicit
    # pre-PM restore branch; that branch must never run PM apply.
    original_init = FirstContactGateway.__init__

    def verified_stopped(self, python, selected, *, require_stopped=False):
        original_init(self, python, selected, require_stopped=require_stopped)
        self.stopped = True

    monkeypatch.setattr(FirstContactGateway, "__init__", verified_stopped)
    restored = admission_cutover.restore_before_admission(
        store,
        actor,
        scope,
        planned["plan_id"],
        confirmed=True,
        control_factory=FirstContactGateway,
        route_seconds=0,
    )
    assert restored == {
        "state": "pre_admission_restored",
        "runtime_verified": False,
        "session_verified": False,
        "delivery_verified": False,
        "gateway_running_verified": True,
    }
    assert (
        admission_journal.read(
            store,
            planned["plan_id"],
            employee_id=actor,
            project_id=scope,
            profile_id=prepared["profile_id"],
            profile=profile,
        )["phase"]
        == "pre_admission_restored"
    )
    assert not admission_journal.unfinished_exists(store.root)
    assert not any(
        request["operation"] == "diagnostic_admission_apply" for request, _kwargs in calls
    )
    assert (
        admission_cutover.execute_first_contact(
            store,
            actor,
            scope,
            planned["plan_id"],
            confirmed=True,
            control_factory=FirstContactGateway,
            drain_seconds=0,
            stop_seconds=0,
            route_seconds=0,
        )["state"]
        == "admission_journal_requires_inspection"
    )


def test_first_contact_failed_drain_clear_keeps_pre_pm_recovery(case, monkeypatch):
    store, project, employee, profile, _python, prepared, calls = case
    actor, scope = employee["id"], project["id"]
    planned = admission.admission(
        store, actor, scope, "preview", preparation_id=prepared["plan_id"]
    )
    synthetic_admission_status(monkeypatch)
    monkeypatch.setattr(admission_cutover, "load_bindings", lambda _home: [])
    original_clear = FirstContactGateway.clear_drain
    first_clear = True

    def clear_once_unconfirmed(self, plan_id):
        nonlocal first_clear
        if first_clear:
            first_clear = False
            raise OSError("synthetic_drain_clear_unconfirmed")
        return original_clear(self, plan_id)

    monkeypatch.setattr(FirstContactGateway, "clear_drain", clear_once_unconfirmed)
    result = admission_cutover.execute_first_contact(
        store,
        actor,
        scope,
        planned["plan_id"],
        confirmed=True,
        control_factory=FirstContactGateway,
        drain_seconds=0,
        stop_seconds=0,
        route_seconds=0,
    )
    assert result["state"] == "admission_requires_inspection"
    assert [event[0] for event in FirstContactGateway.events] == ["drain", "stop"]
    assert not any(
        request["operation"] == "diagnostic_admission_apply" for request, _kwargs in calls
    )
    assert (
        admission_journal.read(
            store,
            planned["plan_id"],
            employee_id=actor,
            project_id=scope,
            profile_id=prepared["profile_id"],
            profile=profile,
        )["phase"]
        == "stop_intent"
    )

    original_init = FirstContactGateway.__init__

    def verified_stopped(self, python, selected, *, require_stopped=False):
        original_init(self, python, selected, require_stopped=require_stopped)
        self.stopped = True

    monkeypatch.setattr(FirstContactGateway, "__init__", verified_stopped)
    restored = admission_cutover.restore_before_admission(
        store,
        actor,
        scope,
        planned["plan_id"],
        confirmed=True,
        control_factory=FirstContactGateway,
        route_seconds=0,
    )
    assert restored["state"] == "pre_admission_restored"
    assert [event[0] for event in FirstContactGateway.events] == [
        "drain",
        "stop",
        "clear",
        "restore",
    ]
    assert not any(
        request["operation"] == "diagnostic_admission_apply" for request, _kwargs in calls
    )


def test_first_contact_failed_restore_retains_restart_intent_without_replay(case, monkeypatch):
    store, project, employee, profile, _python, prepared, calls = case
    actor, scope = employee["id"], project["id"]
    planned = admission.admission(
        store, actor, scope, "preview", preparation_id=prepared["plan_id"]
    )
    synthetic_admission_status(monkeypatch)
    monkeypatch.setattr(admission_cutover, "load_bindings", lambda _home: [])
    FirstContactGateway.events = []
    FirstContactGateway.busy = False
    FirstContactGateway.restored = False

    def failed_restore(cls, *_args, **_kwargs):
        cls.events.append(("restore", None))
        raise ValueError("synthetic_restart_unknown")

    monkeypatch.setattr(FirstContactGateway, "restore_after_pm", classmethod(failed_restore))
    result = admission_cutover.execute_first_contact(
        store,
        actor,
        scope,
        planned["plan_id"],
        confirmed=True,
        control_factory=FirstContactGateway,
        drain_seconds=0,
        stop_seconds=0,
        route_seconds=0,
    )
    assert result["state"] == "admission_requires_inspection"
    assert [event[0] for event in FirstContactGateway.events] == [
        "drain",
        "stop",
        "clear",
        "restore",
    ]
    assert (
        sum(request["operation"] == "diagnostic_admission_apply" for request, _kwargs in calls) == 1
    )
    assert (
        admission_journal.read(
            store,
            planned["plan_id"],
            employee_id=actor,
            project_id=scope,
            profile_id=prepared["profile_id"],
            profile=profile,
        )["phase"]
        == "restart_intent"
    )
    assert admission_cutover.inspect_first_contact(store, actor, scope, planned["plan_id"])[
        "state"
    ] == ("restart_outcome_requires_inspection")
    assert admission_cutover.reconcile_first_contact(
        store, actor, scope, planned["plan_id"], control_factory=FirstContactGateway
    )["state"] == ("restart_outcome_requires_inspection")
    assert (
        admission_cutover.execute_first_contact(
            store,
            actor,
            scope,
            planned["plan_id"],
            confirmed=True,
            control_factory=FirstContactGateway,
            drain_seconds=0,
            stop_seconds=0,
            route_seconds=0,
        )["state"]
        == "admission_journal_requires_inspection"
    )
    assert (
        sum(request["operation"] == "diagnostic_admission_apply" for request, _kwargs in calls) == 1
    )


def test_pre_admission_restore_lost_response_reconciles_without_second_start(case, monkeypatch):
    store, project, employee, profile, _python, prepared, calls = case
    actor, scope = employee["id"], project["id"]
    planned = admission.admission(
        store, actor, scope, "preview", preparation_id=prepared["plan_id"]
    )
    synthetic_admission_status(monkeypatch)
    monkeypatch.setattr(admission_cutover, "load_bindings", lambda _home: [])

    def uncertain_stop(self):
        self.events.append(("stop", None))
        raise HermesStopAfterMarkerUnconfirmed("synthetic_marker_unknown")

    monkeypatch.setattr(FirstContactGateway, "stop", uncertain_stop)
    first = admission_cutover.execute_first_contact(
        store,
        actor,
        scope,
        planned["plan_id"],
        confirmed=True,
        control_factory=FirstContactGateway,
        drain_seconds=0,
        stop_seconds=0,
        route_seconds=0,
    )
    assert first["state"] == "gateway_stop_marker_requires_recovery"
    original_init = FirstContactGateway.__init__

    def verified_stopped(self, python, selected, *, require_stopped=False):
        original_init(self, python, selected, require_stopped=require_stopped)
        self.stopped = True

    monkeypatch.setattr(FirstContactGateway, "__init__", verified_stopped)

    def started_but_unconfirmed(cls, *_args, **_kwargs):
        cls.events.append(("restore", None))
        cls.restored = True
        raise ValueError("synthetic_response_lost_after_start")

    monkeypatch.setattr(
        FirstContactGateway, "restore_after_pm", classmethod(started_but_unconfirmed)
    )
    unknown = admission_cutover.restore_before_admission(
        store,
        actor,
        scope,
        planned["plan_id"],
        confirmed=True,
        control_factory=FirstContactGateway,
        route_seconds=0,
    )
    assert unknown["state"] == "pre_admission_restore_requires_inspection"
    assert (
        admission_journal.read(
            store,
            planned["plan_id"],
            employee_id=actor,
            project_id=scope,
            profile_id=prepared["profile_id"],
            profile=profile,
        )["phase"]
        == "pre_admission_restore_intent"
    )
    observed = admission_cutover.restore_before_admission(
        store,
        actor,
        scope,
        planned["plan_id"],
        confirmed=True,
        control_factory=FirstContactGateway,
        route_seconds=0,
    )
    assert observed["state"] == "pre_admission_restored"
    assert [event[0] for event in FirstContactGateway.events].count("restore") == 1
    assert (
        admission_journal.read(
            store,
            planned["plan_id"],
            employee_id=actor,
            project_id=scope,
            profile_id=prepared["profile_id"],
            profile=profile,
        )["phase"]
        == "pre_admission_restored"
    )
    assert not any(
        request["operation"] == "diagnostic_admission_apply" for request, _kwargs in calls
    )


def test_first_contact_reconciles_observed_restart_without_replaying_pm(case, monkeypatch):
    store, project, employee, profile, _python, prepared, calls = case
    actor, scope = employee["id"], project["id"]
    planned = admission.admission(
        store, actor, scope, "preview", preparation_id=prepared["plan_id"]
    )
    synthetic_admission_status(monkeypatch)
    monkeypatch.setattr(admission_cutover, "load_bindings", lambda _home: [])
    FirstContactGateway.events = []
    FirstContactGateway.busy = False
    FirstContactGateway.restored = False

    def started_but_unconfirmed(cls, *_args, **_kwargs):
        cls.events.append(("restore", None))
        cls.restored = True
        raise ValueError("synthetic_response_lost_after_start")

    monkeypatch.setattr(
        FirstContactGateway, "restore_after_pm", classmethod(started_but_unconfirmed)
    )
    first = admission_cutover.execute_first_contact(
        store,
        actor,
        scope,
        planned["plan_id"],
        confirmed=True,
        control_factory=FirstContactGateway,
        drain_seconds=0,
        stop_seconds=0,
        route_seconds=0,
    )
    assert first["state"] == "admission_requires_inspection"
    assert (
        admission_journal.read(
            store,
            planned["plan_id"],
            employee_id=actor,
            project_id=scope,
            profile_id=prepared["profile_id"],
            profile=profile,
        )["phase"]
        == "restart_intent"
    )
    original_init = FirstContactGateway.__init__

    def changed_launcher(self, python, selected):
        original_init(self, python, selected)
        self.command_digest = bytes.fromhex("c" * 64)

    monkeypatch.setattr(FirstContactGateway, "__init__", changed_launcher)
    refused = admission_cutover.reconcile_first_contact(
        store, actor, scope, planned["plan_id"], control_factory=FirstContactGateway
    )
    assert refused["state"] == "restart_outcome_requires_inspection"
    assert (
        admission_journal.read(
            store,
            planned["plan_id"],
            employee_id=actor,
            project_id=scope,
            profile_id=prepared["profile_id"],
            profile=profile,
        )["phase"]
        == "restart_intent"
    )
    monkeypatch.setattr(FirstContactGateway, "__init__", original_init)
    recovered = admission_cutover.reconcile_first_contact(
        store, actor, scope, planned["plan_id"], control_factory=FirstContactGateway
    )
    assert recovered == {
        "state": "diagnostic_runtime_observed",
        "runtime_verified": True,
        "session_verified": False,
        "delivery_verified": False,
    }
    assert [event[0] for event in FirstContactGateway.events] == [
        "drain",
        "stop",
        "clear",
        "restore",
    ]
    assert (
        sum(request["operation"] == "diagnostic_admission_apply" for request, _kwargs in calls) == 1
    )
    assert (
        admission_journal.read(
            store,
            planned["plan_id"],
            employee_id=actor,
            project_id=scope,
            profile_id=prepared["profile_id"],
            profile=profile,
        )["phase"]
        == "complete"
    )


def test_first_contact_stop_timeout_is_unknown_not_a_retry_signal(case, monkeypatch):
    store, project, employee, _profile, _python, prepared, calls = case
    actor, scope = employee["id"], project["id"]
    planned = admission.admission(
        store, actor, scope, "preview", preparation_id=prepared["plan_id"]
    )
    synthetic_admission_status(monkeypatch)
    monkeypatch.setattr(admission_cutover, "load_bindings", lambda _home: [])
    FirstContactGateway.events = []
    FirstContactGateway.busy = False

    def timed_out(self):
        self.events.append(("stop", None))
        raise subprocess.TimeoutExpired("launchctl bootout", 1)

    monkeypatch.setattr(FirstContactGateway, "stop", timed_out)
    result = admission_cutover.execute_first_contact(
        store,
        actor,
        scope,
        planned["plan_id"],
        confirmed=True,
        control_factory=FirstContactGateway,
        drain_seconds=0,
        stop_seconds=0,
        route_seconds=0,
    )
    assert result["state"] == "gateway_stop_marker_requires_recovery"
    assert [event[0] for event in FirstContactGateway.events] == ["drain", "stop"]
    assert (
        admission_cutover.inspect_first_contact(store, actor, scope, planned["plan_id"])["phase"]
        == "stop_intent"
    )
    assert not any(
        request["operation"] == "diagnostic_admission_apply" for request, _kwargs in calls
    )


def test_first_contact_status_does_not_hide_another_unfinished_journal(case):
    store, project, employee, profile, python, prepared, _calls = case
    actor, scope = employee["id"], project["id"]
    planned = admission.admission(
        store, actor, scope, "preview", preparation_id=prepared["plan_id"]
    )
    installer._private(store.root / "wake", create=True)
    control = FirstContactGateway(python, profile)
    admission_journal.begin(
        store,
        "d" * 32,
        actor,
        scope,
        prepared["profile_id"],
        control,
        {"pid": 123, "start_time": 456},
    )
    assert admission_cutover.inspect_first_contact(store, actor, scope, planned["plan_id"])[
        "state"
    ] == ("admission_journal_requires_inspection")


def test_pm_reselection_recovers_only_a_started_admission_in_same_install(case, monkeypatch):
    store, project, employee, profile, _python, prepared, _calls = case
    actor, scope = employee["id"], project["id"]
    preview = admission.admission(
        store, actor, scope, "preview", preparation_id=prepared["plan_id"]
    )
    install = profile / "hermes-agent"
    key = hashlib.sha256(str(install).encode()).hexdigest()[:16]
    base = profile / "installs" / key / "environments"
    old = base / ("a" * 32) / "venv/bin/python"
    current = base / ("b" * 32) / "venv/bin/python"
    for folder, name in (
        ("hermes-discovery-plans", prepared["plan_id"]),
        ("hermes-admission-plans", preview["plan_id"]),
    ):
        path = store.directory / folder / (name + ".json")
        record = json.loads(path.read_text())
        record["python"] = str(old)
        path.write_text(json.dumps(record))
    monkeypatch.setattr(catalog, "resolve", lambda *_args: (current, profile))
    with pytest.raises(WorkbenchError, match="needs inspection"):
        admission.admission(store, actor, scope, "status", plan_id=preview["plan_id"])
    record = json.loads(
        (store.directory / "hermes-admission-plans" / (preview["plan_id"] + ".json")).read_text()
    )
    folder = profile / "mailbox-diagnostic-admissions" / record["native_plan_id"]
    folder.mkdir(mode=0o700, parents=True)
    (folder / "intent.json").write_text("{}")
    (folder / "intent.json").chmod(0o600)

    def child(runtime, selected, request, **_kwargs):
        assert runtime == current and selected == profile
        assert request["operation"] == "diagnostic_admission_status"
        return {
            "state": "diagnostic_enabled_restart_required",
            "plan_id": record["native_plan_id"],
            "requires_confirmation": True,
            "requires_stopped_gateway": True,
            "runtime_verified": False,
            "session_verified": False,
            "delivery_verified": False,
        }

    monkeypatch.setattr(admission.process, "run", child)
    assert (
        admission.admission(store, actor, scope, "status", plan_id=preview["plan_id"])["state"]
        == "diagnostic_enabled_restart_required"
    )
    assert (
        admission.admission(
            store,
            actor,
            scope,
            "verify",
            plan_id=preview["plan_id"],
            control_factory=RunningDiagnostic,
        )["runtime_verified"]
        is True
    )
    assert (
        admission.admission(store, actor, scope, "lookup", profile_id=prepared["profile_id"])[
            "plan_id"
        ]
        == preview["plan_id"]
    )
    monkeypatch.setattr(catalog, "resolve", lambda *_args: (Path("/other/bin/python"), profile))
    with pytest.raises(WorkbenchError, match="needs inspection"):
        admission.admission(store, actor, scope, "status", plan_id=preview["plan_id"])


def test_admission_rejects_foreign_and_browser_paths(case):
    store, project, employee, _profile, _python, prepared, calls = case
    with pytest.raises(WorkbenchError):
        admission.admission(
            store, employee["id"], project["id"], "preview", preparation_id="0" * 32
        )
    assert not calls
    preview = admission.admission(
        store, employee["id"], project["id"], "preview", preparation_id=prepared["plan_id"]
    )
    with pytest.raises(WorkbenchError):
        admission.admission(store, employee["id"], project["id"], "status", plan_id="0" * 32)
    with pytest.raises(WorkbenchError):
        admission.admission(
            store,
            employee["id"],
            project["id"],
            "status",
            preparation_id=prepared["plan_id"],
            plan_id=preview["plan_id"],
        )


def test_lookup_does_not_hide_enabled_plan_behind_newer_preview(case, monkeypatch):
    store, project, employee, _profile, _python, prepared, calls = case
    actor, scope = employee["id"], project["id"]
    assert (
        admission.admission(store, actor, scope, "lookup", profile_id=prepared["profile_id"])[
            "state"
        ]
        == "not_admitted_here"
    )
    original = admission.process.run
    created = 0

    def child(runtime, selected, request, **kwargs):
        nonlocal created
        if request["operation"] == "diagnostic_admission_preview":
            created += 1
            result = original(runtime, selected, request, **kwargs)
            return {**result, "plan_id": "plan_" + ("b" if created == 1 else "c") * 32}
        if request["operation"] == "diagnostic_admission_status":
            result = original(runtime, selected, request, **kwargs)
            return {
                **result,
                "state": (
                    "preview"
                    if request["plan_id"].endswith("c" * 32)
                    else "diagnostic_enabled_restart_required"
                ),
            }
        return original(runtime, selected, request, **kwargs)

    monkeypatch.setattr(admission.process, "run", child)
    first = admission.admission(store, actor, scope, "preview", preparation_id=prepared["plan_id"])
    admission.admission(store, actor, scope, "preview", preparation_id=prepared["plan_id"])
    selected = admission.admission(store, actor, scope, "lookup", profile_id=prepared["profile_id"])
    assert selected["plan_id"] == first["plan_id"]
    assert selected["state"] == "diagnostic_enabled_restart_required"
    assert selected["session_verified"] is False and selected["delivery_verified"] is False
    assert (
        sum(request["operation"] == "diagnostic_admission_preview" for request, _kwargs in calls)
        == 2
    )


def test_unknown_admission_blocks_new_preview_and_second_mutation(case, monkeypatch):
    store, project, employee, _profile, _python, prepared, calls = case
    actor, scope = employee["id"], project["id"]
    first = admission.admission(store, actor, scope, "preview", preparation_id=prepared["plan_id"])
    original = admission.process.run

    def unknown(runtime, selected, request, **kwargs):
        result = original(runtime, selected, request, **kwargs)
        if request["operation"] == "diagnostic_admission_status":
            return {**result, "state": "admission_unknown"}
        return result

    monkeypatch.setattr(admission.process, "run", unknown)
    assert (
        admission.admission(store, actor, scope, "lookup", profile_id=prepared["profile_id"])[
            "state"
        ]
        == "admission_unknown"
    )
    with pytest.raises(WorkbenchError):
        admission.admission(store, actor, scope, "preview", preparation_id=prepared["plan_id"])
    with pytest.raises(WorkbenchError):
        admission.admission(
            store,
            actor,
            scope,
            "apply",
            plan_id=first["plan_id"],
            confirmed=True,
            control_factory=Stopped,
        )
    assert not any(
        request["operation"] == "diagnostic_admission_apply" for request, _kwargs in calls
    )


def test_verify_requires_published_plan_and_current_runtime_observation(case, monkeypatch):
    store, project, employee, _profile, _python, prepared, calls = case
    actor, scope = employee["id"], project["id"]
    plan = admission.admission(store, actor, scope, "preview", preparation_id=prepared["plan_id"])
    child = admission.process.run

    def unpublished(runtime, profile, request, **kwargs):
        result = child(runtime, profile, request, **kwargs)
        return (
            {**result, "state": "preview"}
            if request["operation"] == "diagnostic_admission_status"
            else result
        )

    monkeypatch.setattr(admission.process, "run", unpublished)
    before = admission.admission(
        store, actor, scope, "verify", plan_id=plan["plan_id"], control_factory=RunningDiagnostic
    )
    assert before["state"] == "preview" and before["runtime_verified"] is False
    monkeypatch.setattr(admission.process, "run", child)
    admission.admission(
        store,
        actor,
        scope,
        "apply",
        plan_id=plan["plan_id"],
        confirmed=True,
        control_factory=Stopped,
    )
    calls.clear()
    try:
        RunningDiagnostic.available = False
        unavailable = admission.admission(
            store,
            actor,
            scope,
            "verify",
            plan_id=plan["plan_id"],
            control_factory=RunningDiagnostic,
        )
        assert unavailable["state"] == "diagnostic_enabled_runtime_unconfirmed"
        assert unavailable["runtime_verified"] is False
        RunningDiagnostic.available = True
        observed = admission.admission(
            store,
            actor,
            scope,
            "verify",
            plan_id=plan["plan_id"],
            control_factory=RunningDiagnostic,
        )
        assert observed["state"] == "diagnostic_runtime_observed"
        assert observed["runtime_verified"] is True
        assert observed["session_verified"] is False and observed["delivery_verified"] is False
        assert all(
            request["operation"] == "diagnostic_admission_status" for request, _kwargs in calls
        )
        assert len(calls) == 3  # unavailable once, verified before and after observation
        inspected = 0

        def changed(runtime, profile, request, **kwargs):
            nonlocal inspected
            result = child(runtime, profile, request, **kwargs)
            if request["operation"] == "diagnostic_admission_status":
                inspected += 1
                if inspected == 2:
                    return {**result, "state": "inspection_required"}
            return result

        monkeypatch.setattr(admission.process, "run", changed)
        stale = admission.admission(
            store,
            actor,
            scope,
            "verify",
            plan_id=plan["plan_id"],
            control_factory=RunningDiagnostic,
        )
        assert stale["state"] == "inspection_required"
        assert stale["runtime_verified"] is False
        with pytest.raises(WorkbenchError):
            admission.admission(
                store,
                actor,
                scope,
                "verify",
                plan_id=plan["plan_id"],
                confirmed=True,
                control_factory=RunningDiagnostic,
            )
    finally:
        RunningDiagnostic.available = True


def test_http_admission_requires_owner_and_rejects_path(case, monkeypatch):
    store, project, employee, _profile, _python, prepared, calls = case
    session = create_session(store, employee["id"], project["id"], "Synthetic")
    server = WorkbenchHTTP(store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = server.endpoint + "/api/workbench/mailbox/installers/hermes/admission"

    def post(token, data):
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(data).encode(),
            method="POST",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    data = {
        "employee_id": employee["id"],
        "project_id": project["id"],
        "action": "preview",
        "preparation_id": prepared["plan_id"],
    }
    try:
        for token in ("", session["token"]):
            with pytest.raises(urllib.error.HTTPError) as error:
                post(token, data)
            assert error.value.code == 401
        assert not calls
        with pytest.raises(urllib.error.HTTPError) as error:
            post(server.token, {**data, "path": "/tmp/foreign"})
        assert error.value.code == 400 and not calls
        planned = post(server.token, data)
        assert planned["state"] == "preview"
        assert len(calls) == 1
        inspected = post(
            server.token,
            {
                "employee_id": employee["id"],
                "project_id": project["id"],
                "action": "first_contact_status",
                "plan_id": planned["plan_id"],
            },
        )
        assert inspected["state"] == "first_contact_not_started"
        reconciled = post(
            server.token,
            {
                "employee_id": employee["id"],
                "project_id": project["id"],
                "action": "first_contact_reconcile",
                "plan_id": planned["plan_id"],
            },
        )
        assert reconciled["state"] == "first_contact_not_started"
        with pytest.raises(urllib.error.HTTPError) as error:
            post(
                server.token,
                {
                    "employee_id": employee["id"],
                    "project_id": project["id"],
                    "action": "first_contact_status",
                    "plan_id": planned["plan_id"],
                    "confirmed": True,
                },
            )
        assert error.value.code == 400
        from agent_mailbox import workbench_hermes_admission_cutover as first_contact

        dispatched = []
        for name in (
            "execute_first_contact",
            "restore_before_admission",
            "resume_committed_admission",
        ):

            def synthetic_action(_store, actor, scope, plan_id, *, confirmed, _name=name):
                dispatched.append((_name, actor, scope, plan_id, confirmed))
                return {"state": "synthetic_" + _name}

            monkeypatch.setattr(first_contact, name, synthetic_action)
        for action, name in (
            ("first_contact_execute", "execute_first_contact"),
            ("first_contact_restore_before_pm", "restore_before_admission"),
            ("first_contact_resume_committed", "resume_committed_admission"),
        ):
            payload = {
                "employee_id": employee["id"],
                "project_id": project["id"],
                "action": action,
                "plan_id": planned["plan_id"],
                "confirmed": True,
            }
            for token, invalid in (
                (session["token"], payload),
                (server.token, {**payload, "confirmed": False}),
                (server.token, {**payload, "path": "/tmp/foreign"}),
            ):
                with pytest.raises(urllib.error.HTTPError) as error:
                    post(token, invalid)
                assert error.value.code == (401 if token == session["token"] else 400)
            assert post(server.token, payload)["state"] == "synthetic_" + name
        assert dispatched == [
            (name, employee["id"], project["id"], planned["plan_id"], True)
            for name in (
                "execute_first_contact",
                "restore_before_admission",
                "resume_committed_admission",
            )
        ]
        # No service exists in this synthetic HOME. Even a child that claims
        # enabled must not let the HTTP endpoint claim runtime observation.
        result = post(
            server.token,
            {
                "employee_id": employee["id"],
                "project_id": project["id"],
                "action": "verify",
                "plan_id": planned["plan_id"],
            },
        )
        assert result["state"] == "diagnostic_enabled_runtime_unconfirmed"
        assert result["runtime_verified"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        server.instance_lock.close()
