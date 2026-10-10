"""Portable temporary CLI fixtures using real Python subprocesses."""

import subprocess
import sys

import pytest

from agent_mailbox import workbench_wake as wk


@pytest.fixture
def python_cli(tmp_path, monkeypatch):
    scripts = set()
    real_popen = subprocess.Popen

    def popen(args, *positional, **kwargs):
        if isinstance(args, (list, tuple)) and args and str(args[0]) in scripts:
            args = [sys.executable, *args]
        return real_popen(args, *positional, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)

    def create(body, name="fake"):
        script = tmp_path / name
        script.write_text(body + "\n", encoding="utf-8")
        script.chmod(0o700)
        scripts.add(str(script))
        return script

    return create


@pytest.fixture
def wake_hook(tmp_path, monkeypatch):
    """A platform-native hook file for tests that inject the host runner."""
    if sys.platform == "win32":
        interpreter = tmp_path / "powershell.exe"
        interpreter.touch()
        monkeypatch.setattr(wk, "_windows_powershell", lambda: str(interpreter))

    def create(home, returncode=0):
        windows = sys.platform == "win32"
        hook = wk.state_dir(home) / (wk.WINDOWS_HOOK_FILENAME if windows else wk.HOOK_FILENAME)
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(
            f"exit {returncode}\n" if windows else f"#!/bin/sh\nexit {returncode}\n",
            encoding="utf-8",
        )
        if not windows:
            hook.chmod(0o700)
        return hook

    return create
