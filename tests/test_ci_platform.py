"""The matrix must reject accidental Rosetta, mixed Node and mislabeled packages."""

import runpy
from pathlib import Path

import pytest

mismatches = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/check-ci-platform.py")
)["mismatches"]


@pytest.mark.parametrize("native", ["AMD64", "x86_64", "x64"])
def test_native_architecture_aliases(native):
    assert (
        mismatches(
            {"python": {"platform": "win32", "architecture": native}, "runner_arch": "X64"},
            "win32",
            "x64",
        )
        == []
    )


@pytest.mark.parametrize("source", ["python", "node", "manifest"])
def test_reject_x64_component_in_arm64_target(source):
    errors = mismatches(
        {source: {"platform": "darwin", "architecture": "x86_64"}}, "darwin", "arm64"
    )
    assert len(errors) == 1
    assert source in errors[0]


def test_reject_wrong_platform_and_missing_manifest_architecture():
    errors = mismatches({"manifest": {"platform": "linux", "architecture": None}}, "win32", "x64")
    assert len(errors) == 2


def test_reject_emulated_runtime_on_different_runner_architecture():
    errors = mismatches(
        {"python": {"platform": "darwin", "architecture": "x86_64"}, "runner_arch": "ARM64"},
        "darwin",
        "x64",
    )
    assert len(errors) == 1
    assert "runner" in errors[0]
