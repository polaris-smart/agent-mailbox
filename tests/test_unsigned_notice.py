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
    assert "隐私与安全性" in source, "point to the macOS system decision path"
    assert "SmartScreen" in source, "point to the Windows system decision path"
    assert "没有做签名与公证" in source, "state it plainly and honestly"
    assert "无害 ✓" not in source, "do not claim that unsigned binaries are automatically safe"
    assert "xattr -d com.apple.quarantine" not in source, (
        "do not recommend removing quarantine from the app"
    )


def test_notice_is_dismissible_and_remembers():
    source = JS.read_text(encoding="utf-8")
    assert "agent-mailbox.unsigned-notice.dismissed" in source, "must remember dismissal"
    assert "localStorage" in source, "persist via localStorage"
    assert "不附带智能体引擎" in source, "repeat the zero-engine promise here too"
    assert ".unsigned-notice" in CSS.read_text(encoding="utf-8"), "notice needs its own styling"
