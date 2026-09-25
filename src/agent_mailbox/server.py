"""agent-mailbox MCP server.

Expose a local, file-backed mailbox as MCP tools. Any MCP-capable agent on
this machine can register once and then message every other agent — no cron,
no polling daemons, no shared markdown files.

Run:  ``agent-mailbox``            (stdio transport, for host apps)
      ``agent-mailbox --http 8642`` (streamable HTTP, for remote agents)
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import logging
import os
import sys
import time

from mcp.server.mcpserver import MCPServer

from .sampling import SamplingNotifier, SamplingRegistry
from .store import MailboxError, MailStore, ghost_open_limit, load_identity_binding

logger = logging.getLogger(__name__)


class _WakeCapabilityMiddleware:
    """MCP 连接登记中间件（v0.7 改点1），两条观测线：

    - ``initialize``：宿主是否声明 ``capabilities.sampling``，逐连接登记
      （per-connection），同时捕获该连接的事件循环供跨线程调度回环。
      initialize 处理器被 SDK runner 保留，官方口径即用 middleware 观察；
    - ``tools/call``：把 acting identity（参数 agent_id / from_id，缺省回退
      AGENT_MAIL_ID env）绑到其连接——send() 落箱后按收件人身份寻址
      sampling 通道的依据。

    纯观察不重写：`call_next(ctx)` 原样放行，注册失败也不拦握手。
    """

    async def __call__(self, ctx: object, call_next: object) -> object:
        method = getattr(ctx, "method", "")
        params = getattr(ctx, "params", None) or {}
        if method == "initialize":
            caps = params.get("capabilities")
            declared = isinstance(caps, dict) and "sampling" in caps
            try:
                _sampling_registry.register_connection(ctx.session, declared)
            except RuntimeError:
                logger.debug("no running loop at initialize; sampling registry skipped")
        elif method == "tools/call":
            args = params.get("arguments") or {}
            me = args.get("agent_id") or args.get("from_id") or ""
            if not me and str(params.get("name") or "").startswith(("mailbox_", "task_")):
                me = os.environ.get("AGENT_MAIL_ID", "")
            if me:
                _sampling_registry.bind_agent(str(me), ctx.session)
        return await call_next(ctx)  # type: ignore[misc]


# v0.7 sampling 唤醒：per-connection 能力登记（改点1）+ 落箱通知器（改点2/3）。
_sampling_registry = SamplingRegistry()
_sampling_notifier = SamplingNotifier(_sampling_registry)

server = MCPServer(
    "agent-mailbox",
    instructions=(
        "A global mailbox for local AI agents. Register once with mailbox_register, "
        "then use mailbox_send / mailbox_check / mailbox_reply / mailbox_list / "
        "mailbox_done / mailbox_broadcast. Check your inbox when you start a session "
        "and after finishing a task — messages wait here even when the recipient is offline. "
        "mailbox_thread(thread) replays a whole conversation in time order across "
        "agents — prefer it over stacking Re: prefixes. "
        "Task cards: task_create / task_move / task_list manage a shared task board; "
        "creating or moving a card auto-messages the assignee, so board motion wakes "
        "agents without polling."
    ),
    middleware=[_WakeCapabilityMiddleware()],
)

_store: MailStore | None = None
_binding: dict | None = None  # identity binding table, loaded once per process


def _store_instance() -> MailStore:
    global _store
    if _store is None:
        _store = MailStore()
        _store.on_delivered = _wake_on_delivered
    return _store


def _wake_on_delivered(landed: list[dict]) -> None:
    """send() 落箱钩子（v0.7 改点2）：对收件人已声明 sampling 的连接异步发
    createMessage。自信（from == to）跳过——发件人自己刚写的信无需被唤醒
    （对齐 self-echo 先例）。notify() 只做寻址+调度，微秒级返回，不阻塞
    send 主链路；实际请求/超时/去重/审计全在连接事件循环侧（sampling 模块）。
    """
    for m in landed:
        if m.get("from") == m.get("to"):
            continue
        try:
            _sampling_notifier.notify(_store_instance(), str(m.get("to")), str(m.get("id")))
        except Exception as exc:  # noqa: BLE001 — 唤醒失败永不影响落箱主链路
            logger.warning("sampling wake dispatch failed for %s: %s", m.get("id"), exc)


def _identity_binding() -> dict:
    """The identity binding table, read once at server startup.

    A malformed ``identity_binding`` block raises here (fail-loud) — via
    ``main()`` before the transport starts, or on the first tool call — so a
    half-written security boundary never silently degrades to "disabled".
    """
    global _binding
    if _binding is None:
        _binding = load_identity_binding(_store_instance().root)
    return _binding


def _verify_identity(agent_id: str) -> None:
    """Enforce identity binding for one tool call.

    With binding enabled, an agent listed in the table must present the
    ``AGENT_MAIL_TOKEN`` env whose sha256 matches the stored hash — compared
    in constant time, per the web.py token precedent. A mismatch rejects the
    call with ``identity mismatch``. Identities not in the table, and every
    call when binding is disabled, keep the default local-trust behavior
    (fail-open compatibility).
    """
    binding = _identity_binding()
    if not binding.get("enabled"):
        return
    expected = binding.get(agent_id or "")
    if not expected:
        return  # unbound identity: local trust as before
    token = os.environ.get("AGENT_MAIL_TOKEN", "")
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(digest, expected):
        raise MailboxError("identity mismatch")


# --------------------------------------------------------------------- tools


@server.tool()
def mailbox_register(agent_id: str, owner: str = "", description: str = "") -> dict:
    """Register this agent and claim its mailbox. Idempotent — safe to call again."""
    _verify_identity(agent_id)
    return _store_instance().register(agent_id, owner, description)


@server.tool()
def mailbox_send(
    to: str | list[str],
    subject: str,
    body: str,
    priority: str = "normal",
    reply_to: str | None = None,
    from_id: str = "",
    dedupe: bool = True,
) -> dict:
    """Send a message to one agent, a list of agents, or \"all\" for broadcast.

    dedupe=True (default) suppresses a re-send of semantically identical
    mail to a recipient whose inbox still holds it non-terminal (pending /
    acked) within the 24h dedup window: that recipient's entry comes back as
    {\"to\", \"deduped\": true, \"existing_id\"} with zero side effects — no
    letter, no sent.log line, no webhook. Pass dedupe=False to exempt
    periodic jobs. \"count\" counts only letters that actually landed.
    """
    frm = from_id or os.environ.get("AGENT_MAIL_ID", "")
    if not frm:
        raise MailboxError("from_id required (or set AGENT_MAIL_ID env)")
    _verify_identity(frm)
    sent = _store_instance().send(
        frm, to, subject, body, reply_to=reply_to, priority=priority, dedupe=dedupe
    )
    delivered = sum(1 for e in sent if not e.get("deduped"))
    return {"delivered": sent, "count": delivered}


@server.tool()
def mailbox_check(agent_id: str = "", mark: bool = True) -> dict:
    """Fetch your pending messages (they become acked). Call at session start.

    Adds ``ghosts`` when any thread you hold more than 5 open (non-done)
    letters in — that is the acked-sinking failure mode; drain those
    threads before starting new work.
    """
    me = agent_id or os.environ.get("AGENT_MAIL_ID", "")
    if not me:
        raise MailboxError("agent_id required (or set AGENT_MAIL_ID env)")
    _verify_identity(me)
    st = _store_instance()
    msgs = st.check(me, mark=mark)
    out: dict = {"agent_id": me, "unread": len(msgs), "messages": msgs}
    ghosts = st.ghost_threads()
    if ghosts:
        out["ghosts"] = ghosts
        out["ghost_warning"] = (
            f"{len(ghosts)} thread(s) hold more than {ghost_open_limit()} open "
            "letters — finish these threads before starting new work"
        )
    return out


@server.tool()
def mailbox_reply(msg_id: str, body: str, agent_id: str = "") -> dict:
    """Reply to a message thread. Routes to the original sender automatically."""
    me = agent_id or os.environ.get("AGENT_MAIL_ID", "")
    if not me:
        raise MailboxError("agent_id required (or set AGENT_MAIL_ID env)")
    _verify_identity(me)
    st = _store_instance()
    mine = [m for m in st.list_messages(me) if m["id"] == msg_id]
    from_archive = False
    if not mine:
        mine = [m for m in st.list_archived(me) if m["id"] == msg_id]
        from_archive = True
    if not mine:
        raise MailboxError(f"message {msg_id!r} not found in inbox or archive for {me!r}")
    original = mine[0]
    sent = st.send(
        me,
        original["from"],
        f"Re: {original['subject']}",
        body,
        reply_to=msg_id,
        dedupe=False,  # replies are thread-addressed; keep the legacy contract
    )
    if from_archive:
        # Original is already done + archived; nothing left to close.
        return {"replied": sent[0], "closed": None, "archived_original": msg_id}
    st.set_status(me, msg_id, "done")
    return {"replied": sent[0], "closed": msg_id}


@server.tool()
def mailbox_list(agent_id: str = "", status: str | None = None, thread: str | None = None) -> dict:
    """List messages in your mailbox, optionally filtered by status and/or thread.

    Spans inbox **and** archive: ``check()`` moves handled letters into the
    archive, so an inbox-only view reports zero for any agent that drains
    regularly (HS 09-24: five status queries all returned 0 against 877
    on-disk letters). ``thread`` takes a thread_id, or any subject on the
    thread (legacy letters without a thread_id are matched by the subject key
    heuristic).
    """
    me = agent_id or os.environ.get("AGENT_MAIL_ID", "")
    if not me:
        raise MailboxError("agent_id required (or set AGENT_MAIL_ID env)")
    _verify_identity(me)
    msgs = _store_instance().list_all_messages(me, status, thread=thread)
    return {"agent_id": me, "count": len(msgs), "messages": msgs}


@server.tool()
def mailbox_thread(thread: str) -> dict:
    """Pull one thread in time order across every agent (inbox + archive).

    ``thread`` may be a thread_id, any message id on the thread, or a subject
    on the thread. Legacy letters without an id/thread_id are included via
    the subject-key heuristic (Re:/Fwd: prefixes stripped), so a 12-deep
    "Re: Re: ..." chain resolves to one thread. Letters come back oldest
    first with their status, so the full cross-agent conversation is
    visible in one call.
    """
    _verify_identity(os.environ.get("AGENT_MAIL_ID", ""))
    st = _store_instance()
    try:
        return st.thread_messages(thread)
    except MailboxError as e:
        # Structured miss instead of an MCP "Error executing tool" bubble:
        # callers probing by commit hashes or session ids (HS 09-24) get an
        # addressable empty result, not a stack trace.
        return {
            "error": str(e),
            "thread_id": None,
            "matched_by": "miss",
            "count": 0,
            "messages": [],
        }


@server.tool()
def mailbox_done(msg_id: str, agent_id: str = "") -> dict:
    """Mark a message as handled. Done messages can be archived."""
    me = agent_id or os.environ.get("AGENT_MAIL_ID", "")
    if not me:
        raise MailboxError("agent_id required (or set AGENT_MAIL_ID env)")
    _verify_identity(me)
    m = _store_instance().set_status(me, msg_id, "done")
    n = _store_instance().archive_done(me)
    return {"message": m["id"], "status": "done", "archived": n}


@server.tool()
def mailbox_broadcast(subject: str, body: str, from_id: str = "", dedupe: bool = True) -> dict:
    """Broadcast to every registered agent (including boss). dedupe=True
    (default) suppresses semantically identical re-broadcasts per recipient
    within the dedup window — see mailbox_send."""
    frm = from_id or os.environ.get("AGENT_MAIL_ID", "")
    if not frm:
        raise MailboxError("from_id required (or set AGENT_MAIL_ID env)")
    _verify_identity(frm)
    sent = _store_instance().send(frm, "all", subject, body, priority="high", dedupe=dedupe)
    delivered = sum(1 for e in sent if not e.get("deduped"))
    return {"delivered": sent, "count": delivered}


@server.tool()
def mailbox_whoami() -> dict:
    """List all registered agents and the mail root location."""
    _verify_identity(os.environ.get("AGENT_MAIL_ID", ""))
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
    _verify_identity(me)
    st = _store_instance()
    deadline = time.time() + max(1.0, min(timeout_seconds, 60.0))
    while True:
        # Atomic claim (v0.6.2): list + ack in one locked pass, so a second
        # waiter on this mailbox can never re-consume the same batch. A
        # claimed-but-unhandled letter stays recoverable via the stale-acked
        # reap round (fail-open).
        got = st.claim(me)
        if got:
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
    _verify_identity(me)
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
    _verify_identity(me)
    task = _store_instance().task_move(
        task_id,
        status,
        moved_by=me,
        assignee=assignee,
        force=force,
        notify=notify,
        note=note,
    )
    return {"task": task}


@server.tool()
def task_list(assignee: str | None = None, status: str | None = None) -> dict:
    """List task cards, optionally filtered by assignee and/or status."""
    _verify_identity(os.environ.get("AGENT_MAIL_ID", ""))
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

    argv = sys.argv[1:]
    # `agent-mailbox wake <sub>` dispatches to the wake-daemon CLI; everything
    # before `wake` is parsed by the server parser (so `--home DIR wake install`
    # keeps working) and forwarded as the wake root.
    if "wake" in argv:
        idx = argv.index("wake")
        pre = argv[:idx]
        import argparse as _argparse

        pre_parser = _argparse.ArgumentParser(prog="agent-mailbox", add_help=False)
        pre_parser.add_argument("--http", type=int, default=None)
        pre_parser.add_argument("--web", type=int, default=None)
        pre_parser.add_argument("--home", default=None)
        pre_args, _ = pre_parser.parse_known_args(pre)
        from .wake import wake_main

        wake_args = argv[idx + 1 :]
        subcommand = next((a for a in wake_args if not a.startswith("-")), "")
        if (
            pre_args.home
            and subcommand != "uninstall"
            and not any(a == "--root" or a.startswith("--root=") for a in wake_args)
        ):
            wake_args = ["--root", pre_args.home, *wake_args]
        wake_main(wake_args)
        return

    parser = argparse.ArgumentParser(prog="agent-mailbox")
    parser.add_argument(
        "--http",
        metavar="PORT",
        type=int,
        default=None,
        help="serve streamable HTTP on PORT (default: stdio)",
    )
    parser.add_argument(
        "--web",
        metavar="PORT",
        type=int,
        default=None,
        help="serve the kanban board UI + JSON API on PORT (default: stdio)",
    )
    parser.add_argument(
        "--home", metavar="DIR", default=None, help="mail root directory (default: ~/.agent-mail)"
    )
    args = parser.parse_args()

    if args.home:
        os.environ["AGENT_MAIL_HOME"] = args.home

    # Load (and validate) the identity binding table at startup: a corrupt
    # identity_binding block kills the server here, before any tool runs,
    # instead of mid-session on the first guarded call.
    _identity_binding()

    if args.web:
        from .web import run_web

        run_web(args.web)
    elif args.http:
        server.run(transport="streamable-http", port=args.http)
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
