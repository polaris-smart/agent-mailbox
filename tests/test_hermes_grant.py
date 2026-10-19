"""Permission repair: owner authentication and bound-conversation invariants."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from agent_mailbox import workbench_hermes_grant as grant
from agent_mailbox.workbench import WorkbenchHTTP
from agent_mailbox.workbench_mail_sessions import create_session
from agent_mailbox.workbench_store import WorkbenchError
from test_hermes_installer import case  # noqa: F401 - pytest exposes this imported fixture to bound


@pytest.fixture
def bound(case, monkeypatch):  # noqa: F811 - pytest injects the fixture named case
    store, _, _, _, _, calls = case
    (store.root / "wake").mkdir(mode=0o700, exist_ok=True)
    binding = {"id": "binding_" + "b" * 32, "host_session_id": "synthetic-original"}
    monkeypatch.setattr(grant, "_binding", lambda *args: {**binding})

    def child(runtime, profile, request, **kwargs):
        calls.append(request)
        assert request["options"]["session_key"] == "synthetic-original"
        assert request["options"]["mailbox_home"] == str(store.root)
        assert set(kwargs["expected_files"]) == set(grant.process.FILES)
        applying = request["operation"] == "grant_apply"
        return {
            "state": "grant_enabled_runtime_unverified" if applying else "grant_preview",
            "enabled": applying,
            "config_sha256": "a" * 64,
            "runtime_verified": False,
            "delivery_verified": False,
        }

    monkeypatch.setattr(grant.process, "run", child)
    return case, binding


def test_preview_apply_only_existing_binding(bound):
    (store, project, _, employee, profile_id, calls), binding = bound
    preview = grant.operation(
        store, employee["id"], project["id"], "preview", profile_id=profile_id
    )
    assert preview["binding_id"] == binding["id"] and not preview["enabled"]
    applied = grant.operation(
        store,
        employee["id"],
        project["id"],
        "apply",
        profile_id=profile_id,
        expected_binding_id=preview["binding_id"],
        expected_config_sha256=preview["config_sha256"],
        confirmed=True,
    )
    assert applied["enabled"] and not applied["delivery_verified"]
    assert calls[-1]["options"]["expected_config_sha256"] == "a" * 64


@pytest.mark.parametrize(
    "patch",
    [
        {"confirmed": False},
        {"confirmed": "true"},
        {"expected_binding_id": "binding_" + "c" * 32},
        {"expected_config_sha256": "invalid"},
    ],
)
def test_bad_confirmation_no_child(bound, patch):
    (store, project, _, employee, profile_id, calls), binding = bound
    args = {
        "profile_id": profile_id,
        "confirmed": True,
        "expected_binding_id": binding["id"],
        "expected_config_sha256": "a" * 64,
    }
    args.update(patch)
    with pytest.raises(WorkbenchError):
        grant.operation(store, employee["id"], project["id"], "apply", **args)
    assert calls == []


def test_wrong_project_no_child(bound):
    (store, _, foreign, employee, profile_id, calls), _ = bound
    with pytest.raises(WorkbenchError):
        grant.operation(store, employee["id"], foreign["id"], "preview", profile_id=profile_id)
    assert calls == []


def test_binding_changes_during_child_unconfirmed(bound, monkeypatch):
    (store, project, _, employee, profile_id, calls), binding = bound
    original = grant.process.run

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        binding["id"] = "binding_" + "c" * 32
        return result

    monkeypatch.setattr(grant.process, "run", changed)
    with pytest.raises(WorkbenchError, match="unconfirmed"):
        grant.operation(store, employee["id"], project["id"], "preview", profile_id=profile_id)
    assert len(calls) == 1


def test_http_owner_only_and_no_browser_path(bound):
    (store, project, _, employee, profile_id, calls), _ = bound
    session = create_session(store, employee["id"], project["id"], "Synthetic")
    server = WorkbenchHTTP(store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    data = {
        "employee_id": employee["id"],
        "project_id": project["id"],
        "profile_id": profile_id,
        "action": "preview",
    }

    def post(token, body):
        request = urllib.request.Request(
            server.endpoint + "/api/workbench/mailbox/receivers/hermes/grant",
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    try:
        for token in ("", session["token"]):
            with pytest.raises(urllib.error.HTTPError) as exc:
                post(token, data)
            assert exc.value.code == 401
        for key in ("path", "session_key"):
            with pytest.raises(urllib.error.HTTPError) as exc:
                post(server.token, {**data, key: "foreign"})
            assert exc.value.code == 400
        assert not calls
        assert post(server.token, data)["state"] == "grant_preview"
        assert len(calls) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        server.instance_lock.close()


@pytest.mark.parametrize("action", [[], {}, None, 1])
def test_invalid_action_is_safe_request_error(bound, action):
    (store, project, _, employee, profile_id, calls), _ = bound
    with pytest.raises(WorkbenchError):
        grant.operation(store, employee["id"], project["id"], action, profile_id=profile_id)
    assert calls == []


def test_successful_repair_signals_pending_worker_without_recovery_scan(bound, monkeypatch):
    import agent_mailbox.workbench as app
    import agent_mailbox.workbench_host_retractions as retractions
    import agent_mailbox.workbench_wake as wake

    (store, project, _, employee, profile_id, _), binding = bound
    checked, rechecked, stop = threading.Event(), threading.Event(), threading.Event()
    passes = []

    def run(*args, **kwargs):
        passes.append(1)
        (checked if len(passes) == 1 else rechecked).set()
        return {"woke": []}

    monkeypatch.setattr(wake, "run_once", run)
    monkeypatch.setattr(retractions, "reconcile_opened_queue", lambda *a: None)
    thread = threading.Thread(
        target=app.wake_redeem_loop,
        args=(store, store.root),
        kwargs={"interval": None, "stop": stop},
        daemon=True,
    )
    thread.start()
    try:
        assert checked.wait(2)
        grant.operation(
            store,
            employee["id"],
            project["id"],
            "apply",
            profile_id=profile_id,
            expected_binding_id=binding["id"],
            expected_config_sha256="a" * 64,
            confirmed=True,
        )
        assert rechecked.wait(2), (
            "No timeout scan: restoring permission must signal the existing worker"
        )
    finally:
        stop.set()
        store.mail_wake_event.set()
        thread.join(3)
    assert not thread.is_alive()


def test_preview_or_changed_binding_never_signals_worker(bound, monkeypatch):
    (store, project, _, employee, profile_id, _), binding = bound
    store.mail_wake_event.clear()
    grant.operation(store, employee["id"], project["id"], "preview", profile_id=profile_id)
    assert not store.mail_wake_event.is_set()
    original = grant.process.run

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        binding["id"] = "binding_" + "d" * 32
        return result

    monkeypatch.setattr(grant.process, "run", changed)
    with pytest.raises(WorkbenchError):
        grant.operation(
            store,
            employee["id"],
            project["id"],
            "apply",
            profile_id=profile_id,
            expected_binding_id=binding["id"],
            expected_config_sha256="a" * 64,
            confirmed=True,
        )
    assert not store.mail_wake_event.is_set()
