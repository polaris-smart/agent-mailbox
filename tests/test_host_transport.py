"""Native sender transport through real local IPC, synthetic hosts only."""

import json
import queue
import socketserver
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from agent_mailbox import workbench_host_transport as transport
from agent_mailbox import workbench_wake as wake

if sys.platform == "win32":
    pytest.skip("Native Unix endpoint transport", allow_module_level=True)


@pytest.fixture
def local(tmp_path):
    with tempfile.TemporaryDirectory(prefix="am-native-", dir=Path("/tmp").resolve()) as d:
        home = tmp_path / "home"
        (home / "wake").mkdir(parents=True)
        endpoint = Path(d) / "host.sock"
        binding = {
            "id": "binding_test",
            "employee_id": "employee_a",
            "project_id": "project_a",
            "host_kind": "hermes",
            "host_session_id": "existing_session",
        }
        (home / "wake/host-bindings.json").write_text(
            json.dumps({"version": 1, "bindings": [binding]})
        )
        config = home / "wake" / transport.CONFIG
        config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "endpoints": [
                        {
                            "binding_id": binding["id"],
                            "employee_id": "employee_a",
                            "host_kind": "hermes",
                            "socket_path": str(endpoint),
                        }
                    ],
                }
            )
        )
        config.chmod(0o600)
        yield home, endpoint, binding, config


def listener(endpoint, outcome, calls, *, kind=None):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            raw = self.rfile.readline()
            if not raw:
                return
            p = json.loads(raw)
            if p.get("operation") == "health":
                assert kind == "codex"
                self.wfile.write(
                    (
                        json.dumps(
                            {
                                "protocol": "agent-mailbox.receiver-health.v1",
                                "request_id": p["request_id"],
                                "thread_id": p["thread_id"],
                                "loaded": True,
                                "admission_available": True,
                            }
                        )
                        + "\n"
                    ).encode()
                )
                return
            if p.get("operation") == "probe":
                assert kind in {"hermes", "deepseek"}
                flag = "gateway_injection_available" if kind == "hermes" else "queue_supported"
                response = {
                    "protocol": "agent-mailbox.host-health.v1"
                    if kind == "hermes"
                    else "agent-mailbox.host-receipt.v1",
                    "host_kind": kind,
                    flag: True,
                }
                if kind == "hermes":
                    assert p["binding"]["host_kind"] == "hermes"
                    response["route_available"] = True
                else:
                    assert p == {"operation": "probe"}
                self.wfile.write((json.dumps(response) + "\n").encode())
                return
            if p.get("operation") == "delivery_status":
                calls.append(p)
                envelope = p["envelope"]
                binding = envelope["binding"]
                response = {
                    "protocol": transport.PROTOCOL,
                    "delivery_id": envelope["delivery_id"],
                    "employee_id": envelope["employee_id"],
                    "message_ids": envelope["fresh"],
                    "binding_id": binding["id"],
                    "project_id": binding["project_id"],
                    "host_kind": binding["host_kind"],
                    "host_session_id": binding["host_session_id"],
                    "status": "received" if outcome == "received" else "unknown",
                }
                if outcome == "wrong_session":
                    response["host_session_id"] = "wrong_session"
                if outcome != "lost":
                    self.wfile.write(json.dumps(response).encode() + b"\n")
                return
            calls.append(p)
            if outcome == "lost":
                return
            b = p["binding"]
            r = {
                "protocol": transport.PROTOCOL,
                "delivery_id": p["delivery_id"],
                "employee_id": p["employee_id"],
                "message_ids": p["fresh"],
                "binding_id": b["id"],
                "project_id": b["project_id"],
                "host_kind": b["host_kind"],
                "host_session_id": b["host_session_id"],
                "status": "not_received" if outcome == "refused" else "received",
            }
            if outcome == "wrong_session":
                r["host_session_id"] = "wrong_session"
            if outcome == "health":
                r = {"protocol": "agent-mailbox.host-health.v1"}
            self.wfile.write(json.dumps(r).encode() + b"\n")

    server = socketserver.UnixStreamServer(str(endpoint), Handler)
    endpoint.chmod(0o600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.mark.parametrize("outcome", ["received", "refused", "lost", "wrong_session", "health"])
def test_native_route_keeps_receipt_and_unknown_guards(local, monkeypatch, outcome):
    home, endpoint, binding, _ = local
    calls = []
    server, thread = listener(endpoint, outcome, calls)
    monkeypatch.setattr(wake, "hook_command", lambda home: ["legacy-must-not-run"])

    def legacy(*args, **kwargs):
        pytest.fail("native attempt must never fall through to shell")

    send = wake.hook_deliver(home, runner=legacy, require_receipt=True, expected_binding=binding)
    try:
        assert send("employee_a", {"fresh": ["message_a"]}) is (outcome == "received")
        suffix = "ok" if outcome == "received" else "pending" if outcome == "refused" else "unknown"
        assert (home / "wake/claims" / ("message_a." + suffix)).exists()
        if suffix in {"ok", "unknown"}:
            send("employee_a", {"fresh": ["message_a"]})
            attempts = [call for call in calls if call.get("operation") != "delivery_status"]
            status_queries = [call for call in calls if call.get("operation") == "delivery_status"]
            assert len(attempts) == 1
            assert len(status_queries) == (1 if suffix == "unknown" else 0)
    finally:
        server.shutdown()
        thread.join(3)
        server.server_close()


@pytest.mark.parametrize("outcome", ["received", "unknown", "wrong_session", "lost"])
def test_existing_hermes_unknown_uses_status_only_without_reinjection(local, monkeypatch, outcome):
    home, endpoint, binding, _ = local
    delivery_id = "wake-existing-synthetic"
    message_id = "message_a"
    payload = {
        "receipt_protocol": transport.PROTOCOL,
        "delivery_id": delivery_id,
        "employee_id": binding["employee_id"],
        "binding": binding,
        "fresh": [message_id],
    }
    claims = home / "wake/claims"
    claims.mkdir()
    unknown = claims / f"{message_id}.unknown"
    unknown.write_text(delivery_id)
    outbox = home / "wake/wake-outbox"
    outbox.mkdir()
    (outbox / f"{delivery_id}.json").write_text(json.dumps(payload))
    calls = []
    server, thread = listener(endpoint, outcome, calls)
    monkeypatch.setattr(wake, "hook_command", lambda _home: ["legacy-must-not-run"])
    send = wake.hook_deliver(
        home,
        runner=lambda *args, **kwargs: pytest.fail("unknown must never run a hook"),
        require_receipt=True,
        expected_binding=binding,
    )
    try:
        assert send(binding["employee_id"], {"fresh": [message_id]}) is (outcome == "received")
        assert calls == [{"operation": "delivery_status", "envelope": payload}]
        assert unknown.exists() is (outcome != "received")
        assert (claims / f"{message_id}.ok").exists() is (outcome == "received")
    finally:
        server.shutdown()
        thread.join(3)
        server.server_close()


def test_offline_and_revoked_routes_are_not_success(local):
    home, _, binding, _ = local
    assert wake.delivery_available(home, "employee_a")
    assert not wake.delivery_available(home, "employee_other")
    send = wake.hook_deliver(home, require_receipt=True, expected_binding=binding)
    assert send("employee_a", {"fresh": ["message_a"]}) is False
    assert (home / "wake/claims/message_a.pending").exists()
    (home / "wake/host-bindings.json").write_text(json.dumps({"version": 1, "bindings": []}))
    assert not wake.delivery_available(home, "employee_a")


@pytest.mark.parametrize("change", ["revoked", "rebound"])
def test_stale_resolved_binding_cannot_send_or_fall_back(local, monkeypatch, change):
    home, endpoint, binding, _ = local
    calls = []
    server, thread = listener(endpoint, "received", calls)
    monkeypatch.setattr(wake, "hook_command", lambda home: ["legacy-must-not-run"])
    changed = {**binding, "host_session_id": "replacement_session"}
    (home / "wake/host-bindings.json").write_text(
        json.dumps({"version": 1, "bindings": [] if change == "revoked" else [changed]})
    )
    send = wake.hook_deliver(
        home,
        require_receipt=True,
        expected_binding=binding,
        runner=lambda *a, **k: pytest.fail("stale route must not fall back"),
    )
    try:
        with pytest.raises(ValueError, match="revoked_or_changed"):
            send("employee_a", {"fresh": ["message_a"]})
        assert calls == []
        assert not (home / "wake/claims/message_a.ok").exists()
    finally:
        server.shutdown()
        thread.join(3)
        server.server_close()


def test_slow_response_has_total_deadline_and_is_not_retried(local):
    home, endpoint, binding, _ = local
    calls, stopped = [], threading.Event()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            calls.append(json.loads(self.rfile.readline()))
            # Each byte arrives within the per-read timeout; the whole response
            # must still expire. No valid receipt is produced.
            try:
                while not stopped.is_set():
                    self.wfile.write(b" ")
                    if stopped.wait(0.025):
                        break
            except OSError:
                pass

    server = socketserver.UnixStreamServer(str(endpoint), Handler)
    endpoint.chmod(0o600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    send = wake.hook_deliver(home, timeout=0.15, require_receipt=True, expected_binding=binding)
    # Bound a broken implementation as well, so this regression cannot hang CI.
    watchdog = threading.Timer(1.5, stopped.set)
    watchdog.start()
    try:
        start = time.monotonic()
        assert send("employee_a", {"fresh": ["message_a"]}) is False
        assert time.monotonic() - start < 1
        assert (home / "wake/claims/message_a.unknown").exists()
        assert not list((home / "wake/receipts").glob("wake-*"))
        assert send("employee_a", {"fresh": ["message_a"]}) is False
        attempts = [call for call in calls if call.get("operation") != "delivery_status"]
        status_queries = [call for call in calls if call.get("operation") == "delivery_status"]
        assert len(attempts) == 1
        assert len(status_queries) == 1
    finally:
        stopped.set()
        watchdog.cancel()
        server.shutdown()
        thread.join(3)
        server.server_close()


def test_codex_health_slow_bytes_have_one_total_deadline(local):
    _, endpoint, _, _ = local
    stopped = threading.Event()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            if not self.rfile.readline():
                return
            try:
                while not stopped.wait(0.025):
                    self.wfile.write(b" ")
                    self.wfile.flush()
            except OSError:
                pass

    server = socketserver.ThreadingUnixStreamServer(str(endpoint), Handler)
    endpoint.chmod(0o600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        start = time.monotonic()
        assert transport._codex_health(endpoint, "existing-thread", timeout=0.15) is False
        assert time.monotonic() - start < 1
    finally:
        stopped.set()
        server.shutdown()
        thread.join(3)
        server.server_close()


@pytest.mark.parametrize("problem", ["public_config", "symlink_config", "duplicate", "mismatch"])
def test_unsafe_endpoint_configuration_fails_closed(local, problem):
    home, _, binding, config = local
    if problem == "public_config":
        config.chmod(0o644)
    elif problem == "symlink_config":
        other = config.with_suffix(".original")
        config.rename(other)
        config.symlink_to(other)
    else:
        d = json.loads(config.read_text())
        if problem == "duplicate":
            d["endpoints"] *= 2
        else:
            d["endpoints"][0]["employee_id"] = "employee_other"
        config.write_text(json.dumps(d))
    with pytest.raises((ValueError, OSError)):
        transport.endpoint_for(home, binding)


@pytest.mark.parametrize("historical_pending", [False, True])
@pytest.mark.parametrize("review_host", ["hermes", "codex"])
def test_commit_event_routes_assignment_and_delivery_to_two_endpoints(
    local, monkeypatch, historical_pending, review_host
):
    from agent_mailbox.workbench import wake_redeem_loop
    from agent_mailbox.workbench_host_bindings import set_binding
    from agent_mailbox.workbench_mail_notifications import pending_ids
    from agent_mailbox.workbench_mail_sessions import create_session
    from agent_mailbox.workbench_mail_tasks import (
        accept_mail_task,
        create_mail_task,
        submit_mail_task,
    )
    from agent_mailbox.workbench_store import WorkbenchStore

    home, endpoint, _, config = local
    store = WorkbenchStore(home)
    project = store.create_project("Synthetic endpoint relay", home.parent)
    a = store.create_employee("A", "codex", project["id"])
    b = store.create_employee("B", "codex", project["id"])
    session = create_session(store, a["id"], project["id"], "Synthetic")
    configs, servers, calls = [], [], [[], []]
    for index, (employee, kind) in enumerate([(a, review_host), (b, "deepseek")]):
        binding = set_binding(
            store,
            employee["id"],
            project["id"],
            host_kind=kind,
            host_session_id="synthetic_" + kind,
        )
        socket_path = endpoint.with_name(str(index) + ".sock")
        configs.append(
            {
                "binding_id": binding["id"],
                "employee_id": employee["id"],
                "host_kind": kind,
                "socket_path": str(socket_path),
            }
        )
        servers.append(listener(socket_path, "received", calls[index], kind=kind))
    config.write_text(json.dumps({"version": 1, "endpoints": configs}))
    old_message = None
    if historical_pending:
        old_message = store.send_message(
            project["id"], "Historical mail", "Before native notifications", recipient_id=a["id"]
        )["id"]
        # Model an upgraded legacy store: mail and a pending marker exist, but
        # there was no durable native notification intent in the old version.
        with store._transaction() as db:
            db.execute("DELETE FROM notifications WHERE state_hash=?", (old_message,))
        (home / "wake/claims").mkdir(parents=True, exist_ok=True)
        wake._mark_pending(home / "wake/claims" / old_message, a["id"])
    monkeypatch.setattr(wake, "hook_command", lambda home: None)
    ready, stop, observed = threading.Event(), threading.Event(), queue.Queue()
    actual_run = wake.run_once

    def record(*args, **kwargs):
        result = actual_run(*args, **kwargs)
        if result.get("woke"):
            observed.put(result["woke"])
        ready.set()
        return result

    monkeypatch.setattr(wake, "run_once", record)
    worker = threading.Thread(
        target=wake_redeem_loop,
        args=(store, home),
        kwargs={"interval": None, "stop": stop},
        daemon=True,
    )
    worker.start()
    try:
        assert ready.wait(3)
        task = create_mail_task(
            store,
            project["id"],
            "Task",
            "Synthetic instructions",
            a["id"],
            review_recipient_id=b["id"],
        )
        assert observed.get(timeout=3) == [a["id"]]
        assert calls[0][0]["fresh"] == [task["request_message_id"]]
        assert pending_ids(store, a["id"]) == []
        accept_mail_task(store, session["token"], task["id"])
        result = submit_mail_task(store, session["token"], task["id"], "Synthetic result")
        assert result["status"] == "review"
        assert observed.get(timeout=3) == [b["id"]]
        assert pending_ids(store, b["id"]) == []
        assert [c[0]["binding"]["host_kind"] for c in calls] == [review_host, "deepseek"]
        assert [len(c) for c in calls] == [1, 1]
        assert not (home / "wake/wake-hook.sh").exists()
        if old_message:
            assert (home / "wake/claims" / (old_message + ".pending")).exists()
            assert not (home / "wake/claims" / (old_message + ".ok")).exists()
    finally:
        stop.set()
        store.mail_wake_event.set()
        worker.join(3)
        for server, thread in servers:
            server.shutdown()
            thread.join(3)
            server.server_close()
    assert not worker.is_alive()
