"""Optional CodeGraph symbol retrieval from disposable index snapshots.

The CLI opens SQLite read/write even for queries. Never give it the real index:
copy a checkpointed database into a private temporary directory, without source,
.git, configuration, or symlinks. This is symbol search, not semantic Q&A.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
from contextlib import closing
from pathlib import Path

from .workbench_store import WorkbenchError

OUTPUT_LIMIT = 100 * 1024
SNAPSHOT_LIMIT = 128 * 1024 * 1024
QUERY_TIMEOUT = 10.0


def _project(value: str | Path) -> Path:
    try:
        root = Path(value).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError
        return root
    except (OSError, ValueError, TypeError, RuntimeError):
        raise WorkbenchError(
            "KNOWLEDGE_INVALID_PROJECT", "Choose an existing project directory."
        ) from None


def _binary() -> str | None:
    explicit = os.environ.get("AGENT_MAIL_CODEGRAPH_BIN")
    candidate = explicit if explicit is not None else shutil.which("codegraph")
    if not candidate:
        return None
    try:
        path = Path(candidate)
        if not path.is_absolute():
            return None
        path = path.resolve(strict=True)
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    except (OSError, ValueError, RuntimeError):
        return None


def _environment() -> dict[str, str]:
    # Do not forward model/API credentials, arbitrary CodeGraph overrides, or Git
    # hooks/config environment. The Node launcher still needs the user's PATH.
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT")
        if key in os.environ
    }
    env.update(
        CODEGRAPH_NO_DOWNLOAD="1",
        CODEGRAPH_NO_DAEMON="1",
        CODEGRAPH_TELEMETRY="0",
        DO_NOT_TRACK="1",
        NO_COLOR="1",
        GIT_OPTIONAL_LOCKS="0",
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
    )
    return env


def _run(args: list[str], cwd: Path, timeout: float = QUERY_TIMEOUT) -> str:
    """Drain both pipes incrementally; bound memory, time and child lifetime."""
    process = None
    try:
        process = subprocess.Popen(
            args,
            cwd=cwd,
            env=_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        output = bytearray()
        size = 0
        deadline = time.monotonic() + min(timeout, 15.0)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, True)
            selector.register(process.stderr, selectors.EVENT_READ, False)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkbenchError(
                        "KNOWLEDGE_TIMEOUT", "CodeGraph exceeded the query time limit."
                    )
                for key, _ in selector.select(min(remaining, 0.1)):
                    block = os.read(key.fileobj.fileno(), 8192)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    size += len(block)
                    if size > OUTPUT_LIMIT:
                        raise WorkbenchError(
                            "KNOWLEDGE_OUTPUT_LIMIT", "CodeGraph output exceeded 100 KiB."
                        )
                    if key.data:
                        output.extend(block)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkbenchError(
                    "KNOWLEDGE_TIMEOUT", "CodeGraph exceeded the query time limit."
                )
            try:
                code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise WorkbenchError(
                    "KNOWLEDGE_TIMEOUT", "CodeGraph exceeded the query time limit."
                ) from None
        if code:
            raise WorkbenchError(
                "KNOWLEDGE_COMMAND_FAILED", "CodeGraph could not complete the read-only query."
            )
        return output.decode("utf-8", errors="replace")
    except OSError:
        raise WorkbenchError(
            "KNOWLEDGE_COMMAND_FAILED", "The configured executable could not run."
        ) from None
    finally:
        if process is not None:
            # Also reap descendants if the launcher exited while leaving pipes
            # or a child alive. These processes belong to our private session.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()


def _git(root: Path) -> tuple[str | None, bool | None]:
    binary = shutil.which("git")
    if not binary:
        return None, None
    try:
        revision = _run(
            [
                binary,
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=" + os.devnull,
                "-C",
                str(root),
                "rev-parse",
                "--verify",
                "HEAD",
            ],
            root,
            2,
        ).strip()
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", revision):
            revision = None
    except WorkbenchError:
        revision = None
    try:
        # Excludes untracked scanning; reflects tracked/staged working-tree changes.
        dirty = bool(
            _run(
                [
                    binary,
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.hooksPath=" + os.devnull,
                    "-C",
                    str(root),
                    "status",
                    "--porcelain",
                    "--untracked-files=no",
                ],
                root,
                2,
            ).strip()
        )
    except WorkbenchError:
        dirty = None
    return revision, dirty


def _index(root: Path) -> Path:
    directory = root / ".codegraph"
    database = directory / "codegraph.db"
    if directory.is_symlink() or database.is_symlink() or not database.is_file():
        raise WorkbenchError(
            "KNOWLEDGE_INDEX_REQUIRED", "This project needs an existing CodeGraph index."
        )
    wal = directory / "codegraph.db-wal"
    if wal.exists() and (wal.is_symlink() or wal.stat().st_size):
        raise WorkbenchError(
            "KNOWLEDGE_INDEX_BUSY",
            "The index has uncheckpointed changes; retry after its owner closes it.",
        )
    try:
        # immutable avoids creating SQLite WAL/SHM sidecars; only safe when no
        # nonempty WAL exists. No statement runs against source files.
        with closing(
            sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1)
        ) as connection:
            deadline = time.monotonic() + 2
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not {"nodes", "files", "project_metadata"}.issubset(tables):
                raise ValueError
            state = connection.execute(
                "SELECT value FROM project_metadata WHERE key='index_state'"
            ).fetchone()
            if state is None or state[0] != "complete":
                raise ValueError
    except (sqlite3.Error, ValueError, OSError):
        raise WorkbenchError(
            "KNOWLEDGE_INDEX_REQUIRED", "The CodeGraph index is invalid or incomplete."
        ) from None
    return database


def knowledge_status(project_path: str | Path) -> dict:
    root = _project(project_path)
    binary = _binary()
    revision, dirty = _git(root)
    result = {
        "provider": "codegraph",
        "version": None,
        "available": binary is not None,
        "index_ready": False,
        "project_path": str(root),
        "mode": "symbols_snapshot",
        "freshness": "unknown",
        "freshness_reason": "Source bytes were not compared with the index.",
        "revision": revision,
        "working_tree_dirty": dirty,
        "working_tree_dirty_scope": "tracked_files",
        "error_code": None,
    }
    if binary is None:
        result["error_code"] = "KNOWLEDGE_TOOL_UNAVAILABLE"
        return result
    try:
        version = _run([binary, "--version"], root, 2).strip()
        if not re.fullmatch(
            r"(?:codegraph(?: version)?\s+)?v?\d+\.\d+\.\d+(?:[-+][\w.-]+)?", version, re.IGNORECASE
        ):
            raise WorkbenchError(
                "KNOWLEDGE_VERSION_UNSUPPORTED", "Could not identify the CodeGraph version."
            )
        result["version"] = version
        _index(root)
        result["index_ready"] = True
    except WorkbenchError as exc:
        result["error_code"] = exc.code
    except OSError:
        result["error_code"] = "KNOWLEDGE_INDEX_REQUIRED"
    return result


def knowledge_query(project_path: str | Path, query: str) -> dict:
    try:
        return _knowledge_query(project_path, query)
    except OSError:
        raise WorkbenchError(
            "KNOWLEDGE_INDEX_UNREADABLE", "The index snapshot could not be read safely."
        ) from None


def _knowledge_query(project_path: str | Path, query: str) -> dict:
    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > 500
        or query.lstrip().startswith("-")
        or any(ord(char) < 32 or ord(char) == 127 for char in query)
        or any(part in query for part in ("/", "\\", "..", "file:", "://"))
    ):
        raise WorkbenchError(
            "KNOWLEDGE_INVALID_QUERY",
            "Enter a symbol search of 1–500 characters, without options or paths.",
        )
    root = _project(project_path)
    status = knowledge_status(root)
    if status["error_code"]:
        raise WorkbenchError(
            status["error_code"], "CodeGraph symbol retrieval is unavailable for this project."
        )
    binary = _binary()
    if binary is None:
        raise WorkbenchError("KNOWLEDGE_TOOL_UNAVAILABLE", "CodeGraph is not available.")
    database = _index(root)
    if database.stat().st_size > SNAPSHOT_LIMIT:
        raise WorkbenchError(
            "KNOWLEDGE_INDEX_TOO_LARGE", "The index exceeds the 128 MiB snapshot limit."
        )
    before = database.stat()
    with tempfile.TemporaryDirectory(prefix="agent-mail-knowledge-") as temporary:
        snapshot_root = Path(temporary)
        snapshot = snapshot_root / ".codegraph"
        snapshot.mkdir()
        with database.open("rb") as source, (snapshot / "codegraph.db").open("xb") as target:
            remaining = SNAPSHOT_LIMIT
            while True:
                block = source.read(min(1024 * 1024, remaining + 1))
                if not block:
                    break
                remaining -= len(block)
                if remaining < 0:
                    raise WorkbenchError(
                        "KNOWLEDGE_INDEX_TOO_LARGE", "The index exceeds the snapshot limit."
                    )
                target.write(block)
        after = database.stat()
        _index(root)  # Recheck WAL and completeness after the copy.
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise WorkbenchError(
                "KNOWLEDGE_INDEX_BUSY", "The index changed during snapshot creation; retry."
            )
        # Commander supports '--' (verified with the installed 1.6.0 CLI).
        # Unknown CLI versions fail closed; no fallback to interpolated commands.
        raw = _run(
            [
                binary,
                "query",
                "--path",
                str(snapshot_root),
                "--limit",
                "10",
                "--json",
                "--",
                query.strip(),
            ],
            snapshot_root,
        )
        try:
            rows = json.loads(raw)
            if not isinstance(rows, list):
                raise TypeError
        except (TypeError, ValueError):
            raise WorkbenchError(
                "KNOWLEDGE_INVALID_OUTPUT", "CodeGraph returned an invalid symbol result."
            ) from None
    symbols = []
    for row in rows[:10]:
        node = row.get("node") if isinstance(row, dict) else None
        if not isinstance(node, dict):
            continue
        value = node.get("filePath")
        if not isinstance(value, str) or "\\" in value or "\x00" in value:
            continue
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            continue
        try:
            (root / relative).resolve().relative_to(root)
        except (ValueError, OSError, RuntimeError):
            continue
        # Whitelist fields: no source bodies, arbitrary metadata, external paths,
        # signatures containing literal credentials, or raw command output.
        item = {"file_path": relative.as_posix()}
        for source, target in (
            ("name", "name"),
            ("qualifiedName", "qualified_name"),
            ("kind", "kind"),
        ):
            if isinstance(node.get(source), str):
                item[target] = node[source][:500]
        for source, target in (("startLine", "start_line"), ("endLine", "end_line")):
            if isinstance(node.get(source), int) and node[source] > 0:
                item[target] = node[source]
        symbols.append(item)
    return {**status, "query": query.strip(), "symbols": symbols}
