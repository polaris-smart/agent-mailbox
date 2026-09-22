"""Tests for v0.6 F1 — wake-daemon.

plist/systemd generation (tmp paths only — nothing is ever installed for
real in tests), count semantics v2, adapter dispatch, retry-then-requeue,
idempotent dedup via handled_log, fail-open, and the CLI wiring.
"""

import json
import time
from pathlib import Path

import pytest

import agent_mailbox.wake as wake_mod
from agent_mailbox.store import MailStore
from agent_mailbox.wake import (
    WAKE_LABEL,
    ClaudeCodeAdapter,
    WakeConfig,
    make_adapter,
    plist_body,
    run_once,
    should_wake,
    systemd_unit_body,
    wake_main,
)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "mail"
    monkeypatch.setenv("AGENT_MAIL_HOME", str(root))
    st = MailStore(root=root)
    st.register("ZC")
    return root, st


def cfg(root, **kw):
    base = {
        "agent_id": "ZC",
        "adapter": "generic-webhook",
        "webhook": {"url": "http://127.0.0.1:9/hook", "secret": "s"},
        "retry_interval": 0,
    }
    base.update(kw)
    return WakeConfig(base, Path(root))


# ------------------------------------------------- unit generation (tmp only)

def test_plist_body_watches_inbox_and_runs_once(env):
    root, _ = env
    body = plist_body(cfg(root), "/usr/bin/python3", root)
    inbox = Path(root) / "inbox" / "ZC"  # str() follows platform separators
    assert f"<string>{inbox}</string>" in body
    assert "agent_mailbox.wake" in body and "--once" in body
    assert f"{WAKE_LABEL}-ZC" in body
    assert "WatchPaths" in body and "RunAtLoad" in body


def test_systemd_units_use_pathchanged(env):
    root, _ = env
    units = systemd_unit_body(cfg(root), "/usr/bin/python3", root)
    path_unit = units[f"{WAKE_LABEL}-ZC.path"]
    service = units[f"{WAKE_LABEL}-ZC.service"]
    assert f"PathChanged={Path(root) / 'inbox' / 'ZC'}" in path_unit
    assert "WantedBy=default.target" in path_unit
    assert "Type=oneshot" in service and "agent_mailbox.wake" in service


def test_install_writes_files_without_activating(env, monkeypatch):
    """Full install flow against tmp dirs: files land, nothing is loaded —
    the launchctl/systemd activation step is intercepted."""
    root, _ = env
    tmp = root / "launchagents-tmp"
    called = []
    monkeypatch.setattr(wake_mod, "_launchctl", lambda *a, **k: called.append(a) or True)
    monkeypatch.setattr(wake_mod.sys, "platform", "darwin")
    out = wake_mod.install(cfg(root), launch_agents_dir=tmp, python_exe="/usr/bin/python3")
    assert (tmp / f"{WAKE_LABEL}-ZC.plist").exists()
    assert out["activated"] is True and called  # activate path exercised, intercepted
    assert (root / "wake.json").exists()  # config persisted for `wake run`
    # clean uninstall removes the file again (idempotent on repeat)
    out_un = wake_mod.uninstall("ZC", launch_agents_dir=tmp, activate=False)
    assert out_un["removed"] and not (tmp / f"{WAKE_LABEL}-ZC.plist").exists()
    assert not wake_mod.uninstall("ZC", launch_agents_dir=tmp, activate=False)["removed"]


# ------------------------------------------------------- count semantics v2

def test_should_wake_count_v2():
    now = time.time()
    assert should_wake({"status": "pending"}, now, 600) is True
    fresh = {"status": "acked", "acked_at": _iso(now - 60)}
    assert should_wake(fresh, now, 600) is False  # being handled — stay quiet
    stale = {"status": "acked", "acked_at": _iso(now - 601)}
    assert should_wake(stale, now, 600) is True  # handler died — re-wake
    assert should_wake({"status": "done"}, now, 600) is False
    assert should_wake({"status": "acked", "acked_at": "garbage"}, now, 600) is True


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + "Z"


# ---------------------------------------------------------------- adapters

def test_make_adapter_dispatch(env):
    root, _ = env
    assert make_adapter(cfg(root, adapter="claude-code")).name == "claude-code"
    assert make_adapter(cfg(root, adapter="generic-webhook")).name == "generic-webhook"
    assert make_adapter(cfg(root, adapter="hermes")).name == "hermes"
    assert make_adapter(cfg(root, adapter="nonsense")).name == "hermes"  # fail-open
    assert ClaudeCodeAdapter().deliver({"from": "HS", "subject": "t"}) is True


class _Recorder:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def deliver(self, msg):
        self.calls.append(msg["id"])
        return self.results.pop(0) if self.results else True


# ------------------------------------------------------------------- drain

def test_run_once_wakes_and_dedups_second_round(env):
    root, st = env
    st.send("HS", "ZC", "hello", "wake me")
    rec = _Recorder([True])
    stats = run_once(root, cfg(root), adapter=rec)
    assert stats["woke"] == 1 and rec.calls
    # same letter again: handled_log carries the wake outcome -> no second POST
    stats2 = run_once(root, cfg(root), adapter=rec)
    assert stats2["woke"] == 0 and stats2["skipped_woken"] == 1
    assert len(rec.calls) == 1  # 同一封信只唤醒一次


def test_run_once_retries_then_leaves_letter_unmarked(env):
    """All attempts fail -> no wake mark -> the next WatchPaths trigger
    re-drains the letter (信不丢)."""
    root, st = env
    out = st.send("HS", "ZC", "hello", "wake me")
    rec = _Recorder([False, False, False])  # retry_max=3 for this test
    stats = run_once(root, cfg(root, retry_max=3), adapter=rec)
    assert stats["failed"] == 3 and stats["woke"] == 0
    letter = json.loads(
        (root / "inbox" / "ZC" / f"{out[0]['id']}.json").read_text(encoding="utf-8")
    )
    assert not any(e.get("action") == "wake" for e in letter.get("handled_log", []))
    # recovery: next round delivers and marks
    rec2 = _Recorder([True])
    stats2 = run_once(root, cfg(root), adapter=rec2)
    assert stats2["woke"] == 1 and rec2.calls == [out[0]["id"]]


def test_run_once_skips_fresh_acked(env):
    root, st = env
    st.send("HS", "ZC", "in progress", "x")
    st.check("ZC")  # pending -> acked seconds ago
    rec = _Recorder([])
    stats = run_once(root, cfg(root), adapter=rec, now=time.time())
    assert stats["woke"] == 0 and not rec.calls  # freshly acked stays quiet


def test_run_once_fail_open_on_adapter_crash(env):
    root, st = env
    st.send("HS", "ZC", "boom", "x")

    class _Boom:
        def deliver(self, msg):
            raise RuntimeError("adapter exploded")

    stats = run_once(root, cfg(root, retry_max=1), adapter=_Boom())
    assert stats["woke"] == 0  # no crash out of run_once; letter waits for next round


def test_run_once_stale_acked_gets_rewoken_once(env):
    """acked beyond the stale window counts again (count semantics v2), but
    the wake dedup still prevents a second POST for a letter already woken."""
    root, st = env
    st.send("HS", "ZC", "orphaned", "x")
    st.check("ZC")
    # backdate the ack past the stale window
    inbox = root / "inbox" / "ZC"
    for p in inbox.glob("*.json"):
        m = json.loads(p.read_text(encoding="utf-8"))
        m["acked_at"] = _iso(time.time() - 1200)
        p.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    rec = _Recorder([True])
    stats = run_once(root, cfg(root), adapter=rec, now=time.time())
    assert stats["due"] == 1 and stats["woke"] == 1
    assert run_once(root, cfg(root), adapter=_Recorder([True]), now=time.time())["woke"] == 0


# --------------------------------------------------------------------- CLI

def test_wake_cli_install_and_status(env, monkeypatch, capsys):
    root, _ = env
    tmp = root / "plist-tmp"
    monkeypatch.setattr(wake_mod, "_launchctl", lambda *a, **k: True)
    monkeypatch.setattr(wake_mod.sys, "platform", "darwin")
    wake_main([
        "install", "--agent", "ZC", "--adapter", "generic-webhook",
        "--webhook-url", "http://127.0.0.1:9/h", "--root", str(root),
        "--launch-agents-dir", str(tmp), "--no-activate",
    ])
    assert (tmp / f"{WAKE_LABEL}-ZC.plist").exists()
    saved = json.loads((root / "wake.json").read_text(encoding="utf-8"))
    assert saved["agent_id"] == "ZC" and saved["webhook"]["url"] == "http://127.0.0.1:9/h"
    assert saved["jev"]["enabled"] is False  # Jev 默认关

    wake_main(["status", "--root", str(root)])
    info = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert info["configured"] is True and info["agent_id"] == "ZC"
