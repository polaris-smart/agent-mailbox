"""Owner-only local profile choices; no browser paths or session/body scans.

The conventional local installation is the first supported discovery location.
Absence means not discovered, not that no other Hermes installation exists.
Candidates do not prove a live Gateway, its routing mode, or receiver readiness.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from . import workbench_hermes_process as process
from .workbench_store import WorkbenchError


def choices(store, employee_id, project_id):
    _member(store, employee_id, project_id)
    _root, _python, result = _discover()
    common = {
        "host_kind": "hermes",
        "session_verified": False,
        "delivery_verified": False,
        "discovery_scope": "local_default_installation",
        "profiles": [],
    }
    if result is None:
        return {**common, "state": "runtime_not_discovered"}
    return {
        **common,
        "state": "profile_candidates",
        "profiles": [
            {
                "id": _identifier(employee_id, project_id, entry["home"]),
                "label": entry["profile_id"],
                "candidate_only": True,
            }
            for entry in result["profiles"]
        ],
    }


def _member(store, employee_id, project_id):
    with store._transaction(readonly=True) as db:
        employee = store._required(db, "employees", employee_id)
        store._required(db, "projects", project_id)
        if (
            employee["kind"] != "hermes"
            or employee["lifecycle"] != "active"
            or employee["node_id"] != store.local_node_id(db)
            or not db.execute(
                "SELECT 1 FROM memberships WHERE employee_id=? AND project_id=?",
                (employee_id, project_id),
            ).fetchone()
        ):
            raise WorkbenchError(
                "RECEIVER_MEMBER_REQUIRED", "Choose an active local Hermes member."
            )


def _identifier(employee_id, project_id, home):
    identity = f"{employee_id}\0{project_id}\0{home}"
    return "hermes_profile_" + hashlib.sha256(identity.encode()).hexdigest()


def _discover():
    root = Path.home() / ".hermes"
    install = root / "hermes-agent"
    try:
        python = _installed_python(root, install)
    except (OSError, ValueError, TypeError, KeyError):
        raise WorkbenchError(
            "RECEIVER_DISCOVERY_UNCONFIRMED",
            "Hermes runtime selection could not be verified; no settings were changed.",
        ) from None
    if python is None:
        return root, install / ".venv/bin/python", None
    try:
        source = process.payload()
        pins = {
            name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in process.FILES
        }
        result = process.run(
            python,
            root,
            {"version": 1, "operation": "profiles", "profile": str(root)},
            source=source,
            expected_files=pins,
        )
        return root, python, result
    except (OSError, ValueError):
        raise WorkbenchError(
            "RECEIVER_DISCOVERY_UNCONFIRMED",
            "Hermes profile discovery could not be verified; no settings were changed.",
        ) from None


def _installed_python(root: Path, install: Path) -> Path | None:
    """Read PM's committed generation; never silently fall back to an old venv.

    The published source launcher bootstraps this generation on every start.
    This is discovery only: it does not execute the launcher or control a live
    Gateway.  Missing PM state behind a published launcher is an error.
    """
    launcher = install / ".hermes/bin/hermes"
    key = hashlib.sha256(str(install).encode()).hexdigest()[:16]
    state = root / "installs" / key
    # An interrupted PM update may remove the published launcher while its
    # committed store remains.  That is a broken PM install, not a legacy one.
    pm_present = launcher.exists() or launcher.is_symlink() or state.exists() or state.is_symlink()
    if pm_present:
        if launcher.is_symlink() or not launcher.is_file():
            raise ValueError("hermes_launcher_unconfirmed")
        process._file(launcher, executable=True)
        if install.resolve() != install or root.resolve() != root or state.is_symlink():
            raise ValueError("hermes_install_scope_unconfirmed")
        facts = state / "facts.json"
        process._file(facts)
        info = facts.lstat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("hermes_runtime_facts_unconfirmed")
        data = json.loads(facts.read_text(encoding="utf-8-sig"))
        value = data["packages"]["venv"]["environment"]
        if not isinstance(value, str):
            raise ValueError("hermes_runtime_facts_unconfirmed")
        venv = Path(value)
        if (
            not re.fullmatch(r"[0-9a-f]{32}", venv.parent.name)
            or venv.name != "venv"
            or venv.parent.parent != state / "environments"
            or venv.resolve() != venv
        ):
            raise ValueError("hermes_runtime_facts_unconfirmed")
        process._file(venv / "pyvenv.cfg")
        python = venv / "bin/python"
        process._file(python, executable=True)
        return python
    previous = install / ".venv/bin/python"
    return previous if previous.exists() else None


def resolve(store, employee_id, project_id, opaque_id):
    """Recheck a browser selection; never accept a path from the browser."""
    _member(store, employee_id, project_id)
    if not isinstance(opaque_id, str) or not opaque_id.startswith("hermes_profile_"):
        raise WorkbenchError("RECEIVER_PROFILE_UNCONFIRMED", "Choose a listed Hermes profile.")
    _root, python, result = _discover()
    matches = (
        []
        if result is None
        else [
            entry
            for entry in result["profiles"]
            if _identifier(employee_id, project_id, entry["home"]) == opaque_id
        ]
    )
    if len(matches) != 1:
        raise WorkbenchError(
            "RECEIVER_PROFILE_UNCONFIRMED", "Refresh and choose a listed Hermes profile."
        )
    return python, Path(matches[0]["home"])
