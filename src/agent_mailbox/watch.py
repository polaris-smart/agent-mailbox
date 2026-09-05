
"""Watch a mail root for new messages (FSEvents/kqueue via polling fallback).

Ships as `agent-mailbox watch` subcommand. Lightweight, pure stdlib.

This is the OPTIONAL human-facing companion: prints each new message as a
JSON line and fires desktop notifications (macOS / Linux / Windows). Agent
wake-up does NOT go through here — `store.send()` posts the webhook itself
(see webhook.py); the watcher only ever adds desktop toasts for people.
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import sys
import time

from .store import MailStore


def _notify_desktop(title: str, body: str) -> None:
    """Post a desktop notification (best-effort on all three platforms)."""
    try:
        if sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{body[:120]}" with title "{title}"'],
                check=False, timeout=5,
            )
        elif sys.platform == "linux" and shutil.which("notify-send"):
            subprocess.run(
                ["notify-send", title, body[:120]],
                check=False, timeout=5,
            )
        elif sys.platform == "win32":
            _notify_windows(title, body)
    except (OSError, subprocess.SubprocessError):
        pass  # notifications are best-effort


def _notify_windows(title: str, body: str) -> None:
    """Windows 10+ toast via the WinRT API through PowerShell — no third-party
    dependencies. Text travels base64-encoded to sidestep quoting issues."""
    def b64(text: str) -> str:
        return base64.b64encode(text.encode()).decode()

    script = (
        '[Windows.UI.Notifications.ToastNotificationManager, '
        'Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; '
        '$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('
        '[Windows.UI.Notifications.ToastTemplateType]::ToastText02); '
        '$x=$t.GetElementsByTagName("text"); '
        '$x.Item(0).AppendChild($t.CreateTextNode([Text.Encoding]::UTF8.GetString('
        f'[Convert]::FromBase64String(\'{b64(title)}\')))) | Out-Null; '
        '$x.Item(1).AppendChild($t.CreateTextNode([Text.Encoding]::UTF8.GetString('
        f'[Convert]::FromBase64String(\'{b64(body[:200])}\')))) | Out-Null; '
        '[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('
        '\'Agent Mailbox\').Show((New-Object Windows.UI.Notifications.ToastNotification $t))'
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False, timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def watch(root: str | None, notify_ids: list[str], poll_interval: float = 1.0,
          once: bool = False) -> None:
    """Watch the mail root. On any new message file: print it (stdout, JSON
    line) and optionally fire desktop notifications for listed agent ids."""
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
                    _notify_desktop(
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
    parser.add_argument("--notify", nargs="*", default=[], help="agent ids to desktop-notify")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--once", action="store_true", help="exit after first scan (for cron/tests)")
    args = parser.parse_args()
    watch(args.root, args.notify, args.interval, args.once)


if __name__ == "__main__":
    main()
