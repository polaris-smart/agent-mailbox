"""Real directory locks serialize processes and recover after holder exit."""

import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from agent_mailbox import workbench_wake as wk

WORKER = """
import pathlib, sys, time
from agent_mailbox import workbench_wake as wk
home, started, acquired, release = map(pathlib.Path, sys.argv[1:])
started.touch()
with wk.state_lock(home):
    wk.save_state(home, wk.empty_state())
    assert wk.load_state(home)['enabled'] is True
    for name in ('claims', 'wake-outbox'):
        directory = wk.state_dir(home) / name
        directory.mkdir(exist_ok=True)
        item = directory / 'test.marker'
        item.write_text('temporary', encoding='utf-8')
        assert item in list(directory.glob('*.marker'))
        assert item.read_text(encoding='utf-8') == 'temporary'
        item.unlink()
        directory.rmdir()
    acquired.touch()
    while not release.exists():
        time.sleep(0.02)
"""


def _wait_for(path, process):
    deadline = time.monotonic() + 10
    while not path.exists():
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f"lock worker exited {process.returncode}: {stderr or stdout}")
        assert time.monotonic() < deadline, f"worker did not create {path}"
        time.sleep(0.02)


def _worker(home, tmp_path, name):
    started, acquired, release = [
        tmp_path / f"{name}.{phase}" for phase in ("start", "lock", "end")
    ]
    process = subprocess.Popen(
        [sys.executable, "-c", WORKER, str(home), str(started), str(acquired), str(release)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return process, started, acquired, release


@pytest.mark.parametrize("abrupt_exit", [False, True])
def test_process_lock_survives_lock_file_deletion_and_recovers_on_exit(tmp_path, abrupt_exit):
    home = tmp_path / "home"
    holder, _, holder_acquired, holder_release = _worker(home, tmp_path, "holder")
    waiter = None
    try:
        _wait_for(holder_acquired, holder)
        # The old file must never become a second lock anchor.
        legacy = wk.state_dir(home) / "wake.lock"
        legacy.touch()
        legacy.unlink()
        waiter, waiter_started, waiter_acquired, waiter_release = _worker(home, tmp_path, "waiter")
        _wait_for(waiter_started, waiter)
        time.sleep(0.3)
        assert waiter.poll() is None, "the contender must wait rather than fail or run unlocked"
        assert not waiter_acquired.exists(), "deleting wake.lock bypassed the directory lock"
        if sys.platform == "win32":
            # No FILE_SHARE_DELETE: replacing the directory cannot introduce a second anchor.
            with pytest.raises(OSError):
                wk.state_dir(home).rename(tmp_path / "replaced-wake")
        if abrupt_exit:
            holder.terminate()
        else:
            holder_release.touch()
        holder.communicate(timeout=10)
        if not abrupt_exit:
            assert holder.returncode == 0
        _wait_for(waiter_acquired, waiter)
        waiter_release.touch()
        stdout, stderr = waiter.communicate(timeout=10)
        assert waiter.returncode == 0, stderr or stdout
    finally:
        for process in (holder, waiter):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=10)


def test_exception_releases_directory_lock(tmp_path):
    with pytest.raises(RuntimeError, match="abort"), wk.state_lock(tmp_path):
        raise RuntimeError("abort")
    with wk.state_lock(tmp_path):
        wk.save_state(tmp_path, wk.empty_state())
    assert wk.load_state(tmp_path)["enabled"] is True


def test_unusable_lock_directory_fails_closed(tmp_path):
    wk.state_dir(tmp_path).write_text("not a directory", encoding="utf-8")
    with pytest.raises(wk.WakeLockBusy), wk.state_lock(tmp_path):
        pytest.fail("an unavailable lock directory must never enter the critical section")


@pytest.mark.parametrize("error", [5, 32])
def test_windows_lock_fails_closed_on_access_denial_or_timeout(tmp_path, monkeypatch, error):
    import ctypes

    calls = []

    def create(*args):
        calls.append(args)
        return ctypes.c_void_p(-1).value

    def close(handle):
        pytest.fail("an invalid Windows handle must never be closed")

    kernel = SimpleNamespace(CreateFileW=create, CloseHandle=close)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error, raising=False)
    monkeypatch.setattr(wk.time, "monotonic", lambda: 2)
    with pytest.raises(wk.WakeLockBusy), wk._windows_directory_lock(tmp_path, deadline=1):
        pytest.fail("lock acquisition failed; the critical section must not run")
    assert len(calls) == 1


def test_windows_lock_retries_contention_and_closes_handle_on_exception(tmp_path, monkeypatch):
    import ctypes

    handles = iter([ctypes.c_void_p(-1).value, 123])
    closed = []

    def create(*args):
        return next(handles)

    def close(handle):
        closed.append(handle)
        return True

    kernel = SimpleNamespace(CreateFileW=create, CloseHandle=close)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 32, raising=False)
    monkeypatch.setattr(wk.time, "sleep", lambda _: None)
    with (
        pytest.raises(RuntimeError, match="abort"),
        wk._windows_directory_lock(tmp_path, deadline=time.monotonic() + 10),
    ):
        raise RuntimeError("abort")
    assert closed == [123]
