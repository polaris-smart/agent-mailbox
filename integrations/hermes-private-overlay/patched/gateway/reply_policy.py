"""Host-owned presentation policy for a plugin-injected gateway event.

The policy is process-local event state, not inbound platform metadata. A normal Human event
always retains its platform reply route, even when it is queued behind a silent plugin turn.
"""

from contextlib import contextmanager
from contextvars import ContextVar

PLATFORM_REPLY = "platform"
MAILBOX_ONLY_REPLY = "mailbox_only"
_active_reply_policy: ContextVar[str] = ContextVar("gateway_active_reply_policy", default=PLATFORM_REPLY)
_active_reply_event: ContextVar[object | None] = ContextVar("gateway_active_reply_event", default=None)
_active_terminal_override: ContextVar[bool] = ContextVar("gateway_active_terminal_override", default=False)


class ReplyPolicyDenied(RuntimeError):
    """A mailbox-only turn tried to use an external chat egress."""


def event_reply_policy(event) -> str:
    """Resolve the outlet of this exact event; an invalid host policy fails closed."""
    if event is None:
        return PLATFORM_REPLY
    # The opening event may be Human while the terminal queued event is plugin-owned (or the
    # reverse). The runner stamps the terminal policy on the opener after its in-band drain.
    policy = getattr(event, "_gateway_reply_policy", PLATFORM_REPLY)
    return policy if policy in (PLATFORM_REPLY, MAILBOX_ONLY_REPLY) else MAILBOX_ONLY_REPLY


def mailbox_only_reply(event) -> bool:
    # Some send paths have no event, or hold a different event while a queued
    # follow-up is running. For the event bound to this task, the immutable
    # context snapshot owns its outlet: another task may repurpose the shared
    # opener object for a later queued terminal reply. Only a verified terminal
    # override in this task may supersede the opener's own immutable policy.
    # Every other mismatch fails closed if either signal says mailbox-only.
    active = _active_reply_policy.get()
    if event is not None and _active_reply_event.get() is event:
        if _active_terminal_override.get():
            return active == MAILBOX_ONLY_REPLY
        # A previously spawned task keeps its opening event and context. The
        # terminal marker belongs to the parent task's final send, not to it.
        return event_reply_policy(event) == MAILBOX_ONLY_REPLY or active == MAILBOX_ONLY_REPLY
    terminal = getattr(event, "_gateway_terminal_reply_policy", None)
    return (
        event_reply_policy(event) == MAILBOX_ONLY_REPLY
        or (terminal is not None and terminal != PLATFORM_REPLY)
        or active == MAILBOX_ONLY_REPLY
    )


def mailbox_only_turn_active() -> bool:
    """Egress backstop; the event remains authoritative and each queued turn rebinds it."""
    return _active_reply_policy.get() == MAILBOX_ONLY_REPLY


def refresh_event_reply_policy(event) -> None:
    """Rebind after an in-band queued turn changes which event owns the terminal reply."""
    terminal = getattr(event, "_gateway_terminal_reply_policy", None)
    if terminal is None:
        _active_reply_policy.set(event_reply_policy(event))
        _active_terminal_override.set(False)
    else:
        _active_reply_policy.set(
            terminal if terminal in (PLATFORM_REPLY, MAILBOX_ONLY_REPLY) else MAILBOX_ONLY_REPLY
        )
        _active_terminal_override.set(True)
    _active_reply_event.set(event)


@contextmanager
def bind_event_reply_policy(event):
    token = _active_reply_policy.set(event_reply_policy(event))
    event_token = _active_reply_event.set(event)
    terminal_token = _active_terminal_override.set(False)
    try:
        yield
    finally:
        _active_terminal_override.reset(terminal_token)
        _active_reply_event.reset(event_token)
        _active_reply_policy.reset(token)
