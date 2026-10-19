"""A source tree without Git metadata must not claim a committed build."""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build-workbench.py"


def _module(root: Path):
    spec = importlib.util.spec_from_file_location("build_workbench_provenance_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.__file__ = str(root / "scripts/build-workbench.py")
    module.app_version = lambda: "0.8.2"
    assets = root / "src/agent_mailbox/workbench_assets"
    assets.mkdir(parents=True)
    (assets / "workbench.js").write_text("const x = 1;\n")
    return module, assets


def test_unknown_checkout_is_not_reported_as_committed(tmp_path, monkeypatch):
    for key in os.environ:
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    module, _assets = _module(tmp_path)
    info = module.write_build_info()
    assert info["revision"] == "(unknown)"
    assert info["dirty"] is None
    assert info["built_from"] == "unknown"


def test_clean_and_dirty_checkout_are_distinguished(tmp_path, monkeypatch):
    for key in os.environ:
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    module, assets = _module(tmp_path)
    (tmp_path / ".gitignore").write_text("build-info.json\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "-m",
            "baseline",
        ],
        check=True,
    )
    clean = module.write_build_info()
    assert clean["revision"] != "(unknown)"
    assert clean["dirty"] is False
    assert clean["built_from"] == "committed"
    (assets / "workbench.js").write_text("const x = 2;\n")
    dirty = module.write_build_info()
    assert dirty["revision"] == clean["revision"]
    assert dirty["dirty"] is True
    assert dirty["built_from"] == "working-tree"
