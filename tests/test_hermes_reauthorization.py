"""Owner preview binds a private plan to one live Gateway instance and route."""

import time
from pathlib import Path
from types import SimpleNamespace

from agent_mailbox import workbench_hermes_reauthorization as reauth
from agent_mailbox import workbench_host_transport as transport
from agent_mailbox import workbench_reauthorization_holds as holds
from agent_mailbox.workbench_host_bindings import resolve_routes, set_binding
from agent_mailbox.workbench_mail_tasks import create_mail_task
from agent_mailbox.workbench_reauthorization_holds import arm_binding_gate, gated, held
from agent_mailbox.workbench_store import WorkbenchStore


def test_prepare_uses_only_verified_profile_and_redacts_session(tmp_path, monkeypatch):
    store = SimpleNamespace(root=tmp_path)
    binding = {
        "id": "binding_test",
        "employee_id": "employee_test",
        "project_id": "project_test",
        "host_kind": "hermes",
        "host_session_id": "agent:main:feishu:dm:synthetic",
    }
    candidate = {
        "state": "candidate_ready",
        "binding_id": binding["id"],
        "candidate_fingerprint": "a" * 64,
        "platform": "feishu",
        "pending_count": 3,
        "unknown_count": 1,
    }
    routing = {
        "protocol": "agent-mailbox.hermes-routing.v1",
        "host_kind": "hermes",
        "instance_id": "instance",
        "pid": 42,
        "profile_home": str(tmp_path / "profile"),
        "routing_home": str(tmp_path / "profile"),
        "scope": str(tmp_path / "profile"),
        "gateway_profile": "default",
        "multiplex": False,
        "observed_at": 1,
    }
    monkeypatch.setattr(reauth, "hermes_reauthorization_preview", lambda *_: candidate)
    monkeypatch.setattr(reauth, "_binding", lambda *_: binding)
    monkeypatch.setattr(transport, "endpoint_for", lambda *_: "/private/tmp/synthetic.sock")
    observations = []

    def request(_endpoint, payload):
        observations.append(payload)
        if payload["operation"] == "reauthorization_resolve":
            return {
                "protocol": "agent-mailbox.host-reauthorization.v1",
                "host_kind": "hermes",
                "binding_id": binding["id"],
                "candidate_fingerprint": candidate["candidate_fingerprint"],
                "candidate_session_id": "private-session",
                "platform": "feishu",
                "route_available": False,
                "session_verified": False,
                "delivery_verified": False,
            }
        return {**routing, "observed_at": len(observations)}

    monkeypatch.setattr(reauth, "_request", request)
    monkeypatch.setattr(
        reauth.catalog,
        "_discover",
        lambda: (
            tmp_path,
            Path("/bin/python3"),
            {"profiles": [{"profile_id": "default", "home": str(tmp_path / "profile")}]},
        ),
    )
    monkeypatch.setattr(reauth.process, "payload", lambda: tmp_path)
    monkeypatch.setattr(reauth.process, "FILES", ())
    calls = []

    def run(_python, _profile, payload, **_kwargs):
        calls.append(payload)
        return {"plan_id": "plan_" + "0" * 32}

    monkeypatch.setattr(reauth.process, "run", run)
    result = reauth.prepare(store, "employee_test", "project_test")
    assert result == {
        "state": "confirmation_required",
        "plan_id": "plan_" + "0" * 32,
        "profile_id": "default",
        "pending_count": 3,
        "unknown_count": 1,
        "requires_stopped_gateway": True,
        "session_verified": False,
        "delivery_verified": False,
    }
    assert [item["operation"] for item in observations] == [
        "routing_context",
        "reauthorization_resolve",
        "routing_context",
    ]
    assert observations[1]["expected_fingerprint"] == candidate["candidate_fingerprint"]
    assert "candidate_fingerprint" not in observations[1]
    assert calls[0]["options"] == {
        "binding_id": binding["id"],
        "session_key": binding["host_session_id"],
        "candidate_fingerprint": candidate["candidate_fingerprint"],
        "candidate_session_id": "private-session",
    }
    assert "candidate_fingerprint" not in result and "session_id" not in result


def test_refresh_only_exact_unstarted_fenced_plan(tmp_path, monkeypatch):
    from agent_mailbox import workbench_hermes_journal as journal

    store = SimpleNamespace(root=tmp_path)
    (tmp_path / "wake" / "claims").mkdir(parents=True, mode=0o700)
    binding = {
        "id": "binding_" + "a" * 32,
        "employee_id": "employee_test",
        "project_id": "project_test",
        "host_kind": "hermes",
    }
    plan = "plan_" + "b" * 32
    monkeypatch.setattr(reauth.catalog, "_member", lambda *_: None)
    monkeypatch.setattr(reauth, "_binding", lambda *_: binding)
    monkeypatch.setattr(holds, "active_fence_for_plan", lambda *_: True)
    monkeypatch.setattr(holds, "_gate_matches", lambda *_: True)
    monkeypatch.setattr(journal, "path", lambda *_: tmp_path / "absent-journal")
    monkeypatch.setattr(
        reauth.catalog,
        "_discover",
        lambda: (
            tmp_path,
            Path("/bin/python3"),
            {"profiles": [{"profile_id": "default", "home": str(tmp_path)}]},
        ),
    )
    monkeypatch.setattr(
        reauth,
        "hermes_reauthorization_preview",
        lambda *_: {
            "state": "candidate_ready",
            "binding_id": binding["id"],
            "candidate_fingerprint": "a" * 64,
        },
    )
    monkeypatch.setattr(reauth.process, "payload", lambda: tmp_path)
    monkeypatch.setattr(reauth.process, "FILES", ())
    calls = []
    monkeypatch.setattr(
        reauth.process,
        "run",
        lambda *a, **k: calls.append(a[2]) or {"state": "preview", "expires_at": 12345},
    )
    result = reauth.refresh_exact_fenced_plan(
        store, "employee_test", "project_test", plan, "default", confirmed=True
    )
    assert result == {
        "state": "plan_refreshed",
        "plan_id": plan,
        "expires_at": 12345,
        "old_mail_released": False,
        "delivery_verified": False,
    }
    assert calls[0]["operation"] == "pin_refresh"
    assert calls[0]["expected_candidate_fingerprint"] == "a" * 64
    monkeypatch.setattr(holds, "_gate_matches", lambda *_: False)
    assert (
        reauth.refresh_exact_fenced_plan(
            store, "employee_test", "project_test", plan, "default", confirmed=True
        )["state"]
        == "fence_requires_recovery"
    )
    assert len(calls) == 1


def test_gateway_instance_change_after_plan_is_not_confirmed(tmp_path, monkeypatch):
    store = SimpleNamespace(root=tmp_path)
    candidate = {
        "state": "candidate_ready",
        "binding_id": "binding_test",
        "candidate_fingerprint": "a" * 64,
        "platform": "feishu",
        "pending_count": 0,
        "unknown_count": 0,
    }
    monkeypatch.setattr(reauth, "hermes_reauthorization_preview", lambda *_: candidate)
    monkeypatch.setattr(
        reauth, "_binding", lambda *_: {"id": "binding_test", "host_session_id": "key"}
    )
    monkeypatch.setattr(transport, "endpoint_for", lambda *_: "/private/tmp/synthetic.sock")
    count = [0]

    def request(_endpoint, _payload):
        if _payload["operation"] == "reauthorization_resolve":
            return {
                "protocol": "agent-mailbox.host-reauthorization.v1",
                "host_kind": "hermes",
                "binding_id": "binding_test",
                "candidate_fingerprint": "a" * 64,
                "candidate_session_id": "private-session",
                "platform": "feishu",
                "route_available": False,
                "session_verified": False,
                "delivery_verified": False,
            }
        count[0] += 1
        return {
            "protocol": "agent-mailbox.hermes-routing.v1",
            "host_kind": "hermes",
            "instance_id": f"instance-{count[0]}",
            "profile_home": str(tmp_path),
        }

    monkeypatch.setattr(reauth, "_request", request)
    monkeypatch.setattr(
        reauth.catalog,
        "_discover",
        lambda: (
            tmp_path,
            Path("/bin/python3"),
            {"profiles": [{"profile_id": "default", "home": str(tmp_path)}]},
        ),
    )
    monkeypatch.setattr(reauth.process, "payload", lambda: tmp_path)
    monkeypatch.setattr(reauth.process, "FILES", ())
    monkeypatch.setattr(reauth.process, "run", lambda *_a, **_k: {"plan_id": "plan_" + "0" * 32})
    assert reauth.prepare(store, "employee_test", "project_test") == {
        "state": "configuration_changed",
        "candidate_available": False,
    }


def test_release_requires_exact_applied_plan_and_live_route(tmp_path, monkeypatch):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Synthetic", tmp_path)
    employee = store.create_employee("Hermes", "hermes", project["id"])
    binding = set_binding(
        store,
        employee["id"],
        project["id"],
        host_kind="hermes",
        host_session_id="agent:main:feishu:dm:synthetic",
    )
    old = create_mail_task(store, project["id"], "Old", "Synthetic", employee["id"])
    plan = "plan_" + "a" * 32
    arm_binding_gate(store, binding, plan)
    profile = tmp_path / "profile"
    routing = {
        "protocol": "agent-mailbox.hermes-routing.v1",
        "host_kind": "hermes",
        "instance_id": "synthetic-instance",
        "profile_home": str(profile),
    }
    monkeypatch.setattr(
        reauth.catalog,
        "_discover",
        lambda: (
            tmp_path,
            Path("/bin/python3"),
            {"profiles": [{"profile_id": "default", "home": str(profile)}]},
        ),
    )
    monkeypatch.setattr(reauth, "_request", lambda _endpoint, _request: dict(routing))
    monkeypatch.setattr(reauth, "reception_status", lambda *_: {"state": "component_available"})
    monkeypatch.setattr(reauth.process, "payload", lambda: tmp_path)
    monkeypatch.setattr(reauth.process, "FILES", ())
    monkeypatch.setattr(transport, "endpoint_for", lambda *_: "/private/tmp/synthetic.sock")
    observed = []
    state = ["preview"]

    def status(_python, _profile, request, **_kwargs):
        observed.append(request)
        return {"plan_id": plan, "state": state[0]}

    monkeypatch.setattr(reauth.process, "run", status)

    assert (
        reauth.release_new_route(store, employee["id"], project["id"], plan, "default")["state"]
        == "confirmation_required"
    )
    assert gated(store.root, binding["id"])

    def release(profile_id="default"):
        return reauth.release_new_route(
            store, employee["id"], project["id"], plan, profile_id, confirmed=True
        )

    assert release()["state"] == "check_unconfirmed"
    assert gated(store.root, binding["id"])
    state[0] = "configured_runtime_unverified"
    assert release("wrong")["state"] == "profile_unconfirmed"
    assert gated(store.root, binding["id"])
    monkeypatch.setattr(
        reauth, "reception_status", lambda *_: {"state": "session_reconnect_required"}
    )
    assert release()["state"] == "check_unconfirmed"
    assert gated(store.root, binding["id"])
    monkeypatch.setattr(reauth, "reception_status", lambda *_: {"state": "component_available"})
    checks = [0]

    def changed_instance(_endpoint, _request):
        checks[0] += 1
        return {**routing, "instance_id": f"instance-{checks[0]}"}

    monkeypatch.setattr(reauth, "_request", changed_instance)
    assert release()["state"] == "check_unconfirmed"
    assert gated(store.root, binding["id"])
    monkeypatch.setattr(reauth, "_request", lambda _endpoint, _request: dict(routing))
    assert release()["state"] == "new_route_released"
    assert not gated(store.root, binding["id"])
    assert observed and all(call["expected_binding_id"] == binding["id"] for call in observed)
    assert resolve_routes(store, employee["id"], [old["request_message_id"]])[0] == []


def test_owner_release_retries_second_epoch_after_gate_unlink_crash(tmp_path, monkeypatch):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Synthetic", tmp_path)
    employee = store.create_employee("Hermes", "hermes", project["id"])
    binding = set_binding(
        store,
        employee["id"],
        project["id"],
        host_kind="hermes",
        host_session_id="agent:main:feishu:dm:synthetic",
    )
    first, second = "plan_" + "a" * 32, "plan_" + "b" * 32
    arm_binding_gate(store, binding, first)
    holds.release_binding_gate(store, binding, first, route_ready=lambda: True)
    arm_binding_gate(store, binding, second)
    profile = tmp_path / "profile"
    routing = {
        "protocol": "agent-mailbox.hermes-routing.v1",
        "host_kind": "hermes",
        "instance_id": "same-instance",
        "profile_home": str(profile),
    }
    monkeypatch.setattr(
        reauth.catalog,
        "_discover",
        lambda: (
            tmp_path,
            Path("/bin/python3"),
            {"profiles": [{"profile_id": "default", "home": str(profile)}]},
        ),
    )
    monkeypatch.setattr(reauth, "_request", lambda _endpoint, _request: dict(routing))
    monkeypatch.setattr(reauth, "reception_status", lambda *_: {"state": "component_available"})
    monkeypatch.setattr(reauth.process, "payload", lambda: tmp_path)
    monkeypatch.setattr(reauth.process, "FILES", ())
    monkeypatch.setattr(
        reauth.process,
        "run",
        lambda *_a, **_k: {"plan_id": second, "state": "configured_runtime_unverified"},
    )
    monkeypatch.setattr(transport, "endpoint_for", lambda *_: "/private/tmp/synthetic.sock")
    gate = holds.gate_path(store.root, binding["id"])
    original_unlink = Path.unlink

    def interrupted_unlink(path, *args, **kwargs):
        if path == gate:
            raise OSError("synthetic gate unlink crash")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", interrupted_unlink)
        assert (
            reauth.release_new_route(
                store, employee["id"], project["id"], second, "default", confirmed=True
            )["state"]
            == "check_unconfirmed"
        )
    assert gated(store.root, binding["id"])
    assert (
        holds._private_json(holds.epoch_path(store.root, binding["id"], second))["phase"]
        == "complete"
    )
    assert (
        reauth.release_new_route(
            store, employee["id"], project["id"], second, "default", confirmed=True
        )["state"]
        == "new_route_released"
    )
    assert not gated(store.root, binding["id"])


def test_arm_old_mail_checks_exact_live_candidate_before_fencing(tmp_path, monkeypatch):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Synthetic", tmp_path)
    employee = store.create_employee("Hermes", "hermes", project["id"])
    binding = set_binding(
        store,
        employee["id"],
        project["id"],
        host_kind="hermes",
        host_session_id="agent:main:feishu:dm:synthetic",
    )
    old = create_mail_task(store, project["id"], "Old", "Synthetic", employee["id"])
    plan = "plan_" + "b" * 32
    fingerprint = "a" * 64
    candidate = {
        "state": "candidate_ready",
        "binding_id": binding["id"],
        "candidate_fingerprint": fingerprint,
        "platform": "feishu",
        "pending_count": 1,
        "unknown_count": 0,
    }
    profile = tmp_path / "profile"
    routing = {
        "protocol": "agent-mailbox.hermes-routing.v1",
        "host_kind": "hermes",
        "instance_id": "synthetic-instance",
        "pid": 42,
        "profile_home": str(profile),
        "routing_home": str(profile),
        "scope": str(profile),
        "gateway_profile": "default",
        "multiplex": False,
    }
    monkeypatch.setattr(
        reauth.catalog,
        "_discover",
        lambda: (
            tmp_path,
            Path("/bin/python3"),
            {"profiles": [{"profile_id": "default", "home": str(profile)}]},
        ),
    )
    monkeypatch.setattr(reauth, "hermes_reauthorization_preview", lambda *_: dict(candidate))
    monkeypatch.setattr(reauth.process, "payload", lambda: tmp_path)
    monkeypatch.setattr(reauth.process, "FILES", ())
    monkeypatch.setattr(transport, "endpoint_for", lambda *_: "/private/tmp/synthetic.sock")
    observations = []
    status = {
        "plan_id": plan,
        "state": "preview",
        "candidate_fingerprint": fingerprint,
        "expires_at": time.time() + 300,
    }

    def run(_python, _profile, request, **_kwargs):
        observations.append(request)
        return dict(status)

    monkeypatch.setattr(reauth.process, "run", run)
    monkeypatch.setattr(reauth, "_request", lambda *_: dict(routing))

    def arm():
        return reauth.arm_old_mail(
            store, employee["id"], project["id"], plan, "default", confirmed=True
        )

    assert (
        reauth.arm_old_mail(store, employee["id"], project["id"], plan, "default")["state"]
        == "confirmation_required"
    )
    assert not gated(store.root, binding["id"])

    status["candidate_fingerprint"] = "c" * 64
    assert arm()["state"] == "check_unconfirmed"
    assert not gated(store.root, binding["id"])
    status["candidate_fingerprint"] = fingerprint
    status["expires_at"] = time.time() - 1
    assert arm()["state"] == "check_unconfirmed"
    assert not gated(store.root, binding["id"])
    status["expires_at"] = time.time() + 300
    changed = [0]

    def changed_instance(*_):
        changed[0] += 1
        return {**routing, "instance_id": f"instance-{changed[0]}"}

    monkeypatch.setattr(reauth, "_request", changed_instance)
    assert arm()["state"] == "check_unconfirmed"
    assert not gated(store.root, binding["id"])

    monkeypatch.setattr(reauth, "_request", lambda *_: dict(routing))

    def crash_after_gate(*_):
        raise OSError("synthetic stop after binding gate")

    with monkeypatch.context() as patch:
        patch.setattr(holds, "hold_prior_pending", crash_after_gate)
        incomplete = arm()
    assert incomplete == {"state": "fence_requires_recovery", "delivery_verified": False}
    assert gated(store.root, binding["id"])
    result = arm()
    assert result == {
        "state": "old_pending_held",
        "held_count": 1,
        "unknown_count": 0,
        "delivery_verified": False,
    }
    assert gated(store.root, binding["id"])
    assert held(store.root, old["request_message_id"])
    assert observations and all(
        call["expected_binding_id"] == binding["id"] for call in observations
    )
    assert resolve_routes(store, employee["id"], [old["request_message_id"]])[0] == []
