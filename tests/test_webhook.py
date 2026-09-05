"""Tests for the webhook wake-up: config resolution, signature, URL pinning,
and the store.send() -> webhook integration."""

import hashlib
import hmac
import json
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
    server = HTTPServer(("127.0.0.1", 0), _Sink)
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
