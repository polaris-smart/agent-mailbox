"""Symmetric connect / uninstall (v0.7.5 §3.5): writing *other people's*
config files only ever happens backed-up, ledgered and revertible.

- :func:`connect` first plans, then — only with ``yes=True`` — backs each
  target up, writes the MCP entry, and records a **point** (original sha256,
  backup path, entry key) in ``<root>/connect-points.json``.
- :func:`revert_points` is the uninstall skeleton: it restores each recorded
  write and verifies byte-identity (sha256 == original, i.e. ``diff`` == 0).
  Anything it cannot cleanly restore is reported as **residual** — never
  silent.

Formats we can round-trip safely (JSON) are writable; YAML/TOML configs are
plan-only: they get the exact manual snippet, no blind textual surgery.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from .discover import CATALOG

POINTS_FILE = "connect-points.json"
BACKUP_DIR = "connect-backups"
ENTRY_KEY = "agent-mailbox"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_ts() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _resolve_root(root: Path | None) -> Path:
    return Path(root or os.environ.get("AGENT_MAIL_HOME", Path.home() / ".agent-mail")).expanduser()


def _server_entry() -> dict[str, Any]:
    """The MCP stdio entry we write: the installed console script if present,
    else the current interpreter running ``-m agent_mailbox.server``."""
    exe = shutil.which("agent-mailbox")
    if exe:
        return {"command": exe, "args": []}
    return {"command": sys.executable, "args": ["-m", "agent_mailbox.server"]}


def _targets(member: str, home: Path) -> list[Path]:
    spec = CATALOG.get(member, {})
    out: list[Path] = []
    for rel in spec.get("mcp_configs", ()):
        p = (home / rel).expanduser()
        if p not in out:
            out.append(p)
    return out


def _format_of(path: Path) -> str:
    return path.suffix.lower().lstrip(".")


def plan_connect(
    member: str, root: Path | None = None, *, home: Path | None = None
) -> dict[str, Any]:
    """What a connect would touch — pure read, no writes anywhere."""
    if member not in CATALOG:
        return {
            "member": member,
            "known": False,
            "targets": [],
            "note": f"不认识的成员 {member!r}；支持清单：{' '.join(sorted(CATALOG))}",
        }
    home = home or Path.home()
    targets: list[dict[str, Any]] = []
    for p in _targets(member, home):
        fmt = _format_of(p)
        entry: dict[str, Any] = {
            "path": str(p),
            "format": fmt,
            "exists": p.exists(),
        }
        if not p.exists():
            # "creatable" only when the file lives inside an existing member
            # config dir (agent installed, config absent). A loose file directly
            # under HOME (e.g. ~/.claude.json) is never conjured out of thin air.
            entry["status"] = "creatable" if p.parent != home and p.parent.exists() else "missing"
        elif fmt == "json":
            try:
                json.loads(p.read_text(encoding="utf-8"))
                entry["status"] = "writable-json"
            except (OSError, json.JSONDecodeError) as exc:
                entry["status"] = "unparseable"
                entry["note"] = f"JSON 解析失败（不会盲写）：{exc}"
        elif fmt in ("toml", "yaml", "yml"):
            entry["status"] = "unsupported-format"
        else:
            entry["status"] = "unknown-format"
        targets.append(entry)
    return {
        "member": member,
        "known": True,
        "targets": targets,
        "entry": _server_entry(),
        "note": "写入前会先备份并记录点位；--yes 才执行。",
    }


def load_points(root: Path) -> list[dict[str, Any]]:
    try:
        return json.loads((root / POINTS_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def save_points(root: Path, points: list[dict[str, Any]]) -> Path:
    path = root / POINTS_FILE
    path.write_text(json.dumps(points, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def connect(
    member: str,
    root: Path | None = None,
    *,
    yes: bool = False,
    home: Path | None = None,
) -> dict[str, Any]:
    """Plan (default) or perform (``yes=True``) the MCP-config write.

    Every write is preceded by a backup copy plus a ledger point carrying the
    original sha256, so :func:`revert_points` can prove byte-identical
    restoration later. YAML/TOML are never touched — the plan says so.
    """
    root = _resolve_root(root)
    plan = plan_connect(member, root, home=home)
    if not plan["known"]:
        return {**plan, "written": [], "skipped": [], "errors": [plan["note"]]}
    if not yes:
        return {
            **plan,
            "written": [],
            "skipped": [],
            "errors": [],
            "note": plan["note"] + "（当前为预览：未写入任何文件）",
        }

    written: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[str] = []
    points = load_points(root)
    entry = _server_entry()
    stamp = _now_ts()

    for target in plan["targets"]:
        path = Path(target["path"])
        status = target["status"]
        if status == "writable-json":
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"{path}: 读不出来（{exc}）")
                continue
            servers = data.setdefault("mcpServers", {})
            if ENTRY_KEY in servers:
                skipped.append({"path": str(path), "why": "已挂 agent-mailbox，无需重复写入"})
                continue
            original = path.read_bytes()
            backup_dir = root / BACKUP_DIR / f"{stamp}-{member}"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / path.name
            shutil.copy2(path, backup_path)
            servers[ENTRY_KEY] = entry
            new_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
            fd_tmp = path.with_suffix(path.suffix + ".amtmp")
            fd_tmp.write_bytes(new_bytes)
            os.replace(fd_tmp, path)
            points.append(
                {
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
                    "member": member,
                    "file": str(path),
                    "kind": "modified",
                    "original_sha256": _sha256_bytes(original),
                    "new_sha256": _sha256_bytes(new_bytes),
                    "backup": str(backup_path),
                    "entry_key": ENTRY_KEY,
                    "reverted": False,
                }
            )
            written.append(
                {"path": str(path), "backup": str(backup_path), "entry": servers[ENTRY_KEY]}
            )
        elif status == "creatable":
            backup_dir = root / BACKUP_DIR / f"{stamp}-{member}"
            backup_dir.mkdir(parents=True, exist_ok=True)
            data = {"mcpServers": {ENTRY_KEY: entry}}
            new_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
            path.write_bytes(new_bytes)
            points.append(
                {
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
                    "member": member,
                    "file": str(path),
                    "kind": "created",
                    "original_sha256": None,
                    "new_sha256": _sha256_bytes(new_bytes),
                    "backup": None,
                    "entry_key": ENTRY_KEY,
                    "reverted": False,
                }
            )
            written.append({"path": str(path), "backup": None, "entry": entry})
        elif status == "unsupported-format":
            servers_rel = "mcp_servers" if target["format"] == "toml" else "mcpServers"
            skipped.append(
                {
                    "path": str(path),
                    "why": f"{target['format'].upper()} 格式暂不自动写入（不做盲文本手术）",
                    "manual": f"在 {path} 的 {servers_rel} 中加入 {ENTRY_KEY} = {json.dumps(entry, ensure_ascii=False)}",
                }
            )
        else:
            errors.append(
                f"{path}: 状态 {status}，不写入（missing=目录不在，"
                "unparseable=解析失败，unknown-format=不认识的格式）"
            )
    if written:
        save_points(root, points)
    return {
        "member": member,
        "known": True,
        "written": written,
        "skipped": skipped,
        "errors": errors,
        "points_file": str(root / POINTS_FILE) if written else None,
    }


def revert_points(root: Path | None = None, *, member: str | None = None) -> dict[str, Any]:
    """Restore every recorded write; verify sha256 == original (diff == 0).

    Returns ``reverted`` / ``already_clean`` / ``residual``; residual items
    each carry a human reason — the caller must surface them, never swallow.
    """
    root = _resolve_root(root)
    points = load_points(root)
    if member is not None:
        points = [p for p in points if p.get("member") == member]
    reverted: list[dict[str, Any]] = []
    already: list[dict[str, Any]] = []
    residual: list[dict[str, Any]] = []
    for point in points:
        if point.get("reverted"):
            already.append({"file": point.get("file"), "why": "已还原过"})
            continue
        path = Path(str(point.get("file", "")))
        kind = point.get("kind")
        if not path.exists():
            # The file is gone entirely: nothing of ours remains either way.
            point["reverted"] = True
            point["revert_note"] = "文件已不存在（视为干净）"
            (reverted if kind == "created" else already).append(
                {"file": str(path), "why": point["revert_note"]}
            )
            continue
        current = _sha256_bytes(path.read_bytes())
        if kind == "created":
            if current == point.get("new_sha256"):
                try:
                    path.unlink()
                except OSError as exc:
                    residual.append({"file": str(path), "reason": f"删除失败：{exc}"})
                    continue
                point["reverted"] = True
                reverted.append({"file": str(path), "why": "删除本工具创建的文件"})
            else:
                residual.append({"file": str(path), "reason": "创建后被改过——不盲删，请人工核对"})
            continue
        backup = Path(str(point.get("backup", "")))
        if not backup.exists():
            residual.append(
                {"file": str(path), "reason": f"备份丢失（{backup}）——无法自动还原，残留请手工处理"}
            )
            continue
        if current == point.get("original_sha256"):
            point["reverted"] = True
            already.append({"file": str(path), "why": "已是原始内容（无需还原）"})
            continue
        if current != point.get("new_sha256"):
            residual.append(
                {
                    "file": str(path),
                    "reason": "写入后被第三方改过——自动还原会覆盖别人的改动，已停止；"
                    f"备份仍在 {backup}",
                }
            )
            continue
        restored = backup.read_bytes()
        path.write_bytes(restored)
        if _sha256_bytes(path.read_bytes()) != point.get("original_sha256"):
            residual.append({"file": str(path), "reason": "还原后校验不一致——残留，请人工核对"})
            continue
        point["reverted"] = True
        reverted.append({"file": str(path), "why": "从备份还原，sha256 与原始一致（diff=0）"})
    # persist revert flags (only when something changed)
    if any(p.get("reverted") for p in points):
        save_points(root, points)
    return {"reverted": reverted, "already_clean": already, "residual": residual}
