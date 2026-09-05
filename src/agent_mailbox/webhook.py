"""Optional webhook notification: new mail is POSTed the moment send() lands.

Config resolves from env (AGENT_MAIL_WEBHOOK_URL / AGENT_MAIL_WEBHOOK_SECRET)
then ~/.agent-mail/webhook.json ({"url": ..., "secret": ...}). No config, no
POST — the mailbox stays fully offline by default. Pure stdlib.

The target is pinned: http/https only, loopback/private addresses by default
(the designed use is a local gateway), redirects refused, system proxy
bypassed. Delivery is best-effort and never raises into the send path.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

SIGNATURE_HEADER = "X-Hub-Signature-256"
EVENT_TYPE = "agent_mailbox_new_message"  # consumers filter on this name
def _config_path() -> Path:
    # resolved per call (not at import time) so AGENT_MAIL_HOME is honoured at runtime
    return Path(os.environ.get("AGENT_MAIL_HOME", Path.home() / ".agent-mail")) / "webhook.json"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# ProxyHandler({}) pins the POST to a direct connection: urllib otherwise
# inherits the system HTTP proxy on macOS, and a local wake-up must never
# detour through one.
_OPENER = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def load_config() -> tuple[str, str] | None:
    """(url, secret) from env, then config file; None when unset."""
    url = os.environ.get("AGENT_MAIL_WEBHOOK_URL")
    secret = os.environ.get("AGENT_MAIL_WEBHOOK_SECRET", "")
    if not url:
        try:
            cfg = json.loads(_config_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        url = cfg.get("url") or None
        secret = cfg.get("secret", "")
    return (url, secret) if url else None


def _validate_url(url: str, allow_public: bool = False) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"webhook url must be http/https, got scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValueError("webhook url carries no host")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"webhook host does not resolve: {host} ({exc})")
    if allow_public:
        return
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not (ip.is_loopback or ip.is_private or ip.is_link_local):
            raise ValueError(f"webhook resolves to non-private {ip}")


def post_message(url: str, secret: str, message: dict, timeout: float = 3.0) -> bool:
    """POST one message as a signed JSON event. Returns True on 2xx."""
    payload = json.dumps(
        # event_type is the key Hermes gateway reads; event kept as an alias
        {"event": EVENT_TYPE, "event_type": EVENT_TYPE, "message": message},
        ensure_ascii=False,
    ).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            SIGNATURE_HEADER: _sign(secret, payload),
        },
    )
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            resp.read()
            return 200 <= resp.status < 300
    except (OSError, urllib.error.URLError):
        return False


def notify_new_messages(
    messages: list[dict],
    *,
    url: str | None = None,
    secret: str = "",
) -> None:
    """Fire-and-forget webhook for freshly persisted mail. Never raises."""
    if not messages:
        return
    if url is None:
        cfg = load_config()
        if not cfg:
            return
        url, secret = cfg
    try:
        _validate_url(url)
    except ValueError:
        return
    for m in messages:
        post_message(url, secret, m)
