"""Real Unix IPC lifecycle; no Gateway, model, mailbox data or credentials."""

import errno
import importlib.util
import os
import select
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

if sys.platform == "win32":
    pytest.skip("Unix receiver only", allow_module_level=True)

SOURCE = Path(__file__).resolve().parents[1] / "integrations/hermes-mailbox-host/__init__.py"
SPEC = importlib.util.spec_from_file_location("hermes_socket_plugin", SOURCE)
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


@pytest.fixture
def endpoint():
    with tempfile.TemporaryDirectory(prefix="am-hs-recover-", dir=Path("/tmp").resolve()) as d:
        yield Path(d) / "host.sock"


def test_crashed_process_recovers_without_manual_deletion(endpoint):
    script = """
import importlib.util, sys
s = importlib.util.spec_from_file_location('plugin', sys.argv[1])
p = importlib.util.module_from_spec(s)
s.loader.exec_module(p)
server = p.LocalServer(sys.argv[2], None)
print('ready', flush=True)
sys.stdin.read()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(SOURCE), str(endpoint)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert select.select([child.stdout], [], [], 5)[0], "receiver failed to start"
        line = child.stdout.readline().strip()
        assert line == "ready", child.stderr.read() if not line else line
        child.kill()
        child.wait(timeout=5)
        assert endpoint.is_socket()
        with plugin.LocalServer(endpoint, None), socket.socket(socket.AF_UNIX) as client:
            client.settimeout(1)
            client.connect(str(endpoint))
        assert not endpoint.exists()
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def test_second_receiver_cannot_steal_live_endpoint(endpoint):
    with plugin.LocalServer(endpoint, None):
        before = endpoint.stat().st_ino
        with pytest.raises((ValueError, OSError)):
            plugin.LocalServer(endpoint, None)
        assert endpoint.stat().st_ino == before
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(endpoint))


def test_unlocked_live_listener_is_also_preserved(endpoint):
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(endpoint))
        listener.listen(1)
        os.chmod(endpoint, 0o600)
        before = endpoint.stat().st_ino
        with pytest.raises((ValueError, OSError)):
            plugin.LocalServer(endpoint, None)
        assert endpoint.stat().st_ino == before


@pytest.mark.parametrize(
    "kind", ["file", "symlink", "public_socket", "lock_symlink", "public_lock"]
)
def test_unsafe_paths_preserved(endpoint, kind):
    listener = socket.socket(socket.AF_UNIX)
    target = endpoint
    if kind == "file":
        endpoint.write_text("keep")
    elif kind == "symlink":
        endpoint.symlink_to(endpoint.parent / "absent")
    elif kind == "public_socket":
        listener.bind(str(endpoint))
        listener.close()
        os.chmod(endpoint, 0o666)
    else:
        target = Path(str(endpoint) + ".lock")
        if kind == "lock_symlink":
            target.symlink_to(endpoint.parent / "absent")
        else:
            target.write_text("keep")
            os.chmod(target, 0o644)
    try:
        before = target.lstat()
        with pytest.raises((ValueError, OSError)):
            plugin.LocalServer(endpoint, None)
        assert target.lstat().st_ino == before.st_ino
    finally:
        listener.close()


def test_old_cleanup_never_deletes_replacement(endpoint):
    server = plugin.LocalServer(endpoint, None)
    endpoint.unlink()
    endpoint.write_text("replacement")
    server.server_close()
    server.server_close()
    assert endpoint.read_text() == "replacement"


@pytest.mark.parametrize("failure", [TimeoutError(), PermissionError(errno.EACCES, "denied")])
def test_ambiguous_liveness_never_removes_socket(endpoint, monkeypatch, failure):
    with socket.socket(socket.AF_UNIX) as stale:
        stale.bind(str(endpoint))
    os.chmod(endpoint, 0o600)
    before = endpoint.stat().st_ino

    class AmbiguousProbe:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, timeout):
            pass

        def connect(self, path):
            raise failure

    monkeypatch.setattr(plugin.socket, "socket", lambda *args: AmbiguousProbe())
    with pytest.raises(ValueError, match="socket_liveness_unknown"):
        plugin.LocalServer(endpoint, None)
    assert endpoint.stat().st_ino == before
