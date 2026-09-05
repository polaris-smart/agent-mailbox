"""Tests for the webhook wake-up: config resolution, signature, URL pinning,
and the store.send() -> webhook integration."""

import hashlib
import hmac
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from agent_mailbox.store import MailStore
from agent_mailbox.webhook import (
    EVENT_TYPE,
    SIGNATURE_HEADER,
    _sign,
    _validate_url,
    load_config,
    post_message,
)


class _SinkServer(HTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind() calls socket.getfqdn(), which can stall
        # ~30s on CI runners with broken reverse DNS. Bind without it.
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(self.server_address)
        host, port = self.socket.getsockname()[:2]
        self.server_address = (host, port)
        self.server_name, self.server_port = host, port


class _Sink(BaseHTTPRequestHandler):
    received: ClassVar[list[tuple[dict, bytes]]] = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _Sink.received.append((dict(self.headers), body))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"accepted"}')

    def log_message(self, *args):  # silence test output
        pass


@pytest.fixture()
def sink():
    _Sink.received = []
    server = _SinkServer(("127.0.0.1", 0), _Sink)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", _Sink.received
    server.shutdown()


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ------------------------------------------------------------------ signature

def test_signature_is_hmac_sha256_hex():
    expected = "sha256=" + hmac.new(b"s3cret", b"payload", hashlib.sha256).hexdigest()
    assert _sign("s3cret", b"payload") == expected


# ------------------------------------------------------------------ config

def test_load_config_env_overrides_file(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MAIL_WEBHOOK_URL", "http://127.0.0.1:1/hook")
    monkeypatch.setenv("AGENT_MAIL_HOME", str(tmp_path))  # no config file here
    assert load_config() == ("http://127.0.0.1:1/hook", "")


def test_load_config_none_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_MAIL_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("AGENT_MAIL_HOME", str(tmp_path))  # no config file here
    assert load_config() is None


# ------------------------------------------------------------------ send integration

def test_send_triggers_signed_webhook(sink, tmp_path, monkeypatch):
    url, received = sink
    monkeypatch.setenv("AGENT_MAIL_WEBHOOK_URL", url)
    monkeypatch.setenv("AGENT_MAIL_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("AGENT_MAIL_HOME", str(tmp_path))

    st = MailStore(root=tmp_path / "mail")
    st.register("HS")
    st.send(from_id="ZC", to="HS", subject="wake up", body="ping")

    assert _wait_for(lambda: len(received) == 1), "webhook never received the message"
    headers, body = received[0]
    assert headers.get(SIGNATURE_HEADER) == _sign("s3cret", body)
    payload = json.loads(body)
    assert payload["event"] == EVENT_TYPE
    assert payload["event_type"] == EVENT_TYPE  # gateway contract key
    assert payload["message"]["subject"] == "wake up"


def test_send_survives_dead_webhook(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MAIL_WEBHOOK_URL", "http://127.0.0.1:1/nowhere")
    monkeypatch.setenv("AGENT_MAIL_HOME", str(tmp_path))

    st = MailStore(root=tmp_path / "mail")
    st.register("HS")
    out = st.send(from_id="ZC", to="HS", subject="still lands", body="mail persists")
    # send() itself succeeds; the dead webhook is swallowed best-effort
    assert out[0]["to"] == "HS"
    assert (st.root / "inbox" / "HS" / f"{out[0]['id']}.json").exists()


def test_post_message_direct(sink):
    url, received = sink
    ok = post_message(url, "", {"id": "m1"})
    assert ok is True
    assert json.loads(received[0][1])["message"]["id"] == "m1"


# ------------------------------------------------------------------ pinning

def test_validate_rejects_non_http_scheme():
    with pytest.raises(ValueError):
        _validate_url("ftp://127.0.0.1/x")


def test_validate_rejects_unresolvable_host():
    with pytest.raises(ValueError):
        _validate_url("http://this-host-does-not-exist.invalid/x")


def test_validate_rejects_public_target_without_opt_in():
    with pytest.raises(ValueError):
        _validate_url("http://example.com/hook")


def test_validate_allows_loopback():
    _validate_url("http://localhost:8644/webhooks/agent-mailbox")
    _validate_url("http://127.0.0.1:9999/")


# ------------------------------------------------------- config/root binding

def test_custom_root_store_does_not_read_other_home_webhook(sink, tmp_path, monkeypatch):
    """Regression (2026-09-06 phantom-notification incident): a store built on
    a custom root must not pick up the ambient home's webhook.json and wake
    the production gateway with throw-away mail."""
    url, received = sink
    monkeypatch.delenv("AGENT_MAIL_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("AGENT_MAIL_WEBHOOK_SECRET", raising=False)
    # a "production" home whose webhook.json points at the sink
    prod = tmp_path / "prod-home"
    prod.mkdir()
    (prod / "webhook.json").write_text(json.dumps({"url": url, "secret": "s3cret"}))
    monkeypatch.setenv("AGENT_MAIL_HOME", str(prod))

    st = MailStore(root=tmp_path / "scratch-root")  # explicit root != prod home
    st.register("HS")
    st.send(from_id="ZC", to="HS", subject="m3-1", body="burst test")

    time.sleep(0.3)  # would have landed by now if the leak were still there
    assert not received, "custom-root store leaked a notification to the production webhook"


def test_store_root_webhook_config_is_used(sink, tmp_path, monkeypatch):
    """Positive side of the binding: webhook.json inside the store's own root
    is honoured even when AGENT_MAIL_HOME points elsewhere."""
    url, received = sink
    monkeypatch.delenv("AGENT_MAIL_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("AGENT_MAIL_WEBHOOK_SECRET", raising=False)
    root = tmp_path / "mail"
    root.mkdir()
    (root / "webhook.json").write_text(json.dumps({"url": url, "secret": "s3cret"}))
    monkeypatch.setenv("AGENT_MAIL_HOME", str(tmp_path / "unrelated"))

    st = MailStore(root=root)
    st.register("HS")
    st.send(from_id="ZC", to="HS", subject="wake up", body="ping")
    assert _wait_for(lambda: len(received) == 1), "webhook.json in store root was ignored"


def test_send_appends_sent_log_audit(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_MAIL_WEBHOOK_URL", raising=False)
    st = MailStore(root=tmp_path / "mail")
    st.register("HS")
    st.send(from_id="ZC", to="HS", subject="audited", body="x")
    lines = (tmp_path / "mail" / "sent.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["subject"] == "audited" and entry["to"] == "HS"
