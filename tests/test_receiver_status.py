"""Owner diagnostics against disposable real sockets; no host or message calls."""

import contextlib
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from agent_mailbox import workbench_receiver_status as status
from agent_mailbox.workbench_host_bindings import set_binding
from agent_mailbox.workbench_mail_config import export_session
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


@pytest.fixture(params=["hermes", "deepseek"])
def setup(tmp_path, request):
    root = Path(tempfile.mkdtemp(prefix="rs-", dir="/private/tmp")).resolve()
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Synthetic", tmp_path)
    employee = store.create_employee("Synthetic", request.param, project["id"])
    binding = set_binding(
        store,
        employee["id"],
        project["id"],
        host_kind=request.param,
        host_session_id="synthetic-original",
    )
    endpoint = root / "receiver.sock"
    config = store.root / "wake/host-endpoints.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "endpoints": [
                    {
                        "binding_id": binding["id"],
                        "employee_id": employee["id"],
                        "host_kind": request.param,
                        "socket_path": str(endpoint),
                    }
                ],
            }
        )
    )
    config.chmod(0o600)
    value = {
        "protocol": "agent-mailbox.host-health.v1"
        if request.param == "hermes"
        else "agent-mailbox.host-receipt.v1",
        "host_kind": request.param,
        "gateway_injection_available": True,
        "queue_supported": True,
        "route_available": True,
        "private_field": "PRIVATE must not reach UI",
    }
    try:
        yield store, project, employee, endpoint, config, value
    finally:
        shutil.rmtree(root)


@contextlib.contextmanager
def receiver(endpoint, value, *, delay=False):
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(endpoint))
    endpoint.chmod(0o600)
    server.listen()
    server.settimeout(0.1)
    stop = threading.Event()
    requests = []
    errors = []

    def serve():
        while not stop.is_set():
            try:
                channel, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with channel:
                channel.settimeout(2)
                try:
                    raw = b""
                    while b"\n" not in raw:
                        chunk = channel.recv(1024)
                        if not chunk:
                            break
                        raw += chunk
                    requests.append(json.loads(raw))
                    if delay:
                        for _ in range(30):
                            if stop.wait(0.02):
                                break
                            channel.sendall(b" ")
                    else:
                        reply = value(requests[-1]) if callable(value) else value
                        channel.sendall(
                            reply
                            if isinstance(reply, bytes)
                            else json.dumps(reply).encode() + b"\n"
                        )
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as exc:  # noqa: BLE001 — report errors from the socket worker
                    errors.append(exc)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        yield requests
    finally:
        stop.set()
        server.close()
        worker.join(3)
        assert not worker.is_alive() and not errors


def check(f):
    store, project, employee, *_ = f
    return status.reception_status(store, employee["id"], project["id"])


def test_codex_original_thread_receiver_status_uses_scoped_health(tmp_path):
    root = Path(tempfile.mkdtemp(prefix="rs-codex-", dir="/private/tmp")).resolve()
    try:
        store = WorkbenchStore(tmp_path / "home")
        project = store.create_project("Synthetic", tmp_path)
        employee = store.create_employee("Codex", "codex", project["id"])
        binding = set_binding(
            store,
            employee["id"],
            project["id"],
            host_kind="codex",
            host_session_id="original-thread",
        )
        endpoint = root / "receiver.sock"
        config = store.root / "wake/host-endpoints.json"
        config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "endpoints": [
                        {
                            "binding_id": binding["id"],
                            "employee_id": employee["id"],
                            "host_kind": "codex",
                            "socket_path": str(endpoint),
                        }
                    ],
                }
            )
        )
        config.chmod(0o600)

        def answer(request):
            return {
                "protocol": "agent-mailbox.receiver-health.v1",
                "request_id": request["request_id"],
                "thread_id": request["thread_id"],
                "loaded": True,
                "admission_available": True,
            }

        with receiver(endpoint, answer) as requests:
            result = status.reception_status(store, employee["id"], project["id"])
        assert result == {
            "host_kind": "codex",
            "component_available": True,
            "binding_configured": True,
            "route_configured": True,
            "session_verified": False,
            "delivery_verified": False,
            "pending_alert_count": 0,
            "held_alert_count": 0,
            "unknown_delivery_alert_count": 0,
            "oldest_pending_alert_at": None,
            "state": "component_available",
        }
        assert len(requests) == 1
        assert requests[0]["operation"] == "health"
        assert requests[0]["thread_id"] == "original-thread"
        assert not (store.root / "wake/receipts").exists()
    finally:
        shutil.rmtree(root)


@pytest.mark.parametrize("admission_available", [True, False])
def test_codex_cli_receiver_status_uses_resumable_health(tmp_path, admission_available):
    root = Path(tempfile.mkdtemp(prefix="rs-codex-cli-", dir="/private/tmp")).resolve()
    try:
        store = WorkbenchStore(tmp_path / "home")
        project = store.create_project("Synthetic", tmp_path)
        employee = store.create_employee("Codex CLI", "codex", project["id"])
        binding = set_binding(
            store,
            employee["id"],
            project["id"],
            host_kind="codex",
            host_session_id="original-thread",
        )
        endpoint = root / "receiver.sock"
        config = store.root / "wake/host-endpoints.json"
        config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "endpoints": [
                        {
                            "binding_id": binding["id"],
                            "employee_id": employee["id"],
                            "host_kind": "codex",
                            "mode": "on_demand_cli",
                            "socket_path": str(endpoint),
                        }
                    ],
                }
            )
        )
        config.chmod(0o600)

        def answer(request):
            return {
                "protocol": "agent-mailbox.cli-receiver-health.v1",
                "request_id": request["request_id"],
                "thread_id": request["thread_id"],
                "resumable": True,
                "admission_available": admission_available,
            }

        with receiver(endpoint, answer) as requests:
            result = status.reception_status(store, employee["id"], project["id"])
        assert result["state"] == (
            "component_available" if admission_available else "host_unavailable"
        )
        assert result["component_available"] is admission_available
        assert result["session_verified"] is False and result["delivery_verified"] is False
        assert len(requests) == 1 and requests[0]["thread_id"] == "original-thread"
    finally:
        shutil.rmtree(root)


@pytest.mark.parametrize("available", [True, False])
def test_status_requires_exact_hermes_route_without_claiming_delivery(setup, available):
    store, _, employee, endpoint, config, value = setup
    value["gateway_injection_available"] = value["queue_supported"] = available
    value["route_available"] = available
    before = config.read_bytes()
    with receiver(endpoint, value) as requests:
        result = check(setup)
    assert result == {
        "host_kind": employee["kind"],
        "component_available": available,
        "binding_configured": True,
        "route_configured": True,
        "session_verified": False,
        "delivery_verified": False,
        "pending_alert_count": 0,
        "held_alert_count": 0,
        "unknown_delivery_alert_count": 0,
        "oldest_pending_alert_at": None,
        **({"hermes_route_present": True} if employee["kind"] == "hermes" else {}),
        "state": "component_available" if available else "host_unavailable",
    }
    if employee["kind"] == "hermes":
        assert len(requests) == 1 and requests[0]["operation"] == "probe"
        assert requests[0]["binding"]["employee_id"] == employee["id"]
        assert requests[0]["binding"]["host_session_id"] == "synthetic-original"
    else:
        assert requests == [{"operation": "probe"}]
    assert "PRIVATE" not in json.dumps(result) and config.read_bytes() == before
    assert not (store.root / "wake/receipts").exists()


def test_pending_alert_diagnostic_is_scoped_and_does_not_expire_mail(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    first = store.create_project("First", tmp_path)
    second = store.create_project("Second", tmp_path)
    employee = store.create_employee("WorkBuddy", "workbuddy", first["id"])
    store.add_project_member(second["id"], employee["id"])
    first_letter = store.send_message(
        first["id"], "First letter", "Synthetic", recipient_id=employee["id"]
    )
    store.send_message(second["id"], "Second letter", "Synthetic", recipient_id=employee["id"])

    first_status = status.reception_status(store, employee["id"], first["id"])
    second_status = status.reception_status(store, employee["id"], second["id"])

    assert first_status["state"] == second_status["state"] == "unsupported"
    assert first_status["pending_alert_count"] == second_status["pending_alert_count"] == 1
    assert (
        first_status["unknown_delivery_alert_count"]
        == second_status["unknown_delivery_alert_count"]
        == 0
    )
    with store._transaction(readonly=True) as db:
        notification = db.execute(
            "SELECT created_at FROM notifications WHERE kind='mailbox_wake' "
            "AND state_hash=? AND status='pending'",
            (first_letter["id"],),
        ).fetchone()
        assert first_status["oldest_pending_alert_at"] == notification["created_at"]
        assert second_status["oldest_pending_alert_at"] is not None
        assert (
            db.execute(
                "SELECT count(*) FROM notifications WHERE kind='mailbox_wake' AND status='pending'"
            ).fetchone()[0]
            == 2
        )


def test_unknown_delivery_alerts_are_counted_only_in_their_project(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    first = store.create_project("First", tmp_path)
    second = store.create_project("Second", tmp_path)
    employee = store.create_employee("Hermes", "hermes", first["id"])
    store.add_project_member(second["id"], employee["id"])
    first_letter = store.send_message(
        first["id"], "First", "Synthetic", recipient_id=employee["id"]
    )
    store.send_message(second["id"], "Second", "Synthetic", recipient_id=employee["id"])
    claims = store.root / "wake" / "claims"
    claims.mkdir(parents=True, exist_ok=True)
    (claims / f"{first_letter['id']}.unknown").touch()

    first_status = status.reception_status(store, employee["id"], first["id"])
    second_status = status.reception_status(store, employee["id"], second["id"])

    assert first_status["pending_alert_count"] == second_status["pending_alert_count"] == 1
    assert first_status["unknown_delivery_alert_count"] == 1
    assert second_status["unknown_delivery_alert_count"] == 0


def test_held_alerts_remain_visible_in_exact_project_diagnostics(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    first = store.create_project("First", tmp_path)
    second = store.create_project("Second", tmp_path)
    employee = store.create_employee("Hermes", "hermes", first["id"])
    store.add_project_member(second["id"], employee["id"])
    first_letter = store.send_message(
        first["id"], "First", "Synthetic", recipient_id=employee["id"]
    )
    store.send_message(second["id"], "Second", "Synthetic", recipient_id=employee["id"])
    with store._transaction() as db:
        db.execute(
            "UPDATE notifications SET status='reauthorization_held' "
            "WHERE kind='mailbox_wake' AND state_hash=?",
            (first_letter["id"],),
        )

    first_status = status.reception_status(store, employee["id"], first["id"])
    second_status = status.reception_status(store, employee["id"], second["id"])
    assert first_status["pending_alert_count"] == 0
    assert first_status["held_alert_count"] == 1
    assert second_status["pending_alert_count"] == 1
    assert second_status["held_alert_count"] == 0
    with store._transaction(readonly=True) as db:
        assert (
            db.execute(
                "SELECT status FROM notifications WHERE state_hash=?", (first_letter["id"],)
            ).fetchone()["status"]
            == "reauthorization_held"
        )


@pytest.mark.parametrize("other_project", [False, True])
def test_unbound_hermes_member_sees_another_members_existing_route(tmp_path, other_project):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Synthetic", tmp_path)
    selected = store.create_employee("New member", "hermes", project["id"])
    route_project = store.create_project("Other project", tmp_path) if other_project else project
    other = store.create_employee("Existing member", "hermes", route_project["id"])
    assert (
        status.reception_status(store, selected["id"], project["id"])["hermes_route_present"]
        is False
    )
    set_binding(
        store,
        other["id"],
        route_project["id"],
        host_kind="hermes",
        host_session_id="existing-session",
    )

    result = status.reception_status(store, selected["id"], project["id"])

    assert result["state"] == "binding_required"
    assert result["binding_configured"] is False
    assert result["hermes_route_present"] is True
    assert result["session_verified"] is False
    assert result["delivery_verified"] is False


def test_unreadable_hermes_routes_never_look_like_empty_home(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Synthetic", tmp_path)
    employee = store.create_employee("New member", "hermes", project["id"])
    bindings = store.root / "wake" / "host-bindings.json"
    bindings.parent.mkdir(exist_ok=True)
    bindings.write_text("{invalid json", encoding="utf-8")

    result = status.reception_status(store, employee["id"], project["id"])

    assert result["state"] == "check_unconfirmed"
    assert result["hermes_route_present"] is None
    assert result["binding_configured"] is False
    assert result["session_verified"] is False
    assert result["delivery_verified"] is False


def test_hermes_gateway_online_with_stale_session_pin_is_unavailable(setup):
    store, _, employee, endpoint, _, value = setup
    if employee["kind"] != "hermes":
        pytest.skip("Only Hermes has the pinned Gateway session contract")
    value["gateway_injection_available"] = True
    value["route_available"] = False

    def answer(request):
        if request["operation"] == "probe":
            return value
        return {
            "protocol": "agent-mailbox.host-reauthorization.v1",
            "host_kind": "hermes",
            "binding_id": request["binding"]["id"],
            "candidate_fingerprint": "a" * 64,
            "platform": "feishu",
            "route_available": False,
            "session_verified": False,
            "delivery_verified": False,
        }

    with receiver(endpoint, answer) as requests:
        result = check(setup)
    assert result["state"] == "session_reconnect_required"
    assert result["component_available"] is False
    assert [request["operation"] for request in requests] == ["probe", "reauthorization_candidate"]
    assert requests[0]["binding"]["employee_id"] == employee["id"]
    assert not (store.root / "wake/receipts").exists()


def test_hermes_gateway_without_reauthorization_candidate_does_not_offer_reconnect(setup):
    _, _, employee, endpoint, _, value = setup
    if employee["kind"] != "hermes":
        pytest.skip("Hermes-only candidate gate")
    value["gateway_injection_available"] = True
    value["route_available"] = False

    def answer(request):
        return value if request["operation"] == "probe" else {"error": "delivery_unconfirmed"}

    with receiver(endpoint, answer) as requests:
        result = check(setup)
        preview = status.hermes_reauthorization_preview(setup[0], employee["id"], setup[1]["id"])
    assert result["state"] == "host_unavailable"
    assert result["component_available"] is False
    assert preview == {"state": "host_unavailable", "candidate_available": False}
    assert [request["operation"] for request in requests] == [
        "probe",
        "reauthorization_candidate",
        "probe",
        "reauthorization_candidate",
    ]


def test_hermes_reauthorization_preview_is_scoped_and_read_only(setup):
    store, project, employee, endpoint, config, value = setup
    if employee["kind"] != "hermes":
        pytest.skip("Hermes-only reauthorization candidate")
    value["route_available"] = False
    fingerprint = "a" * 64

    def reply(request):
        if request["operation"] == "probe":
            return value
        assert request["operation"] == "reauthorization_candidate"
        assert request["binding"]["employee_id"] == employee["id"]
        return {
            "protocol": "agent-mailbox.host-reauthorization.v1",
            "host_kind": "hermes",
            "binding_id": request["binding"]["id"],
            "candidate_fingerprint": fingerprint,
            "platform": "feishu",
            "route_available": False,
            "session_verified": False,
            "delivery_verified": False,
        }

    before = config.read_bytes()
    with receiver(endpoint, reply) as requests:
        result = status.hermes_reauthorization_preview(store, employee["id"], project["id"])
    assert result == {
        "state": "candidate_ready",
        "candidate_available": True,
        "binding_id": requests[-1]["binding"]["id"],
        "candidate_fingerprint": fingerprint,
        "platform": "feishu",
        "pending_count": 0,
        "unknown_count": 0,
        "session_verified": False,
        "delivery_verified": False,
    }
    assert [request["operation"] for request in requests] == [
        "probe",
        "reauthorization_candidate",
        "reauthorization_candidate",
    ]
    assert config.read_bytes() == before
    assert not (store.root / "wake/receipts").exists()


@pytest.mark.parametrize("fault", ["protocol", "kind", "flag", "oversize", "invalid"])
def test_bad_response_cannot_report_component_available(setup, fault):
    _, _, _, endpoint, _, value = setup
    if fault == "protocol":
        value["protocol"] = "wrong"
    elif fault == "kind":
        value["host_kind"] = "codex"
    elif fault == "flag":
        value["gateway_injection_available"] = value["queue_supported"] = value[
            "route_available"
        ] = 1
    elif fault == "oversize":
        value = b"x" * 8193 + b"\n"
    else:
        value = b"not-json\n"
    with receiver(endpoint, value):
        result = check(setup)
    assert result["state"] == "check_unconfirmed" and not result["component_available"]


def test_missing_binding_route_and_socket_are_distinct(setup):
    store, project, employee, _, config, _ = setup
    assert check(setup)["state"] == "receiver_offline"
    config.unlink()
    assert check(setup)["state"] == "route_required"
    set_binding(store, employee["id"], project["id"])
    assert check(setup)["state"] == "binding_required"


@pytest.mark.parametrize("fault", ["public_config", "fifo_config", "public_socket", "symlink"])
def test_unsafe_endpoint_never_probed(setup, fault):
    _, _, _, endpoint, config, value = setup
    with receiver(endpoint, value) as requests:
        if fault == "public_config":
            config.chmod(0o644)
        elif fault == "fifo_config":
            config.unlink()
            os.mkfifo(config, 0o600)
        elif fault == "public_socket":
            endpoint.chmod(0o666)
        else:
            original = endpoint.with_suffix(".original")
            endpoint.rename(original)
            endpoint.symlink_to(original)
        assert check(setup)["state"] == "check_unconfirmed"
        assert requests == []


def test_slow_trickle_has_total_deadline(setup):
    _, _, employee, endpoint, _, value = setup
    with receiver(endpoint, value, delay=True):
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            status._probe(str(endpoint), employee["kind"], timeout=0.1)
        assert time.monotonic() - started < 0.4


def test_binding_revoked_during_probe_is_not_reported_available(setup, monkeypatch):
    store, project, employee, _, _, _ = setup

    def probe(*args, **kwargs):
        set_binding(store, employee["id"], project["id"])
        return True

    monkeypatch.setattr(status, "_probe", probe)
    assert check(setup)["state"] == "configuration_changed"


def test_nonmember_rejected_before_any_probe(setup, monkeypatch):
    store, project, employee, _, _, _ = setup
    with store._transaction() as db:
        db.execute(
            "DELETE FROM memberships WHERE employee_id=? AND project_id=?",
            (employee["id"], project["id"]),
        )
    monkeypatch.setattr(status, "_probe", lambda *a: pytest.fail("must not probe"))
    with pytest.raises(WorkbenchError):
        check(setup)


def test_owner_http_only_and_no_arbitrary_probe_arguments(setup):
    from agent_mailbox.workbench import WorkbenchHTTP

    store, project, employee, _, _, _ = setup
    exported = export_session(store, employee["id"], project["id"], "Synthetic")
    credential = json.loads(
        (store.directory / "mail-sessions" / (exported["id"] + ".json")).read_text()
    )["token"]
    server = WorkbenchHTTP(store)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def get(token, extra=""):
        query = (
            urllib.parse.urlencode({"employee_id": employee["id"], "project_id": project["id"]})
            + extra
        )
        req = urllib.request.Request(
            server.endpoint + "/api/workbench/mailbox/receivers/native?" + query,
            headers={"Authorization": "Bearer " + token},
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    try:
        for token in ("", "wrong", credential):
            assert get(token)[0] == 401
        code, result = get(server.token)
        assert code == 200
        if employee["kind"] == "deepseek":
            assert result["opened_alert_retractions"] == {
                "pending": 0,
                "unknown": 0,
                "binding_changed": 0,
            }
        else:
            assert "opened_alert_retractions" not in result
        assert get(server.token, "&socket_path=/secret")[0] == 400
        assert get(server.token, "&project_id=another")[0] == 400
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)


def test_hermes_reauthorization_plan_is_owner_only_and_accepts_no_session_id(setup, monkeypatch):
    from agent_mailbox import workbench_hermes_reauthorization as reauth
    from agent_mailbox.workbench import WorkbenchHTTP

    store, project, employee, *_ = setup
    if employee["kind"] != "hermes":
        pytest.skip("Hermes-only plan")
    exported = export_session(store, employee["id"], project["id"], "Synthetic")
    credential = json.loads(
        (store.directory / "mail-sessions" / (exported["id"] + ".json")).read_text()
    )["token"]
    calls = []

    def prepared(_store, employee_id, project_id):
        calls.append((employee_id, project_id))
        return {
            "state": "confirmation_required",
            "plan_id": "plan_" + "a" * 32,
            "session_verified": False,
            "delivery_verified": False,
        }

    monkeypatch.setattr(reauth, "prepare", prepared)
    server = WorkbenchHTTP(store)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    route = server.endpoint + "/api/workbench/mailbox/receivers/hermes/reauthorization/preview"

    def post(token, data):
        req = urllib.request.Request(
            route,
            data=json.dumps(data).encode(),
            method="POST",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    try:
        fields = {"employee_id": employee["id"], "project_id": project["id"]}
        for token in ("", "wrong", credential):
            assert post(token, fields)[0] == 401
        assert post(server.token, {**fields, "candidate_session_id": "private"})[0] == 400
        code, value = post(server.token, fields)
        assert code == 200 and value["state"] == "confirmation_required"
        assert "session_id" not in json.dumps(value)
        assert calls == [(employee["id"], project["id"])]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)


def test_hermes_old_mail_fence_is_owner_only_and_requires_explicit_confirmation(setup, monkeypatch):
    from agent_mailbox import workbench_hermes_reauthorization as reauth
    from agent_mailbox.workbench import WorkbenchHTTP

    store, project, employee, *_ = setup
    if employee["kind"] != "hermes":
        pytest.skip("Hermes-only fence")
    exported = export_session(store, employee["id"], project["id"], "Synthetic")
    credential = json.loads(
        (store.directory / "mail-sessions" / (exported["id"] + ".json")).read_text()
    )["token"]
    calls = []

    def arm(_store, employee_id, project_id, plan_id, profile_id, *, confirmed=False):
        calls.append((employee_id, project_id, plan_id, profile_id, confirmed))
        return {"state": "old_pending_held", "delivery_verified": False}

    monkeypatch.setattr(reauth, "arm_old_mail", arm)
    server = WorkbenchHTTP(store)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    route = server.endpoint + "/api/workbench/mailbox/receivers/hermes/reauthorization/arm"

    def post(token, data):
        req = urllib.request.Request(
            route,
            data=json.dumps(data).encode(),
            method="POST",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    try:
        fields = {
            "employee_id": employee["id"],
            "project_id": project["id"],
            "plan_id": "plan_" + "a" * 32,
            "profile_id": "default",
            "confirmed": True,
        }
        for token in ("", "wrong", credential):
            assert post(token, fields)[0] == 401
        assert post(server.token, {**fields, "candidate_session_id": "private"})[0] == 400
        assert post(server.token, {**fields, "confirmed": "true"})[0] == 400
        assert calls == []
        code, value = post(server.token, fields)
        assert code == 200 and value["state"] == "old_pending_held"
        assert calls == [(employee["id"], project["id"], fields["plan_id"], "default", True)]
        releases = []

        def release(_store, employee_id, project_id, plan_id, profile_id, *, confirmed=False):
            releases.append((employee_id, project_id, plan_id, profile_id, confirmed))
            return {
                "state": "new_route_released",
                "old_mail_released": False,
                "delivery_verified": False,
            }

        monkeypatch.setattr(reauth, "release_new_route", release)
        route = server.endpoint + "/api/workbench/mailbox/receivers/hermes/reauthorization/release"
        for token in ("", "wrong", credential):
            assert post(token, fields)[0] == 401
        assert post(server.token, {**fields, "candidate_session_id": "private"})[0] == 400
        assert post(server.token, {**fields, "confirmed": "true"})[0] == 400
        assert releases == []
        code, value = post(server.token, fields)
        assert code == 200 and value["state"] == "new_route_released"
        assert value["old_mail_released"] is False
        assert releases == [(employee["id"], project["id"], fields["plan_id"], "default", True)]
        from agent_mailbox import workbench_hermes_cutover as cutover

        commits = []

        def commit(_store, employee_id, project_id, plan_id, profile_id, *, confirmed=False):
            commits.append((employee_id, project_id, plan_id, profile_id, confirmed))
            return {
                "state": "new_route_released",
                "old_mail_released": False,
                "delivery_verified": False,
            }

        monkeypatch.setattr(cutover, "execute", commit)
        route = server.endpoint + "/api/workbench/mailbox/receivers/hermes/reauthorization/commit"
        for token in ("", "wrong", credential):
            assert post(token, fields)[0] == 401
        assert post(server.token, {**fields, "candidate_session_id": "private"})[0] == 400
        assert post(server.token, {**fields, "confirmed": 1})[0] == 400
        assert commits == []
        code, value = post(server.token, fields)
        assert code == 200 and value["state"] == "new_route_released"
        assert commits == [(employee["id"], project["id"], fields["plan_id"], "default", True)]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)
