"""Private, advisory host-idle signal for prompt retry of durable mail intents.

The socket carries only a session identifier. Mail authorization and receiver
readiness are rechecked by the normal wake worker before any delivery.
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import stat
import threading
from pathlib import Path

from .workbench_host_bindings import load_bindings

PROTOCOL = "agent-mailbox.host-activity.v1"


def start_host_activity_listener(store, socket_path: Path):
    """Listen for Codex turn completion; return a close function.

    The caller supplies an owner-private IPC directory and retains ownership
    of the listener for the lifetime of its single-writer Workbench process.
    """
    socket_path = Path(socket_path)
    parent = socket_path.parent
    info = parent.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or parent.resolve() != parent
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or len(os.fsencode(socket_path)) > 100
    ):
        raise ValueError("owner_private_activity_directory_required")

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            raw = self.rfile.readline(513)
            if len(raw) > 512 or not raw.endswith(b"\n"):
                return
            try:
                message = json.loads(raw)
            except (UnicodeError, json.JSONDecodeError):
                return
            if (
                not isinstance(message, dict)
                or set(message) != {"protocol", "kind", "thread_id"}
                or message["protocol"] != PROTOCOL
                or message["kind"] != "turn_completed"
            ):
                return
            thread_id = message["thread_id"]
            if (
                not isinstance(thread_id, str)
                or not 1 <= len(thread_id) <= 128
                or any(not (c.isascii() and (c.isalnum() or c in "_-")) for c in thread_id)
            ):
                return
            try:
                bound = any(
                    b["host_kind"] == "codex" and b["host_session_id"] == thread_id
                    for b in load_bindings(store.root)
                )
            except (OSError, ValueError):
                return
            if not bound:
                return
            waiter = {"event": threading.Event(), "completed": False}
            with store.mail_wake_ready_lock:
                store.mail_wake_ready_threads.add(thread_id)
                store.mail_wake_waiters.setdefault(thread_id, []).append(waiter)
            store.mail_wake_event.set()
            # The launcher holds Desktop's completion frame until this pass
            # finishes. Otherwise Desktop can start another turn before the
            # worker checks that the original thread is idle.
            waiter["event"].wait(8)
            with store.mail_wake_ready_lock:
                waiting = store.mail_wake_waiters.get(thread_id, [])
                if waiter in waiting:
                    waiting.remove(waiter)
                if not waiting:
                    store.mail_wake_waiters.pop(thread_id, None)
            answer = {
                "protocol": PROTOCOL,
                "thread_id": thread_id,
                "processed": waiter["completed"],
            }
            try:
                self.wfile.write((json.dumps(answer) + "\n").encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

    # A crashed Workbench can leave the socket inode behind. Only the current
    # single-writer owner may remove an owner-private, demonstrably unserved
    # socket. If another process accepts connections, fail closed.
    if socket_path.exists():
        prior = socket_path.lstat()
        if not stat.S_ISSOCK(prior.st_mode) or prior.st_uid != os.getuid() or prior.st_mode & 0o077:
            raise ValueError("owner_private_activity_socket_required")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            try:
                probe.connect(str(socket_path))
            except ConnectionRefusedError:
                socket_path.unlink()
            else:
                raise ValueError("activity_listener_already_running")
    server = Server(str(socket_path), Handler)
    socket_path.chmod(0o600)
    thread = threading.Thread(
        target=server.serve_forever, daemon=True, name="mailbox-host-activity"
    )
    thread.start()

    def close():
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        socket_path.unlink(missing_ok=True)

    return close
