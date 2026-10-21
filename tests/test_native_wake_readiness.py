"""A stopped original receiver must not create a delivery every recovery scan."""

import json
import os
import socket
import socketserver
import tempfile
import threading
import time
from pathlib import Path

import pytest

from agent_mailbox.workbench_host_activity import PROTOCOL, start_host_activity_listener
from agent_mailbox.workbench_host_bindings import resolve_routes, set_binding
from agent_mailbox.workbench_mail_notifications import mark_host_received, pending_ids, ready_ids
from agent_mailbox.workbench_mail_tasks import create_mail_task
from agent_mailbox.workbench_store import WorkbenchStore
from agent_mailbox.workbench_wake import hook_deliver, run_once, unread_ids_for_employee

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Unix receiver only")


def test_codex_idle_event_retries_recent_pending_once_without_new_mailbox(
    tmp_path,
    private_ipc,
    monkeypatch,
):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Synthetic", tmp_path)
    employee = store.create_employee("Codex App", "codex", project["id"])
    binding = set_binding(
        store, employee["id"], project["id"], host_kind="codex", host_session_id="original-thread"
    )
    mid = create_mail_task(store, project["id"], "Review", "Synthetic", employee["id"])[
        "request_message_id"
    ]
    claims = store.root / "wake/claims"
    claims.mkdir(parents=True, exist_ok=True)
    (claims / f"{mid}.pending").touch()
    monkeypatch.setattr(
        "agent_mailbox.workbench_host_transport.configured_for_binding",
        lambda _home, _binding: True,
    )
    monkeypatch.setattr(
        "agent_mailbox.workbench_host_transport.receiver_online", lambda _home, _binding: True
    )
    assert ready_ids(store, employee["id"]) == []
    store.mail_wake_event.clear()
    close = start_host_activity_listener(store, private_ipc / "idle.sock")
    try:

        def signal(thread_id):
            with socket.socket(socket.AF_UNIX) as client:
                client.connect(str(private_ipc / "idle.sock"))
                client.sendall(
                    (
                        json.dumps(
                            {"protocol": PROTOCOL, "kind": "turn_completed", "thread_id": thread_id}
                        )
                        + "\n"
                    ).encode()
                )

        signal("another-thread")
        assert not store.mail_wake_event.wait(0.1)
        signal(binding["host_session_id"])
        assert store.mail_wake_event.wait(1)
        with store.mail_wake_ready_lock:
            hints = frozenset(store.mail_wake_ready_threads)
            store.mail_wake_ready_threads.clear()
        assert hints == {"original-thread"}
        assert ready_ids(store, employee["id"], ready_threads=hints) == [mid]
        assert ready_ids(store, employee["id"]) == []
        assert len(list(store.root.glob("wake/wake-outbox/*.json"))) == 0
    finally:
        close()


@pytest.fixture
def private_ipc():
    with tempfile.TemporaryDirectory(prefix="am-rdy-", dir="/private/tmp") as raw:
        yield Path(raw)


def test_offline_receiver_stays_pending_without_recovery_storm_then_reconnects(
    tmp_path, private_ipc
):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Synthetic", tmp_path)
    employee = store.create_employee("Worker", "codex", project["id"])
    binding = set_binding(
        store,
        employee["id"],
        project["id"],
        host_kind="codex",
        host_session_id="existing-thread",
    )
    endpoint = private_ipc / "receiver.sock"
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
    task = create_mail_task(store, project["id"], "Review", "Synthetic", employee["id"])
    mid = task["request_message_id"]

    def drain():
        return run_once(
            store,
            store.root,
            employee_ids=[employee["id"]],
            unread_provider=unread_ids_for_employee,
            pending_provider=ready_ids,
            acknowledge=mark_host_received,
            event_driven=True,
            deliver=hook_deliver(
                store.root,
                require_receipt=True,
                include_pending=False,
                binding_resolver=lambda eid, mids: resolve_routes(store, eid, mids),
            ),
        )

    for _ in range(3):
        assert drain()["woke"] == []
    assert pending_ids(store, employee["id"]) == [mid]
    assert list((store.root / "wake/wake-outbox").glob("*.json")) == []

    # Lose the in-memory commit signal while the Host is absent. The durable
    # intent must still be eligible when a new Workbench process starts.
    store = WorkbenchStore(store.root)
    assert pending_ids(store, employee["id"]) == [mid]
    assert ready_ids(store, employee["id"]) == []

    received = []
    host_status = ["received"]
    health_status = ["legacy"]

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            raw = self.rfile.readline()
            if not raw:  # The liveness probe sends no mail.
                return
            payload = json.loads(raw)
            if payload.get("operation") == "health":
                if health_status[0] == "legacy":
                    self.wfile.write(b'{"error":"delivery_unconfirmed"}\n')
                    return
                self.wfile.write(
                    (
                        json.dumps(
                            {
                                "protocol": "agent-mailbox.receiver-health.v1",
                                "request_id": payload["request_id"],
                                "thread_id": payload["thread_id"],
                                "loaded": health_status[0] == "available",
                                "admission_available": health_status[0] == "available",
                            }
                        )
                        + "\n"
                    ).encode()
                )
                return
            received.append(payload)
            route = payload["binding"]
            self.wfile.write(
                (
                    json.dumps(
                        {
                            "protocol": "agent-mailbox.host-receipt.v1",
                            "delivery_id": payload["delivery_id"],
                            "employee_id": payload["employee_id"],
                            "message_ids": payload["fresh"],
                            "binding_id": route["id"],
                            "project_id": route["project_id"],
                            "host_kind": route["host_kind"],
                            "host_session_id": route["host_session_id"],
                            "status": host_status[0],
                        }
                    )
                    + "\n"
                ).encode()
            )

    server = socketserver.ThreadingUnixStreamServer(str(endpoint), Handler)
    endpoint.chmod(0o600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert drain()["woke"] == []
        assert pending_ids(store, employee["id"]) == [mid]
        assert list((store.root / "wake/wake-outbox").glob("*.json")) == []
        health_status[0] = "unavailable"
        assert drain()["woke"] == []
        assert list((store.root / "wake/wake-outbox").glob("*.json")) == []
        health_status[0] = "available"
        assert drain()["woke"] == [employee["id"]]
        assert pending_ids(store, employee["id"]) == []
        assert [payload["fresh"] for payload in received] == [[mid]]
        assert drain()["woke"] == []
        assert len(received) == 1

        followup = create_mail_task(store, project["id"], "Follow-up", "Synthetic", employee["id"])[
            "request_message_id"
        ]
        host_status[0] = "not_received"
        assert drain()["woke"] == []
        assert pending_ids(store, employee["id"]) == [followup]
        outbox = store.root / "wake/wake-outbox"
        attempted = len(list(outbox.glob("*.json")))
        marker = store.root / "wake/claims" / f"{followup}.pending"
        # Keep this assertion independent of how long the surrounding socket
        # suite takes; the retry boundary itself is exercised below.
        deferred = time.time() + 3600
        os.utime(marker, (deferred, deferred))
        assert drain()["woke"] == []
        assert len(list(outbox.glob("*.json"))) == attempted

        old = time.time() - 31
        os.utime(marker, (old, old))
        host_status[0] = "received"
        assert drain()["woke"] == [employee["id"]]
        assert pending_ids(store, employee["id"]) == []
        assert [payload["fresh"] for payload in received] == [[mid], [followup], [followup]]
    finally:
        server.shutdown()
        thread.join(3)
        server.server_close()


@pytest.mark.parametrize(
    "receiver_error", [None, "ambiguous_live_codex_launchers", "owner_private_socket_required"]
)
def test_one_offline_native_project_does_not_block_another_legacy_project(
    tmp_path, private_ipc, monkeypatch, receiver_error
):
    store = WorkbenchStore(tmp_path / "home")
    first = store.create_project("Native", tmp_path)
    second = store.create_project("Legacy", tmp_path)
    employee = store.create_employee("Worker", "codex", first["id"])
    store.add_project_member(second["id"], employee["id"])
    native = set_binding(
        store, employee["id"], first["id"], host_kind="codex", host_session_id="native-thread"
    )
    set_binding(
        store, employee["id"], second["id"], host_kind="synthetic", host_session_id="legacy-thread"
    )
    config = store.root / "wake/host-endpoints.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "endpoints": [
                    {
                        "binding_id": native["id"],
                        "employee_id": employee["id"],
                        "host_kind": "codex",
                        "socket_path": str(private_ipc / "absent" / "receiver.sock"),
                    }
                ],
            }
        )
    )
    config.chmod(0o600)
    hook = store.root / "wake/wake-hook.sh"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o700)
    native_mid = create_mail_task(store, first["id"], "Native", "Synthetic", employee["id"])[
        "request_message_id"
    ]
    legacy_mid = create_mail_task(store, second["id"], "Legacy", "Synthetic", employee["id"])[
        "request_message_id"
    ]
    if receiver_error:
        from agent_mailbox import workbench_host_transport as transport

        def invalid_receiver(home, binding):
            assert binding["id"] == native["id"]
            raise ValueError(receiver_error)

        monkeypatch.setattr(transport, "receiver_online", invalid_receiver)
    assert pending_ids(store, employee["id"]) == [native_mid, legacy_mid]
    assert ready_ids(store, employee["id"]) == [legacy_mid]
    # A rejected native route must remain pending, never fall back to the hook.
    assert pending_ids(store, employee["id"]) == [native_mid, legacy_mid]


def test_hermes_readiness_requires_a_positive_health_probe(tmp_path, private_ipc):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Synthetic", tmp_path)
    employee = store.create_employee("Worker", "hermes", project["id"])
    binding = set_binding(
        store, employee["id"], project["id"], host_kind="hermes", host_session_id="existing-session"
    )
    endpoint = private_ipc / "hermes.sock"
    config = store.root / "wake/host-endpoints.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "endpoints": [
                    {
                        "binding_id": binding["id"],
                        "employee_id": employee["id"],
                        "host_kind": "hermes",
                        "socket_path": str(endpoint),
                    }
                ],
            }
        )
    )
    config.chmod(0o600)
    mid = create_mail_task(store, project["id"], "Review", "Synthetic", employee["id"])[
        "request_message_id"
    ]
    state = ["error"]
    requests = []

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            payload = json.loads(self.rfile.readline())
            requests.append(payload)
            if state[0] == "error":
                response = {"error": "delivery_unconfirmed"}
            else:
                response = {
                    "protocol": "agent-mailbox.host-health.v1",
                    "host_kind": "hermes",
                    "gateway_injection_available": state[0] in {"available", "stale_pin"},
                    "route_available": state[0] == "available",
                }
            self.wfile.write((json.dumps(response) + "\n").encode())

    server = socketserver.ThreadingUnixStreamServer(str(endpoint), Handler)
    endpoint.chmod(0o600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert ready_ids(store, employee["id"]) == []
        state[0] = "unavailable"
        assert ready_ids(store, employee["id"]) == []
        state[0] = "stale_pin"
        assert ready_ids(store, employee["id"]) == []
        state[0] = "available"
        assert ready_ids(store, employee["id"]) == [mid]
        assert requests == [{"operation": "probe", "binding": binding}] * 4
        assert list((store.root / "wake/wake-outbox").glob("*.json")) == []
    finally:
        server.shutdown()
        thread.join(3)
        server.server_close()


@pytest.mark.parametrize("entry", ("native", "cli"))
def test_unknown_delivery_does_not_block_other_mail_and_late_receipt_reconciles(
    tmp_path,
    private_ipc,
    entry,
):
    """One unconfirmed host effect must neither replay nor poison another letter."""
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Synthetic", tmp_path)
    employee = store.create_employee("Recipient", "hermes", project["id"])
    binding = set_binding(
        store, employee["id"], project["id"], host_kind="hermes", host_session_id="existing-session"
    )
    endpoint = private_ipc / "receiver.sock"
    config = store.root / "wake/host-endpoints.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "endpoints": [
                    {
                        "binding_id": binding["id"],
                        "employee_id": employee["id"],
                        "host_kind": "hermes",
                        "socket_path": str(endpoint),
                    }
                ],
            }
        )
    )
    config.chmod(0o600)
    uncertain = store.send_message(
        project["id"], "Unknown", "Synthetic", recipient_id=employee["id"]
    )["id"]
    ordinary = store.send_message(
        project["id"], "Independent", "Synthetic", recipient_id=employee["id"]
    )["id"]
    wake = store.root / "wake"
    claims = wake / "claims"
    claims.mkdir(exist_ok=True)
    outbox = wake / "wake-outbox"
    outbox.mkdir(exist_ok=True)
    delivery_id = "wake-synthetic-unknown"
    (claims / f"{uncertain}.unknown").write_text(delivery_id)
    (outbox / f"{delivery_id}.json").write_text(
        json.dumps(
            {
                "receipt_protocol": "agent-mailbox.host-receipt.v1",
                "delivery_id": delivery_id,
                "employee_id": employee["id"],
                "binding": binding,
                "fresh": [uncertain],
            }
        )
    )
    health = [True]
    delivered = []

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            payload = json.loads(self.rfile.readline())
            if payload.get("operation") == "probe":
                response = {
                    "protocol": "agent-mailbox.host-health.v1",
                    "host_kind": "hermes",
                    "gateway_injection_available": health[0],
                    "route_available": health[0],
                }
            else:
                delivered.append(payload["fresh"])
                route = payload["binding"]
                response = {
                    "protocol": "agent-mailbox.host-receipt.v1",
                    "delivery_id": payload["delivery_id"],
                    "employee_id": payload["employee_id"],
                    "message_ids": payload["fresh"],
                    "binding_id": route["id"],
                    "project_id": route["project_id"],
                    "host_kind": route["host_kind"],
                    "host_session_id": route["host_session_id"],
                    "status": "received",
                }
            self.wfile.write((json.dumps(response) + "\n").encode())

    def drain(now=None):
        return run_once(
            store,
            store.root,
            employee_ids=[employee["id"]],
            unread_provider=unread_ids_for_employee,
            pending_provider=ready_ids if entry == "native" else pending_ids,
            acknowledge=mark_host_received,
            event_driven=entry == "native",
            now=now,
            deliver=hook_deliver(
                store.root,
                require_receipt=True,
                include_pending=entry == "cli",
                binding_resolver=lambda eid, mids: resolve_routes(store, eid, mids),
            ),
        )

    server = socketserver.ThreadingUnixStreamServer(str(endpoint), Handler)
    endpoint.chmod(0o600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # The original unknown may be queried for its durable host journal,
        # while the independent fresh message must still be delivered.
        assert ready_ids(store, employee["id"]) == [uncertain, ordinary]
        assert drain()["woke"] == [employee["id"]]
        assert delivered == [[ordinary]]
        assert pending_ids(store, employee["id"]) == [uncertain]
        assert (claims / f"{uncertain}.unknown").exists()

        receipts = wake / "receipts"
        receipts.mkdir(exist_ok=True)
        late_receipt = {
            "protocol": "agent-mailbox.host-receipt.v1",
            "delivery_id": delivery_id,
            "employee_id": employee["id"],
            "message_ids": [uncertain],
            "binding_id": binding["id"],
            "project_id": project["id"],
            "host_kind": binding["host_kind"],
            "host_session_id": binding["host_session_id"],
            "status": "received",
        }
        health[0] = False  # Existing proof is usable even if the host is now offline.
        (receipts / delivery_id).write_text(
            json.dumps(
                {
                    **late_receipt,
                    "host_session_id": "different-session",
                }
            )
        )
        assert ready_ids(store, employee["id"]) == []
        assert pending_ids(store, employee["id"]) == [uncertain]
        assert delivered == [[ordinary]]
        (receipts / delivery_id).write_text(json.dumps(late_receipt))
        assert ready_ids(store, employee["id"]) == [uncertain]
        assert drain(now=time.time() + 1000 if entry == "cli" else None)["woke"] == [employee["id"]]
        assert delivered == [[ordinary]]  # Reconciliation is not another host call.
        assert pending_ids(store, employee["id"]) == []
        assert not (claims / f"{uncertain}.unknown").exists()
    finally:
        server.shutdown()
        thread.join(3)
        server.server_close()
