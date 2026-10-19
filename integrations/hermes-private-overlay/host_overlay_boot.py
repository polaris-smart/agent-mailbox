"""Run a pinned Hermes host with process-local mailbox changes.

The original Hermes installation is read only. This launcher is deliberately
separate from plugin setup: selecting a profile and starting a Gateway are
later, explicit operations. An unknown or mixed SDK fails before import.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.abc
import importlib.util
import json
import os
import re
import stat
import sys
from pathlib import Path

MANIFEST_SHA256 = "0356a650b7f0c16ed1fd74738c2390f9f6c75d2f962dc65e0634be8fadcb945f"
LICENSE_SHA256 = "821556e6336796450ab852d375117b48a4887e71d255794fd6318d99982a5ab6"
HASH = re.compile(r"[0-9a-f]{64}\Z")


def _canonical_directory(path: Path) -> Path:
    if not path.is_absolute() or path.resolve() != path.absolute() or not path.is_dir():
        raise ValueError("canonical_absolute_directory_required")
    return path


def _read_regular(path: Path, *, limit: int) -> bytes:
    if path.resolve() != path.absolute():
        raise ValueError("unsafe_host_patch_file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
            raise ValueError("unsafe_host_patch_file")
        if info.st_size > limit:
            raise ValueError("host_patch_file_too_large")
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("host_patch_file_too_large")
        return raw


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def verify(host_workspace: Path, bundle: Path) -> dict[str, Path]:
    """Verify complete old/new images before loading any Hermes module."""
    host_workspace = _canonical_directory(Path(host_workspace))
    bundle = _canonical_directory(Path(bundle))
    raw = _read_regular(bundle / "manifest.json", limit=32_768)
    if _sha(raw) != MANIFEST_SHA256:
        raise ValueError("host_patch_manifest_changed")
    if _sha(_read_regular(bundle / "LICENSE", limit=16_384)) != LICENSE_SHA256:
        raise ValueError("host_patch_license_changed")
    manifest = json.loads(raw)
    rows = manifest.get("modules")
    if manifest.get("format") != "agent-mailbox-hermes-private-host-candidate-v1" or not isinstance(rows, list) or len(rows) != 12:
        raise ValueError("host_patch_manifest_invalid")
    patches: dict[str, Path] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("host_patch_manifest_invalid")  # noqa: TRY004 - invalid persisted manifest
        name, rel = row.get("module"), row.get("relative_path")
        old_hash, new_hash = row.get("old_sha256"), row.get("patched_sha256")
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+", name) is None
            or rel != name.replace(".", "/") + ".py"
            or name in patches
            or not isinstance(new_hash, str)
            or HASH.fullmatch(new_hash) is None
            or (old_hash is not None and (not isinstance(old_hash, str) or HASH.fullmatch(old_hash) is None))
        ):
            raise ValueError("host_patch_manifest_invalid")
        old_path = host_workspace / rel
        if old_hash is None:
            if old_path.exists() or old_path.is_symlink():
                raise ValueError("host_patch_unexpected_original")
        elif _sha(_read_regular(old_path, limit=512_000)) != old_hash:
            raise ValueError("unsupported_hermes_dispatch_version")
        patch_path = bundle / "patched" / rel
        if _sha(_read_regular(patch_path, limit=512_000)) != new_hash:
            raise ValueError("host_patch_file_changed")
        patches[name] = patch_path
    if any(name in sys.modules for name in patches):
        raise ValueError("hermes_module_loaded_before_patch")
    return patches


class _ExactPatchFinder(importlib.abc.MetaPathFinder):
    def __init__(self, patches: dict[str, Path]):
        self.patches = patches

    def find_spec(self, fullname, path=None, target=None):
        patch = self.patches.get(fullname)
        if patch is None:
            return None
        return importlib.util.spec_from_file_location(fullname, patch)


def install(host_workspace: Path, bundle: Path) -> dict[str, Path]:
    patches = verify(host_workspace, bundle)
    # A signed App's resource directory must remain byte-for-byte unchanged.
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(host_workspace))
    sys.meta_path.insert(0, _ExactPatchFinder(patches))
    return patches


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Pinned Hermes private-work host overlay")
    parser.add_argument("--host-workspace", required=True, type=Path)
    parser.add_argument("--bundle", type=Path, default=Path(__file__).parent)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("hermes_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not os.environ.get("HERMES_HOME"):
        raise ValueError("explicit_hermes_home_required")
    patches = install(args.host_workspace, args.bundle)
    if args.probe:
        for name in patches:
            module = importlib.import_module(name)
            if Path(module.__file__).resolve() != patches[name]:
                raise ValueError("host_patch_import_shadowed")
        print(json.dumps({"patched_modules": len(patches), "host_files_written": 0}))
        return 0
    sys.argv = ["hermes", *(args.hermes_args[1:] if args.hermes_args[:1] == ["--"] else args.hermes_args)]
    from hermes_cli.main import main as hermes_main

    return hermes_main()


if __name__ == "__main__":
    raise SystemExit(main())
