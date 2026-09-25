"""Tests for v0.7 — LocalCommandAdapter (按需 CLI 型 agent 的 wake).

On-demand CLI agents (codex etc.) have no resident process: waking them means
running a configured local command. Covered here: exit 0 = delivered, any
failure rides the existing retry path (信不丢), timeout hard-kill, the
anti-injection posture (argv list only, letter content via env never shell),
and backward compatibility of make_adapter dispatch.
"""

import json
import sys
import time
from pathlib import Path

import pytest

from agent_mailbox.store import MailStore
from agent_mailbox.wake import (
    DEFAULT_COMMAND_TIMEOUT,
    ClaudeCodeAdapter,
    GenericWebhookAdapter,
    HermesAdapter,
    LocalCommandAdapter,
    WakeConfig,
    make_adapter,
    run_once,
)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "mail"
    monkeypatch.setenv("AGENT_MAIL_HOME", str(root))
    st = MailStore(root=root)
    st.register("ZC")
    return root, st


def lcfg(root, **kw):
    base = {
        "agent_id": "ZC",
        "adapter": "local-command",
        "command": [sys.executable, "-c", "import sys; sys.exit(0)"],
        "timeout": 30,
        "retry_interval": 0,
    }
    base.update(kw)
    return WakeConfig(base, Path(root))


_ENV_ECHO = (
    "import os, sys\n"
    "p = sys.argv[1]\n"
    "with open(p, 'w', encoding='utf-8') as f:\n"
    "    f.write('BODY:' + os.environ.get('AGENT_MAIL_MSG_BODY', '') + '\\n')\n"
    "    f.write('SUBJ:' + os.environ.get('AGENT_MAIL_SUBJECT', '') + '\\n')\n"
    "    f.write('ID:' + os.environ.get('AGENT_MAIL_MSG_ID', '') + '\\n')\n"
    "    f.write('AGENT:' + os.environ.get('AGENT_MAIL_AGENT_ID', '') + '\\n')\n"
)


class _Recorder:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def deliver(self, msg):
        self.calls.append(msg["id"])
        return self.results.pop(0) if self.results else True


def _letter(root, msg_id):
    path = Path(root) / "inbox" / "ZC" / f"{msg_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------- exit 0 = delivered


def test_exit_zero_delivers_and_env_transit(env, tmp_path):
    """Exit 0 = delivered; letter content reaches the command only through
    AGENT_MAIL_* environment variables, verbatim."""
    marker = tmp_path / "env-dump.txt"
    adapter = LocalCommandAdapter([sys.executable, "-c", _ENV_ECHO, str(marker)], timeout=30)
    msg = {"id": "m1", "to": "ZC", "subject": "hello world", "body": "wake up body"}
    assert adapter.deliver(msg) is True
    dump = marker.read_text(encoding="utf-8")
    assert "BODY:wake up body" in dump
    assert "SUBJ:hello world" in dump
    assert "ID:m1" in dump
    assert "AGENT:ZC" in dump


def test_run_once_exit_zero_marks_wake_and_dedups(env):
    """A full drain round with the local-command config: delivered, the wake
    outcome lands in handled_log (note=local-command, 执行留痕), and a second
    round dedups."""
    root, st = env
    out = st.send("HS", "ZC", "hello", "wake me")
    stats = run_once(root, lcfg(root))
    assert stats["woke"] == 1 and stats["failed"] == 0
    letter = _letter(root, out[0]["id"])
    wake_entries = [e for e in letter.get("handled_log", []) if e.get("action") == "wake"]
    assert len(wake_entries) == 1 and wake_entries[0].get("note") == "local-command"
    stats2 = run_once(root, lcfg(root))
    assert stats2["woke"] == 0 and stats2["skipped_woken"] == 1  # idempotent dedup


# --------------------------------------- failure rides the retry path


def test_nonzero_exit_retries_and_letter_not_lost(env):
    """Non-zero exit -> every attempt fails -> no wake mark -> the letter is
    re-drained by the next trigger (信不丢), and a later good command wakes it."""
    root, st = env
    out = st.send("HS", "ZC", "hello", "wake me")
    bad = lcfg(root, command=[sys.executable, "-c", "import sys; sys.exit(3)"], retry_max=2)
    stats = run_once(root, bad)
    assert stats["failed"] == 2 and stats["woke"] == 0
    assert not any(
        e.get("action") == "wake" for e in _letter(root, out[0]["id"]).get("handled_log", [])
    )
    # recovery: the next round runs the fixed command and marks the letter
    stats2 = run_once(root, lcfg(root))
    assert stats2["woke"] == 1


def test_missing_binary_and_empty_command_return_false_no_raise(env):
    """Spawn failure (binary missing) and a misconfigured empty command both
    return False instead of raising — the retry path keeps the letter."""
    assert (
        LocalCommandAdapter(["definitely-not-a-real-binary-xyz-12345"]).deliver({"id": "m"})
        is False
    )
    assert LocalCommandAdapter([]).deliver({"id": "m"}) is False
    root, st = env
    st.send("HS", "ZC", "hello", "x")
    stats = run_once(
        root, lcfg(root, command=["definitely-not-a-real-binary-xyz-12345"], retry_max=1)
    )
    assert stats["woke"] == 0 and stats["failed"] == 1


# --------------------------------------------------- timeout hard-kill


def test_timeout_hard_kills_process(env):
    """A command hung past the deadline is killed and deliver returns False
    promptly — the drain never blocks for the full sleep."""
    adapter = LocalCommandAdapter(
        [sys.executable, "-c", "import time; time.sleep(30)"], timeout=1.0
    )
    start = time.monotonic()
    assert adapter.deliver({"id": "m"}) is False
    elapsed = time.monotonic() - start
    assert elapsed < 15, f"timeout kill took {elapsed:.1f}s — process was not killed"


def test_timeout_keeps_letter_for_retry(env):
    """At drain level a timed-out command counts as failed: no wake mark, the
    letter waits for the next trigger (信不丢)."""
    root, st = env
    out = st.send("HS", "ZC", "hello", "wake me")
    hung = lcfg(
        root,
        command=[sys.executable, "-c", "import time; time.sleep(30)"],
        timeout=1.0,
        retry_max=1,
    )
    stats = run_once(root, hung)
    assert stats["failed"] == 1 and stats["woke"] == 0
    assert not any(
        e.get("action") == "wake" for e in _letter(root, out[0]["id"]).get("handled_log", [])
    )
    stats2 = run_once(root, lcfg(root))
    assert stats2["woke"] == 1


# ------------------------------------------------------ anti-injection


def test_letter_content_never_hits_shell(env, tmp_path):
    """Shell metacharacters in the letter body/subject must never execute:
    the command is an argv list without a shell, content rides env verbatim."""
    pwned1 = tmp_path / "pwned1"
    pwned2 = tmp_path / "pwned2"
    pwned3 = tmp_path / "pwned3"
    body = f'x"; touch {pwned1}; # $(touch {pwned2}) `touch {pwned3}`'
    subject = f"a; rm -rf {tmp_path / 'pwned-subj'} #"
    marker = tmp_path / "env-dump.txt"
    adapter = LocalCommandAdapter([sys.executable, "-c", _ENV_ECHO, str(marker)], timeout=30)
    assert adapter.deliver({"id": "m1", "to": "ZC", "subject": subject, "body": body}) is True
    dump = marker.read_text(encoding="utf-8")
    assert f"BODY:{body}" in dump  # verbatim transit, nothing interpreted
    assert f"SUBJ:{subject}" in dump
    assert not pwned1.exists() and not pwned2.exists() and not pwned3.exists()


def test_string_command_form_rejected_never_executed(env, tmp_path):
    """A shell-string command shape is structurally impossible: the config
    loader keeps only list/tuple commands — a string collapses to [] and the
    adapter refuses to run (no shell-concatenation wake path exists)."""
    pwned = tmp_path / "pwned-str"
    c = WakeConfig(
        {
            "agent_id": "ZC",
            "adapter": "local-command",
            "command": f"touch {pwned}",  # wrong shape on purpose
            "retry_interval": 0,
        },
        Path(env[0]),
    )
    assert c.command == []  # string rejected at load time
    assert make_adapter(c).deliver({"id": "m"}) is False
    assert not pwned.exists()


# ------------------------------------------------- backward compatibility


def test_make_adapter_dispatch_local_command_and_unchanged(env):
    root, _ = env
    assert make_adapter(lcfg(root)).name == "local-command"
    assert isinstance(make_adapter(lcfg(root)), LocalCommandAdapter)
    # the three pre-existing adapters dispatch exactly as before
    assert make_adapter(WakeConfig({"adapter": "claude-code"}, Path(root))).name == "claude-code"
    assert isinstance(
        make_adapter(WakeConfig({"adapter": "claude-code"}, Path(root))), ClaudeCodeAdapter
    )
    gw = WakeConfig({"adapter": "generic-webhook", "webhook": {"url": "u"}}, Path(root))
    assert isinstance(make_adapter(gw), GenericWebhookAdapter)
    assert isinstance(make_adapter(WakeConfig({"adapter": "hermes"}, Path(root))), HermesAdapter)
    assert make_adapter(WakeConfig({"adapter": "nonsense"}, Path(root))).name == "hermes"


def test_legacy_wake_json_without_command_keys_loads_and_drains(env):
    """A pre-v0.7 wake.json (no command/timeout keys) loads with the defaults
    and the hermes drain path still wakes mail unchanged."""
    root, st = env
    legacy = WakeConfig({"agent_id": "ZC", "adapter": "hermes"}, Path(root))
    assert legacy.command == []
    assert legacy.command_timeout == DEFAULT_COMMAND_TIMEOUT == 300.0
    # round-trip: save keeps the new keys, load restores them
    lc = lcfg(root, timeout=42)
    lc.save()
    loaded = WakeConfig.load(Path(root))
    assert loaded.command == [sys.executable, "-c", "import sys; sys.exit(0)"]
    assert loaded.command_timeout == 42.0
    # and the legacy hermes drain path still works end to end
    st.send("HS", "ZC", "hello", "wake me")
    stats = run_once(root, legacy, adapter=_Recorder([True]))
    assert stats["woke"] == 1


def test_cmd_run_adapter_cli_override(env, monkeypatch):
    """wake run --adapter CLI 覆盖 wake.json 顶层 adapter——多 agent 共享
    wake.json 时各 plist 按 --agent --adapter 各取所需（HS=hermes 不动，
    codex=local-command），覆盖只影响本次 run。"""
    import types

    from agent_mailbox import wake as wk

    root, st = env
    st.send("HS", "codex", "wake codex", "process me")
    # 模拟共享 wake.json：顶层 adapter=hermes（HS 的默认）
    (root / "wake.json").write_text(
        json.dumps({"agent_id": "HS", "adapter": "hermes"}), encoding="utf-8"
    )
    captured = {}

    def fake_run(root_, cfg_, once=False):
        captured["adapter"] = cfg_.adapter
        captured["agent"] = cfg_.agent_id

    monkeypatch.setattr(wk, "run", fake_run)
    args = types.SimpleNamespace(agent="codex", root=str(root), once=True, adapter="local-command")
    wk._cmd_run(args)
    assert captured == {"adapter": "local-command", "agent": "codex"}

    # 不传 --adapter 时回落 wake.json 顶层（HS 语义零变化）
    captured.clear()
    wk._cmd_run(types.SimpleNamespace(agent="HS", root=str(root), once=True, adapter=""))
    assert captured["adapter"] == "hermes" and captured["agent"] == "HS"
