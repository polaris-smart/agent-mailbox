"""Portable temporary CLI fixtures using real Python subprocesses."""

import subprocess
import sys

import pytest


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
