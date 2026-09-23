# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately via **GitHub Security Advisories**
(“Report a vulnerability” on `polaris-smart/agent-mailbox`), not as a public
issue. If you cannot use advisories, open a minimal public issue asking for a
private contact — do not include exploit details there.

Please include: affected version, a minimal reproduction, and your assessment
of impact. You will get an acknowledgement within a few days and a fix
timeline once the report is triaged. Please keep details private until a fix
is released.

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.6.x   | ✅        |
| < 0.6   | ❌ upgrade |

## Security model

**Default: local trust.** agent-mailbox is designed for agents running on one
machine you control. The mail root (`~/.agent-mail/`) is plain JSON files;
**any local process or user account that can read those files can read every
mailbox**, and any local MCP client can act as any agent. The web board and
the webhook receiver are the only network-facing surfaces and both enforce
constant-time token checks (`hmac.compare_digest`); the store itself has no
authentication.

**Hardening mode: identity binding (opt-in, v0.6.2).** For machines where
multiple untrusted MCP clients share the same user account, `identity_binding`
in `<mail-root>/config.json` pins agent ids to tokens:

```json
{
  "identity_binding": {
    "enabled": true,
    "HS": "<sha256 hex of the agent's token>"
  }
}
```

Each bound caller presents its secret via the `AGENT_MAIL_TOKEN` environment
variable; the MCP server hashes it with sha256 and compares against the
binding table in constant time. A mismatch rejects the tool call with
`identity mismatch`. Agents not listed in the table are unaffected — the
feature is incremental and disabled by default, so existing deployments keep
today's behavior with zero configuration.

Notes:

- The token is the bearer credential; protect it like any env-carried secret.
  Identity binding raises the bar against casual identity confusion between
  local MCP clients, but it cannot help once an attacker can read the mail
  root or the host's environment.
- A malformed `identity_binding` block fails loudly at server startup
  (fail-loud config) — a half-written security boundary never silently
  degrades to "disabled".
- `claimed_by` markers and `handled_log` entries (v0.5/v0.6.2) make
  multi-session mailbox activity auditable after the fact.
