"""Private, persistent macOS launcher; never modifies the vendor application."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

NAME = "Codex - Mailbox.app"


def _command():
    if not getattr(sys, "frozen", False):
        raise ValueError("receiver_shortcut_requires_packaged_app")
    binary = Path(sys.executable).resolve()
    app = binary.parent.parent.parent
    if (
        binary.parent != app / "Contents/MacOS"
        or app.suffix != ".app"
        or plistlib.loads((app / "Contents/Info.plist").read_bytes()).get("CFBundleIdentifier")
        != "com.polaris-smart.agent-mailbox"
    ):
        raise ValueError("receiver_shortcut_requires_packaged_app")
    return str(binary)


def _directory(store, employee, project, *, create=False):
    from . import workbench_codex_connection as receiver

    root, _ = receiver._paths(store.root)
    if not receiver._installed(root):
        raise ValueError("receiver_install_required")
    # The employee owns one mailbox across projects, so it needs one launcher.
    # Keep old employee/project shortcut directories untouched for rollback.
    scope = hashlib.sha256(json.dumps([employee]).encode()).hexdigest()[:24]
    parent = root / "shortcuts"
    target = parent / scope
    for p in (parent, target):
        if not create and not p.exists():
            return target
        receiver._private(p, create=create)
    return target


def _verify(directory, employee, project, home, record=None):
    from .workbench_codex_connection import _private
    from .workbench_codex_installer import _read_private

    if record is None:
        record = _read_private(directory, "entry.json", 65536)
    release = record.get("release")
    files = record.get("files")
    if (
        record.get("version") != 2
        or record.get("home") != str(Path(home).resolve())
        or record.get("employee_id") != employee
        or "project_id" in record
        or not isinstance(release, str)
        or len(release) != 64
        or any(c not in "0123456789abcdef" for c in release)
        or not isinstance(files, dict)
        or not 1 <= len(files) <= 64
    ):
        raise ValueError("receiver_shortcut_record_invalid")
    app = directory / release / NAME
    _private(directory / release)
    _private(app)
    actual = {}
    for p in app.rglob("*"):
        info = p.lstat()
        if p.is_symlink() or p.resolve() != p or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("receiver_shortcut_files_changed")
        if p.is_file():
            if info.st_nlink != 1 or info.st_size > 8 * 1024 * 1024:
                raise ValueError("receiver_shortcut_files_changed")
            actual[str(p.relative_to(app))] = hashlib.sha256(p.read_bytes()).hexdigest()
            if len(actual) > 64:
                raise ValueError("receiver_shortcut_files_changed")
        elif not p.is_dir():
            raise ValueError("receiver_shortcut_files_changed")
    if actual != files or not actual:
        raise ValueError("receiver_shortcut_files_changed")
    return app


def shortcut(store, employee, project, *, create=False, reveal=False):
    from . import workbench_codex_connection as receiver

    directory = _directory(store, employee, project, create=create)
    old = None
    if directory.exists() and (directory / "entry.json").exists():
        old = _verify(directory, employee, project, store.root)
    if create:
        command = shlex.join(
            [
                _command(),
                "codex-receiver-start",
                "--home",
                str(store.root.resolve()),
                "--employee",
                employee,
            ]
        )
        # JSON string escaping is AppleScript string escaping here, NOT shell quoting.
        literal = json.dumps(command, ensure_ascii=False)
        source = (
            "on run\nlocal outcome\ntry\nset outcome to do shell script " + literal + "\n"
            'if outcome is "restart_required" then\n'
            'display alert "Codex · Mailbox" message "请正常退出 Codex 后，再从此图标启动。不会强退当前聊天。 / Quit Codex normally, then open this icon. Your current chat will not be closed automatically."\n'
            'else if outcome is not "launch_requested" and outcome is not "receiver_online" then\n'
            'display alert "Codex · Mailbox" message "请回到 Mailbox 接收设置检查连接。 / Check the connection in Mailbox reception settings."\n'
            "end if\non error\n"
            'display alert "Codex · Mailbox" message "启动未完成。请在 Mailbox 接收设置重新检查；若主 App 已移动，请重新创建启动图标。 / Startup was not completed. Check Mailbox reception settings; recreate this icon if the main app moved."\n'
            "end try\nend run\n"
        )
        # The original source-only digest named an agent app hidden from Dock.
        # Include the bundle policy so an existing icon is rebuilt as a visible app.
        release = hashlib.sha256((source + "\nmailbox-shortcut-bundle-v3").encode()).hexdigest()
        target = directory / release
        if old and old.parent == target:
            if reveal:
                receiver._run(["/usr/bin/open", "-R", str(old)])
            return {"available": True, "state": "shortcut_ready"}
        if target.exists():
            # Resume only a fully generated and hash-verified owned bundle after
            # interruption between directory rename and descriptor publication.
            from .workbench_codex_installer import _read_private

            record = _read_private(target, "install.json", 65536)
            if record.get("release") != release:
                raise ValueError("receiver_shortcut_unowned_target")
            old = _verify(directory, employee, project, store.root, record)
            receiver._atomic(directory / "entry.json", json.dumps(record).encode())
            if reveal:
                receiver._run(["/usr/bin/open", "-R", str(old)])
            return {"available": True, "state": "shortcut_ready"}
        staging = Path(tempfile.mkdtemp(prefix=".shortcut-", dir=directory))
        try:
            script = staging / "launcher.applescript"
            script.write_text(source)
            script.chmod(0o600)
            app = staging / NAME
            receiver._run(["/usr/bin/osacompile", "-o", str(app), str(script)])
            plist = app / "Contents/Info.plist"
            info = plistlib.loads(plist.read_bytes())
            info.update(
                CFBundleIdentifier="com.polaris-smart.mailbox.codex." + release[:24],
                CFBundleName="Codex · Mailbox",
                LSUIElement=False,
            )
            plist.write_bytes(plistlib.dumps(info))
            receiver._run(["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(app)])
            files = {}
            for p in [app, *app.rglob("*")]:
                if p.is_symlink():
                    raise ValueError("receiver_shortcut_files_changed")
                p.chmod(0o700 if p.is_dir() or p.stat().st_mode & 0o111 else 0o600)
                if p.is_file():
                    files[str(p.relative_to(app))] = hashlib.sha256(p.read_bytes()).hexdigest()
            receiver._run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)])
            script.unlink()
            record = {
                "version": 2,
                "release": release,
                "home": str(store.root.resolve()),
                "employee_id": employee,
                "files": files,
            }
            receiver._atomic(staging / "install.json", json.dumps(record).encode())
            staging.rename(target)
            receiver._atomic(directory / "entry.json", json.dumps(record).encode())
            old = _verify(directory, employee, project, store.root)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    if reveal:
        if old is None:
            raise ValueError("receiver_shortcut_missing")
        receiver._run(["/usr/bin/open", "-R", str(old)])
    return {"available": old is not None, "state": "shortcut_ready" if old else "shortcut_missing"}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Start Codex through its installed mailbox receiver."
    )
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--employee", required=True)
    # Older per-project icons keep working. New icons resolve a live project
    # binding at each launch, so moving the employee between projects does not
    # require creating another app icon.
    parser.add_argument("--project")
    args = parser.parse_args(argv)
    from . import workbench_codex_connection as receiver
    from .workbench_host_bindings import load_bindings
    from .workbench_host_transport import endpoints
    from .workbench_store import WorkbenchError, WorkbenchStore

    try:
        home = args.home.resolve()
        if not (home / "workbench/state.db").is_file():
            raise ValueError("receiver_existing_home_required")
        store = WorkbenchStore(home)
        project = args.project
        if project is None:
            _, ipc = receiver._paths(home)
            routed_ids = {
                row["binding_id"]
                for row in endpoints(home)
                if row["employee_id"] == args.employee
                and row["host_kind"] == "codex"
                and row.get("launcher_directory") == str(ipc)
            }
            bindings = sorted(
                load_bindings(home),
                key=lambda row: (row["id"] not in routed_ids, row["project_id"]),
            )
            for binding in bindings:
                if binding["employee_id"] != args.employee or binding["host_kind"] != "codex":
                    continue
                try:
                    receiver._member(store, args.employee, binding["project_id"])
                except WorkbenchError:
                    continue
                project = binding["project_id"]
                break
            if project is None:
                raise ValueError("receiver_binding_required")
        result = receiver.connection(store, args.employee, project, "launch")
        state = result["state"]
        # Already online can still need a project binding: do not pretend it is configured.
        print("receiver_online" if result.get("receiver_online") else state)
        return 0
    except (OSError, ValueError, WorkbenchError, subprocess.SubprocessError):
        print("receiver_start_unconfirmed")
        return 2
