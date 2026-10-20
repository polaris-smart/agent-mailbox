"""Bounded project TOML plans and POSIX transactions; not an owner API.

Callers must authorize the registered workspace and employee, check effective
host configuration layers, and retain plans/receipts privately. Never accept a
command, workspace or ownership block from an untrusted HTTP request. No host
reload, credential issuance, project trust or delivery is performed here.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

LIMIT = 256 * 1024
SERVER = "agent-mailbox-project"


def _parse(data):
    if not isinstance(data, bytes) or len(data) > LIMIT:
        raise ValueError("codex_config_size")
    try:
        return tomllib.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("codex_config_invalid_toml") from exc


def _mailbox_servers(document):
    servers = document.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ValueError("codex_config_invalid_servers")  # noqa: TRY004 - invalid TOML value
    found = []
    for name, settings in servers.items():
        if not isinstance(settings, dict):
            raise ValueError("codex_config_invalid_server")  # noqa: TRY004 - invalid TOML value
        # Conservative local detection, not proof about arbitrary wrapper scripts
        # or inherited/global host layers (the owner service must check those).
        text = json.dumps([name, settings.get("command"), settings.get("args")]).lower()
        if any(
            token in text
            for token in ("agent-mailbox", "agent_mailbox", "mailbox-mcp", "--session-file")
        ):
            found.append(name)
    return found


@dataclass(frozen=True)
class ConfigPlan:
    before: bytes | None
    after: bytes
    owned_block: bytes | None

    def preview(self):
        """Never include unrelated config: it may contain secrets."""
        return {
            "before_sha256": _sha(self.before) if self.before is not None else None,
            "after_sha256": _sha(self.after),
            "added_config": self.owned_block.decode() if self.owned_block else "",
            "scope": "workspace_codex_sessions",
            "state": "confirmation_required",
        }


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def plan_install(before, *, command, args):
    """Append without rewriting user bytes; reject all pre-existing mailboxes."""
    source = before if before is not None else b""
    document = _parse(source)
    if _mailbox_servers(document):
        raise ValueError("codex_mailbox_identity_conflict")
    if (
        not isinstance(command, str)
        or not Path(command).is_absolute()
        or not isinstance(args, list)
        or not all(isinstance(a, str) for a in args)
        or len(args) not in (3, 5, 6)
    ):
        raise ValueError("codex_mailbox_invalid_command")
    if (
        args[-3:-1] != ["mailbox-mcp", "--session-file"]
        or (len(args) == 5 and args[:2] != ["-m", "agent_mailbox"])
        or (len(args) == 6 and args[:3] != ["-E", "-m", "agent_mailbox"])
        or not Path(args[-1]).is_absolute()
        or any("\x00" in value or len(value) > 8192 for value in [command, *args])
    ):
        raise ValueError("codex_mailbox_invalid_command")
    # JSON strings/arrays here are a subset of TOML basic strings/arrays.
    settings = {"command": command, "args": args}
    block = (
        "\n# agent-mailbox managed MCP begin v1\n"
        f'[mcp_servers."{SERVER}"]\n'
        f"command = {json.dumps(command, ensure_ascii=False)}\n"
        f"args = {json.dumps(args, ensure_ascii=False)}\n"
        "# agent-mailbox managed MCP end v1\n"
    ).encode()
    after = source + block
    try:
        actual = _parse(after)
    except ValueError as exc:
        # Includes sealed inline mcp_servers tables. Do not silently reformat.
        raise ValueError("codex_config_append_requires_manual_merge") from exc
    expected = {**document, "mcp_servers": {**document.get("mcp_servers", {}), SERVER: settings}}
    if actual != expected:
        raise ValueError("codex_config_semantic_change")
    return ConfigPlan(before, after, block)


def plan_remove(before, *, owned_block):
    """Remove only a privately recorded exact block, preserving later edits."""
    actual = _parse(before)
    owned = _parse(owned_block)
    if (
        set(owned) != {"mcp_servers"}
        or set(owned["mcp_servers"]) != {SERVER}
        or _mailbox_servers(actual) != [SERVER]
        or actual["mcp_servers"][SERVER] != owned["mcp_servers"][SERVER]
        or not owned_block
        or before.count(owned_block) != 1
    ):
        raise ValueError("codex_config_owned_block_changed")
    after = before.replace(owned_block, b"", 1)
    expected = {**actual, "mcp_servers": dict(actual["mcp_servers"])}
    del expected["mcp_servers"][SERVER]
    parsed = _parse(after)
    if not expected["mcp_servers"] and "mcp_servers" not in parsed:
        del expected["mcp_servers"]
    if parsed != expected:
        raise ValueError("codex_config_semantic_change")
    return ConfigPlan(before, after, None)


def plan_replace(before, *, owned_block, command, args):
    """Renew only a verified owned entry, in one write, never remove then add."""
    removal = plan_remove(before, owned_block=owned_block)
    replacement = plan_install(removal.after, command=command, args=args)
    return ConfigPlan(before, replacement.after, replacement.owned_block)


def _directory(path, *, private=False):
    path = Path(path)
    if os.name != "posix":
        raise ValueError("codex_config_transaction_platform_unsupported")
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("codex_config_canonical_directory_required")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & (0o077 if private else 0o022):
        os.close(fd)
        raise ValueError("codex_config_unsafe_directory")
    return fd


def _read(fd):
    try:
        file_fd = os.open("config.toml", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None, 0o600
    with os.fdopen(file_fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or info.st_mode & 0o7022
            or info.st_size > LIMIT
        ):
            raise ValueError("codex_config_unsafe_file")
        data = stream.read(LIMIT + 1)
    _parse(data)
    return data, stat.S_IMODE(info.st_mode)


@contextmanager
def _config_dir(workspace, *, create=False):
    root = Path(workspace)
    root_fd = _directory(root)
    try:
        path = root / ".codex"
        if create:
            try:
                os.mkdir(".codex", mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
        try:
            fd = _directory(path)
        except FileNotFoundError:
            yield None
            return
        try:
            yield fd
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)


def read_config(workspace):
    """Read for server-side preview, without creating the config directory."""
    with _config_dir(workspace) as fd:
        return _read(fd)[0] if fd is not None else None


def apply_plan(workspace, plan, *, journal_directory):
    """Apply a server-retained, owner-confirmed plan. Never retry implicitly.

    Prepared journal contains exact backup bytes; it must stay owner-private.
    Directory flock serializes our writers. An external editor ignoring the
    lock can still race the last preimage check: this is not filesystem CAS.
    """
    if os.name != "posix":
        raise ValueError("codex_config_transaction_platform_unsupported")
    import fcntl

    _parse(plan.after)
    journal_fd = _directory(journal_directory, private=True)
    transaction = uuid.uuid4().hex
    temporary = ".mailbox-" + transaction + ".tmp"
    try:
        with _config_dir(workspace, create=True) as fd:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before, mode = _read(fd)
            if before != plan.before:
                raise ValueError("codex_config_changed_refresh_preview")
            record = {
                "version": 1,
                "state": "prepared",
                "workspace": str(workspace),
                "before": base64.b64encode(before).decode() if before is not None else None,
                "before_mode": mode,
                "after_sha256": _sha(plan.after),
                "owned_block": base64.b64encode(plan.owned_block).decode()
                if plan.owned_block
                else None,
            }
            record_fd = os.open(
                transaction + ".json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=journal_fd,
            )
            with os.fdopen(record_fd, "w") as stream:
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(journal_fd)
            try:
                temp_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
                with os.fdopen(temp_fd, "wb") as stream:
                    stream.write(plan.after)
                    stream.flush()
                    os.fchmod(stream.fileno(), mode)
                    os.fsync(stream.fileno())
                if _read(fd) != (before, mode):
                    raise ValueError("codex_config_changed_refresh_preview")
                current_dir = os.stat(Path(workspace) / ".codex", follow_symlinks=False)
                opened_dir = os.fstat(fd)
                if (current_dir.st_dev, current_dir.st_ino) != (
                    opened_dir.st_dev,
                    opened_dir.st_ino,
                ):
                    raise ValueError("codex_config_directory_changed")
                os.replace(temporary, "config.toml", src_dir_fd=fd, dst_dir_fd=fd)
                os.fsync(fd)
                if _read(fd)[0] != plan.after:
                    raise ValueError("codex_config_write_unconfirmed")
            finally:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass
            # The prepared record is intentionally immutable. Its after hash
            # supports recovery even if the process dies before this response.
            return {
                "state": "written_waiting_for_host",
                "transaction_id": transaction,
                "after_sha256": _sha(plan.after),
            }
    finally:
        os.close(journal_fd)
