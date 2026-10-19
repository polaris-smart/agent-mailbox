"""Private Gateway route for mailbox work, with no external chat transport.

This adapter is deliberately inert until the owner's Gateway configuration
explicitly enables it. A separate platform/session key keeps project mail out
of the Human-facing Feishu transcript and cached Agent conversation.
"""

from __future__ import annotations

from typing import Any

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult

PLATFORM_NAME = "agent_mailbox_private"


class PrivateMailboxAdapter(BasePlatformAdapter):
    """Accept only this plugin's authenticated injections; never send to chat."""

    supports_async_delivery = False
    interactive_resume = False

    def __init__(self, config: Any, plugin_id: str, authorize_event):
        super().__init__(config=config, platform=Platform(PLATFORM_NAME))
        self.plugin_id = plugin_id
        self.authorize_event = authorize_event

    @property
    def name(self) -> str:
        return "Agent Mailbox private work"

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def handle_message(self, event) -> None:
        metadata = event.metadata or {}
        if not (
            event.internal is True
            and event.allow_gateway_control is False
            and metadata.get("hermes_plugin_injection") is True
            and metadata.get("hermes_plugin_id") == self.plugin_id
            and getattr(event.source.platform, "value", None) == PLATFORM_NAME
            and metadata.get("gateway_session_key") == self._event_session_key(event)
            and getattr(event, "_gateway_reply_policy", None) == "mailbox_only"
            and self.authorize_event(event) is True
        ):
            raise ValueError("private_mailbox_event_unverified")
        await super().handle_message(event)

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=False, error="private_mailbox_has_no_chat_egress")

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id) -> dict:
        return {"name": "Agent Mailbox private work", "type": "dm", "chat_id": chat_id}

    def toolsets_for_source(self, source) -> list[str]:
        # Private notification turns use the project mailbox, never inherited
        # Feishu/CLI file, shell, browser or other configured MCP tools.
        return ["agent-mailbox"]


def register_private_platform(ctx, authorize_event) -> type[PrivateMailboxAdapter]:
    """Register a closed local route; owner config still must opt in explicitly."""
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="Agent Mailbox private work",
        adapter_factory=lambda config: PrivateMailboxAdapter(config, ctx.plugin_id, authorize_event),
        check_fn=lambda: True,
        is_connected=lambda config: (
            config.enabled is True
            and isinstance(config.extra, dict)
            and config.extra.get("receiver_enabled") is True
        ),
        trusted_inbound=True,
        allow_update_command=False,
    )
    return PrivateMailboxAdapter
