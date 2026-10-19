"""Private launcher discovery: real Unix listeners, no Codex or model calls."""

import json
import os
import socket
import socketserver
import tempfile
import threading
from pathlib import Path

import pytest

from agent_mailbox import workbench_host_transport as transport
from agent_mailbox import workbench_wake as wake

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Unix receiver only")


@pytest.fixture
def routing():
    with tempfile.TemporaryDirectory(prefix="am-disc-", dir=Path("/tmp").resolve()) as raw:
        root = Path(raw)
        home, base = root / "home", root / "ipc"
        (home / "wake").mkdir(parents=True)
        base.mkdir(mode=0o700)
        binding = {
            "id": "binding_a",
            "employee_id": "employee_a",
            "project_id": "project_a",
            "host_kind": "codex",
            "host_session_id": "original-thread",
        }
        (home / "wake/host-bindings.json").write_text(
            json.dumps({"version": 1, "bindings": [binding]})
        )
        config = home / "wake/host-endpoints.json"
        config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "endpoints": [
                        {
                            "binding_id": binding["id"],
                            "employee_id": binding["employee_id"],
                            "host_kind": "codex",
                            "launcher_directory": str(base),
                        }
                    ],
                }
            )
        )
        config.chmod(0o600)
        sockets = []

        def host(index=1, *, live=True, other_home=False):
            name = f"{index}-123456789abc"
            endpoint = base / (name + ".mail.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(endpoint))
            endpoint.chmod(0o600)
            if live:
                server.listen(16)
                sockets.append(server)
            else:
                server.close()  # Crash-like stale inode, no listening process.
            manifest = base / (name + ".json")
            manifest.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "notification_socket": str(endpoint),
                        "mailbox_home": str(root / "other" if other_home else home.resolve()),
                    }
                )
            )
            manifest.chmod(0o600)
            return endpoint, manifest

        yield home, base, binding, config, host
        for s in sockets:
            s.close()


def test_restart_finds_new_socket_without_rewriting_owner_config(routing):
    home, _, binding, config, host = routing
    original = config.read_bytes()
    assert transport.configured(home, binding["employee_id"])
    assert transport.endpoint_for(home, binding) is None
    host(1, live=False)
    second, _ = host(2)
    assert transport.endpoint_for(home, binding) == str(second)
    assert config.read_bytes() == original


def test_absent_receiver_does_not_fall_back_to_legacy_hook(routing, monkeypatch):
    home, _, binding, _, _ = routing
    monkeypatch.setattr(wake, "hook_command", lambda h: ["must-not-run"])
    send = wake.hook_deliver(
        home,
        require_receipt=True,
        expected_binding=binding,
        runner=lambda *a, **kw: pytest.fail("native discovery must not invoke a legacy hook"),
    )
    assert send(binding["employee_id"], {"fresh": ["message_a"]}) is False
    assert (home / "wake/claims/message_a.pending").exists()
    assert not (home / "wake/claims/message_a.ok").exists()


def test_two_live_launchers_fail_closed(routing):
    home, base, binding, _, host = routing
    host(1)
    host(2)
    # Without a bound thread, discovery cannot choose between live launchers.
    with pytest.raises(ValueError, match="ambiguous"):
        transport._codex_launcher_endpoint(home, base)
    # A bound thread absent from both launchers also stays undelivered.
    assert transport.endpoint_for(home, binding) is None


def test_two_launchers_select_only_the_bound_original_thread(routing, monkeypatch):
    home, _, binding, _, host = routing
    wrong, _ = host(1)
    original, _ = host(2)
    seen = []

    def health(path, thread_id):
        seen.append((Path(path), thread_id))
        return Path(path) == original and thread_id == binding["host_session_id"]

    monkeypatch.setattr(transport, "_codex_health", health)
    assert transport.endpoint_for(home, binding) == str(original)
    assert set(seen) == {(wrong, "original-thread"), (original, "original-thread")}
    assert len(seen) == 2


def test_two_launchers_without_unique_original_thread_remain_closed(routing, monkeypatch):
    home, _, binding, _, host = routing
    host(1)
    host(2)
    monkeypatch.setattr(transport, "_codex_health", lambda *_: False)
    assert transport.endpoint_for(home, binding) is None
    monkeypatch.setattr(transport, "_codex_health", lambda *_: True)
    with pytest.raises(ValueError, match="ambiguous"):
        transport.endpoint_for(home, binding)


def test_two_real_sockets_deliver_only_to_the_loaded_original_thread(routing):
    home, base, binding, _, _ = routing
    received = []
    servers = []

    def start(index, loaded_thread):
        endpoint = base / f"{index}-123456789abc.mail.sock"

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                raw = self.rfile.readline()
                if not raw:
                    return
                request = json.loads(raw)
                if request.get("operation") == "health":
                    if request["thread_id"] == loaded_thread:
                        answer = {
                            "protocol": transport.CODEX_HEALTH,
                            "request_id": request["request_id"],
                            "thread_id": loaded_thread,
                            "loaded": True,
                            "admission_available": True,
                        }
                        self.wfile.write((json.dumps(answer) + "\n").encode())
                    return
                received.append((index, request))
                answer = {
                    "protocol": transport.PROTOCOL,
                    "delivery_id": request["delivery_id"],
                    "employee_id": request["employee_id"],
                    "message_ids": request["fresh"],
                    "binding_id": request["binding"]["id"],
                    "project_id": request["binding"]["project_id"],
                    "host_kind": request["binding"]["host_kind"],
                    "host_session_id": request["binding"]["host_session_id"],
                    "status": "received",
                }
                self.wfile.write((json.dumps(answer) + "\n").encode())

        server = socketserver.UnixStreamServer(str(endpoint), Handler)
        endpoint.chmod(0o600)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        manifest = base / f"{index}-123456789abc.json"
        manifest.write_text(
            json.dumps(
                {
                    "version": 1,
                    "notification_socket": str(endpoint),
                    "mailbox_home": str(home.resolve()),
                }
            )
        )
        manifest.chmod(0o600)
        return endpoint

    try:
        start(1, "another-thread")
        original = start(2, "original-thread")
        assert transport.endpoint_for(home, binding) == str(original)
        payload = {
            "receipt_protocol": transport.PROTOCOL,
            "delivery_id": "wake-synthetic-original",
            "employee_id": binding["employee_id"],
            "binding": binding,
            "fresh": ["message_synthetic"],
        }
        transport.deliver(home, original, payload)
        assert [index for index, _ in received] == [2]
        receipt = json.loads((home / "wake/receipts/wake-synthetic-original").read_text())
        assert receipt["status"] == "received"
        assert receipt["host_session_id"] == "original-thread"
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def test_other_home_is_never_selected(routing):
    home, _, binding, _, host = routing
    host(other_home=True)
    assert transport.endpoint_for(home, binding) is None


@pytest.mark.parametrize(
    "fault",
    [
        "public_directory",
        "linked_directory",
        "public_manifest",
        "linked_manifest",
        "fifo_manifest",
        "foreign_socket",
        "public_socket",
        "linked_socket",
        "oversized_manifest",
    ],
)
def test_unsafe_discovery_paths_fail_closed(routing, fault):
    home, base, binding, config, host = routing
    endpoint, manifest = host()
    if fault == "public_directory":
        base.chmod(0o755)
    elif fault == "linked_directory":
        alias = base.with_name("alias")
        alias.symlink_to(base, target_is_directory=True)
        data = json.loads(config.read_text())
        data["endpoints"][0]["launcher_directory"] = str(alias)
        config.write_text(json.dumps(data))
    elif fault == "public_manifest":
        manifest.chmod(0o644)
    elif fault == "linked_manifest":
        target = base / "saved-manifest"
        manifest.rename(target)
        manifest.symlink_to(target)
    elif fault == "fifo_manifest":
        manifest.unlink()
        os.mkfifo(manifest, 0o600)
    elif fault == "foreign_socket":
        data = json.loads(manifest.read_text())
        data["notification_socket"] = str(base / "foreign.sock")
        manifest.write_text(json.dumps(data))
    elif fault == "public_socket":
        endpoint.chmod(0o666)
    elif fault == "linked_socket":
        target = base / "saved.sock"
        endpoint.rename(target)
        endpoint.symlink_to(target)
    else:
        manifest.write_text(" " * 16385)
    with pytest.raises((ValueError, OSError)):
        transport.endpoint_for(home, binding)


@pytest.mark.parametrize("fault", ["both", "wrong_host", "relative"])
def test_discovery_configuration_is_explicit(routing, fault):
    home, _, _, config, _ = routing
    data = json.loads(config.read_text())
    entry = data["endpoints"][0]
    if fault == "both":
        entry["socket_path"] = "/tmp/other.sock"
    elif fault == "wrong_host":
        entry["host_kind"] = "hermes"
    else:
        entry["launcher_directory"] = "relative"
    config.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        transport.endpoints(home)


def test_revoked_binding_stays_revoked_with_discovery(routing):
    home, _, binding, _, host = routing
    host()
    (home / "wake/host-bindings.json").write_text(json.dumps({"version": 1, "bindings": []}))
    with pytest.raises(ValueError, match="revoked_or_changed"):
        transport.endpoint_for(home, binding)


def test_legacy_manifest_requires_explicit_new_launcher(routing):
    home, _, binding, _, host = routing
    _, manifest = host()
    data = json.loads(manifest.read_text())
    del data["mailbox_home"]
    manifest.write_text(json.dumps(data))
    assert transport.endpoint_for(home, binding) is None


def test_manifest_scan_is_bounded(routing):
    home, base, binding, _, _ = routing
    for i in range(17):
        (base / f"{i}-123456789abc.json").write_text("{}")
    with pytest.raises(ValueError, match="too_many"):
        transport.endpoint_for(home, binding)
