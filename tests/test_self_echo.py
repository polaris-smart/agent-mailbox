"""B4 self-echo protection: R1 default suppression + R2 [echo] prefix on
opt-in delivery, per the 2026-09-09 requirement (acceptance 1-3)."""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from agent_mailbox.store import MailStore


class _EchoSinkServer(HTTPServer):
    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(self.server_address)
        host, port = self.socket.getsockname()[:2]
        self.server_address = (host, port)
        self.server_name, self.server_port = host, port


class _EchoSink(BaseHTTPRequestHandler):
    received: ClassVar[list[tuple[dict, bytes]]] = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _EchoSink.received.append((dict(self.headers), body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture()
def echo_sink():
    _EchoSink.received = []
    server = _EchoSinkServer(("127.0.0.1", 0), _EchoSink)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", _EchoSink.received
    server.shutdown()


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _sent_lines(root):
    return (root / "sent.log").read_text(encoding="utf-8").splitlines()


# ---- acceptance 1: default off — self-echo notification dropped, letter lands


def test_self_echo_suppressed_by_default(echo_sink, tmp_path, monkeypatch):
    url, received = echo_sink
    monkeypatch.setenv("AGENT_MAIL_WEBHOOK_URL", url)
    monkeypatch.delenv("AGENT_MAIL_NOTIFY_SELF_ECHO", raising=False)

    st = MailStore(root=tmp_path / "mail")
    st.register("A")
    out = st.send("A", "A", "note to self", "body")

    # letter lands and stays visible
    assert (st.root / "inbox" / "A" / f"{out[0]['id']}.json").exists()
    assert [m["subject"] for m in st.list_messages("A")] == ["note to self"]
    # no wake-up notification for the sender's own echo
    time.sleep(0.3)
    assert not received, "self-echo notification was delivered despite default suppression"
    # audit trail keeps the trace
    line = json.loads(_sent_lines(st.root)[-1])
    assert line["subject"] == "note to self"
    assert line["echo_suppressed"] is True


# ---- acceptance 2: opt-in delivery, [echo] prefix on the notification only


def test_self_echo_delivered_with_echo_prefix_when_enabled(echo_sink, tmp_path, monkeypatch):
    url, received = echo_sink
    monkeypatch.setenv("AGENT_MAIL_WEBHOOK_URL", url)
    monkeypatch.setenv("AGENT_MAIL_NOTIFY_SELF_ECHO", "true")

    st = MailStore(root=tmp_path / "mail")
    st.register("A")
    out = st.send("A", "A", "note to self", "body")

    assert _wait_for(lambda: len(received) == 1), "opt-in self-echo was not delivered"
    payload = json.loads(received[0][1])
    assert payload["message"]["subject"] == "[echo] note to self"
    # the stored letter keeps the original subject (regex-strippable)
    assert [m["subject"] for m in st.list_messages("A")] == ["note to self"]
    line = json.loads(_sent_lines(st.root)[-1])
    assert "echo_suppressed" not in line
    assert out[0]["id"] == payload["message"]["id"]


def test_config_json_enables_delivery(echo_sink, tmp_path, monkeypatch):
    url, received = echo_sink
    monkeypatch.setenv("AGENT_MAIL_WEBHOOK_URL", url)
    monkeypatch.delenv("AGENT_MAIL_NOTIFY_SELF_ECHO", raising=False)

    root = tmp_path / "mail"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({"notify_self_echo": True}))

    st = MailStore(root=root)
    st.register("A")
    st.send("A", "A", "cfg echo", "body")
    assert _wait_for(lambda: len(received) == 1), "config.json opt-in was ignored"
    assert json.loads(received[0][1])["message"]["subject"] == "[echo] cfg echo"


def test_env_overrides_config_false(echo_sink, tmp_path, monkeypatch):
    """env AGENT_MAIL_NOTIFY_SELF_ECHO wins over config.json."""
    url, received = echo_sink
    monkeypatch.setenv("AGENT_MAIL_WEBHOOK_URL", url)
    root = tmp_path / "mail"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({"notify_self_echo": True}))
    monkeypatch.setenv("AGENT_MAIL_NOTIFY_SELF_ECHO", "0")  # env says off
    st = MailStore(root=root)
    st.register("A")
    st.send("A", "A", "env wins", "body")
    time.sleep(0.3)
    assert not received
    assert json.loads(_sent_lines(st.root)[-1])["echo_suppressed"] is True


# ---- acceptance 3: regression — normal A→B mail is untouched


def test_normal_a_to_b_unaffected(echo_sink, tmp_path, monkeypatch):
    url, received = echo_sink
    monkeypatch.setenv("AGENT_MAIL_WEBHOOK_URL", url)
    monkeypatch.delenv("AGENT_MAIL_NOTIFY_SELF_ECHO", raising=False)

    st = MailStore(root=tmp_path / "mail")
    st.register("A")
    st.register("B")
    st.send("A", "B", "normal mail", "body")

    assert _wait_for(lambda: len(received) == 1), "normal A→B wake-up missing"
    payload = json.loads(received[0][1])
    assert payload["message"]["subject"] == "normal mail"  # no [echo] prefix
    line = json.loads(_sent_lines(st.root)[-1])
    assert "echo_suppressed" not in line


def test_broadcast_self_copy_suppressed_others_delivered(echo_sink, tmp_path, monkeypatch):
    url, received = echo_sink
    monkeypatch.setenv("AGENT_MAIL_WEBHOOK_URL", url)

    st = MailStore(root=tmp_path / "mail")
    st.register("A")
    st.register("B")
    st.send("A", "all", "to everyone", "body")

    assert _wait_for(lambda: len(received) == 1), "B should still be woken by broadcast"
    targets = {json.loads(body)["message"]["to"] for _, body in received}
    assert targets == {"B"}  # A's own copy suppressed
    entries = [json.loads(line) for line in _sent_lines(st.root)]
    by_to = {e["to"]: e for e in entries}
    assert by_to["A"]["echo_suppressed"] is True
    assert "echo_suppressed" not in by_to["B"]
