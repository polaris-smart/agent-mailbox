"""Synthetic immutable archives; no installed host, network, or model calls."""

import concurrent.futures
import hashlib
import json
import os
import struct
from pathlib import Path

import pytest

from agent_mailbox import workbench_dsh_sdk as sdk
from agent_mailbox.workbench_dsh_process import REQUIRED_SDK

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Unix DSH adapter")


def archive_bytes(entries=None):
    root = {"files": {}}
    payload = bytearray()
    for name in sorted(REQUIRED_SDK):
        folder = root
        parts = name.split("/")
        for part in parts[:-1]:
            folder = folder["files"].setdefault(part, {"files": {}})
        content = ("synthetic:" + name).encode()
        folder["files"][parts[-1]] = {"offset": str(len(payload)), "size": len(content)}
        payload.extend(content)
    if entries:
        root["files"].update(entries)
    header = json.dumps(
        {"files": {"dsh": {"files": {"node_modules": root}}}}, separators=(",", ":")
    ).encode()
    padded = (len(header) + 3) // 4 * 4
    return (
        struct.pack("<4I", 4, padded + 8, padded + 4, len(header))
        + header
        + b"\0" * (padded - len(header))
        + payload
    )


@pytest.fixture
def case(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    cache = root / "cache"
    cache.mkdir(mode=0o700)
    archive = root / "app.asar"

    def set_archive(raw):
        archive.write_bytes(raw)
        archive.chmod(0o600)
        monkeypatch.setattr(sdk, "SUPPORTED_ASAR", {hashlib.sha256(raw).hexdigest()})

    set_archive(archive_bytes())
    return archive, cache, set_archive


def test_extracts_exact_source_bytes_and_reuses_verified_cache(case):
    archive, cache, _ = case
    before = archive.read_bytes()
    directory, hashes = sdk.prepare(archive, cache)
    assert set(hashes) == REQUIRED_SDK
    stats = {n: (directory / n).stat().st_ino for n in hashes}
    for n, h in hashes.items():
        assert (directory / n).read_bytes() == ("synthetic:" + n).encode()
        assert hashlib.sha256((directory / n).read_bytes()).hexdigest() == h
    assert sdk.prepare(archive, cache) == (directory, hashes)
    assert stats == {n: (directory / n).stat().st_ino for n in hashes}
    assert archive.read_bytes() == before


def test_unknown_version_does_not_create_cache(case, monkeypatch):
    archive, cache, _ = case
    monkeypatch.setattr(sdk, "SUPPORTED_ASAR", set())
    with pytest.raises(ValueError, match="version_unverified"):
        sdk.prepare(archive, cache)
    assert not list(cache.iterdir())


@pytest.mark.parametrize("limit", ["MAX_ARCHIVE", "MAX_HEADER", "MAX_PACKED", "MAX_FILES"])
def test_size_limits_fail_before_writing(case, monkeypatch, limit):
    archive, cache, _ = case
    monkeypatch.setattr(sdk, limit, 1)
    with pytest.raises(ValueError):
        sdk.prepare(archive, cache)
    assert not list(cache.iterdir())


def test_symlink_cache_generation_is_not_followed(case):
    archive, cache, _ = case
    destination = cache / ("dsh-" + hashlib.sha256(archive.read_bytes()).hexdigest())
    other = cache / "outside"
    other.mkdir(mode=0o700)
    destination.symlink_to(other)
    with pytest.raises(ValueError, match="cache_not_private"):
        sdk.prepare(archive, cache)
    assert destination.is_symlink() and not list(other.iterdir())


def test_symlink_lock_does_not_touch_target(case):
    archive, cache, _ = case
    victim = cache / "victim"
    victim.write_bytes(b"unchanged")
    (cache / ".dsh-sdk.lock").symlink_to(victim)
    with pytest.raises(OSError):
        sdk.prepare(archive, cache)
    assert victim.read_bytes() == b"unchanged"


@pytest.mark.parametrize(
    "entry",
    [
        {"../escape": {"offset": "0", "size": 1}},
        {"/absolute": {"offset": "0", "size": 1}},
        {"back\\slash": {"offset": "0", "size": 1}},
        {"linked": {"link": "../../victim"}},
        {"outside": {"offset": "9999999", "size": 1}},
        {"negative": {"offset": "0", "size": -1}},
        {"bool": {"offset": "0", "size": True}},
        {"badfolder": {"files": []}},
    ],
)
def test_malformed_members_rejected_before_writing(case, entry):
    archive, cache, set_archive = case
    set_archive(archive_bytes(entry))
    with pytest.raises(ValueError, match="archive_invalid"):
        sdk.prepare(archive, cache)
    assert not list(cache.iterdir())


def test_unpacked_native_assets_are_not_followed(case):
    archive, cache, set_archive = case
    set_archive(archive_bytes({"native.node": {"size": 900000000, "unpacked": True}}))
    directory, hashes = sdk.prepare(archive, cache)
    assert "native.node" not in hashes and not (directory / "native.node").exists()


@pytest.mark.parametrize("mutation", ["content", "extra", "symlink"])
def test_changed_cache_is_refused_and_never_repaired(case, mutation):
    archive, cache, _ = case
    directory, hashes = sdk.prepare(archive, cache)
    file = directory / next(iter(hashes))
    if mutation == "content":
        file.write_bytes(b"external edit")
    elif mutation == "extra":
        file = directory / "extra.js"
        file.write_bytes(b"external edit")
    else:
        file.unlink()
        file.symlink_to(archive)
    with pytest.raises(ValueError):
        sdk.prepare(archive, cache)
    assert file.is_symlink() if mutation == "symlink" else file.read_bytes() == b"external edit"


def test_private_cache_required_before_writes(case):
    archive, cache, _ = case
    cache.chmod(0o755)
    with pytest.raises(ValueError, match="cache_not_private"):
        sdk.prepare(archive, cache)
    assert not list(cache.iterdir())


def test_failed_publication_removes_only_own_staging(case, monkeypatch):
    archive, cache, _ = case
    unrelated = cache / "unrelated"
    unrelated.write_text("keep")

    def fail(*args):
        raise OSError("simulated interruption")

    monkeypatch.setattr(Path, "rename", fail)
    with pytest.raises(OSError):
        sdk.prepare(archive, cache)
    assert unrelated.read_text() == "keep"
    assert {p.name for p in cache.iterdir()} == {"unrelated", ".dsh-sdk.lock"}


def test_concurrent_preparations_share_one_intact_generation(case):
    archive, cache, _ = case
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(lambda _: sdk.prepare(archive, cache), range(2)))
    assert values[0] == values[1]
    assert len(list(cache.glob("dsh-*"))) == 1
    assert not list(cache.glob(".dsh-sdk-*"))
