"""Bounded child adapter for an explicitly selected installed Hermes runtime.

Internal only: caller authenticates owner, local membership and profile. Full
receiver preparation also requires a session pin and exported credential;
diagnostic preparation cannot grant injection or install mailbox credentials.
Paths are server-resolved, never accepted from mail or a browser. The separate
diagnostic admission operation may synchronize plugin dependencies after explicit
confirmation; neither operation starts or reloads the host or retries unknowns.
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
from pathlib import Path

FILES = (
    "bridge.py",
    "setup.py",
    "discovery.py",
    "__init__.py",
    "pinned.py",
    "live.py",
    "plugin.yaml",
    "README.md",
)
LIMIT = 65536
RESULT_FIELDS = {
    "plan_id",
    "state",
    "requires_confirmation",
    "will_activate",
    "will_write_injection_grant",
    "session_verified",
    "delivery_verified",
    "activation_requested",
}
STATES = {
    "preview",
    "preparation_incomplete",
    "preparation_removed",
    "prepared_not_activated",
    "configuration_changed",
}
PIN_STATES = {
    "preview",
    "cancelled_before_apply",
    "interrupted_before_config_write",
    "configured_runtime_unverified",
    "rollback_completed_unrecorded",
    "rolled_back",
    "inspection_required",
}
UPGRADE_STATES = {
    "preview",
    "published_runtime_unverified",
    "interrupted_before_publication",
    "rollback_completed_unrecorded",
    "rolled_back",
    "inspection_required",
}
MCP_COMMAND_STATES = {
    "preview",
    "cancelled",
    "interrupted_before_config_write",
    "configured_runtime_unverified",
    "rollback_incomplete_before_config_write",
    "rollback_completed_unrecorded",
    "rolled_back",
    "inspection_required",
}
DIAGNOSTIC_ADMISSION_STATES = {
    "preview",
    "admission_unknown",
    "rollback_unknown",
    "diagnostic_enabled_restart_required",
    "rolled_back",
    "inspection_required",
}


def payload():
    if getattr(sys, "frozen", False):
        # macOS PyInstaller places a Frameworks link to the sealed Resources
        # directory.  Resolve it before the exact bundle-scope check below.
        return (Path(sys._MEIPASS) / "agent_mailbox/host_receivers/hermes").resolve(strict=True)
    return Path(__file__).resolve().parents[2] / "integrations/hermes-mailbox-host"


def _file(path, *, executable=False):
    resolved = path.resolve(strict=True)
    if not resolved.is_absolute():
        raise ValueError("hermes_runtime_invalid")
    info = resolved.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid not in {os.getuid(), 0}
        or info.st_mode & 0o022
        or (executable and not os.access(resolved, os.X_OK))
    ):
        raise ValueError("hermes_runtime_invalid")
    # Venv Python is usually a symlink: validate target, preserve original venv
    # path when launching, and reject writable ancestors on both spellings.
    for item in {*path.parents, *resolved.parents}:
        check = item.stat()
        if not stat.S_ISDIR(check.st_mode) or check.st_uid not in {os.getuid(), 0}:
            raise ValueError("hermes_runtime_invalid")
        if (
            check.st_mode & 0o022
            and not (check.st_uid == 0 and check.st_mode & stat.S_ISVTX)
            and not _macos_applications_ancestor(item, check)
        ):
            raise ValueError("hermes_runtime_invalid")
    return resolved


def _macos_applications_ancestor(path, info):
    # macOS normally makes /Applications root:admin 0775.  This one system
    # directory is admissible for a signed App; no other writable ancestor is.
    return (
        sys.platform == "darwin"
        and path == Path("/Applications")
        and info.st_uid == 0
        and info.st_gid == 80
        and stat.S_ISDIR(info.st_mode)
        and info.st_mode & 0o777 == 0o775
        and not path.is_symlink()
    )


def _verify_frozen_bundle(source):
    if not getattr(sys, "frozen", False) or sys.platform != "darwin":
        return
    bundle = Path(sys.executable).resolve().parents[2]
    expected = bundle / "Contents/Resources/agent_mailbox/host_receivers/hermes"
    if source != expected or bundle.suffix != ".app":
        raise ValueError("hermes_bundle_scope_unconfirmed")
    try:
        result = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle)],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("hermes_bundle_signature_unconfirmed") from None
    if result.returncode != 0:
        raise ValueError("hermes_bundle_signature_unconfirmed")


def _profiles(projection, root):
    """Validate the private child projection before any owner-facing mapping."""
    if (
        set(projection) != {"state", "profiles", "session_verified", "delivery_verified"}
        or projection.get("state") != "profile_candidates"
        or projection.get("session_verified") is not False
        or projection.get("delivery_verified") is not False
        or not isinstance(projection.get("profiles"), list)
        or len(projection["profiles"]) > 64
    ):
        raise ValueError("hermes_preparation_result_unknown")
    seen = set()
    for item in projection["profiles"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"profile_id", "home"}
            or not isinstance(item["profile_id"], str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", item["profile_id"])
            or item["profile_id"] in seen
            or not isinstance(item["home"], str)
        ):
            raise ValueError("hermes_preparation_result_unknown")
        seen.add(item["profile_id"])
        expected = (
            root if item["profile_id"] == "default" else root / "profiles" / item["profile_id"]
        )
        if item["home"] != str(expected) or expected.resolve() != expected:
            raise ValueError("hermes_preparation_result_unknown")
        info = expected.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("hermes_preparation_result_unknown")
    return projection


def run(python, profile, request, *, source=None, expected_files=None, timeout=20):
    """Execute once; timeout/malformed results are unknown, never retry mutations."""
    if os.name == "nt":
        raise ValueError("hermes_unix_adapter_required")
    python, profile = Path(python), Path(profile)
    if not python.is_absolute() or not profile.is_absolute() or profile.resolve() != profile:
        raise ValueError("hermes_runtime_invalid")
    _file(python, executable=True)
    info = profile.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("hermes_profile_private_required")
    source = Path(source or payload())
    _verify_frozen_bundle(source)
    if source.resolve() != source or expected_files is None or set(expected_files) != set(FILES):
        raise ValueError("hermes_component_pin_required")
    for name in FILES:
        path = source / name
        if (
            _file(path) != path
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected_files[name]
        ):
            raise ValueError("hermes_component_changed")
    if not isinstance(request, dict) or request.get("profile") != str(profile):
        raise ValueError("hermes_profile_mismatch")
    raw = json.dumps(request).encode()
    if len(raw) > LIMIT:
        raise ValueError("hermes_request_too_large")
    # Do not inherit CODEX_*, proxies, API keys, PYTHONPATH, startup hooks or
    # another host's runtime flags. Hermes uses its own selected profile.
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(Path.home()),
        "HERMES_HOME": str(profile),
        "LANG": "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    with tempfile.TemporaryDirectory(prefix="mailbox-hermes-") as temporary:
        env["TMPDIR"] = temporary
        with tempfile.TemporaryFile() as data:
            data.write(raw)
            data.seek(0)
            child = subprocess.Popen(
                [str(python), "-I", "-B", str(source / "bridge.py")],
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
                            raise ValueError("hermes_preparation_result_unknown")
                        if not selector.select(remaining):
                            continue
                        block = os.read(child.stdout.fileno(), 8192)
                        if not block:
                            break
                        output.extend(block)
                        if len(output) > LIMIT:
                            raise ValueError("hermes_preparation_result_unknown")
                rc = child.wait(timeout=max(0.001, deadline - time.monotonic()))
                finished = True
                result = json.loads(output)
                if (
                    not isinstance(result, dict)
                    or result.get("version") != 1
                    or type(result.get("ok")) is not bool
                ):
                    raise ValueError("hermes_preparation_result_unknown")
                if rc != 0 or not result["ok"]:
                    raise ValueError("hermes_preparation_refused_or_incomplete")
                if set(result) != {"version", "ok", "result"} or not isinstance(
                    result["result"], dict
                ):
                    raise ValueError("hermes_preparation_result_unknown")
                projection = result["result"]
                if request.get("operation") in {"grant_preview", "grant_apply"}:
                    expected_states = (
                        {"grant_preview", "already_enabled"}
                        if request["operation"] == "grant_preview"
                        else {"grant_enabled_runtime_unverified", "already_enabled"}
                    )
                    if (
                        set(projection)
                        != {
                            "state",
                            "config_sha256",
                            "enabled",
                            "runtime_verified",
                            "delivery_verified",
                        }
                        or not isinstance(projection.get("state"), str)
                        or projection.get("state") not in expected_states
                        or not isinstance(projection.get("config_sha256"), str)
                        or re.fullmatch(r"[a-f0-9]{64}", projection["config_sha256"]) is None
                        or type(projection.get("enabled")) is not bool
                        or projection["enabled"] != (projection["state"] != "grant_preview")
                        or projection.get("runtime_verified") is not False
                        or projection.get("delivery_verified") is not False
                    ):
                        raise ValueError("hermes_grant_result_unknown")
                    return projection
                if request.get("operation") == "profiles":
                    return _profiles(projection, profile)
                if request.get("operation") in {
                    "diagnostic_admission_preview",
                    "diagnostic_admission_apply",
                    "diagnostic_admission_status",
                    "diagnostic_admission_rollback",
                }:
                    if (
                        set(projection)
                        != {
                            "plan_id",
                            "state",
                            "requires_confirmation",
                            "requires_stopped_gateway",
                            "runtime_verified",
                            "session_verified",
                            "delivery_verified",
                        }
                        or not isinstance(projection.get("plan_id"), str)
                        or re.fullmatch(r"plan_[0-9a-f]{32}", projection["plan_id"]) is None
                        or projection.get("state") not in DIAGNOSTIC_ADMISSION_STATES
                        or projection.get("requires_confirmation") is not True
                        or projection.get("requires_stopped_gateway") is not True
                        or any(
                            projection.get(key) is not False
                            for key in ("runtime_verified", "session_verified", "delivery_verified")
                        )
                        or (
                            request["operation"] != "diagnostic_admission_preview"
                            and projection["plan_id"] != request.get("plan_id")
                        )
                    ):
                        raise ValueError("hermes_diagnostic_admission_result_unknown")
                    return projection
                if request.get("operation") in {
                    "upgrade_preview",
                    "upgrade_apply",
                    "upgrade_status",
                    "upgrade_rollback",
                }:
                    if (
                        not set(projection)
                        <= {
                            "plan_id",
                            "state",
                            "requires_confirmation",
                            "requires_stopped_gateway",
                            "will_write_config",
                            "will_reload_gateway",
                            "runtime_verified",
                            "session_verified",
                            "delivery_verified",
                        }
                        or not isinstance(projection.get("plan_id"), str)
                        or not re.fullmatch(r"plan_[0-9a-f]{32}", projection["plan_id"])
                        or projection.get("state") not in UPGRADE_STATES
                        or any(
                            projection.get(key) is not False
                            for key in ("runtime_verified", "session_verified", "delivery_verified")
                            if key in projection
                        )
                        or any(
                            projection.get(key) is not True
                            for key in ("requires_confirmation", "requires_stopped_gateway")
                            if key in projection
                        )
                        or any(
                            projection.get(key) is not False
                            for key in ("will_write_config", "will_reload_gateway")
                            if key in projection
                        )
                        or (
                            request["operation"] != "upgrade_preview"
                            and projection["plan_id"] != request.get("plan_id")
                        )
                    ):
                        raise ValueError("hermes_upgrade_result_unknown")
                    return projection
                if request.get("operation") in {
                    "pin_preview",
                    "pin_apply",
                    "pin_status",
                    "pin_rollback",
                    "pin_refresh",
                }:
                    if (
                        not set(projection)
                        <= {
                            "plan_id",
                            "state",
                            "requires_confirmation",
                            "requires_stopped_gateway",
                            "session_verified",
                            "delivery_verified",
                            "candidate_fingerprint",
                            "expires_at",
                        }
                        or not isinstance(projection.get("plan_id"), str)
                        or not re.fullmatch(r"plan_[0-9a-f]{32}", projection["plan_id"])
                        or projection.get("state") not in PIN_STATES
                        or projection.get("session_verified") is not False
                        or projection.get("delivery_verified") is not False
                        or (
                            "candidate_fingerprint" in projection
                            and (
                                not isinstance(projection["candidate_fingerprint"], str)
                                or not re.fullmatch(
                                    r"[a-f0-9]{64}", projection["candidate_fingerprint"]
                                )
                            )
                        )
                        or (
                            "expires_at" in projection
                            and (
                                type(projection["expires_at"]) not in {int, float}
                                or not 0 < projection["expires_at"] < 10**12
                            )
                        )
                        or any(
                            projection.get(key) is not True
                            for key in ("requires_confirmation", "requires_stopped_gateway")
                            if key in projection
                        )
                        or (
                            request["operation"] != "pin_preview"
                            and projection["plan_id"] != request.get("plan_id")
                        )
                    ):
                        raise ValueError("hermes_pin_result_unknown")
                    return projection
                if request.get("operation") in {
                    "mcp_command_preview",
                    "mcp_command_apply",
                    "mcp_command_status",
                    "mcp_command_rollback",
                }:
                    if (
                        set(projection)
                        != {
                            "plan_id",
                            "state",
                            "requires_confirmation",
                            "requires_stopped_gateway",
                            "runtime_verified",
                            "session_verified",
                            "delivery_verified",
                        }
                        or not isinstance(projection.get("plan_id"), str)
                        or not re.fullmatch(r"plan_[0-9a-f]{32}", projection["plan_id"])
                        or projection.get("state") not in MCP_COMMAND_STATES
                        or projection.get("requires_confirmation") is not True
                        or projection.get("requires_stopped_gateway") is not True
                        or any(
                            projection.get(key) is not False
                            for key in ("runtime_verified", "session_verified", "delivery_verified")
                        )
                        or (
                            request["operation"] != "mcp_command_preview"
                            and projection["plan_id"] != request.get("plan_id")
                        )
                    ):
                        raise ValueError("hermes_mcp_command_result_unknown")
                    return projection
                if (
                    not set(projection) <= RESULT_FIELDS
                    or not isinstance(projection.get("plan_id"), str)
                    or not re.fullmatch(r"plan_[0-9a-f]{32}", projection["plan_id"])
                    or not isinstance(projection.get("state"), str)
                    or projection.get("state") not in STATES
                    or projection.get("session_verified") is not False
                    or projection.get("delivery_verified") is not False
                    or any(
                        projection.get(key, False) is not False
                        for key in ("activation_requested", "will_activate")
                    )
                    or any(
                        type(projection[key]) is not bool
                        for key in ("requires_confirmation", "will_write_injection_grant")
                        if key in projection
                    )
                    or (
                        request.get("operation") == "preview_discovery"
                        and projection.get("will_write_injection_grant") is not False
                    )
                    or (
                        request.get("operation") not in {"preview", "preview_discovery"}
                        and projection["plan_id"] != request.get("plan_id")
                    )
                ):
                    raise ValueError("hermes_preparation_result_unknown")
                return result["result"]
            except (OSError, UnicodeError, json.JSONDecodeError, subprocess.TimeoutExpired):
                raise ValueError("hermes_preparation_result_unknown") from None
            finally:
                # Only our isolated child process group; never the existing host.
                if not finished:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        # Some macOS runners deny a signal to the process
                        # group after its leader exits. If it is still our
                        # child, kill that exact process before reaping it.
                        if child.poll() is None:
                            child.kill()
                child.wait()
                child.stdout.close()
