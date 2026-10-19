"""Candidate metadata only, executed by the selected installed Hermes runtime.

Uses official profile identity and session namespace APIs. A routing context must
be supplied by the server from independently established Gateway metadata; it is
NOT inferred from the selected profile, config substrings, or a sessions.json
mirror. Database rows propose a selection; they never prove a live receiver or
authorize injection. The receiver must verify its current exact session pin.

SQLite mode=ro honours committed WAL and never migrates/updates business tables.
SQLite may maintain shared-memory coordination files: this is not a zero-write
filesystem probe. No production database is used by the isolated test suite.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from pathlib import Path

MAX_PROFILES = 64
MAX_SESSIONS = 500
MAX_ENTRY = 65536


def _directory(value):
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("metadata_directory_not_canonical")
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("metadata_directory_not_private")
    return path


def _file(path, *, optional=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if optional:
            return
        raise ValueError("metadata_database_missing") from None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 1
            or info.st_size > 256 * 1024 * 1024):
        raise ValueError("metadata_database_not_private")


def profiles(root):
    """Official live names, with a bounded scan and private canonical paths."""
    from hermes_cli.profiles import get_profile_dir, list_profile_names, profile_exists

    root = _directory(root)
    if Path(get_profile_dir("default")).resolve() != root:
        raise ValueError("metadata_runtime_root_mismatch")
    folder = root / "profiles"
    if folder.exists():
        _directory(folder)
        # Bound even discarded/invalid names before official sorted listing.
        with os.scandir(folder) as entries:
            for index, _entry in enumerate(entries):
                if index >= MAX_PROFILES + 8:
                    raise ValueError("metadata_profile_limit")
    result = []
    for name in list_profile_names():
        if len(result) >= MAX_PROFILES:
            raise ValueError("metadata_profile_limit")
        try:
            home = _directory(get_profile_dir(name))
            if not profile_exists(name):
                continue
        except (ValueError, OSError):
            continue
        result.append({"profile_id": name, "home": str(home)})
    return result


def candidates(root, profile_id, *, routing_home, scope, gateway_profile, multiplex):
    """Read one explicit routing scope, never guess a multiplex database home.

    All path/identity arguments are server-selected, not mail or browser values.
    Caller must re-establish routing context before preparation and live binding.
    """
    from gateway.config import Platform
    from gateway.session import profile_from_session_key_namespace

    homes = {item["profile_id"]: item["home"] for item in profiles(root)}
    if (profile_id not in homes or gateway_profile not in homes
            or type(multiplex) is not bool
            or (not multiplex and profile_id != gateway_profile)):
        raise ValueError("metadata_routing_context_unproven")
    home = _directory(routing_home)
    if str(home) != homes[gateway_profile]:
        raise ValueError("metadata_routing_home_mismatch")
    selected_scope = str(_directory(scope))
    db = home / "state.db"
    for path in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
        _file(path, optional=path != db)
    deadline = time.monotonic() + 3
    rows = []
    try:
        with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=.2) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            # Extract only approved scalar fields. Entire entry_json, prompts,
            # model configuration, origins and messages never enter Python.
            columns = ("session_key", "session_id", "platform", "display_name")
            extract = ",".join(
                f"CASE WHEN length(entry_json)<=? AND json_valid(entry_json) "
                f"THEN json_extract(entry_json,'$.{field}') END" for field in columns)
            transport = ("AND CASE WHEN length(entry_json)<=? AND json_valid(entry_json) "
                         "THEN json_extract(entry_json,'$.transport_profile') END = ?") if multiplex else ""
            parameters = [MAX_ENTRY] * len(columns) + [selected_scope]
            if multiplex:
                parameters.extend([MAX_ENTRY, profile_id])
            parameters.append(MAX_SESSIONS + 1)
            rows = connection.execute(
                f"SELECT session_key,{extract},updated_at FROM gateway_routing "
                f"WHERE scope=? {transport} ORDER BY updated_at DESC LIMIT ?", parameters).fetchall()
    except sqlite3.Error:
        raise ValueError("metadata_routing_read_unconfirmed") from None
    finally:
        if 'connection' in locals():
            connection.close()
    if len(rows) > MAX_SESSIONS:
        raise ValueError("metadata_session_limit")
    result = []
    for key, embedded_key, session_id, platform, label, updated_at in rows:
        if (not isinstance(key, str) or key != embedded_key or not 0 < len(key) <= 512
                or not isinstance(session_id, str) or not 0 < len(session_id) <= 128
                or any(ord(c) < 32 for c in key + session_id)
                or ".." in session_id or "/" in session_id or "\\" in session_id):
            continue
        parts = key.split(":")
        if len(parts) < 4 or parts[0] != "agent":
            continue
        owner = profile_from_session_key_namespace(parts[1])
        # Standalone Gateway historically uses agent:main even for a named home.
        if owner != profile_id and not (not multiplex and owner == "default"):
            continue
        try:
            value = Platform(platform).value
        except (ValueError, TypeError):
            continue
        if value in {"cli", "tui", "desktop", "local", "api", "api_server"} or parts[2] != value:
            continue
        if label is not None and (not isinstance(label, str) or len(label) > 256):
            label = None
        result.append({"session_key": key, "session_id": session_id, "platform": value,
                       "display_name": label, "scope": selected_scope,
                       "candidate_only": True, "session_verified": False})
    return result


def recheck_candidate(root, profile_id, selected, **routing_context):
    """Fresh durable comparison; still not live session authorization."""
    for item in candidates(root, profile_id, **routing_context):
        if all(item.get(key) == selected.get(key)
               for key in ("session_key", "session_id", "platform", "scope")):
            return item
    raise ValueError("metadata_candidate_changed")
