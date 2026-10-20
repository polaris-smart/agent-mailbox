"""Owner-pinned local receiver endpoints. No shell, model, or guessed sessions."""

from __future__ import annotations

import errno
import json
import os
import re
import socket
import stat
import tempfile
import time
import uuid
from pathlib import Path

CONFIG = "host-endpoints.json"
PROTOCOL = "agent-mailbox.host-receipt.v1"
CODEX_HEALTH = "agent-mailbox.receiver-health.v1"
CODEX_CLI_HEALTH = "agent-mailbox.cli-receiver-health.v1"


def endpoints(home):
    path = Path(home) / "wake" / CONFIG
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return []
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("owner_private_endpoint_config_required")
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("endpoint_config_too_large")
    data = json.loads(raw)
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(data.get("endpoints"), list)
    ):
        raise ValueError("invalid_endpoint_config")
    ids = set()
    for row in data["endpoints"]:
        if not isinstance(row, dict) or any(
            not isinstance(row.get(k), str) or not row[k]
            for k in ("binding_id", "employee_id", "host_kind")
        ):
            raise ValueError("invalid_endpoint")
        if row["binding_id"] in ids or row["host_kind"] not in {"hermes", "deepseek", "codex"}:
            raise ValueError("ambiguous_or_unsupported_endpoint")
        ids.add(row["binding_id"])
        direct, discovery = "socket_path" in row, "launcher_directory" in row
        if direct == discovery or (discovery and row["host_kind"] != "codex"):
            raise ValueError("one_explicit_endpoint_location_required")
        mode = row.get("mode")
        if mode is not None and (
            mode != "on_demand_cli" or row["host_kind"] != "codex" or not direct
        ):
            raise ValueError("invalid_endpoint_mode")
        location = row.get("socket_path") if direct else row.get("launcher_directory")
        if not isinstance(location, str) or not location:
            raise ValueError("invalid_endpoint_path")
        path = Path(location)
        if not path.is_absolute() or "\0" in location or (direct and len(os.fsencode(path)) > 100):
            raise ValueError("invalid_endpoint_path")
    return data["endpoints"]


def configured(home, employee_id):
    if os.name == "nt":
        return False  # Native Windows transport is a separate acceptance gate.
    from .workbench_host_bindings import load_bindings

    current = {b["id"]: b for b in load_bindings(home) if b["employee_id"] == employee_id}
    return any(
        row["employee_id"] == employee_id
        and row["binding_id"] in current
        and row["host_kind"] == current[row["binding_id"]]["host_kind"]
        for row in endpoints(home)
    )


def configured_for_binding(home, binding):
    """Whether this exact route owns a native endpoint (including while offline)."""
    if os.name == "nt":
        return False
    return any(
        row["binding_id"] == binding["id"]
        and row["employee_id"] == binding["employee_id"]
        and row["host_kind"] == binding["host_kind"]
        for row in endpoints(home)
    )


def endpoint_for(home, binding):
    if os.name == "nt" or not binding:
        return None
    for row in endpoints(home):
        if row["binding_id"] == binding["id"]:
            if any(row[k] != binding[k] for k in ("employee_id", "host_kind")):
                raise ValueError("endpoint_binding_mismatch")
            from .workbench_host_bindings import load_bindings

            if not any(
                all(
                    current[k] == binding[k]
                    for k in ("id", "employee_id", "project_id", "host_kind", "host_session_id")
                )
                for current in load_bindings(home)
            ):
                raise ValueError("endpoint_binding_revoked_or_changed")
            if "launcher_directory" in row:
                return _codex_launcher_endpoint(
                    home, Path(row["launcher_directory"]), binding["host_session_id"]
                )
            return row["socket_path"]
    return None


def receiver_online(home, binding):
    """Check the selected private receiver without submitting a delivery."""
    endpoint = endpoint_for(home, binding)
    if endpoint is None:
        return False
    path = Path(endpoint)
    parent = path.parent
    try:
        info = parent.lstat()
    except FileNotFoundError:
        return False  # An unstarted receiver may not have created its socket directory.
    if (
        parent.resolve() != parent
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("owner_private_endpoint_directory_required")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("owner_private_socket_required")
    if binding["host_kind"] in {"hermes", "deepseek"}:
        # These adapters have a read-only health protocol. A listening socket
        # alone can be an older/broken receiver that rejects every delivery.
        from .workbench_receiver_status import _probe

        try:
            return _probe(
                endpoint,
                binding["host_kind"],
                timeout=1.0,
                binding=binding if binding["host_kind"] == "hermes" else None,
            )
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ECONNREFUSED, errno.ETIMEDOUT):
                raise
            return False
        except (ValueError, TimeoutError):
            return False
    mode = next(
        (row.get("mode") for row in endpoints(home) if row["binding_id"] == binding["id"]), None
    )
    if mode == "on_demand_cli":
        return _codex_cli_health(path, binding["host_session_id"])
    return _codex_health(path, binding["host_session_id"])


def _codex_cli_health(path, thread_id, *, timeout=2):
    """A stopped CLI may be resumable; this never claims its thread is loaded."""
    request = {
        "protocol": "agent-mailbox.receiver-control.v1",
        "request_id": uuid.uuid4().hex,
        "operation": "health",
        "thread_id": thread_id,
    }
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(timeout)
            probe.connect(str(path))
            probe.sendall(json.dumps(request).encode() + b"\n")
            raw = bytearray()
            while b"\n" not in raw and len(raw) <= 2048:
                chunk = probe.recv(2049 - len(raw))
                if not chunk:
                    return False
                raw.extend(chunk)
    except OSError as exc:
        if isinstance(exc, TimeoutError) or exc.errno in (
            errno.ENOENT,
            errno.ECONNREFUSED,
            errno.ETIMEDOUT,
        ):
            return False
        raise
    if len(raw) > 2048 or not raw.endswith(b"\n"):
        return False
    try:
        answer = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(answer, dict)
        and set(answer)
        == {"protocol", "request_id", "thread_id", "resumable", "admission_available"}
        and answer["protocol"] == CODEX_CLI_HEALTH
        and answer["request_id"] == request["request_id"]
        and answer["thread_id"] == thread_id
        and answer["resumable"] is True
        and answer["admission_available"] is True
    )


def _codex_health(path, thread_id, *, timeout=2):
    """Read-only original-thread admission check, with no mail or model call."""
    request = {
        "protocol": "agent-mailbox.receiver-control.v1",
        "request_id": uuid.uuid4().hex,
        "operation": "health",
        "thread_id": thread_id,
    }
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            deadline = time.monotonic() + timeout
            probe.settimeout(timeout)
            probe.connect(str(path))
            probe.settimeout(max(0.001, deadline - time.monotonic()))
            probe.sendall(json.dumps(request).encode() + b"\n")
            raw = bytearray()
            while b"\n" not in raw and len(raw) <= 2048:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                probe.settimeout(remaining)
                chunk = probe.recv(2049 - len(raw))
                if not chunk:
                    return False
                raw.extend(chunk)
    except OSError as exc:
        if isinstance(exc, TimeoutError) or exc.errno in (
            errno.ENOENT,
            errno.ECONNREFUSED,
            errno.ETIMEDOUT,
        ):
            return False
        raise
    if len(raw) > 2048 or not raw.endswith(b"\n"):
        return False
    try:
        answer = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(answer, dict)
        and set(answer) == {"protocol", "request_id", "thread_id", "loaded", "admission_available"}
        and answer["protocol"] == CODEX_HEALTH
        and answer["request_id"] == request["request_id"]
        and answer["thread_id"] == thread_id
        and answer["loaded"] is True
        and answer["admission_available"] is True
    )


def _codex_launcher_endpoint(home, directory, thread_id=None):
    """Resolve one live private launcher, using the bound thread when needed."""
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return None
    if (
        directory.resolve() != directory
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("owner_private_launcher_directory_required")
    manifests = []
    for file in directory.iterdir():
        if not re.fullmatch(r"[0-9]+-[a-f0-9]{12}\.json", file.name):
            continue
        manifests.append(file)
        # A crashed launcher can leave a manifest behind. Bound total work,
        # but do not let a few stale entries hide a live original session.
        if len(manifests) > 256:
            raise ValueError("too_many_launcher_manifests")
    candidates = []
    for file in manifests:
        try:
            fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            continue  # A launcher may finish while discovery is running.
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("owner_private_launcher_manifest_required")
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError("launcher_manifest_too_large")
        manifest = json.loads(raw)
        if not isinstance(manifest, dict) or manifest.get("version") != 1:
            raise ValueError("invalid_launcher_manifest")
        if manifest.get("mailbox_home") != str(Path(home).resolve()):
            continue  # Older manifests and another home's receivers are not ours.
        endpoint = file.with_suffix(".mail.sock")
        if manifest.get("notification_socket") != str(endpoint) or len(os.fsencode(endpoint)) > 100:
            raise ValueError("launcher_endpoint_path_mismatch")
        try:
            info = endpoint.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("owner_private_launcher_socket_required")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.1)
            try:
                probe.connect(str(endpoint))
            except OSError as exc:
                if exc.errno in (errno.ENOENT, errno.ECONNREFUSED):
                    continue
                raise  # Timeout/permissions are uncertainty, never proof of death.
        candidates.append(str(endpoint))
        if len(candidates) > 16:
            raise ValueError("too_many_live_codex_launchers")
    if len(candidates) > 1:
        if thread_id is None:
            raise ValueError("ambiguous_live_codex_launchers")
        matching = [path for path in candidates if _codex_health(Path(path), thread_id)]
        if len(matching) > 1:
            raise ValueError("ambiguous_live_codex_launchers")
        return matching[0] if matching else None
    return candidates[0] if candidates else None


def deliver(home, socket_path, payload, *, timeout=8):
    """Write a receipt only after exact identity validation; never retry after connect."""
    if not isinstance(payload.get("delivery_id"), str) or not re.fullmatch(
        r"wake-[A-Za-z0-9_-]+", payload["delivery_id"]
    ):
        raise ValueError("invalid_delivery_id")
    binding = payload["binding"]
    expected = {
        "protocol": PROTOCOL,
        "delivery_id": payload["delivery_id"],
        "employee_id": payload["employee_id"],
        "message_ids": payload["fresh"],
        "binding_id": binding["id"],
        "project_id": binding["project_id"],
        "host_kind": binding["host_kind"],
        "host_session_id": binding["host_session_id"],
    }
    if (
        payload.get("receipt_protocol") != PROTOCOL
        or expected["employee_id"] != binding["employee_id"]
    ):
        raise ValueError("outbox_identity_mismatch")
    path = Path(socket_path)
    parent = path.parent
    info = parent.lstat()
    if (
        parent.resolve() != parent
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("owner_private_endpoint_directory_required")
    result = {**expected, "status": "not_received"}
    try:
        info = path.lstat()
    except FileNotFoundError:
        pass  # Definitely not submitted.
    else:
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("owner_private_socket_required")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            deadline = time.monotonic() + timeout
            channel.settimeout(timeout)
            try:
                channel.connect(str(path))
            except OSError as exc:
                if exc.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                    raise
            else:
                channel.settimeout(max(0.001, deadline - time.monotonic()))
                channel.sendall(json.dumps(payload).encode() + b"\n")
                raw = bytearray()
                while b"\n" not in raw and len(raw) <= 65536:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("host_response_deadline_exceeded")
                    channel.settimeout(remaining)
                    chunk = channel.recv(65537 - len(raw))
                    if not chunk:
                        break
                    raw.extend(chunk)
                if len(raw) > 65536 or not raw.endswith(b"\n"):
                    raise ValueError("host_response_unconfirmed")
                result = json.loads(raw)
                if (
                    not isinstance(result, dict)
                    or result.get("status") not in {"received", "not_received"}
                    or any(result.get(k) != v for k, v in expected.items())
                ):
                    raise ValueError("host_receipt_mismatch")
    directory = Path(home) / "wake/receipts"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("owner_private_receipt_directory_required")
    fd, temporary = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(result, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / payload["delivery_id"])
    finally:
        Path(temporary).unlink(missing_ok=True)


def retract_dsh_queue(socket_path, payload, *, timeout=5):
    """Remove one exact queued DSH alert; never alter the original host receipt.

    ``None`` means the socket was definitively unavailable before connect.
    Once connected, any missing or malformed response is unknown to the caller.
    """
    binding = payload.get("binding")
    fresh = payload.get("fresh")
    delivery_id = payload.get("delivery_id")
    if (
        payload.get("operation") != "retract_opened"
        or payload.get("receipt_protocol") != "agent-mailbox.host-retraction.v1"
        or not isinstance(binding, dict)
        or binding.get("host_kind") != "deepseek"
        or payload.get("employee_id") != binding.get("employee_id")
        or not isinstance(delivery_id, str)
        or not re.fullmatch(r"wake-[A-Za-z0-9_-]+", delivery_id)
        or not isinstance(fresh, list)
        or not fresh
        or len(fresh) > 200
        or any(
            not isinstance(mid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", mid)
            for mid in fresh
        )
        or len(set(fresh)) != len(fresh)
        or any(
            not isinstance(binding.get(key), str) or not binding[key]
            for key in ("id", "employee_id", "project_id", "host_session_id")
        )
    ):
        raise ValueError("invalid_retraction_envelope")
    expected = {
        "protocol": "agent-mailbox.host-retraction.v1",
        "delivery_id": delivery_id,
        "binding_id": binding["id"],
        "project_id": binding["project_id"],
        "employee_id": binding["employee_id"],
        "message_ids": fresh,
    }
    path = Path(socket_path)
    parent = path.parent
    info = parent.lstat()
    if (
        parent.resolve() != parent
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("owner_private_endpoint_directory_required")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("owner_private_socket_required")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        deadline = time.monotonic() + timeout
        channel.settimeout(timeout)
        try:
            channel.connect(str(path))
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ECONNREFUSED):
                return None
            raise
        channel.sendall(json.dumps(payload).encode() + b"\n")
        raw = bytearray()
        while b"\n" not in raw and len(raw) <= 65536:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("host_retraction_deadline_exceeded")
            channel.settimeout(remaining)
            chunk = channel.recv(65537 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
    if len(raw) > 65536 or not raw.endswith(b"\n"):
        raise ValueError("host_retraction_unconfirmed")
    answer = json.loads(raw)
    if (
        not isinstance(answer, dict)
        or set(answer) != set(expected) | {"status"}
        or any(answer.get(key) != value for key, value in expected.items())
        or answer.get("status") not in {"retracted", "not_pending", "binding_changed"}
    ):
        raise ValueError("host_retraction_response_mismatch")
    return answer["status"]


def query_existing_hermes_delivery(home, socket_path, payload, *, timeout=2):
    """Reconcile an existing unknown delivery; this request can never schedule work."""
    if not isinstance(payload, dict) or not isinstance(payload.get("binding"), dict):
        raise TypeError("invalid_status_envelope")
    binding = payload["binding"]
    delivery_id = payload.get("delivery_id")
    if (
        binding.get("host_kind") != "hermes"
        or payload.get("receipt_protocol") != PROTOCOL
        or payload.get("employee_id") != binding.get("employee_id")
        or not isinstance(delivery_id, str)
        or not re.fullmatch(r"wake-[A-Za-z0-9_-]+", delivery_id)
        or not isinstance(payload.get("fresh"), list)
        or not payload["fresh"]
        or len(payload["fresh"]) > 200
        or any(not isinstance(item, str) or not item for item in payload["fresh"])
        or len(set(payload["fresh"])) != len(payload["fresh"])
        or any(
            not isinstance(binding.get(field), str) or not binding[field]
            for field in ("id", "employee_id", "project_id", "host_session_id")
        )
    ):
        raise ValueError("invalid_status_envelope")
    expected = {
        "protocol": PROTOCOL,
        "delivery_id": delivery_id,
        "employee_id": payload["employee_id"],
        "message_ids": payload["fresh"],
        "binding_id": binding["id"],
        "project_id": binding["project_id"],
        "host_kind": "hermes",
        "host_session_id": binding["host_session_id"],
    }
    path = Path(socket_path)
    parent = path.parent
    parent_info = parent.lstat()
    if (
        parent.resolve() != parent
        or not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != os.getuid()
        or parent_info.st_mode & 0o077
    ):
        raise ValueError("owner_private_endpoint_directory_required")
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("owner_private_socket_required")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        deadline = time.monotonic() + timeout
        channel.settimeout(timeout)
        channel.connect(str(path))
        channel.sendall(
            json.dumps(
                {
                    "operation": "delivery_status",
                    "envelope": payload,
                }
            ).encode()
            + b"\n"
        )
        raw = bytearray()
        while b"\n" not in raw and len(raw) <= 65536:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("host_status_deadline_exceeded")
            channel.settimeout(remaining)
            chunk = channel.recv(65537 - len(raw))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > 65536 or not raw.endswith(b"\n"):
            raise ValueError("host_status_unconfirmed")
    result = json.loads(raw)
    if (
        not isinstance(result, dict)
        or result.get("status") not in {"received", "unknown"}
        or any(result.get(key) != value for key, value in expected.items())
    ):
        raise ValueError("host_status_mismatch")
    if result["status"] != "received":
        return False
    directory = Path(home) / "wake/receipts"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory_info = directory.lstat()
    if (
        not stat.S_ISDIR(directory_info.st_mode)
        or directory_info.st_uid != os.getuid()
        or directory_info.st_mode & 0o077
    ):
        raise ValueError("owner_private_receipt_directory_required")
    fd, temporary = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(result, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / delivery_id)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True
