"""Build output must land in the dev area (two-zone home layout, 2026-10-08)."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "build-workbench.py"


def _module():
    spec = importlib.util.spec_from_file_location("build_layout_mod", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_default_output_lives_in_the_dev_area(tmp_path):
    module = _module()
    out = module.default_output_dir(tmp_path)
    assert out == tmp_path / ".agent-mailbox-dev" / "build" / module.app_version(), out
    assert out.name == "0.8.1", "版本作子目录 ✓（不写进活库名 ✗）"


def test_purge_intermediates_keeps_dist(tmp_path):
    module = _module()
    for name in ("build", "cache", "dist"):
        (tmp_path / name).mkdir()
    (tmp_path / "dist" / "keep.txt").write_text("x", encoding="utf-8")
    removed = module.purge_intermediates(tmp_path)
    assert sorted(removed) == ["build", "cache"], removed
    assert (tmp_path / "dist" / "keep.txt").is_file(), "dist 必须留着 ✓"
    assert module.purge_intermediates(tmp_path) == [], "幂等 ✓（没有就返回空 ✓）"
