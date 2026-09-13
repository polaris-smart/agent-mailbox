"""Scan a mail root for suspected test residue (dry-run by default).

``python -m agent_mailbox.cleanup --dry-run`` lists — never deletes —
directories and letters that look like leftovers:

- agent ``inbox/``/``archive/`` directories whose id is not in registry.json;
- NEWBIE/WBTEST-style test-named directories;
- orphan letters: stray files directly under inbox/ or archive/, JSON a
  mailbox_list can no longer parse, and ``*.tmp`` left by an interrupted
  atomic write.

The default mode is dry-run. Actual deletion requires explicit ``--yes`` and
an interactive confirmation; registered-but-test-named directories are
reported as review-only and never deleted. Pure stdlib.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

# case-insensitive substrings that mark a directory as test residue
TEST_NAME_MARKERS = ("test", "newbie", "demo", "scratch")


def _is_test_name(name: str) -> bool:
    low = name.lower()
    return any(marker in low for marker in TEST_NAME_MARKERS)


def _human(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def _dir_stats(d: Path) -> tuple[int, int]:
    files = [p for p in d.rglob("*") if p.is_file()]
    return sum(p.stat().st_size for p in files), len(files)


def scan(root: Path) -> list[dict]:
    """Return suspected-residue findings for ``root`` (no mutation)."""
    findings: list[dict] = []
    agents: set[str] = set()
    try:
        reg = json.loads((root / "registry.json").read_text(encoding="utf-8"))
        agents = set(reg.get("agents", {}))
    except FileNotFoundError:
        pass  # empty/missing registry: every agent dir counts as residue
    except json.JSONDecodeError as e:
        raise SystemExit(f"cleanup: corrupt registry.json: {e}") from e

    for base_name in ("inbox", "archive"):
        base = root / base_name
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            rel = f"{base_name}/{entry.name}"
            if entry.is_file():
                findings.append({
                    "kind": "orphan-letter",
                    "path": rel,
                    "size": entry.stat().st_size,
                    "reason": "stray file directly under inbox/archive (no agent dir)",
                    "action": "delete file",
                })
                continue
            size, n_files = _dir_stats(entry)
            registered = entry.name in agents
            test_named = _is_test_name(entry.name)
            if not registered:
                kind = "test-named-dir" if test_named else "unregistered-dir"
                reason = "agent not in registry.json"
                if test_named:
                    reason += "; name matches test pattern"
                findings.append({
                    "kind": kind, "path": rel, "size": size,
                    "reason": reason, "action": f"delete dir ({n_files} files)",
                })
            elif test_named:
                findings.append({
                    "kind": "test-named-dir", "path": rel, "size": size,
                    "reason": f"test-named but registered ({n_files} files)",
                    "action": "review only — not deleted",
                })
            else:
                for p in sorted(entry.glob("*.json")):
                    try:
                        json.loads(p.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError):
                        findings.append({
                            "kind": "orphan-letter",
                            "path": f"{rel}/{p.name}",
                            "size": p.stat().st_size,
                            "reason": "unparseable JSON — invisible to mailbox_list",
                            "action": "delete file",
                        })

    for p in sorted(root.glob("*.tmp")):
        findings.append({
            "kind": "orphan-letter",
            "path": p.name,
            "size": p.stat().st_size,
            "reason": "stray *.tmp from an interrupted atomic write",
            "action": "delete file",
        })
    return findings


def _delete(root: Path, findings: list[dict]) -> int:
    n = 0
    for f in findings:
        if not f["action"].startswith("delete"):
            continue
        path = root / f["path"]
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        n += 1
    return n


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="agent-mailbox-cleanup",
        description="List (default) or delete suspected test residue in a mail root.",
    )
    parser.add_argument(
        "--root", default=None,
        help="mail root (default $AGENT_MAIL_HOME or ~/.agent-mail)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="list suspects only, never delete (the default behaviour)",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="actually delete the listed suspects (asks for confirmation)",
    )
    args = parser.parse_args(argv)

    root = Path(args.root or os.environ.get("AGENT_MAIL_HOME", "~/.agent-mail")).expanduser()
    if not root.is_dir():
        print(f"cleanup: mail root not found: {root}", file=sys.stderr)
        raise SystemExit(1)

    findings = scan(root)
    if not findings:
        print(f"cleanup: no suspected residue in {root}")
        return

    total = 0
    for f in findings:
        total += f["size"]
        print(f"[{f['kind']}] {f['path']}  {_human(f['size'])}  ({f['reason']})  -> {f['action']}")
    print(f"\n{len(findings)} finding(s), {_human(total)} total")

    if args.dry_run or not args.yes:
        print("nothing deleted (pass --yes to actually delete)")
        return
    answer = input(f"Delete the listed items in {root}? Type 'yes' to confirm: ")
    if answer.strip().lower() != "yes":
        print("aborted — nothing deleted")
        return
    print(f"deleted {_delete(root, findings)} item(s)")


if __name__ == "__main__":
    main()
