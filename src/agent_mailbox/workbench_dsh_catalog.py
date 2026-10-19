"""Owner-only discovery of conventional local DSH profile names."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

from .workbench_store import WorkbenchError

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def _identifier(employee_id, project_id, profile):
    value = f"{employee_id}\0{project_id}\0{profile}"
    return "dsh_profile_" + hashlib.sha256(value.encode()).hexdigest()


def _directory(path, *, private=False):
    info = path.lstat()
    if (
        path.resolve(strict=True) != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & (0o077 if private else 0o022)
    ):
        raise ValueError("dsh_profile_directory_unconfirmed")


def _manifest(path):
    info = path.lstat()
    if (
        path.resolve(strict=True) != path
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_size > 1024 * 1024
        or info.st_mode & 0o022
    ):
        raise ValueError("dsh_profile_manifest_unconfirmed")


def choices(store, employee_id, project_id):
    with store._transaction(readonly=True) as db:
        employee = store._required(db, "employees", employee_id)
        store._required(db, "projects", project_id)
        if (
            employee["kind"] != "deepseek"
            or employee["lifecycle"] != "active"
            or employee["node_id"] != store.local_node_id(db)
            or not db.execute(
                "SELECT 1 FROM memberships WHERE employee_id=? AND project_id=?",
                (employee_id, project_id),
            ).fetchone()
        ):
            raise WorkbenchError("RECEIVER_MEMBER_REQUIRED", "Choose an active local DSH member.")
    common = {
        "host_kind": "deepseek",
        "discovery_scope": "local_default_installation",
        "session_verified": False,
        "delivery_verified": False,
        "profiles": [],
    }
    home = Path.home() / ".dsh"
    root = home / "profiles"
    try:
        if home.is_symlink() or root.is_symlink():
            raise ValueError("dsh_profile_directory_unconfirmed")
        if not home.exists() or not root.exists():
            return {**common, "state": "runtime_not_discovered"}
        _directory(home, private=True)
        _directory(root)
        profiles = []
        for entry in sorted(root.iterdir(), key=lambda p: p.name):
            if not _NAME.fullmatch(entry.name):
                continue
            _directory(entry)
            manifest = entry / "package.json"
            if manifest.is_symlink():
                raise ValueError("dsh_profile_manifest_unconfirmed")
            if not manifest.exists():
                continue
            _manifest(manifest)
            profiles.append(
                {
                    "id": _identifier(employee_id, project_id, entry),
                    "label": entry.name,
                    "candidate_only": True,
                }
            )
            if len(profiles) > 64:
                raise ValueError("dsh_profile_count_unconfirmed")
        return {**common, "state": "profile_candidates", "profiles": profiles}
    except (OSError, ValueError):
        raise WorkbenchError(
            "RECEIVER_DISCOVERY_UNCONFIRMED",
            "DSH profile discovery could not be verified; no settings were changed.",
        ) from None
