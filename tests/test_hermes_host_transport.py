"""Gateway adapter contracts; synthetic sessions, no live host/model/credentials."""

import copy
import importlib.util
import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_mailbox import workbench_host_transport as transport
from agent_mailbox import workbench_wake as wake

if sys.platform == "win32":
    pytest.skip("Hermes Unix socket receiver is not a Windows transport", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "mailbox_hermes_plugin", ROOT / "integrations/hermes-mailbox-host/__init__.py"
)
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


def test_diagnostic_only_mode_has_no_delivery_journal_or_injection(monkeypatch):
    metadata = {"protocol": "agent-mailbox.hermes-routing.v1", "session_verified": False}
    monkeypatch.setattr(
        plugin, "live_factory", lambda ctx: SimpleNamespace(snapshot=lambda: metadata)
    )
    diagnostic = plugin.Diagnostics(SimpleNamespace())
    assert diagnostic.available() is True
    assert diagnostic.routing_context() == metadata
    assert diagnostic.probe()["gateway_injection_available"] is False
    assert diagnostic.probe()["pinned_sessions"] == 0
    assert not hasattr(diagnostic, "db")
    with pytest.raises(ValueError, match="diagnostics_cannot_receive"):
        diagnostic.receive({"delivery_id": "wake-synthetic"})
    diagnostic.close()
    assert diagnostic.available() is False


def test_diagnostic_registration_exposes_only_private_metadata_without_turn_hook(monkeypatch):
    import time

    metadata = {"protocol": "agent-mailbox.hermes-routing.v1", "session_verified": False}
    monkeypatch.setattr(
        plugin, "live_factory", lambda ctx: SimpleNamespace(snapshot=lambda: metadata)
    )
    with tempfile.TemporaryDirectory(prefix="am-discovery-", dir=Path("/tmp").resolve()) as tmp:
        address = str(Path(tmp) / "r.sock")
        callbacks, hooks = [], []

        def config(key):
            assert key in {"discovery_only", "socket_path"}
            return True if key == "discovery_only" else address

        ctx = SimpleNamespace(
            get_config=config,
            on_unload=callbacks.append,
            register_hook=lambda *args: hooks.append(args),
        )
        plugin.register(ctx)
        try:
            deadline = time.monotonic() + 3
            while not Path(address).exists() and time.monotonic() < deadline:
                time.sleep(0.01)

            def request(payload):
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(2)
                    client.connect(address)
                    client.sendall(json.dumps(payload).encode() + b"\n")
                    return json.loads(client.makefile("rb").readline())

            assert request({"operation": "routing_context"}) == metadata
            assert request({"operation": "probe"})["gateway_injection_available"] is False
            for payload in (
                {"delivery_id": "wake-synthetic"},
                {"operation": "enable"},
                {"operation": "routing_context", "profile": "foreign"},
            ):
                assert request(payload) == {"error": "delivery_unconfirmed"}
            assert hooks == []
            assert not list(Path(tmp).glob("*.sqlite3"))
        finally:
            for cleanup in callbacks:
                cleanup()
        assert not Path(address).exists()


def test_private_mode_refuses_unsupported_host_before_journal_or_registration(
    tmp_path, monkeypatch
):
    state = tmp_path / "state"
    hooks, platforms = [], []

    def refuse_host():
        raise ValueError("unsupported_hermes_dispatch_version")

    monkeypatch.setattr(plugin, "require_private_host", refuse_host)
    values = {"discovery_only": False, "private_work_session": True}
    ctx = SimpleNamespace(
        get_config=lambda key: values.get(key),
        register_hook=lambda *args: hooks.append(args),
        register_platform=lambda **kwargs: platforms.append(kwargs),
    )
    with pytest.raises(ValueError, match="unsupported_hermes_dispatch_version"):
        plugin.register(ctx)
    assert not state.exists()
    assert hooks == [] and platforms == []


def journal_dispatch_fixture(ctx, *, session_key, session_id, platform, still_authorized):
    """Journal-only scheduling double; real SDK pin tests live in test_hermes_pinned."""
    assert session_id == "existing_session" and platform == "feishu"
    return SimpleNamespace(
        current=still_authorized,
        schedule=lambda content: ctx.inject_message(content, session_key=session_key),
    )


@pytest.fixture
def setup(tmp_path):
    home = tmp_path / "home"
    (home / "wake").mkdir(parents=True)
    binding = {
        "id": "binding_test",
        "employee_id": "employee_test",
        "project_id": "project_test",
        "host_kind": "hermes",
        "host_session_id": "agent:main:feishu:dm:synthetic",
    }
    (home / "wake/host-bindings.json").write_text(json.dumps({"version": 1, "bindings": [binding]}))
    envelope = {
        "receipt_protocol": plugin.PROTOCOL,
        "employee_id": "employee_test",
        "delivery_id": "wake-test",
        "binding": binding,
        "fresh": ["message_test"],
    }
    calls = []
    manager = SimpleNamespace(
        _cli_ref=None, has_tui_message_injector=False, has_gateway_message_injector=True
    )
    ctx = SimpleNamespace(
        _manager=manager,
        _gateway_injection_allowed=lambda: True,
        inject_message=lambda content, **kwargs: calls.append((content, kwargs)) or True,
    )
    sessions = {
        binding["host_session_id"]: {"session_id": "existing_session", "platform": "feishu"}
    }
    receiver = plugin.Receiver(
        ctx, home, tmp_path / "state", sessions, pin_factory=journal_dispatch_fixture
    )
    yield SimpleNamespace(
        home=home,
        binding=binding,
        envelope=envelope,
        ctx=ctx,
        receiver=receiver,
        calls=calls,
        sessions=sessions,
        state=tmp_path / "state",
    )
    receiver.close()


@pytest.mark.parametrize("scheduling", [True, False, "exception"])
def test_schedule_is_not_receipt_and_never_reinjects(setup, scheduling):
    def inject(content, **kwargs):
        setup.calls.append((content, kwargs))
        if scheduling == "exception":
            raise RuntimeError("ambiguous scheduling")
        return scheduling

    setup.ctx.inject_message = inject
    for _ in range(3):
        assert setup.receiver.receive(setup.envelope, 0)["status"] == "unknown"
    assert len(setup.calls) == 1
    assert setup.calls[0][1] == {"session_key": setup.binding["host_session_id"]}


def test_health_probe_reports_unconfirmed_without_receipting_or_injecting(setup):
    def probe(address, payload):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(2)
            client.connect(address)
            client.sendall(json.dumps(payload).encode() + b"\n")
            return json.loads(client.makefile("rb").readline())

    with tempfile.TemporaryDirectory(prefix="am-health-", dir=Path("/tmp").resolve()) as directory:
        server = plugin.LocalServer(Path(directory) / "s", setup.receiver)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            before = probe(server.server_address, {"operation": "probe"})
            assert before == {
                "protocol": "agent-mailbox.host-health.v1",
                "host_kind": "hermes",
                "gateway_injection_available": True,
                "pinned_sessions": 1,
                "unconfirmed_deliveries": 0,
                "recorded_turn_receipts": 0,
            }
            exact = probe(server.server_address, {"operation": "probe", "binding": setup.binding})
            assert exact["route_available"] is True
            assert setup.calls == []
            assert setup.receiver.receive(setup.envelope, 0)["status"] == "unknown"
            pending = probe(server.server_address, {"operation": "probe"})
            assert pending["unconfirmed_deliveries"] == 1
            assert pending["recorded_turn_receipts"] == 0
            assert "status" not in pending and "host_session_id" not in pending
            # Invalid operations must not fall through to the delivery path.
            assert probe(server.server_address, {**setup.envelope, "operation": "unknown"}) == {
                "error": "delivery_unconfirmed"
            }
            assert len(setup.calls) == 1
            setup.receiver.observe_turn(
                session_id="existing_session", platform="feishu", user_message=setup.calls[0][0]
            )
            setup.ctx._gateway_injection_allowed = lambda: False
            after = probe(server.server_address, {"operation": "probe"})
            assert after["unconfirmed_deliveries"] == 0
            assert after["recorded_turn_receipts"] == 1
            assert after["gateway_injection_available"] is False
            assert (
                probe(server.server_address, {"operation": "probe", "binding": setup.binding})[
                    "route_available"
                ]
                is False
            )
            assert len(setup.calls) == 1
        finally:
            server.shutdown()
            worker.join(3)
            server.server_close()


def test_delivery_status_queries_only_exact_existing_journal(setup):
    with tempfile.TemporaryDirectory(prefix="am-status-", dir=Path("/tmp").resolve()) as directory:
        server = plugin.LocalServer(Path(directory) / "s", setup.receiver)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:

            def query(envelope):
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(2)
                    client.connect(server.server_address)
                    client.sendall(
                        json.dumps(
                            {
                                "operation": "delivery_status",
                                "envelope": envelope,
                            }
                        ).encode()
                        + b"\n"
                    )
                    return json.loads(client.makefile("rb").readline())

            assert query(setup.envelope)["status"] == "unknown"
            assert not setup.calls
            assert setup.receiver.receive(setup.envelope, 0)["status"] == "unknown"
            assert len(setup.calls) == 1
            assert query(setup.envelope)["status"] == "unknown"
            assert query({**setup.envelope, "fresh": ["message_other"]}) == {
                "error": "delivery_unconfirmed"
            }
            setup.receiver.observe_turn(
                session_id="existing_session",
                platform="feishu",
                user_message=setup.calls[0][0],
            )
            assert query(setup.envelope)["status"] == "received"
            (setup.home / "wake/host-bindings.json").write_text(
                json.dumps({"version": 1, "bindings": []})
            )
            assert query(setup.envelope)["status"] == "unknown"
            assert len(setup.calls) == 1
        finally:
            server.shutdown()
            worker.join(3)
            server.server_close()


def test_receipt_mirror_failure_keeps_durable_received_journal(setup, monkeypatch):
    assert setup.receiver.receive(setup.envelope, 0)["status"] == "unknown"

    def fail_mirror(_receipt):
        raise OSError("synthetic receipt directory failure")

    monkeypatch.setattr(setup.receiver, "write_receipt", fail_mirror)
    setup.receiver.observe_turn(
        session_id="existing_session",
        platform="feishu",
        user_message=setup.calls[0][0],
    )
    assert setup.receiver.delivery_status(setup.envelope)["status"] == "received"
    with sqlite3.connect(setup.state / "deliveries.sqlite3") as reopened:
        assert reopened.execute(
            "SELECT status FROM deliveries WHERE id='wake-test'"
        ).fetchone() == ("received",)
    assert not (setup.home / "wake/receipts/wake-test").exists()
    assert len(setup.calls) == 1


def test_database_failure_never_writes_received_mirror(setup):
    assert setup.receiver.receive(setup.envelope, 0)["status"] == "unknown"
    setup.receiver.db.execute(
        "CREATE TRIGGER deny_received BEFORE UPDATE ON deliveries "
        "BEGIN SELECT RAISE(FAIL, 'synthetic db failure'); END"
    )
    setup.receiver.db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="synthetic db failure"):
        setup.receiver.observe_turn(
            session_id="existing_session",
            platform="feishu",
            user_message=setup.calls[0][0],
        )
    assert setup.receiver.delivery_status(setup.envelope)["status"] == "unknown"
    assert not (setup.home / "wake/receipts/wake-test").exists()
    assert len(setup.calls) == 1


def test_wake_reconciles_real_gateway_journal_without_reinjection(setup, monkeypatch):
    assert setup.receiver.receive(setup.envelope, 0)["status"] == "unknown"
    assert len(setup.calls) == 1

    def fail_mirror(_receipt):
        raise OSError("synthetic receipt mirror loss")

    monkeypatch.setattr(setup.receiver, "write_receipt", fail_mirror)
    setup.receiver.observe_turn(
        session_id="existing_session",
        platform="feishu",
        user_message=setup.calls[0][0],
    )
    assert setup.receiver.delivery_status(setup.envelope)["status"] == "received"
    assert not (setup.home / "wake/receipts/wake-test").exists()

    claims = setup.home / "wake/claims"
    claims.mkdir()
    (claims / "message_test.unknown").write_text("wake-test")
    outbox = setup.home / "wake/wake-outbox"
    outbox.mkdir()
    (outbox / "wake-test.json").write_text(json.dumps(setup.envelope))

    with tempfile.TemporaryDirectory(
        prefix="am-reconcile-", dir=Path("/tmp").resolve()
    ) as directory:
        server = plugin.LocalServer(Path(directory) / "s", setup.receiver)
        endpoint_config = setup.home / "wake" / transport.CONFIG
        endpoint_config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "endpoints": [
                        {
                            "binding_id": setup.binding["id"],
                            "employee_id": setup.binding["employee_id"],
                            "host_kind": "hermes",
                            "socket_path": server.server_address,
                        }
                    ],
                }
            )
        )
        endpoint_config.chmod(0o600)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        monkeypatch.setattr(wake, "hook_command", lambda _home: ["legacy-must-not-run"])
        try:
            send = wake.hook_deliver(
                setup.home,
                runner=lambda *args, **kwargs: pytest.fail("must not redeliver unknown"),
                require_receipt=True,
                expected_binding=setup.binding,
            )
            assert send(setup.binding["employee_id"], {"fresh": ["message_test"]}) is True
            assert (claims / "message_test.ok").exists()
            assert not (claims / "message_test.unknown").exists()
            assert len(setup.calls) == 1
        finally:
            server.shutdown()
            worker.join(3)
            server.server_close()


def test_exact_original_turn_required_and_does_not_mark_read(setup):
    receiver = setup.receiver
    assert receiver.receive(setup.envelope, 0)["status"] == "unknown"
    content = setup.calls[0][0]
    for sid, platform, text in [
        ("wrong", "feishu", content),
        ("existing_session", "cli", content),
        ("existing_session", "feishu", "arbitrary text"),
    ]:
        receiver.observe_turn(session_id=sid, platform=platform, user_message=text)
        assert receiver.receive(setup.envelope, 0)["status"] == "unknown"
    receiver.observe_turn(session_id="existing_session", platform="feishu", user_message=content)
    receipt = receiver.receive(setup.envelope, 0)
    assert receipt["status"] == "received"
    assert receipt["host_session_id"] == setup.binding["host_session_id"]
    assert "read" not in receipt and "accepted" not in receipt
    assert len(setup.calls) == 1


@pytest.mark.parametrize("mode", ["cli", "tui", "no_gateway", "denied"])
def test_other_surfaces_and_disabled_permission_refused(setup, mode):
    if mode == "cli":
        setup.ctx._manager._cli_ref = object()
    elif mode == "tui":
        setup.ctx._manager.has_tui_message_injector = True
    elif mode == "no_gateway":
        setup.ctx._manager.has_gateway_message_injector = False
    else:
        setup.ctx._gateway_injection_allowed = lambda: False
    assert setup.receiver.receive(setup.envelope, 0)["status"] == "not_received"
    assert not setup.calls


def test_revocation_cannot_be_acknowledged(setup):
    setup.receiver.receive(setup.envelope, 0)
    (setup.home / "wake/host-bindings.json").write_text(json.dumps({"version": 1, "bindings": []}))
    setup.receiver.observe_turn(
        session_id="existing_session", platform="feishu", user_message=setup.calls[0][0]
    )
    assert setup.receiver.receive(setup.envelope, 0)["status"] == "unknown"


def test_duplicate_identity_conflict(setup):
    setup.receiver.receive(setup.envelope, 0)
    envelope = copy.deepcopy(setup.envelope)
    envelope["fresh"] = ["message_other"]
    with pytest.raises(ValueError, match="identity_conflict"):
        setup.receiver.receive(envelope, 0)
    assert len(setup.calls) == 1


def test_persist_before_injection_and_recover_late_receipt_after_restart(setup):
    setup.receiver.receive(setup.envelope, 0)
    other = plugin.Receiver(
        setup.ctx, setup.home, setup.state, setup.sessions, pin_factory=journal_dispatch_fixture
    )
    try:
        assert other.receive(setup.envelope, 0)["status"] == "unknown"
        assert len(setup.calls) == 1
        other.observe_turn(
            session_id="existing_session", platform="feishu", user_message=setup.calls[0][0]
        )
        assert other.receive(setup.envelope, 0)["status"] == "received"
    finally:
        other.close()


def test_concurrent_duplicate_has_one_injection(setup):
    barrier = threading.Barrier(5)
    results = []

    def send():
        barrier.wait()
        results.append(setup.receiver.receive(setup.envelope, 0))

    threads = [threading.Thread(target=send) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert len(results) == 5
    assert len(setup.calls) == 1


@pytest.mark.parametrize(
    "late_receipt", ["valid", "wrong_binding", "wrong_session", "wrong_messages", "not_received"]
)
def test_busy_turn_late_evidence_recovers_without_reinjection(setup, monkeypatch, late_receipt):
    monkeypatch.setattr(wake, "hook_command", lambda home: ["synthetic-transport"])
    transports = []

    def host(command, **kwargs):
        transports.append(command)
        envelope = json.loads((setup.home / "wake/wake-outbox" / f"{command[-1]}.json").read_text())
        assert setup.receiver.receive(envelope, 0)["status"] == "unknown"
        return SimpleNamespace(returncode=1)

    send = wake.hook_deliver(
        setup.home, runner=host, require_receipt=True, expected_binding=setup.binding
    )
    decision = {"fresh": ["message_test"], "reason": "busy"}
    assert not send("employee_test", decision)
    unknown = setup.home / "wake/claims/message_test.unknown"
    assert unknown.exists()
    # The actual target turn starts later, after the caller timed out.
    setup.receiver.observe_turn(
        session_id="existing_session", platform="feishu", user_message=setup.calls[0][0]
    )
    path = setup.home / "wake/receipts" / transports[0][-1]
    receipt = json.loads(path.read_text())
    if late_receipt != "valid":
        field, value = {
            "wrong_binding": ("binding_id", "revoked_binding"),
            "wrong_session": ("host_session_id", "other_session"),
            "wrong_messages": ("message_ids", ["other_message"]),
            "not_received": ("status", "not_received"),
        }[late_receipt]
        receipt[field] = value
        path.write_text(json.dumps(receipt))
    for _ in range(3):
        assert send("employee_test", decision) is (late_receipt == "valid")
    assert len(transports) == len(setup.calls) == 1
    assert unknown.exists() is (late_receipt != "valid")
    assert (setup.home / "wake/claims/message_test.ok").exists() is (late_receipt == "valid")


@pytest.mark.parametrize("change", ["session", "employee", "duplicate_messages", "protocol"])
def test_unbound_or_malformed_envelope_never_injected(setup, change):
    e = copy.deepcopy(setup.envelope)
    if change == "session":
        e["binding"]["host_session_id"] = "unbound_session"
    elif change == "employee":
        e["employee_id"] = "another_employee"
    elif change == "duplicate_messages":
        e["fresh"] *= 2
    else:
        e["receipt_protocol"] = "wrong"
    with pytest.raises(ValueError):
        setup.receiver.receive(e, 0)
    assert not setup.calls


def test_private_directory_rejects_world_readable_and_symlink(tmp_path):
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="private"):
        plugin.private_directory(public)
    link = tmp_path / "link"
    link.symlink_to(public, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        plugin.private_directory(link)


@pytest.mark.skipif(not shutil.which("node"), reason="shared Node transport required")
def test_shared_hook_transport_waits_for_real_turn_observer(setup):
    def inject(content, **kwargs):
        setup.calls.append((content, kwargs))
        setup.receiver.observe_turn(
            session_id="existing_session", platform="feishu", user_message=content
        )
        return True

    setup.ctx.inject_message = inject
    outbox = setup.home / "wake/wake-outbox"
    outbox.mkdir()
    (outbox / "wake-test.json").write_text(json.dumps(setup.envelope))
    # macOS's /tmp is a symlink. Canonicalize before private path validation.
    with tempfile.TemporaryDirectory(prefix="am-hs-", dir=Path("/tmp").resolve()) as directory:
        server = plugin.LocalServer(Path(directory) / "s", setup.receiver)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = ROOT / "integrations/dsh-mailbox-host/client.mjs"
            result = subprocess.run(
                [
                    shutil.which("node"),
                    str(client),
                    str(setup.home),
                    server.server_address,
                    "employee_test",
                    "wake-test",
                ],
                capture_output=True,
                text=True,
                timeout=12,
                check=False,
            )
            assert result.returncode == 0, result.stderr
            receipt = json.loads((setup.home / "wake/receipts/wake-test").read_text())
            assert receipt["status"] == "received" and receipt["host_kind"] == "hermes"
            assert len(setup.calls) == 1
        finally:
            server.shutdown()
            thread.join(3)
            server.server_close()


def test_default_factory_refuses_unverified_host_without_public_fallback(setup):
    receiver = plugin.Receiver(setup.ctx, setup.home, setup.state / "default", setup.sessions)
    try:
        assert receiver.receive(setup.envelope, 0)["status"] == "not_received"
        assert setup.calls == []
        assert receiver.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
    finally:
        receiver.close()


def test_closed_receiver_cannot_dispatch_or_record_late_hook(setup):
    setup.receiver.close()
    assert setup.receiver.receive(setup.envelope, 0)["status"] == "not_received"
    setup.receiver.observe_turn(
        session_id="existing_session", user_message="late", platform="feishu"
    )
    assert setup.calls == []
