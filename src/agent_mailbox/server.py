"""agent-mailbox MCP server.

Expose a local, file-backed mailbox as MCP tools. Any MCP-capable agent on
this machine can register once and then message every other agent — no cron,
no polling daemons, no shared markdown files.

Run:  ``agent-mailbox``            (stdio transport, for host apps)
      ``agent-mailbox --http 8642`` (streamable HTTP, for remote agents)
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from mcp.server.mcpserver import MCPServer

from .store import MailboxError, MailStore

server = MCPServer(
    "agent-mailbox",
    instructions=(
        "A global mailbox for local AI agents. Register once with mailbox_register, "
        "then use mailbox_send / mailbox_check / mailbox_reply / mailbox_list / "
        "mailbox_done / mailbox_broadcast. Check your inbox when you start a session "
        "and after finishing a task — messages wait here even when the recipient is offline. "
        "Task cards: task_create / task_move / task_list manage a shared task board; "
        "creating or moving a card auto-messages the assignee, so board motion wakes "
        "agents without polling."
    ),
)

_store: MailStore | None = None


def _store_instance() -> MailStore:
    global _store
    if _store is None:
        _store = MailStore()
    return _store


# --------------------------------------------------------------------- tools

@server.tool()
def mailbox_register(agent_id: str, owner: str = "", description: str = "") -> dict:
    """Register this agent and claim its mailbox. Idempotent — safe to call again."""
    return _store_instance().register(agent_id, owner, description)


@server.tool()
def mailbox_send(
    to: str | list[str],
    subject: str,
    body: str,
    priority: str = "normal",
    reply_to: str | None = None,
    from_id: str = "",
) -> dict:
    """Send a message to one agent, a list of agents, or \"all\" for broadcast."""
    frm = from_id or os.environ.get("AGENT_MAIL_ID", "")
    if not frm:
        raise MailboxError("from_id required (or set AGENT_MAIL_ID env)")
    sent = _store_instance().send(frm, to, subject, body, reply_to=reply_to, priority=priority)
    return {"delivered": sent, "count": len(sent)}


@server.tool()
def mailbox_check(agent_id: str = "", mark: bool = True) -> dict:
    """Fetch your pending messages (they become acked). Call at session start."""
    me = agent_id or os.environ.get("AGENT_MAIL_ID", "")
    if not me:
        raise MailboxError("agent_id required (or set AGENT_MAIL_ID env)")
    msgs = _store_instance().check(me, mark=mark)
    return {"agent_id": me, "unread": len(msgs), "messages": msgs}


@server.tool()
def mailbox_reply(msg_id: str, body: str, agent_id: str = "") -> dict:
    """Reply to a message thread. Routes to the original sender automatically."""
    me = agent_id or os.environ.get("AGENT_MAIL_ID", "")
    if not me:
        raise MailboxError("agent_id required (or set AGENT_MAIL_ID env)")
    st = _store_instance()
    mine = [m for m in st.list_messages(me) if m["id"] == msg_id]
    from_archive = False
    if not mine:
        mine = [m for m in st.list_archived(me) if m["id"] == msg_id]
        from_archive = True
    if not mine:
        raise MailboxError(
            f"message {msg_id!r} not found in inbox or archive for {me!r}"
        )
    original = mine[0]
    sent = st.send(
        me,
        original["from"],
        f"Re: {original['subject']}",
        body,
        reply_to=msg_id,
    )
    if from_archive:
        # Original is already done + archived; nothing left to close.
        return {"replied": sent[0], "closed": None, "archived_original": msg_id}
    st.set_status(me, msg_id, "done")
    return {"replied": sent[0], "closed": msg_id}


@server.tool()
def mailbox_list(agent_id: str = "", status: str | None = None) -> dict:
    """List messages in your mailbox, optionally filtered by status."""
    me = agent_id or os.environ.get("AGENT_MAIL_ID", "")
    if not me:
        raise MailboxError("agent_id required (or set AGENT_MAIL_ID env)")
    msgs = _store_instance().list_messages(me, status)
    return {"agent_id": me, "count": len(msgs), "messages": msgs}


@server.tool()
def mailbox_done(msg_id: str, agent_id: str = "") -> dict:
    """Mark a message as handled. Done messages can be archived."""
    me = agent_id or os.environ.get("AGENT_MAIL_ID", "")
    if not me:
        raise MailboxError("agent_id required (or set AGENT_MAIL_ID env)")
    m = _store_instance().set_status(me, msg_id, "done")
    n = _store_instance().archive_done(me)
    return {"message": m["id"], "status": "done", "archived": n}


@server.tool()
def mailbox_broadcast(subject: str, body: str, from_id: str = "") -> dict:
    """Broadcast to every registered agent (including boss)."""
    frm = from_id or os.environ.get("AGENT_MAIL_ID", "")
    if not frm:
        raise MailboxError("from_id required (or set AGENT_MAIL_ID env)")
    sent = _store_instance().send(frm, "all", subject, body, priority="high")
    return {"delivered": sent, "count": len(sent)}


@server.tool()
def mailbox_whoami() -> dict:
    """List all registered agents and the mail root location."""
    st = _store_instance()
    reg = st.registry()
    return {
        "mail_root": str(st.root),
        "default_identity": os.environ.get("AGENT_MAIL_ID", ""),
        "agents": reg["agents"],
    }


@server.tool()
def mailbox_wait(agent_id: str = "", timeout_seconds: float = 25.0) -> dict:
    """Block until a new message arrives (long-poll, up to timeout). Returns
    immediately if pending messages exist. Import 'time' is at module top."""
    me = agent_id or os.environ.get("AGENT_MAIL_ID", "")
    if not me:
        raise MailboxError("agent_id required (or set AGENT_MAIL_ID env)")
    st = _store_instance()
    deadline = time.time() + max(1.0, min(timeout_seconds, 60.0))
    while True:
        msgs = st.list_messages(me, status="pending")
        if msgs:
            got = st.check(me, mark=True)
            return {"agent_id": me, "received": len(got), "messages": got}
        if time.time() >= deadline:
            return {"agent_id": me, "received": 0, "messages": [], "timeout": True}
        time.sleep(0.5)


# ---------------------------------------------------------------- task tools

@server.tool()
def task_create(
    title: str, assignee: str, due: str = "", from_id: str = "", notify: bool = True
) -> dict:
    """Create a task card (starts at todo). The assignee is auto-messaged —
    skip with notify=False."""
    me = from_id or os.environ.get("AGENT_MAIL_ID", "")
    if not me:
        raise MailboxError("from_id required (or set AGENT_MAIL_ID env)")
    task = _store_instance().task_create(title, assignee, me, due, notify=notify)
    return {"task": task}


@server.tool()
def task_move(
    task_id: str,
    status: str,
    assignee: str | None = None,
    note: str = "",
    force: bool = False,
    notify: bool = True,
    from_id: str = "",
) -> dict:
    """Move a task along todo→doing→review→done. Skips need force=True;
    done is terminal. Pass assignee to reassign. The (new) assignee is
    auto-messaged — moving a card wakes its owner."""
    me = from_id or os.environ.get("AGENT_MAIL_ID", "")
    if not me:
        raise MailboxError("from_id required (or set AGENT_MAIL_ID env)")
    task = _store_instance().task_move(
        task_id, status, moved_by=me, assignee=assignee,
        force=force, notify=notify, note=note,
    )
    return {"task": task}


@server.tool()
def task_list(assignee: str | None = None, status: str | None = None) -> dict:
    """List task cards, optionally filtered by assignee and/or status."""
    tasks = _store_instance().task_list(assignee=assignee, status=status)
    return {"count": len(tasks), "tasks": tasks}


def main() -> None:
    # MCP stdio speaks UTF-8; on Windows/macOS CI the default console codec
    # (cp1252 etc.) cannot encode arrows/CJK in tool output and crashes the
    # child process before the handshake completes.
    for _stream in (sys.stdout, sys.stderr):
        if _stream is not None and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass

    parser = argparse.ArgumentParser(prog="agent-mailbox")
    parser.add_argument("--http", metavar="PORT", type=int, default=None,
                        help="serve streamable HTTP on PORT (default: stdio)")
    parser.add_argument("--web", metavar="PORT", type=int, default=None,
                        help="serve the kanban board UI + JSON API on PORT (default: stdio)")
    parser.add_argument("--home", metavar="DIR", default=None,
                        help="mail root directory (default: ~/.agent-mail)")
    args = parser.parse_args()

    if args.home:
        os.environ["AGENT_MAIL_HOME"] = args.home

    if args.web:
        from .web import run_web

        run_web(args.web)
    elif args.http:
        server.run(transport="streamable-http", port=args.http)
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
