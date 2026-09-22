"""Tests for v0.6 F3 — Jev routing bypass (default OFF).

The gate must be invisible when disabled (v0.5 behaviour regression), apply
the fail-open iron law on any error/timeout, route noul/batch/score, and log
every decision with its score (never the api_key). Pure stdlib — no deps.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from tests.test_wake import _Recorder, cfg

import agent_mailbox.wake as wake_mod
from agent_mailbox.store import MailStore
from agent_mailbox.wake import jev_decide, jev_gate, run_once


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "mail"
    monkeypatch.setenv("AGENT_MAIL_HOME", str(root))
    st = MailStore(root=root)
    st.register("ZC")
    return root, st


def jev_cfg(root, **kw):
    base = {
        "adapter": "generic-webhook",
        "webhook": {"url": "http://127.0.0.1:9/hook", "secret": "s"},
        "retry_interval": 0,
        "jev": {
            "enabled": True,
            "api_key": "sk-test-fake-key",
            "endpoint": "http://127.0.0.1:9/jev",
        },
    }
    base["jev"].update(kw)
    return cfg(root, **base)


# ------------------------------------------------------------- default off

def test_jev_disabled_by_default_and_regression(env):
    """Jev 关闭（或未配置）→ 行为与 v0.5 完全一致：信到就醒，无 Jev 调用。"""
    root, st = env
    st.send("HS", "ZC", "plain", "x")
    rec = _Recorder([True])
    stats = run_once(root, cfg(root), adapter=rec)  # no jev block at all
    assert stats["woke"] == 1 and stats["jev_skipped"] == 0
    assert not (root / "wake-jev.log").exists()

    plain = cfg(root)
    assert plain.jev_enabled is False  # WakeConfig default: OFF


# ------------------------------------------------------- fail-open iron law

def test_jev_gate_fails_open_on_error(env):
    """Jev 报错/超时/断网 → 立即按「有信就醒」走，宁多勿漏。"""
    root, _ = env
    c = jev_cfg(root)
    m = {"id": "m1", "subject": "s", "body": "b"}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wake_mod, "jev_decide", lambda c_, m_: (_ for _ in ()).throw(OSError("net down")))
        wake, reason = jev_gate(c, m, root / "wake-jev.log")
    assert wake is True and reason == "fail-open"


def test_run_once_falls_open_when_jev_unreachable(env):
    """断网端到端：endpoint 不可达 → run_once 仍唤醒（60s 内 fail-open）。"""
    root, st = env
    st.send("HS", "ZC", "urgent", "x")
    rec = _Recorder([True])
    stats = run_once(root, jev_cfg(root), adapter=rec)  # endpoint :9 = closed port
    assert stats["woke"] == 1 and stats["jev_skipped"] == 0
    assert (root / "wake-jev.log").exists()  # the failure was logged, not hidden


# ----------------------------------------------------------------- routing

def _routed(root, monkeypatch, result):
    c = jev_cfg(root)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wake_mod, "jev_decide", lambda c_, m_: result)
        return jev_gate(c, {"id": "m1", "subject": "s", "body": "b"},
                        root / "wake-jev.log")


def test_jev_routes_noul_to_no_wake(env):
    root, _ = env
    wake, reason = _routed(root, None, {"noul": True, "score": 1.5})
    assert wake is False and reason == "noul"


def test_jev_routes_low_score_to_batch(env):
    root, _ = env
    wake, reason = _routed(root, None, {"noul": False, "score": 1.0})
    assert wake is False and reason == "batch"  # below threshold 3.0 → 攒批


def test_jev_routes_high_score_to_wake(env):
    root, _ = env
    wake, reason = _routed(root, None, {"noul": False, "score": 8.0})
    assert wake is True and reason == "score>=threshold"


# --------------------------------------------------------------- logging

def test_jev_log_records_score_never_key(env):
    """决策可解释：日志含 score；api_key 绝不上屏。"""
    root, _ = env
    _routed(root, None, {"noul": False, "score": 7.5})
    line = (root / "wake-jev.log").read_text(encoding="utf-8").splitlines()[0]
    entry = json.loads(line)
    assert entry["score"] == 7.5 and entry["decision"] == "score>=threshold"
    assert "sk-test-fake-key" not in line


def test_jev_decide_parses_api_response(env):
    """Integration: a fake Jev endpoint's JSON is parsed into noul/score."""
    root, _ = env
    c = jev_cfg(root)

    class _Handler(BaseHTTPRequestHandler):
        auth = None

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            _Handler.auth = self.headers.get("Authorization")
            _Handler.body = body
            resp = json.dumps({"noul": False, "score": 6.5}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        c.jev_endpoint = f"http://127.0.0.1:{server.server_port}/jev"
        out = jev_decide(c, {"id": "m", "subject": "中文紧急信", "body": "马上处理"})
        assert out == {"noul": False, "score": 6.5}
        assert _Handler.auth == "Bearer sk-test-fake-key"  # key in header only
        assert "中文紧急信".encode() in _Handler.body
    finally:
        server.shutdown()


def test_jev_timeout_is_bounded(env):
    """The gate never blocks the wake path longer than ~timeout+1s."""
    root, _ = env
    c = jev_cfg(root)
    c.jev_timeout = 0.5

    def _slow(c_, m_):
        time.sleep(10)  # would stall the drain if unbounded

    start = time.monotonic()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wake_mod, "jev_decide", _slow)
        wake, reason = jev_gate(c, {"id": "m"}, root / "wake-jev.log")
    elapsed = time.monotonic() - start
    assert wake is True and reason == "fail-open"
    assert elapsed < 5.0
