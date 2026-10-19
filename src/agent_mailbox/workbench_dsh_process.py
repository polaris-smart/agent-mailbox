"""Bounded private DSH preparation child, never a host or model launcher.

The caller authenticates the owner and resolves the selected launch context and
approved SDK manifest. No paths, pins, or commands may come straight from mail
or browser input. This internal boundary does not activate/bind the receiver.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath

FILES = (
    "prepare-bridge.mjs",
    "patch-plan.mjs",
    "setup.mjs",
    "index.mjs",
    "client.mjs",
    "package.json",
    "README.md",
)
REQUIRED_SDK = {
    "@deepseek-ai/dsh/package.json",
    "@deepseek-ai/dsh-app-boot/lib/index.js",
    "@deepseek-ai/dsh-atomic-write/lib/index.js",
    "@deepseek-ai/cordis-plugin-include/lib/index.js",
    "js-yaml/index.js",
    "yaml/dist/index.js",
}
LIMIT = 65536
STATES = {
    "preview",
    "expired",
    "prepared_disabled",
    "removed",
    "configuration_changed",
    "preparation_unconfirmed",
    "removal_unconfirmed",
}
FIELDS = {
    "plan_id",
    "state",
    "requires_confirmation",
    "will_activate",
    "session_verified",
    "delivery_verified",
}


def payload():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "agent_mailbox/host_receivers/dsh"
    return Path(__file__).resolve().parents[2] / "integrations/dsh-mailbox-host"


def checked_file(path, digest=None, *, executable=False):
    admin_gid = None
    if sys.platform == "darwin":
        import grp

        admin_gid = grp.getgrnam("admin").gr_gid
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("dsh_component_untrusted")
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid not in {0, os.getuid()}
        or info.st_mode & 0o022
        or (executable and not os.access(path, os.X_OK))
    ):
        raise ValueError("dsh_component_untrusted")
    for parent in path.parents:
        s = parent.stat()
        # macOS /Applications is root:admin 0775. System administrators are
        # already privileged; do not confuse it with a user-writable ancestor.
        admin_directory = (
            admin_gid is not None
            and s.st_uid == 0
            and s.st_gid == admin_gid
            and not s.st_mode & 0o002
        )
        if (
            not stat.S_ISDIR(s.st_mode)
            or s.st_uid not in {0, os.getuid()}
            or (
                s.st_mode & 0o022
                and not admin_directory
                and not (s.st_uid == 0 and s.st_mode & stat.S_ISVTX)
            )
        ):
            raise ValueError("dsh_component_untrusted")
    if digest is not None and (
        not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or hashlib.sha256(path.read_bytes()).hexdigest() != digest
    ):
        raise ValueError("dsh_component_changed")


def verify_sdk(root, expected):
    """Verify every file in the approved snapshot, including absence of extra code."""
    if (
        not root.is_absolute()
        or root.resolve(strict=True) != root
        or not isinstance(expected, dict)
        or not REQUIRED_SDK <= expected.keys()
        or not len(expected) <= 20000
    ):
        raise ValueError("dsh_sdk_pin_required")
    for name, digest in expected.items():
        if (
            not isinstance(name, str)
            or not name
            or "\\" in name
            or PurePosixPath(name).is_absolute()
            or ".." in PurePosixPath(name).parts
            or str(PurePosixPath(name)) != name
        ):
            raise ValueError("dsh_sdk_pin_required")
        checked_file(root / name, digest)
    actual = set()
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                raise ValueError("dsh_sdk_changed")
        for name in files:
            # macOS archive resource forks are not module resolver candidates.
            if name == ".DS_Store" or name.startswith("._"):
                continue
            actual.add((Path(directory) / name).relative_to(root).as_posix())
    if actual != set(expected):
        raise ValueError("dsh_sdk_changed")


def run(
    node, profile, request, *, expected_node, expected_files, expected_sdk, source=None, timeout=20
):
    """One invocation. Unknown completion never retries or kills the real host."""
    if os.name == "nt":
        raise ValueError("dsh_unix_adapter_required")
    node, profile, source = Path(node), Path(profile), Path(source or payload())
    if expected_node is None:
        raise ValueError("dsh_runtime_pin_required")
    checked_file(node, expected_node, executable=True)
    if not profile.is_absolute() or profile.resolve(strict=True) != profile:
        raise ValueError("dsh_profile_private_required")
    p = profile.lstat()
    if not stat.S_ISDIR(p.st_mode) or p.st_uid != os.getuid() or p.st_mode & 0o077:
        raise ValueError("dsh_profile_private_required")
    if not isinstance(expected_files, dict) or set(expected_files) != set(FILES):
        raise ValueError("dsh_component_pin_required")
    for name in FILES:
        checked_file(source / name, expected_files[name])
    if (
        not isinstance(request, dict)
        or not isinstance(request.get("context"), dict)
        or request["context"].get("dir") != str(profile)
        or not isinstance(request.get("sdk"), str)
    ):
        raise ValueError("dsh_profile_mismatch")
    raw = json.dumps(request).encode()
    if len(raw) > LIMIT:
        raise ValueError("dsh_request_too_large")
    verify_sdk(Path(request["sdk"]), expected_sdk)
    with tempfile.TemporaryDirectory(prefix="mailbox-dsh-") as temporary:
        # macOS commonly exposes the same temp directory as /var and /private/var.
        # Node canonicalizes cwd; HOME/TMPDIR must identify that same directory.
        temporary = str(Path(temporary).resolve(strict=True))
        # No NODE_OPTIONS/loaders, CODEX_*, proxies, keys or real HOME discovery.
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": temporary,
            "TMPDIR": temporary,
            "LANG": "en_US.UTF-8",
        }
        with tempfile.TemporaryFile() as data:
            data.write(raw)
            data.seek(0)
            child = subprocess.Popen(
                [str(node), str(source / "prepare-bridge.mjs")],
                stdin=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=temporary,
                env=env,
                start_new_session=True,
            )
            finished = False
            try:
                output = bytearray()
                deadline = time.monotonic() + timeout
                with selectors.DefaultSelector() as selector:
                    selector.register(child.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ValueError("dsh_preparation_result_unknown")
                        if not selector.select(remaining):
                            continue
                        chunk = os.read(child.stdout.fileno(), 8192)
                        if not chunk:
                            break
                        output.extend(chunk)
                        if len(output) > LIMIT:
                            raise ValueError("dsh_preparation_result_unknown")
                rc = child.wait(timeout=max(0.001, deadline - time.monotonic()))
                finished = True
                result = json.loads(output)
                if (
                    not isinstance(result, dict)
                    or result.get("version") != 1
                    or type(result.get("ok")) is not bool
                ):
                    raise ValueError("dsh_preparation_result_unknown")
                if rc != 0 or result["ok"] is not True:
                    raise ValueError("dsh_preparation_refused_or_incomplete")
                if set(result) != {"version", "ok", "result"} or not isinstance(
                    result["result"], dict
                ):
                    raise ValueError("dsh_preparation_result_unknown")
                view = result["result"]
                if (
                    set(view) != FIELDS
                    or not isinstance(view["plan_id"], str)
                    or not re.fullmatch(r"plan_[0-9a-f]{32}", view["plan_id"])
                    or not isinstance(view["state"], str)
                    or view["state"] not in STATES
                    or view["will_activate"] is not False
                    or view["session_verified"] is not False
                    or view["delivery_verified"] is not False
                    or type(view["requires_confirmation"]) is not bool
                    or (
                        request.get("operation") != "preview"
                        and view["plan_id"] != request.get("plan_id")
                    )
                ):
                    raise ValueError("dsh_preparation_result_unknown")
                return view
            except (OSError, UnicodeError, json.JSONDecodeError, subprocess.TimeoutExpired):
                raise ValueError("dsh_preparation_result_unknown") from None
            finally:
                if not finished:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                child.wait()
                child.stdout.close()
