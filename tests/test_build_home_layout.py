"""Build output must land in the dev area (two-zone home layout, 2026-10-08)."""

from __future__ import annotations

import importlib.util
import pathlib
import shutil
import sys
from types import SimpleNamespace

import pytest

from agent_mailbox import __version__

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
    assert out.name == __version__, "版本作子目录 ✓（不写进活库名 ✗）"


def test_internal_build_cannot_share_daily_launchservices_identity(tmp_path):
    module = _module()
    public = module.bundle_identifier_for_build("Agent Mailbox", tmp_path)
    renamed = module.bundle_identifier_for_build("Agent Mailbox Probe", tmp_path)
    isolated = module.bundle_identifier_for_build("Agent Mailbox", tmp_path, isolated=True)
    assert public == "com.polaris-smart.agent-mailbox"
    assert renamed != public
    assert isolated != public
    assert isolated != module.bundle_identifier_for_build(
        "Agent Mailbox", tmp_path / "another", isolated=True
    )


def test_purge_intermediates_keeps_dist(tmp_path):
    module = _module()
    for name in ("build", "cache", "dist"):
        (tmp_path / name).mkdir()
    (tmp_path / "dist" / "keep.txt").write_text("x", encoding="utf-8")
    removed = module.purge_intermediates(tmp_path)
    assert sorted(removed) == ["build", "cache"], removed
    assert (tmp_path / "dist" / "keep.txt").is_file(), "dist 必须留着 ✓"
    assert module.purge_intermediates(tmp_path) == [], "幂等 ✓（没有就返回空 ✓）"


def test_license_inventory_excludes_debug_symbols_and_native_binaries(tmp_path):
    module = _module()
    license_file = tmp_path / "LICENSE.APACHE"
    license_file.write_text("Synthetic license text\n")
    debug_binary = tmp_path / "copying.cpython-313-darwin.so"
    debug_binary.write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 16)
    assert module._collectable_notice(pathlib.Path("package/licenses/LICENSE.APACHE"), license_file)
    assert not module._collectable_notice(
        pathlib.Path(
            "PyObjCTest/copying.cpython-313-darwin.so.dSYM/Contents/Resources/DWARF/copying.cpython-313-darwin.so"
        ),
        debug_binary,
    )
    assert not module._collectable_notice(
        pathlib.Path("package/copying.cpython-313-darwin.so"), debug_binary
    )

    disguised_binary = tmp_path / "LICENSE.bin"
    disguised_binary.write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 16)
    assert not module._collectable_notice(pathlib.Path("package/LICENSE.bin"), disguised_binary)


def test_license_inventory_rejects_distribution_without_notice(tmp_path):
    module = _module()
    package = tmp_path / "synthetic"
    package.mkdir()
    (package / "module.py").write_text("pass\n", encoding="utf-8")
    distribution = SimpleNamespace(
        metadata={"Name": "synthetic"},
        version="1.0",
        files=[pathlib.Path("synthetic/module.py")],
        locate_file=lambda filename: tmp_path / filename,
    )
    with pytest.raises(RuntimeError, match="license notice.*synthetic"):
        module._collect_dependency_notices([distribution], tmp_path / "notices")

    (package / "LICENSE").write_text("Synthetic license text\n", encoding="utf-8")
    distribution.files.append(pathlib.Path("synthetic/LICENSE"))
    inventory = module._collect_dependency_notices([distribution], tmp_path / "notices")
    assert inventory == [
        {"name": "synthetic", "version": "1.0", "notices": ["synthetic/0/LICENSE"]}
    ]


def test_pyobjc_core_notice_fallback_is_pinned_and_text_only(tmp_path, monkeypatch):
    module = _module()
    notice = module._missing_notice_fallback("pyobjc-core", "12.2.2")
    assert notice is not None and notice.is_file()
    assert notice.suffix == ".txt"
    assert "Ronald Oussoren" in notice.read_text(encoding="utf-8")
    assert module._missing_notice_fallback("another-package", "12.2.2") is None
    with pytest.raises(RuntimeError, match="PyObjC core license"):
        module._missing_notice_fallback("pyobjc-core", "12.2.3")
    changed = tmp_path / "LICENSES/pyobjc-core-12.2.2-License.txt"
    changed.parent.mkdir()
    changed.write_text("changed license text\n", encoding="utf-8")
    monkeypatch.setattr(module, "REPOSITORY", tmp_path)
    with pytest.raises(RuntimeError, match="PyObjC core license"):
        module._missing_notice_fallback("pyobjc-core", "12.2.2")


def test_old_environment_metadata_is_rejected_before_writing_build_info(monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "app_version", lambda: "0.8.2")
    monkeypatch.setattr(module.importlib.metadata, "version", lambda name: "0.8.1")
    monkeypatch.setattr(module, "write_build_info", lambda: pytest.fail("must fail before writes"))
    with pytest.raises(RuntimeError, match="another checkout's editable metadata"):
        module.main()


def test_matching_build_metadata_passes(monkeypatch):
    module = _module()
    monkeypatch.setattr(module.importlib.metadata, "version", lambda name: module.app_version())
    module.require_current_project_metadata()


def test_hermes_private_overlay_bundle_is_exact_and_rejects_changed_source(tmp_path, monkeypatch):
    module = _module()
    assets = module.hermes_overlay_assets()
    assert len(assets) == 17  # 12 host modules + two entries, manifest, MIT license, notes.
    assert all(pathlib.Path(source).is_file() for source, _ in assets)
    assert not any("__pycache__" in source or source.endswith(".pyc") for source, _ in assets)

    original = SCRIPT.parents[1] / "integrations/hermes-private-overlay"
    changed = tmp_path / "integrations/hermes-private-overlay"
    shutil.copytree(original, changed, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    monkeypatch.setattr(module, "REPOSITORY", tmp_path)
    assert len(module.hermes_overlay_assets()) == 17
    victim = changed / "patched/gateway/run_turn.py"
    original = victim.read_bytes()
    victim.write_bytes(original + b"\n# changed\n")
    with pytest.raises(ValueError, match="Hermes private host source changed"):
        module.hermes_overlay_assets()
    victim.write_bytes(original)
    entry = changed / "hermes"
    entry.write_bytes(entry.read_bytes() + b"\n# changed\n")
    with pytest.raises(ValueError, match="Hermes private supervisor entry changed"):
        module.hermes_overlay_assets()


def test_missing_build_metadata_is_actionable(monkeypatch):
    module = _module()

    def missing(name):
        raise module.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(module.importlib.metadata, "version", missing)
    with pytest.raises(RuntimeError, match="isolated build environment"):
        module.require_current_project_metadata()
