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
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

SIGNATURE_HEADER = "X-Hub-Signature-256"  # github style — the default, kept as a named constant
EVENT_TYPE = "agent_mailbox_new_message"  # consumers filter on this name

# Signature styles selectable via AGENT_MAIL_SIGNATURE_STYLE. The HMAC itself
# is always hmac-sha256 over the raw request body (see _sign); only the header
# name and value format differ per style.
_STYLE_HEADERS: dict[str, str] = {
    "github": "X-Hub-Signature-256",   # value sha256=<hex> (default)
    "generic": "X-Webhook-Signature",  # value <hex>, no scheme prefix
    "slack": "X-Slack-Signature",      # value v0=<hex> — see note in _sign
}
def _default_config_root() -> Path:
    # resolved per call (not at import time) so AGENT_MAIL_HOME is honoured at runtime
    return Path(os.environ.get("AGENT_MAIL_HOME", Path.home() / ".agent-mail"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# ProxyHandler({}) pins the POST to a direct connection: urllib otherwise
# inherits the system HTTP proxy on macOS, and a local wake-up must never
# detour through one.
_OPENER = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))


def signature_style() -> str:
    """Resolve AGENT_MAIL_SIGNATURE_STYLE: github (default) | generic | slack.

    Unknown values fall back to github with a stderr note rather than raising:
    notification is best-effort and must never break the send path.
    """
    raw = (os.environ.get("AGENT_MAIL_SIGNATURE_STYLE") or "github").strip().lower()
    if raw not in _STYLE_HEADERS:
        print(
            f"[agent-mailbox] unknown AGENT_MAIL_SIGNATURE_STYLE {raw!r}; using github",
            file=sys.stderr,
            flush=True,
        )
        return "github"
    return raw


def _sign(secret: str, body: bytes, style: str = "github") -> str:
    hex_digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if style == "generic":
        return hex_digest  # bare hex, no scheme prefix
    if style == "slack":
        # NOTE: canonical Slack signing is hmac-sha256 over "v0:{ts}:{body}"
        # using the X-Slack-Request-Timestamp header value. This webhook does
        # not emit a ts field, so the slack style currently differs only in
        # header name and value prefix ("v0=<hex>" over the raw body); a
        # receiver must NOT verify it against the full Slack base string.
        return "v0=" + hex_digest
    return "sha256=" + hex_digest  # github


def load_config(config_root: str | os.PathLike[str] | None = None) -> tuple[str, str] | None:
    """(url, secret) from env, then a webhook.json; None when unset.

    ``config_root`` pins webhook.json to a specific mail root (the store's
    own root). Without it, env ``AGENT_MAIL_HOME`` — then the default home —
    decides, which let a ``MailStore(root=<custom>)`` store read the
    *production* webhook.json and wake the real gateway with throw-away mail.
    """
    url = os.environ.get("AGENT_MAIL_WEBHOOK_URL")
    secret = os.environ.get("AGENT_MAIL_WEBHOOK_SECRET", "")
    if not url:
        base = Path(config_root) if config_root is not None else _default_config_root()
        try:
            cfg = json.loads((base / "webhook.json").read_text(encoding="utf-8"))
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
    style = signature_style()
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            _STYLE_HEADERS[style]: _sign(secret, payload, style),
            # X-Request-ID is the gateway's idempotency key: a retry of the
            # same message is deduped instead of waking the agent again.
            "X-Request-ID": str(message.get("id", "")),
        },
    )
    last_error: Exception | None = None
    for attempt in range(2):  # one retry: 429 means a burst, back off once
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                resp.read()
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 429 and attempt == 0:
                time.sleep(2.0)
                continue
            break
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            break
    print(f"[agent-mailbox] webhook post failed: {last_error}", file=sys.stderr, flush=True)
    return False


def notify_new_messages(
    messages: list[dict],
    *,
    url: str | None = None,
    secret: str = "",
    config_root: str | os.PathLike[str] | None = None,
) -> None:
    """Fire-and-forget webhook for freshly persisted mail. Never raises."""
    if not messages:
        return
    if url is None:
        cfg = load_config(config_root=config_root)
        if not cfg:
            return
        url, secret = cfg
    try:
        _validate_url(url)
    except ValueError:
        return
    for m in messages:
        post_message(url, secret, m)
