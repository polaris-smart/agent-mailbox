"""Explicit release checks and private, quiescent update preparation. No updater."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import sqlite3
import stat
import sys
import threading
import urllib.error
import urllib.request
from contextlib import closing, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import __version__
from .workbench_private import private_mode
from .workbench_store import WorkbenchError

REPOSITORY = "polaris-smart/agent-mailbox"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=100"
MAX_RESPONSE = 2 * 1024 * 1024
MAX_BACKUP = 1024 * 1024 * 1024
MAX_FILE = 512 * 1024 * 1024
MAX_FILES = 10000


def _now():
    return datetime.now(timezone.utc).isoformat()


def version_key(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    match = re.fullmatch(r"v?(\d{1,6})\.(\d{1,6})\.(\d{1,6})(?:(a|b|rc)(\d{1,6}))?", value)
    if not match:
        return None
    major, minor, patch, stage, serial = match.groups()
    return (
        int(major),
        int(minor),
        int(patch),
        {"a": 0, "b": 1, "rc": 2, None: 3}[stage],
        int(serial or 0),
    )


def select_release(rows, channel):
    if not isinstance(rows, list):
        raise WorkbenchError("UPDATE_INVALID_RESPONSE", "发行信息格式无效，请稍后重试。")
    candidates = []
    for row in rows:
        if not isinstance(row, dict) or row.get("draft") is not False:
            continue
        key = version_key(row.get("tag_name"))
        if key is None or (
            channel == "stable" and (row.get("prerelease") is not False or key[3] != 3)
        ):
            continue
        # Construct a trusted repository URL; never display an arbitrary feed link.
        tag = row["tag_name"]
        notes = row.get("body")
        candidates.append(
            (
                key,
                {
                    "version": tag.removeprefix("v"),
                    "url": RELEASES_URL + "/tag/" + tag,
                    "notes": notes[:12000] if isinstance(notes, str) else "",
                },
            )
        )
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_releases():
    # No owner token, GitHub key, project metadata, or model credential is sent.
    request = urllib.request.Request(
        API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "agent-mailbox-update-check",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect())
    with opener.open(request, timeout=8) as response:
        data = response.read(MAX_RESPONSE + 1)
    if len(data) > MAX_RESPONSE:
        raise WorkbenchError(
            "UPDATE_RESPONSE_TOO_LARGE", "发行信息过大，请前往 GitHub 发行页核对。"
        )
    return json.loads(data)


def installation(store):
    kind = "unknown"
    if getattr(sys, "frozen", False):
        kind = "app"
    else:
        try:
            dist = importlib.metadata.distribution("agent-mailbox")
            raw = dist.read_text("direct_url.json")
            direct = json.loads(raw) if raw else {}
            kind = "source" if direct.get("dir_info", {}).get("editable") else "wheel"
            package = Path(__file__).resolve()
            if not package.is_relative_to(Path(dist.locate_file("")).resolve()):
                kind = "source"
        except (importlib.metadata.PackageNotFoundError, ValueError, TypeError):
            pass
    common = [
        {
            "zh": "等待已有任务和未确认回执结束；准备成功后退出应用。",
            "en": "Wait for active tasks and unconfirmed receipts. Quit only after preparation succeeds.",
        },
        {
            "zh": "保留本页显示的数据目录。备份包含私有身份，请勿上传到公开仓库。",
            "en": "Keep the data directory shown here. Backups contain private identities; never upload them publicly.",
        },
    ]
    methods = {
        "app": {
            "zh": "从可信发行页下载同操作系统和架构的 App；退出后替换程序，使用原数据目录启动。macOS 公证情况须核对发行说明。",
            "en": "Get the App for this OS and architecture from the trusted release page. Quit, replace the program, and start with the same home. Check release notes for macOS notarization.",
        },
        "wheel": {
            "zh": "退出后，在原 Python 环境安装选定版本的 wheel（核对发行渠道和校验值）；继续使用原 --home。不要安装旧版本替代本地 Beta。",
            "en": "After quitting, install the chosen wheel in the original Python environment, checking channel and checksums. Keep the same --home. Do not replace a local Beta with an older release.",
        },
        "source": {
            "zh": "退出后保留本地代码改动，按该版本发行说明切换源码并更新依赖；使用原 --home 启动。应用不会自动执行 git pull。",
            "en": "After quitting, preserve local code changes, switch source and dependencies following that release's instructions, and start with the same --home. The app does not run git pull.",
        },
        "unknown": {
            "zh": "无法可靠识别安装方式。先核对原启动入口与发行说明，不要盲目替换程序或删除数据。",
            "en": "Installation type could not be verified. Check the original launcher and release instructions before replacing anything or deleting data.",
        },
    }
    common.append(methods[kind])
    common.append(
        {
            "zh": "启动后核对员工、项目和历史。升级失败时停止使用，并按发行说明恢复对应程序和数据备份；目前没有自动回滚。",
            "en": "After starting, verify employees, projects and history. On failure, stop and restore the matching program and backup per release instructions; there is no automatic rollback.",
        }
    )
    return {
        "kind": kind,
        "platform": platform.system(),
        "architecture": platform.machine(),
        "home": str(store.root),
        "executable": sys.executable,
        "instructions": common,
    }


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remote_blockers(store):
    blockers = []
    for name in ("active-runs.json", "terminal-receipts.json"):
        path = store.directory / "fleet" / name
        if not path.exists():
            continue
        if path.is_symlink():
            raise WorkbenchError("UPDATE_UNSAFE_FILE", "执行记录是符号链接，请先检查数据目录。")
        try:
            if path.stat().st_size > 1024 * 1024:
                raise ValueError()
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                raise TypeError()
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            raise WorkbenchError(
                "UPDATE_PENDING_STATE_INVALID", "远端执行记录无法核实，请先恢复连接并检查回执。"
            ) from exc
        if value:
            blockers.append(name)
    return blockers


def private_backup(store):
    parent = store.root / "update-backups"
    if parent.is_symlink():
        raise WorkbenchError("UPDATE_UNSAFE_FILE", "备份目录是符号链接，请先检查数据目录。")
    parent.mkdir(mode=0o700, exist_ok=True)
    private_mode(parent, 0o700)
    target = parent / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex)
    target.mkdir(mode=0o700)
    private_mode(target, 0o700)
    total = 0
    entries = []
    try:
        # Pause and quiescence were established before this call. The DB write
        # transaction prevents new claims and gives a consistent SQLite snapshot.
        with store._transaction() as db:
            if not db.execute("SELECT paused FROM update_settings WHERE id=1").fetchone()[0]:
                raise WorkbenchError("UPDATE_NOT_PAUSED", "请先暂停接单，再准备升级。")
            if (
                db.execute(
                    "SELECT 1 FROM tasks WHERE status IN ('starting','running','waiting_approval') LIMIT 1"
                ).fetchone()
                or db.execute("SELECT 1 FROM update_claims LIMIT 1").fetchone()
                or _remote_blockers(store)
            ):
                raise WorkbenchError("UPDATE_BUSY", "仍有执行或领取请求未结束，请等待后重试。")
            folder = target / "workbench"
            folder.mkdir(mode=0o700)
            private_mode(folder, 0o700)
            path = folder / "state.db"
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
            private_mode(path, 0o600)
            # sqlite3.Connection's context manager commits but does not close.
            # Keep the offline snapshot self-contained: copying a WAL database
            # also copies its journal setting, which can create unprotected
            # transient sidecars until connections are garbage-collected.
            with (
                closing(sqlite3.connect(store.db_path)) as src,
                closing(sqlite3.connect(path)) as dst,
            ):
                src.backup(dst)
                if dst.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                    raise WorkbenchError("UPDATE_BACKUP_INVALID", "备份数据库无法转为独立文件。")
                if (
                    dst.execute("PRAGMA quick_check").fetchone()[0] != "ok"
                    or dst.execute("PRAGMA foreign_key_check").fetchone()
                ):
                    raise WorkbenchError(
                        "UPDATE_BACKUP_INVALID", "数据库备份校验失败，不能继续升级。"
                    )
            if path.stat().st_size > MAX_FILE:
                raise WorkbenchError("UPDATE_BACKUP_LIMIT", "数据库备份过大，请检查数据目录。")
            candidates = [path]
            for base, dirs, files in os.walk(store.directory, followlinks=False):
                base = Path(base)
                for name in list(dirs):
                    item = base / name
                    if item.is_symlink():
                        raise WorkbenchError(
                            "UPDATE_UNSAFE_FILE", "数据目录包含符号链接，无法安全备份。"
                        )
                    if name in {"runtime", "__pycache__", "knowledge-cache", "cache"}:
                        dirs.remove(name)
                for name in files:
                    item = base / name
                    if item.is_symlink():
                        raise WorkbenchError(
                            "UPDATE_UNSAFE_FILE", "数据目录包含符号链接，无法安全备份。"
                        )
                    if name in {
                        "state.db",
                        "state.db-wal",
                        "state.db-shm",
                        "instance.json",
                        "instance.lock",
                    } or name.endswith((".sqlite", ".log")):
                        continue
                    relative = item.relative_to(store.directory)
                    destination = folder / relative
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    private_mode(destination.parent, 0o700)
                    fd = os.open(item, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    with os.fdopen(fd, "rb") as source:
                        info = os.fstat(source.fileno())
                        if (
                            not stat.S_ISREG(info.st_mode)
                            or info.st_size > MAX_FILE
                            or total + info.st_size > MAX_BACKUP
                        ):
                            raise WorkbenchError(
                                "UPDATE_BACKUP_LIMIT", "备份文件过大或格式不支持，请检查数据目录。"
                            )
                        outfd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                        with os.fdopen(outfd, "wb") as output:
                            private_mode(destination, 0o600)
                            remaining = info.st_size
                            while remaining:
                                chunk = source.read(min(remaining, 1024 * 1024))
                                if not chunk:
                                    raise WorkbenchError(
                                        "UPDATE_BACKUP_CHANGED", "备份期间文件改变，请重试。"
                                    )
                                output.write(chunk)
                                remaining -= len(chunk)
                            if (
                                source.read(1)
                                or os.fstat(source.fileno()).st_mtime_ns != info.st_mtime_ns
                            ):
                                raise WorkbenchError(
                                    "UPDATE_BACKUP_CHANGED", "备份期间文件改变，请重试。"
                                )
                            output.flush()
                            os.fsync(output.fileno())
                    candidates.append(destination)
                    total += info.st_size
                    if len(candidates) > MAX_FILES:
                        raise WorkbenchError(
                            "UPDATE_BACKUP_LIMIT", "备份文件数量过多，请检查数据目录。"
                        )
            entries = [
                {
                    "path": p.relative_to(target).as_posix(),
                    "bytes": p.stat().st_size,
                    "sha256": _hash(p),
                }
                for p in candidates
            ]
            total = sum(e["bytes"] for e in entries)
            if total > MAX_BACKUP:
                raise WorkbenchError("UPDATE_BACKUP_LIMIT", "备份超过容量上限，请检查数据目录。")
        manifest = {
            "format": 1,
            "version": __version__,
            "created_at": _now(),
            "files": entries,
            "excluded": [
                "runtime/cache/logs",
                "live owner token/lock",
                "old migration backups",
                "external project files and provider sign-in",
            ],
        }
        fd = os.open(target / "manifest.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            private_mode(target / "manifest.json", 0o600)
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        if any(_hash(target / e["path"]) != e["sha256"] for e in entries):
            raise WorkbenchError("UPDATE_BACKUP_INVALID", "备份校验失败，请重试。")
        return {
            "path": str(target),
            "created_at": manifest["created_at"],
            "files": len(entries),
            "bytes": total,
            "verified": True,
        }
    except BaseException:
        shutil.rmtree(target)
        raise


class UpdateService:
    def __init__(self, server):
        self.server = server
        self.store = server.store
        self.lock = threading.RLock()
        self.check = None
        self.last_backup = None

    def status(self):
        from .workbench_compatibility import compatibility_nodes

        with self.lock:
            return {
                "current_version": __version__,
                "channel": self.store.update_channel(),
                "installation": installation(self.store),
                "maintenance": self.store.update_maintenance(),
                "check": self.check,
                "last_backup": self.last_backup,
                "nodes": compatibility_nodes(self.store, self.server.fleet),
            }

    def channel(self, value):
        with self.lock:
            self.store.update_channel(value)
            self.check = None
            return self.status()

    def check_releases(self):
        with self.lock:
            try:
                latest = select_release(fetch_releases(), self.store.update_channel())
                code = (
                    "no_releases"
                    if latest is None
                    else "update_available"
                    if version_key(latest["version"]) > version_key(__version__)
                    else "current"
                )
                self.check = {"status": code, "checked_at": _now(), "latest": latest, "error": None}
            except (OSError, ValueError, TypeError, WorkbenchError) as exc:
                if isinstance(exc, WorkbenchError):
                    code, message = exc.code, exc.message
                elif isinstance(exc, urllib.error.HTTPError) and exc.code in {403, 429}:
                    code, message = (
                        "UPDATE_RATE_LIMIT",
                        "GitHub 暂时限制请求，请稍后重试或打开发行页。",
                    )
                else:
                    code, message = (
                        "UPDATE_CHECK_FAILED",
                        "无法核实公开发行版，请检查网络或稍后重试。",
                    )
                self.check = {
                    "status": "error",
                    "checked_at": _now(),
                    "latest": None,
                    "error": {"code": code, "message": message},
                }
            return self.status()

    def prepare(self):
        with self.lock:
            self.last_backup = None
            self.store.pause_updates(True)
            self.server.ui_notify()
            maintenance = self.store.update_maintenance()
            blockers = _remote_blockers(self.store)
            if maintenance["active_tasks"] or maintenance["pending_claims"] or blockers:
                return {
                    **self.status(),
                    "status": "waiting",
                    "remote_pending": blockers,
                    "backup": None,
                }
            try:
                with self.server.fleet.lock if self.server.fleet else nullcontext():
                    self.last_backup = private_backup(self.store)
            except WorkbenchError:
                raise
            except OSError as exc:
                raise WorkbenchError(
                    "UPDATE_BACKUP_FAILED",
                    "无法写入备份，请检查磁盘空间和目录权限。接单仍暂停，可重试或恢复接单。",
                ) from exc
            return {**self.status(), "status": "ready", "backup": self.last_backup}

    def assert_can_quit(self):
        with self.lock:
            maintenance = self.store.update_maintenance()
            if not maintenance["paused"]:
                return
            if (
                maintenance["active_tasks"]
                or maintenance["pending_claims"]
                or _remote_blockers(self.store)
            ):
                raise WorkbenchError(
                    "UPDATE_BUSY", "更新准备尚未空闲，请等待任务和回执结束后重试。"
                )
            if not self.last_backup:
                raise WorkbenchError(
                    "UPDATE_BACKUP_REQUIRED",
                    "请先完成更新备份，再退出升级；也可以恢复接单后正常退出。",
                )

    def resume(self):
        with self.lock:
            self.store.pause_updates(False)
            self.server.notify()
            self.server.ui_notify()
            return self.status()
