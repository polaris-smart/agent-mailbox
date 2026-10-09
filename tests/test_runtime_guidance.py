"""Engine-missing guidance (HS 0.8.1 body item #2).

Must tell the user WHAT to install, HOW (official), and that they can come back --
not a bare unavailable one-liner. Also: we never bundle engines as our own plugin.
"""

from __future__ import annotations

from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "agent_mailbox" / "workbench_runtime.py"


def test_missing_engine_message_is_actionable():
    text = SOURCE.read_text(encoding="utf-8")
    assert (
        "Install the execution runtime (Node.js >=22.13) before starting an employee." not in text
    ), "bare one-liner must be gone"
    assert "Node.js >=22.13" in text, "state the Node requirement"
    assert "@openai/codex" in text and "@anthropic-ai/claude-code" in text, (
        "name both engines with install commands"
    )
    assert "no restart needed" in text, "tell the user they can come back without restarting"
    assert "Mailbox mode" in text, "reassure that mailbox mode needs no engine"


def test_we_never_ship_engines_as_our_own_plugin():
    text = SOURCE.read_text(encoding="utf-8")
    assert "不把引擎打成我们的插件" in text, (
        "the hard rule against an engine plugin package must stay visible"
    )
