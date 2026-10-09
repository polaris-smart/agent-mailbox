"""First-run unsigned-build notice (HS body item #3): in-app, both platforms, dismissible.

Docstrings ASCII-only; Chinese quoting uses 「」 per this session's lesson.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "src" / "agent_mailbox" / "workbench_assets" / "workbench.js"
CSS = ROOT / "src" / "agent_mailbox" / "workbench_assets" / "workbench.css"


def test_notice_is_in_app_and_covers_both_platforms():
    source = JS.read_text(encoding="utf-8")
    assert "unsignedNotice" in source, "notice must live in the UI, not only in README"
    assert "xattr -d com.apple.quarantine" in source, "macOS bypass command must be given"
    assert "SmartScreen" in source or "仍要运行" in source, "Windows bypass path must be given"
    assert "没有做签名与公证" in source, "state it plainly and honestly"


def test_notice_is_dismissible_and_remembers():
    source = JS.read_text(encoding="utf-8")
    assert "agent-mailbox.unsigned-notice.dismissed" in source, "must remember dismissal"
    assert "localStorage" in source, "persist via localStorage"
    assert "不含任何内置智能体" in source, "repeat the zero-engine promise here too"
    assert ".unsigned-notice" in CSS.read_text(encoding="utf-8"), "notice needs its own styling"
