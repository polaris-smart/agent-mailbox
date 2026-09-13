"""Reap orphaned ``acked`` mail back to ``pending`` (task t-6 wiring, v0.5).

``MailStore.check()`` moves pending -> acked and hands the letters to the
caller; a caller that dies, is cancelled, or was a foreign-identity check
leaves them acked forever, invisible to a pending-only drain. This CLI is the
thin shell the wake loop calls *before* counting pending, so those letters
come back into view instead of hiding behind a zero count.

``python -m agent_mailbox.reap --agent ZC --ttl 3600``
Never deletes anything: status flips back to pending and ``handled_log``
records a ``reclaimed`` entry. Pure stdlib.
"""

from __future__ import annotations

import argparse
import sys

from .store import MailboxError, MailStore


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent-mailbox-reap")
    parser.add_argument("--agent", required=True, help="mailbox to reap (defaults to AGENT_MAIL_ID when omitted)")
    parser.add_argument(
        "--ttl",
        type=float,
        default=3600.0,
        help="seconds a letter may stay acked before it is reclaimed (default: 3600)",
    )
    args = parser.parse_args()

    try:
        reaped = MailStore().reap_stale_acked(args.agent, ttl_seconds=args.ttl)
    except MailboxError as e:
        print(f"reap: {e}", file=sys.stderr)
        raise SystemExit(1) from e
    for mid in reaped:
        print(f"reclaimed {mid}")
    print(f"reap: {len(reaped)} reclaimed (agent={args.agent}, ttl={args.ttl:g}s)")


if __name__ == "__main__":
    main()
