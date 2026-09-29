"""t-50② 锚A + U2: wake-attempts.jsonl anchor rows and the wake-fail alert.

- 锚A: every wake attempt (belt/daemon, success or failure) lands one
  8-field JSON line in ``<root>/wake-attempts.jsonl`` (0600, append-only).
- U2: when all delivery attempts for a letter fail, an alert letter goes to
  the responsible party HS + the registered sender — once per letter
  (handled_log marker). t-59（A-2）收件人收敛: boss 席一律不发（仅存档语义，
  老板只看飞书）。
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from agent_mailbox.store import MailStore
from agent_mailbox.wake import (
    WAKE_ATTEMPTS_FILE,
    ClaudeCodeAdapter,
    HermesAdapter,
    LocalCommandAdapter,
    WakeConfig,
    record_wake_attempt,
    run_once,
)

FIELDS = {"ts", "agent", "route", "attempt", "outcome", "error_class", "executor", "latency_ms"}

# Cross-platform spawn fixtures: /usr/bin/false|true don't exist on Windows
# (Popen → WinError 2 → rows classified spawn_failed, not exit_nonzero).
# sys.executable spawns and exits identically on every CI flavor.
FALSE_CMD = [sys.executable, "-c", "import sys; sys.exit(1)"]
TRUE_CMD = [sys.executable, "-c", "pass"]


def _rows(root: Path) -> list[dict]:
    path = root / WAKE_ATTEMPTS_FILE
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _root_mode(root: Path) -> int:
    return stat.S_IMODE((root / WAKE_ATTEMPTS_FILE).stat().st_mode)


def _mkstore(tmp_path: Path) -> MailStore:
    store = MailStore(str(tmp_path))
    store.register("boss")
    store.register("HS")
    store.register("ZC")
    return store


def test_record_wake_attempt_shape_perms_and_append(tmp_path):
    ok = record_wake_attempt(tmp_path, "ZC", "daemon", 1, "ok", executor="hermes", latency_ms=12)
    fail = record_wake_attempt(
        tmp_path, "ZC", "belt", 2, "fail", error_class="no_progress", executor="zcode-drain"
    )
    assert ok and fail
    rows = _rows(tmp_path)
    assert [r["outcome"] for r in rows] == ["ok", "fail"]  # append-only, both kept
    for r in rows:
        assert set(r) == FIELDS
        assert r["agent"] == "ZC"
        assert r["latency_ms"] is None or isinstance(r["latency_ms"], int)
    assert rows[0]["executor"] == "hermes"
    assert rows[1]["route"] == "belt" and rows[1]["error_class"] == "no_progress"
    if os.name == "posix":
        assert _root_mode(tmp_path) == 0o600  # owner-only iron law (POSIX mode bits)
    else:
        # Windows has no POSIX mode bits: os.open(mode=0o600) only toggles the
        # read-only attribute (st_mode reads 0o666 there). The 0600 creation
        # law is POSIX-scoped; on Windows assert the file landed as a file.
        assert (tmp_path / WAKE_ATTEMPTS_FILE).is_file()


def test_adapter_error_classes():
    no_url = HermesAdapter("", "")
    assert no_url.deliver({}) is False and no_url.last_error_class == "no_url"
    empty = LocalCommandAdapter([])
    assert empty.deliver({}) is False and empty.last_error_class == "no_command"
    missing = LocalCommandAdapter(["/nonexistent-binary-t50-锚A"])
    assert missing.deliver({}) is False and missing.last_error_class == "spawn_failed"
    # ClaudeCode best-effort path never reports a failure class
    assert ClaudeCodeAdapter().deliver({"from": "x", "subject": "y"}) is True


def _fail_cfg(tmp_path: Path) -> WakeConfig:
    return WakeConfig({"agent_id": "ZC", "retry_max": 2, "retry_interval": 0}, tmp_path)


def test_failed_wake_writes_fail_rows_and_alerts_once(tmp_path):
    store = _mkstore(tmp_path)
    store.send("HS", "ZC", "please handle", "body")
    # FALSE_CMD exits 1 => exit_nonzero, both attempts fail
    stats = run_once(
        tmp_path, _fail_cfg(tmp_path), adapter=LocalCommandAdapter(FALSE_CMD), store=store
    )
    assert stats["woke"] == 0 and stats["failed"] == 2
    rows = _rows(tmp_path)
    assert [r["outcome"] for r in rows] == ["fail", "fail"]
    assert [r["attempt"] for r in rows] == [1, 2]
    assert all(r["route"] == "daemon" for r in rows)
    assert rows[0]["error_class"] == "exit_nonzero" and rows[0]["executor"] == "local-command"
    # U2: alert lands in the responsible party HS + sender boxes (t-59: boss
    # 席一律不发), letter marked wake_alert
    boss_inbox = list((tmp_path / "inbox" / "boss").glob("*.json"))
    hs_inbox = list((tmp_path / "inbox" / "HS").glob("*.json"))
    assert not boss_inbox, "t-59: 告警不得发 boss 席（仅存档语义，老板只看飞书）"
    assert len(hs_inbox) == 1
    alert = json.loads(hs_inbox[0].read_text(encoding="utf-8"))
    assert "[wake-fail]" in alert["subject"] and alert["from"] == "ZC"
    zc_letter = json.loads(
        next((tmp_path / "inbox" / "ZC").glob("*.json")).read_text(encoding="utf-8")
    )
    assert "wake_alert" in [e["action"] for e in zc_letter["handled_log"]]
    # second round re-runs the failed wake but must NOT re-alert
    run_once(tmp_path, _fail_cfg(tmp_path), adapter=LocalCommandAdapter(FALSE_CMD), store=store)
    assert len(list((tmp_path / "inbox" / "HS").glob("*.json"))) == 1


def test_successful_wake_writes_ok_row_no_alert(tmp_path):
    store = _mkstore(tmp_path)
    store.send("HS", "ZC", "quick ping", "body")
    stats = run_once(
        tmp_path, _fail_cfg(tmp_path), adapter=LocalCommandAdapter(TRUE_CMD), store=store
    )
    assert stats["woke"] == 1
    rows = _rows(tmp_path)
    assert [r["outcome"] for r in rows] == ["ok"]
    assert rows[0]["error_class"] == "" and rows[0]["latency_ms"] is not None
    # no alert on success (boss box was created empty by register())
    assert not list((tmp_path / "inbox" / "boss").glob("*.json"))


def test_unregistered_sender_alerts_hs_only(tmp_path):
    """t-59 改名重定义：未注册/外部发件人的失败告警只落负责方 HS——
    boss 席永不收告警，也绝不 mint 死箱（死箱 hygiene）。"""
    store = _mkstore(tmp_path)
    store.send("OUTSIDE", "ZC", "stranger", "body", origin="external")
    # open the external gate so the letter is due at all
    (tmp_path / "config.json").write_text(
        json.dumps({"visibility": {"external_auto_execute": True}}), encoding="utf-8"
    )
    run_once(tmp_path, _fail_cfg(tmp_path), adapter=LocalCommandAdapter(FALSE_CMD), store=store)
    hs_inbox = list((tmp_path / "inbox" / "HS").glob("*.json"))
    assert len(hs_inbox) == 1, "告警只发负责方 HS"
    assert not list((tmp_path / "inbox" / "boss").glob("*.json")), "boss 席不收告警"
    assert not (tmp_path / "inbox" / "OUTSIDE").exists()  # 死箱 hygiene: no dead dir
