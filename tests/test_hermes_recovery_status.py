"""A crashed cutover must be visible without clearing its safety fence."""

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_mailbox import workbench_hermes_journal as journal
from agent_mailbox import workbench_hermes_recovery as recovery
from agent_mailbox.workbench import WorkbenchHTTP
from agent_mailbox.workbench_reauthorization_holds import (
    cutoff_path,
    epoch_path,
    gate_path,
    required_path,
)
from agent_mailbox.workbench_store import WorkbenchStore


def test_recovery_inspection_distinguishes_cutover_fence_and_external_marker(monkeypatch, tmp_path):
    store = SimpleNamespace(root=tmp_path)
    profile = tmp_path / "profile"
    profile.mkdir()
    binding = {
        "id": "binding_" + "a" * 32,
        "employee_id": "employee",
        "project_id": "project",
        "host_kind": "hermes",
    }
    plan = "plan_" + "b" * 32
    monkeypatch.setattr(recovery.catalog, "resolve", lambda *_: (Path("/fake/python"), profile))
    monkeypatch.setattr(recovery, "_binding", lambda *_: binding)
    args = (store, "employee", "project", "hermes_profile_selected")
    assert recovery.inspect(*args)["state"] == "no_cutover_recovery"

    marker = profile / ".gateway-planned-stop.json"
    marker.write_text('{"target_pid":42}')
    assert recovery.inspect(*args)["state"] == "external_stop_marker_present"

    gate = gate_path(tmp_path, binding["id"])
    gate.parent.mkdir(parents=True, mode=0o700)
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
    assert recovery.inspect(*args)["state"] == "stop_marker_requires_recovery"
    marker.unlink()
    assert recovery.inspect(*args)["state"] == "fence_requires_review"
    control = SimpleNamespace(
        profile=profile,
        domain=f"gui/{os.getuid()}",
        plist_digest=bytes.fromhex("ab" * 32),
        command_digest=bytes.fromhex("cd" * 32),
    )
    journal.begin(
        store, binding, plan, "hermes_profile_selected", control, {"pid": 42, "start_time": 100}
    )
    assert recovery.inspect(*args)["state"] == "fence_requires_review"
    journal.advance(
        store,
        binding["id"],
        plan,
        "stop_intent",
        "pin_intent",
        employee_id="employee",
        project_id="project",
        profile_id="hermes_profile_selected",
        profile=profile,
    )
    assert recovery.inspect(*args)["state"] == "pin_outcome_requires_inspection"
    marker.write_text('{"target_pid":42}')
    uncertain = recovery.inspect(*args)
    assert uncertain["state"] == "stop_marker_requires_recovery"
    assert uncertain["pin_may_have_run"] is True
    marker.unlink()
    marker.symlink_to(tmp_path / "foreign")
    assert recovery.inspect(*args)["state"] == "check_unconfirmed"
    marker.unlink()
    gate.write_text("not-json")
    assert recovery.inspect(*args)["state"] == "check_unconfirmed"


def test_interrupted_journal_advance_keeps_original_intent_readable(tmp_path, monkeypatch):
    store = SimpleNamespace(root=tmp_path)
    profile = tmp_path / "profile"
    profile.mkdir()
    binding = {
        "id": "binding_" + "a" * 32,
        "employee_id": "employee",
        "project_id": "project",
        "host_kind": "hermes",
    }
    plan = "plan_" + "b" * 32
    scope = {
        "employee_id": "employee",
        "project_id": "project",
        "profile_id": "selected",
        "profile": profile,
    }
    control = SimpleNamespace(
        profile=profile,
        domain=f"gui/{os.getuid()}",
        plist_digest=bytes.fromhex("ab" * 32),
        command_digest=bytes.fromhex("cd" * 32),
    )
    claims = tmp_path / "wake" / "claims"
    claims.mkdir(parents=True, mode=0o700)
    journal.begin(store, binding, plan, "selected", control, {"pid": 42, "start_time": 100})
    actual_unlink = Path.unlink

    def interrupt_replace(source, _destination):
        assert source.is_file()
        raise OSError("synthetic journal interruption")

    def leave_partial(path, *args, **kwargs):
        if path.name.endswith(".tmp"):
            return None
        return actual_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(journal.os, "replace", interrupt_replace)
        patch.setattr(Path, "unlink", leave_partial)
        with pytest.raises(OSError, match="synthetic journal interruption"):
            journal.advance(store, binding["id"], plan, "stop_intent", "pin_intent", **scope)
    assert journal.read(store, binding["id"], plan, **scope)["phase"] == "stop_intent"
    assert list(claims.glob("*.tmp"))
    assert journal.phases_for_binding(store, binding["id"], **scope) == {plan: "stop_intent"}
    journal.advance(store, binding["id"], plan, "stop_intent", "pin_intent", **scope)
    assert journal.phases_for_binding(store, binding["id"], **scope) == {plan: "pin_intent"}
    assert not list(claims.glob("*.tmp"))
    suspicious = claims / f".{binding['id']}.{plan}.hermes-cutover.bad.tmp"
    suspicious.write_text("partial")
    suspicious.chmod(0o600)
    with pytest.raises(ValueError, match="hermes_cutover_journal_unconfirmed"):
        journal.phases_for_binding(store, binding["id"], **scope)


def test_legacy_journal_temp_from_older_candidate_does_not_wedge_retry(tmp_path):
    store = SimpleNamespace(root=tmp_path)
    profile = tmp_path / "profile"
    profile.mkdir()
    binding = {
        "id": "binding_" + "a" * 32,
        "employee_id": "employee",
        "project_id": "project",
        "host_kind": "hermes",
    }
    plan = "plan_" + "b" * 32
    scope = {
        "employee_id": "employee",
        "project_id": "project",
        "profile_id": "selected",
        "profile": profile,
    }
    control = SimpleNamespace(
        profile=profile,
        domain=f"gui/{os.getuid()}",
        plist_digest=bytes.fromhex("ab" * 32),
        command_digest=bytes.fromhex("cd" * 32),
    )
    (tmp_path / "wake" / "claims").mkdir(parents=True, mode=0o700)
    target = journal.path(tmp_path, binding["id"], plan)
    legacy = target.with_name(target.name + "." + "1" * 16 + ".tmp")
    legacy.write_text("partial")
    legacy.chmod(0o644)
    with pytest.raises(ValueError, match="hermes_cutover_journal_unconfirmed"):
        journal.phases_for_binding(store, binding["id"], **scope)
    legacy.chmod(0o600)
    with pytest.raises(ValueError, match="hermes_cutover_journal_unconfirmed"):
        journal.phases_for_binding(store, binding["id"], **scope)
    legacy.unlink()
    journal.begin(store, binding, plan, "selected", control, {"pid": 42, "start_time": 100})
    legacy.write_text("partial")
    legacy.chmod(0o600)
    assert journal.phases_for_binding(store, binding["id"], **scope) == {plan: "stop_intent"}
    journal.advance(store, binding["id"], plan, "stop_intent", "pin_intent", **scope)
    assert not legacy.exists()


def test_second_epoch_restart_intent_is_visible_with_old_cutoff_preserved(monkeypatch, tmp_path):
    store = SimpleNamespace(root=tmp_path)
    profile = tmp_path / "profile"
    profile.mkdir()
    binding = {
        "id": "binding_" + "a" * 32,
        "employee_id": "employee",
        "project_id": "project",
        "host_kind": "hermes",
    }
    first, second = "plan_" + "a" * 32, "plan_" + "b" * 32
    cutoff_time = "2026-10-14T00:00:00+00:00"
    monkeypatch.setattr(recovery.catalog, "resolve", lambda *_: (Path("/fake/python"), profile))
    monkeypatch.setattr(recovery, "_binding", lambda *_: binding)
    claims = tmp_path / "wake" / "claims"
    claims.mkdir(parents=True, mode=0o700)
    for path, payload in (
        (
            required_path(tmp_path, binding["id"]),
            {"version": 1, "binding_id": binding["id"], "plan_id": first},
        ),
        (
            cutoff_path(tmp_path, binding["id"]),
            {
                "version": 1,
                "binding_id": binding["id"],
                "plan_id": first,
                "authorized_after": cutoff_time,
            },
        ),
        (
            epoch_path(tmp_path, binding["id"], second),
            {
                "version": 1,
                "binding_id": binding["id"],
                "plan_id": second,
                "phase": "pending",
                "previous_cutoff": cutoff_time,
                "authorized_after": None,
            },
        ),
        (
            gate_path(tmp_path, binding["id"]),
            {
                "version": 1,
                "binding_id": binding["id"],
                "plan_id": second,
                "employee_id": "employee",
                "project_id": "project",
            },
        ),
    ):
        path.write_text(json.dumps(payload))
        path.chmod(0o600)
    control = SimpleNamespace(
        profile=profile,
        domain=f"gui/{os.getuid()}",
        plist_digest=bytes.fromhex("ab" * 32),
        command_digest=bytes.fromhex("cd" * 32),
    )
    scope = {
        "employee_id": "employee",
        "project_id": "project",
        "profile_id": "selected",
        "profile": profile,
    }
    journal.begin(store, binding, first, "selected", control, {"pid": 40, "start_time": 80})
    journal.advance(store, binding["id"], first, "stop_intent", "pin_intent", **scope)
    journal.advance(store, binding["id"], first, "pin_intent", "complete", **scope)
    journal.begin(store, binding, second, "selected", control, {"pid": 42, "start_time": 100})
    journal.advance(store, binding["id"], second, "stop_intent", "restart_intent", **scope)
    assert recovery.inspect(store, "employee", "project", "selected")["state"] == (
        "gateway_restart_outcome_requires_inspection"
    )
    monkeypatch.setattr(recovery.catalog, "_member", lambda *_: None)
    monkeypatch.setattr(recovery, "endpoint_for", lambda *_: tmp_path / "receiver.sock")
    monkeypatch.setattr(recovery, "_pin_preview", lambda *_: True)

    class RestoredControl:
        def __init__(self, _python, selected, *, require_stopped=False):
            assert selected == profile and require_stopped is False
            self.domain = control.domain
            self.plist_digest = control.plist_digest
            self.command_digest = control.command_digest

        def restored_original_route(self, *_args):
            return True

    result = recovery.restore_pre_pin(
        store,
        "employee",
        "project",
        second,
        "selected",
        confirmed=True,
        control_factory=RestoredControl,
    )
    assert result["state"] == "old_gateway_restored_mail_fenced"
    assert journal.read(store, binding["id"], second, **scope)["phase"] == "restart_verified"
    assert gate_path(tmp_path, binding["id"]).exists()


def test_journal_rejects_retry_and_tamper_without_marking_pin_success(tmp_path):
    store = SimpleNamespace(root=tmp_path)
    (tmp_path / "wake").mkdir(mode=0o700)
    binding = {
        "id": "binding_" + "c" * 32,
        "employee_id": "employee",
        "project_id": "project",
        "host_kind": "hermes",
    }
    plan = "plan_" + "d" * 32
    profile = tmp_path / "profile"
    profile.mkdir()
    control = SimpleNamespace(
        profile=profile,
        domain=f"gui/{os.getuid()}",
        plist_digest=bytes.fromhex("ab" * 32),
        command_digest=bytes.fromhex("cd" * 32),
    )
    runtime = {"pid": 42, "start_time": 100}
    value = journal.begin(store, binding, plan, "selected", control, runtime)
    assert value["phase"] == "stop_intent"
    with pytest.raises(ValueError, match="journal_unconfirmed"):
        journal.read(
            store,
            binding["id"],
            plan,
            employee_id="employee",
            project_id="project",
            profile_id="wrong",
            profile=profile,
        )
    with pytest.raises(ValueError, match="existing_hermes_cutover_journal"):
        journal.begin(store, binding, plan, "selected", control, runtime)
    target = journal.path(tmp_path, binding["id"], plan)
    edited = {**value, "pin_may_have_run": True}
    target.write_text(json.dumps(edited))
    with pytest.raises(ValueError, match="journal_unconfirmed"):
        journal.read(
            store,
            binding["id"],
            plan,
            employee_id="employee",
            project_id="project",
            profile_id="selected",
            profile=profile,
        )
    with pytest.raises(ValueError, match="journal_unconfirmed"):
        journal.advance(
            store,
            binding["id"],
            plan,
            "stop_intent",
            "pin_intent",
            employee_id="employee",
            project_id="project",
            profile_id="selected",
            profile=profile,
        )


def test_released_gate_still_exposes_incomplete_journal(monkeypatch, tmp_path):
    store = SimpleNamespace(root=tmp_path)
    (tmp_path / "wake").mkdir(mode=0o700)
    profile = tmp_path / "profile"
    profile.mkdir()
    binding = {
        "id": "binding_" + "e" * 32,
        "employee_id": "employee",
        "project_id": "project",
        "host_kind": "hermes",
    }
    plan = "plan_" + "f" * 32
    monkeypatch.setattr(recovery.catalog, "resolve", lambda *_: (Path("/fake/python"), profile))
    monkeypatch.setattr(recovery, "_binding", lambda *_: binding)
    control = SimpleNamespace(
        profile=profile,
        domain=f"gui/{os.getuid()}",
        plist_digest=bytes.fromhex("ab" * 32),
        command_digest=bytes.fromhex("cd" * 32),
    )
    journal.begin(store, binding, plan, "selected", control, {"pid": 42, "start_time": 100})
    args = (store, "employee", "project", "selected")
    assert recovery.inspect(*args)["state"] == "check_unconfirmed"
    required = required_path(tmp_path, binding["id"])
    required.write_text(json.dumps({"version": 1, "plan_id": plan, "binding_id": binding["id"]}))
    required.chmod(0o600)
    assert recovery.inspect(*args)["state"] == "check_unconfirmed"
    cutoff = cutoff_path(tmp_path, binding["id"])
    cutoff.write_text(
        json.dumps(
            {
                "version": 1,
                "plan_id": plan,
                "binding_id": binding["id"],
                "authorized_after": "2026-10-14T00:00:00+00:00",
            }
        )
    )
    cutoff.chmod(0o600)
    assert recovery.inspect(*args)["state"] == "route_released_journal_requires_review"
    journal.advance(
        store,
        binding["id"],
        plan,
        "stop_intent",
        "pin_intent",
        employee_id="employee",
        project_id="project",
        profile_id="selected",
        profile=profile,
    )
    journal.advance(
        store,
        binding["id"],
        plan,
        "pin_intent",
        "complete",
        employee_id="employee",
        project_id="project",
        profile_id="selected",
        profile=profile,
    )
    assert recovery.inspect(*args)["state"] == "authorization_cutoff_present"
    second = "plan_" + "e" * 32
    epoch = epoch_path(tmp_path, binding["id"], second)
    epoch.write_text(
        json.dumps(
            {
                "version": 1,
                "binding_id": binding["id"],
                "plan_id": second,
                "phase": "complete",
                "previous_cutoff": "2026-10-14T00:00:00+00:00",
                "authorized_after": "2026-10-14T00:05:00+00:00",
            }
        )
    )
    epoch.chmod(0o600)
    journal.begin(store, binding, second, "selected", control, {"pid": 43, "start_time": 101})
    journal.advance(
        store,
        binding["id"],
        second,
        "stop_intent",
        "pin_intent",
        employee_id="employee",
        project_id="project",
        profile_id="selected",
        profile=profile,
    )
    journal.advance(
        store,
        binding["id"],
        second,
        "pin_intent",
        "complete",
        employee_id="employee",
        project_id="project",
        profile_id="selected",
        profile=profile,
    )
    assert recovery.inspect(*args)["state"] == "authorization_cutoff_present"
    required.unlink()
    cutoff.unlink()
    assert recovery.inspect(*args)["state"] == "check_unconfirmed"


def test_recovery_status_http_is_owner_only_and_requires_exact_scope(monkeypatch, tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Synthetic", tmp_path)
    employee = store.create_employee("Hermes", "hermes", project["id"])
    server = WorkbenchHTTP(store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    calls = []

    def inspected(*args):
        calls.append(args)
        return {"state": "fence_requires_review", "delivery_verified": False}

    monkeypatch.setattr(recovery, "inspect", inspected)

    def get(query, token):
        url = (
            server.endpoint + "/api/workbench/mailbox/receivers/hermes/"
            "reauthorization/recovery?" + urllib.parse.urlencode(query)
        )
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
        return urllib.request.urlopen(request, timeout=5)

    def post(payload, token):
        request = urllib.request.Request(
            server.endpoint
            + "/api/workbench/mailbox/receivers/hermes/reauthorization/restore-pre-pin",
            data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=5)

    query = {
        "employee_id": employee["id"],
        "project_id": project["id"],
        "profile_id": "hermes_profile_synthetic",
    }
    try:
        with get(query, server.token) as response:
            assert json.load(response)["state"] == "fence_requires_review"
        assert calls == [(store, employee["id"], project["id"], "hermes_profile_synthetic")]
        with pytest.raises(urllib.error.HTTPError) as denied:
            get(query, "employee-capability")
        assert denied.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as invalid:
            get({"employee_id": employee["id"], "project_id": project["id"]}, server.token)
        assert invalid.value.code == 400
        assert len(calls) == 1
        restores = []
        monkeypatch.setattr(
            recovery,
            "restore_pre_pin",
            lambda *args, **kwargs: (
                restores.append((args, kwargs))
                or {"state": "old_gateway_restored_mail_fenced", "delivery_verified": False}
            ),
        )
        payload = {**query, "plan_id": "plan_" + "a" * 32, "confirmed": True}
        with pytest.raises(urllib.error.HTTPError) as denied_post:
            post(payload, "employee-capability")
        assert denied_post.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as missing_confirmation:
            post({**payload, "confirmed": False}, server.token)
        assert missing_confirmation.value.code == 400
        with post(payload, server.token) as response:
            assert json.load(response)["state"] == "old_gateway_restored_mail_fenced"
        assert restores == [
            (
                (
                    store,
                    employee["id"],
                    project["id"],
                    payload["plan_id"],
                    "hermes_profile_synthetic",
                ),
                {"confirmed": True},
            )
        ]
        retries = []
        monkeypatch.setattr(
            recovery,
            "retry_restore_pre_pin",
            lambda *args, **kwargs: (
                retries.append((args, kwargs))
                or {"state": "old_gateway_restored_mail_fenced", "delivery_verified": False}
            ),
        )
        retry_url = (
            server.endpoint
            + "/api/workbench/mailbox/receivers/hermes/reauthorization/retry-restore-pre-pin"
        )

        def post_retry(body, token):
            request = urllib.request.Request(
                retry_url,
                data=json.dumps(body).encode(),
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                method="POST",
            )
            return urllib.request.urlopen(request, timeout=5)

        with pytest.raises(urllib.error.HTTPError) as denied_retry:
            post_retry(payload, "employee-capability")
        assert denied_retry.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as unconfirmed_retry:
            post_retry({**payload, "confirmed": False}, server.token)
        assert unconfirmed_retry.value.code == 400
        with post_retry(payload, server.token) as response:
            assert json.load(response)["state"] == "old_gateway_restored_mail_fenced"
        assert retries == [
            (
                (
                    store,
                    employee["id"],
                    project["id"],
                    payload["plan_id"],
                    "hermes_profile_synthetic",
                ),
                {"confirmed": True},
            )
        ]
    finally:
        server.shutdown()
        thread.join(5)
        server.close()


@pytest.mark.parametrize(
    ("condition", "expected_state", "expected_phase", "expected_events"),
    [
        (
            "healthy",
            "old_gateway_restored_mail_fenced",
            "restart_verified",
            ["pin_status", "clear", "start", "verify"],
        ),
        ("pin_changed", "gateway_restore_scope_changed", "stop_intent", ["pin_status"]),
        ("marker_present", "gateway_stop_unconfirmed", "stop_intent", []),
        ("socket_present", "gateway_stop_unconfirmed", "stop_intent", []),
        ("live_lock", "gateway_stop_unconfirmed", "stop_intent", []),
        (
            "start_failed",
            "gateway_restart_outcome_requires_inspection",
            "restart_intent",
            ["pin_status", "clear", "start"],
        ),
        (
            "route_missing",
            "gateway_restart_outcome_requires_inspection",
            "restart_intent",
            ["pin_status", "clear", "start", "verify"],
        ),
    ],
)
def test_restore_pre_pin_starts_original_once_but_keeps_old_mail_fenced(
    monkeypatch, tmp_path, condition, expected_state, expected_phase, expected_events
):
    store = SimpleNamespace(root=tmp_path)
    (tmp_path / "wake").mkdir(mode=0o700)
    profile = tmp_path / "profile"
    profile.mkdir()
    binding = {
        "id": "binding_" + "1" * 32,
        "employee_id": "employee",
        "project_id": "project",
        "host_kind": "hermes",
    }
    plan = "plan_" + "2" * 32
    domain = f"gui/{os.getuid()}"
    expected = {"version": 1, "plan_id": plan, "binding_id": binding["id"]}
    gate = gate_path(tmp_path, binding["id"])
    gate.parent.mkdir(mode=0o700, parents=True)
    gate.write_text(json.dumps({**expected, "employee_id": "employee", "project_id": "project"}))
    gate.chmod(0o600)
    required = required_path(tmp_path, binding["id"])
    required.write_text(json.dumps(expected))
    required.chmod(0o600)
    first = SimpleNamespace(
        profile=profile,
        domain=domain,
        plist_digest=bytes.fromhex("ab" * 32),
        command_digest=bytes.fromhex("cd" * 32),
    )
    journal.begin(store, binding, plan, "selected", first, {"pid": 42, "start_time": 100})
    source = tmp_path / "source"
    source.mkdir()
    (source / "bridge.py").write_text("synthetic")
    events = []

    class StoppedControl:
        def __init__(self, _python, selected, *, require_stopped=False):
            assert require_stopped is True and selected == profile
            self.plist_digest, self.command_digest = first.plist_digest, first.command_digest

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
            return True

        def receiver_socket_absent(self, _endpoint):
            return condition != "socket_present"

        def planned_stop_marker_absent(self):
            return condition != "marker_present"

        def strict_gateway_absent(self):
            return condition != "live_lock"

        def _drain_marker(self, _plan):
            return profile / ".drain_request.json", {"principal": f"agent-mailbox-cutover:{plan}"}

        def clear_drain(self, _plan):
            events.append("clear")

        def start_stopped_original(self, selected, **hashes):
            assert selected == domain
            assert hashes == {"plist_sha256": "ab" * 32, "command_sha256": "cd" * 32}
            events.append("start")
            if condition == "start_failed":
                raise ValueError("synthetic launchctl uncertainty")

        def restored_original_route(self, *_args):
            events.append("verify")
            return condition != "route_missing"

    monkeypatch.setattr(recovery.catalog, "_member", lambda *_: None)
    monkeypatch.setattr(recovery.catalog, "resolve", lambda *_: (Path("/fake/python"), profile))
    monkeypatch.setattr(recovery, "_binding", lambda *_: binding)
    monkeypatch.setattr(recovery, "endpoint_for", lambda *_: tmp_path / "receiver.sock")
    monkeypatch.setattr(recovery.process, "payload", lambda: source)
    monkeypatch.setattr(recovery.process, "FILES", ("bridge.py",))
    monkeypatch.setattr(
        recovery.process,
        "run",
        lambda *_args, **_kwargs: (
            events.append("pin_status")
            or {"state": "inspection_required" if condition == "pin_changed" else "preview"}
        ),
    )
    args = (store, "employee", "project", plan, "selected")
    result = recovery.restore_pre_pin(
        *args, confirmed=True, control_factory=StoppedControl, route_seconds=0
    )
    assert result == {"state": expected_state, "delivery_verified": False}
    assert events == expected_events
    assert gate.exists() and required.exists() and not cutoff_path(tmp_path, binding["id"]).exists()
    record = journal.read(
        store,
        binding["id"],
        plan,
        employee_id="employee",
        project_id="project",
        profile_id="selected",
        profile=profile,
    )
    assert record["phase"] == expected_phase and record["pin_may_have_run"] is False
    if expected_phase == "restart_verified":
        assert recovery.restore_pre_pin(*args, confirmed=True, control_factory=StoppedControl)[
            "state"
        ] == ("old_gateway_restored_recorded_mail_fenced")
        assert events == expected_events


def test_restore_pre_pin_crash_after_intent_never_restarts_twice(monkeypatch, tmp_path):
    # The full fixture above proves the happy path. This one pins the journal
    # transition itself: a restart-intent cannot be replayed as a pin-intent.
    store = SimpleNamespace(root=tmp_path)
    (tmp_path / "wake").mkdir(mode=0o700)
    binding = {
        "id": "binding_" + "3" * 32,
        "employee_id": "employee",
        "project_id": "project",
        "host_kind": "hermes",
    }
    profile = tmp_path / "profile"
    profile.mkdir()
    plan = "plan_" + "4" * 32
    control = SimpleNamespace(
        profile=profile,
        domain=f"gui/{os.getuid()}",
        plist_digest=bytes.fromhex("ab" * 32),
        command_digest=bytes.fromhex("cd" * 32),
    )
    journal.begin(store, binding, plan, "selected", control, {"pid": 42, "start_time": 100})
    journal.advance(
        store,
        binding["id"],
        plan,
        "stop_intent",
        "restart_intent",
        employee_id="employee",
        project_id="project",
        profile_id="selected",
        profile=profile,
    )
    with pytest.raises(ValueError, match="transition_unconfirmed"):
        journal.advance(
            store,
            binding["id"],
            plan,
            "restart_intent",
            "pin_intent",
            employee_id="employee",
            project_id="project",
            profile_id="selected",
            profile=profile,
        )
    value = journal.read(
        store,
        binding["id"],
        plan,
        employee_id="employee",
        project_id="project",
        profile_id="selected",
        profile=profile,
    )
    assert value["phase"] == "restart_intent" and value["pin_may_have_run"] is False


@pytest.mark.parametrize("pin_state", ["preview", "inspection_required"])
@pytest.mark.parametrize("retry", [False, True])
def test_restart_intent_reconciles_live_route_without_second_start(
    monkeypatch, tmp_path, pin_state, retry
):
    store = SimpleNamespace(root=tmp_path)
    (tmp_path / "wake").mkdir(mode=0o700)
    profile = tmp_path / "profile"
    profile.mkdir()
    binding = {
        "id": "binding_" + "5" * 32,
        "employee_id": "employee",
        "project_id": "project",
        "host_kind": "hermes",
    }
    plan = "plan_" + "6" * 32
    domain = f"gui/{os.getuid()}"
    control = SimpleNamespace(
        profile=profile,
        domain=domain,
        plist_digest=bytes.fromhex("ab" * 32),
        command_digest=bytes.fromhex("cd" * 32),
    )
    journal.begin(store, binding, plan, "selected", control, {"pid": 42, "start_time": 100})
    journal.advance(
        store,
        binding["id"],
        plan,
        "stop_intent",
        "restart_intent",
        employee_id="employee",
        project_id="project",
        profile_id="selected",
        profile=profile,
    )
    if retry:
        journal.advance(
            store,
            binding["id"],
            plan,
            "restart_intent",
            "restart_retry_intent",
            employee_id="employee",
            project_id="project",
            profile_id="selected",
            profile=profile,
        )
    gate = gate_path(tmp_path, binding["id"])
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
    source = tmp_path / "source"
    source.mkdir()
    (source / "bridge.py").write_text("synthetic")

    class LiveControl:
        def __init__(self, _python, selected, *, require_stopped=False):
            assert selected == profile and require_stopped is False
            self.domain = domain
            self.plist_digest = control.plist_digest
            self.command_digest = control.command_digest

        def restored_original_route(self, *_args):
            return True

        def start_stopped_original(self, *_args, **_kwargs):
            pytest.fail("a restart intent must never replay bootstrap")

    monkeypatch.setattr(recovery.catalog, "_member", lambda *_: None)
    monkeypatch.setattr(recovery.catalog, "resolve", lambda *_: (Path("/fake/python"), profile))
    monkeypatch.setattr(recovery, "_binding", lambda *_: binding)
    monkeypatch.setattr(recovery, "endpoint_for", lambda *_: tmp_path / "receiver.sock")
    monkeypatch.setattr(recovery.process, "payload", lambda: source)
    monkeypatch.setattr(recovery.process, "FILES", ("bridge.py",))
    monkeypatch.setattr(recovery.process, "run", lambda *_args, **_kwargs: {"state": pin_state})
    result = recovery.restore_pre_pin(
        store, "employee", "project", plan, "selected", confirmed=True, control_factory=LiveControl
    )
    expected = (
        "old_gateway_restored_mail_fenced"
        if pin_state == "preview"
        else "gateway_restart_retry_outcome_requires_inspection"
        if retry
        else "gateway_restart_outcome_requires_inspection"
    )
    assert result["state"] == expected
    phase = journal.read(
        store,
        binding["id"],
        plan,
        employee_id="employee",
        project_id="project",
        profile_id="selected",
        profile=profile,
    )["phase"]
    assert phase == (
        ("restart_retry_verified" if retry else "restart_verified")
        if pin_state == "preview"
        else ("restart_retry_intent" if retry else "restart_intent")
    )
