
"""Watch a mail root for new messages (FSEvents/kqueue via polling fallback).

Ships as `agent-mailbox watch` subcommand. Lightweight (<100 lines), pure stdlib.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time

from .store import MailStore


def _notify_macos(title: str, body: str) -> None:
    """Post a macOS notification center alert (best-effort)."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{body[:120]}" with title "{title}"'],
            check=False, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        pass  # notifications are best-effort


def watch(root: str | None, notify_ids: list[str], poll_interval: float = 1.0,
          once: bool = False) -> None:
    """Watch the mail root. On any new message file, print it (stdout, JSON line)
    and optionally fire macOS notifications for listed agent ids."""
    st = MailStore(root)
    seen: set[str] = set()

    # baseline: everything currently on disk counts as seen
    for p in (st.root / "inbox").rglob("*.json"):
        seen.add(p.name)

    print(json.dumps({"event": "watching", "root": str(st.root)}), flush=True)
    while True:
        new = []
        for p in (st.root / "inbox").rglob("*.json"):
            if p.name not in seen:
                seen.add(p.name)
                try:
                    m = json.loads(p.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue  # skip partially-written files
                new.append(m)
                rid = m.get("to", "")
                if rid in notify_ids:
                    _notify_macos(
                        f"agent-mailbox: {m.get('from', '?')} -> {rid}",
                        m.get("subject", ""),
                    )
        for m in new:
            print(json.dumps({"event": "new_message", "message": m}, ensure_ascii=False), flush=True)
        if once and new:
            return
        if once:
            return
        time.sleep(poll_interval)


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent-mailbox-watch")
    parser.add_argument("--root", default=None, help="mail root (default ~/.agent-mail)")
    parser.add_argument("--notify", nargs="*", default=[], help="agent ids to macOS-notify")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--once", action="store_true", help="exit after first scan (for cron/tests)")
    args = parser.parse_args()
    watch(args.root, args.notify, args.interval, args.once)


if __name__ == "__main__":
    main()
