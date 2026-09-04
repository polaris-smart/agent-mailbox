"""Pytest wrapper for the MCP stdio E2E check."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_stdio_end_to_end():
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "test_e2e_stdio.py")],
        capture_output=True, text=True, timeout=60, check=False,
        env={**__import__("os").environ, "AGENT_MAIL_HOME": str(ROOT / ".test-mail")},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "E2E PASS" in result.stdout
