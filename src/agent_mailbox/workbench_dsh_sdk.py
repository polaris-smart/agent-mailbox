"""Prepare the audited macOS DSH archive's packed configuration modules.

Internal owner-side helper: archive and private cache are server-selected paths,
never browser parameters. This does not boot DSH, load a profile or activate a
plugin. Unpacked native assets are deliberately not copied; this subset is for
the fixed configuration bridge, not a replacement DSH installation/runtime.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import struct
import tempfile
import time
from pathlib import Path

from .workbench_dsh_process import checked_file, verify_sdk

# Current official bundle used for the independent real-loader/prepare checks.
# New upstream versions need compatibility verification, not automatic trust.
SUPPORTED_ASAR = frozenset({"18d5036bbb58eaff41d351ede99314b8d06505aec06fb2e164c49874ada30f9e"})
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_HEADER = 8 * 1024 * 1024
MAX_PACKED = 128 * 1024 * 1024
MAX_FILES = 20000


def _private(path):
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("dsh_sdk_cache_not_private")
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("dsh_sdk_cache_not_private")
    for parent in path.parents:
        info = parent.stat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in {0, os.getuid()}
            or (info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))
        ):
            raise ValueError("dsh_sdk_cache_not_private")


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("dsh_sdk_archive_invalid")
        value[key] = item
    return value


def _members(raw):
    """Read bounded ASAR metadata only; no archive names are executed."""
    try:
        size_size, header_size, pickle_size, json_size = struct.unpack("<4I", raw[:16])
        start = 8 + header_size
        if (
            size_size != 4
            or header_size != pickle_size + 4
            or not 0 < json_size <= MAX_HEADER
            or not 16 + json_size <= start <= len(raw)
            or start - (16 + json_size) > 3
        ):
            raise ValueError
        tree = json.loads(raw[16 : 16 + json_size], object_pairs_hook=_object)
        node = tree["files"]["dsh"]["files"]["node_modules"]
        found = {}
        total = 0
        count = 0

        def walk(folder, parts):
            nonlocal total, count
            if (
                len(parts) > 32
                or not isinstance(folder, dict)
                or not isinstance(folder.get("files"), dict)
            ):
                raise ValueError
            for name, entry in folder["files"].items():
                if (
                    not isinstance(name, str)
                    or not name
                    or name in {".", ".."}
                    or any(c in name for c in ("/", "\\", "\x00"))
                    or not isinstance(entry, dict)
                    or "link" in entry
                ):
                    raise ValueError
                count += 1
                if count > MAX_FILES * 2:
                    raise ValueError
                if "files" in entry:
                    walk(entry, parts + [name])
                    continue
                # These files live outside the pinned archive's byte payload.
                # Never follow app.asar.unpacked or copy native executables.
                if entry.get("unpacked") is True:
                    continue
                size, offset = entry.get("size"), entry.get("offset")
                if (
                    type(size) is not int
                    or size < 0
                    or not isinstance(offset, str)
                    or not offset.isascii()
                    or not offset.isdigit()
                    or len(offset) > 12
                ):
                    raise ValueError
                begin = start + int(offset)
                end = begin + size
                total += size
                if end > len(raw) or total > MAX_PACKED or len(found) >= MAX_FILES:
                    raise ValueError
                key = "/".join(parts + [name])
                found[key] = (begin, end)

        walk(node, [])
        return found
    except (KeyError, TypeError, UnicodeError, ValueError, struct.error, RecursionError):
        raise ValueError("dsh_sdk_archive_invalid") from None


@contextlib.contextmanager
def _cache_lock(cache):
    import fcntl

    fd = os.open(
        cache / ".dsh-sdk.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
    )
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise ValueError("dsh_sdk_cache_not_private")
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("dsh_sdk_cache_busy") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def prepare(archive, cache):
    """Return (SDK directory, source-derived file hashes), never cached trust.

    Writes only this helper's cache. Reuses an intact generation; a modified
    generation is refused, not silently deleted/repaired. Cooperative writers
    serialize; the same-user filesystem is not an OS security sandbox.
    """
    if os.name == "nt":
        raise ValueError("dsh_unix_adapter_required")
    archive, cache = Path(archive), Path(cache)
    checked_file(archive)
    _private(cache)
    fd = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid not in {0, os.getuid()}
            or info.st_mode & 0o022
        ):
            raise ValueError("dsh_component_untrusted")
        if info.st_size > MAX_ARCHIVE:
            raise ValueError("dsh_sdk_archive_too_large")
        raw = stream.read(MAX_ARCHIVE + 1)
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) > MAX_ARCHIVE or digest not in SUPPORTED_ASAR:
        raise ValueError("dsh_sdk_version_unverified")
    members = _members(raw)
    hashes = {
        name: hashlib.sha256(memoryview(raw)[begin:end]).hexdigest()
        for name, (begin, end) in members.items()
    }
    destination = cache / ("dsh-" + digest)
    with _cache_lock(cache):
        if destination.exists() or destination.is_symlink():
            _private(destination)
            sdk = destination / "node_modules"
            verify_sdk(sdk, hashes)
            return sdk, hashes
        temporary = Path(tempfile.mkdtemp(prefix=".dsh-sdk-", dir=cache))
        try:
            sdk = temporary / "node_modules"
            sdk.mkdir(mode=0o700)
            for name, (begin, end) in members.items():
                target = sdk / name
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(memoryview(raw)[begin:end])
                target.chmod(0o600)
            verify_sdk(sdk, hashes)
            temporary.rename(destination)
        finally:
            # Only a directory created by this invocation, never someone else's
            # cache/host files. Interrupted preparations are never auto-replayed.
            if temporary.exists():
                shutil.rmtree(temporary)
        sdk = destination / "node_modules"
        verify_sdk(sdk, hashes)
        return sdk, hashes
