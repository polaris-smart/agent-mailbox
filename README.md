# agent-mailbox

**Give every local AI agent its own mailbox.**

One MCP server. Register once, message any agent on this machine. No cron. No polling daemons. No shared markdown files. No cloud.

📖 **Docs**: [English](README.md) · [中文](README.zh-CN.md) · [日本語](README.ja.md) · [Español](README.es.md) — [Architecture diagram](docs/architecture-en.html) · [中文版](docs/architecture.html)

![agent-mailbox architecture](docs/architecture.png)

```bash
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox          # stdio transport, ready for any MCP host
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox --http 8642   # or expose it over HTTP for remote agents
```

---

## The problem

Your coding agent, your ops agent, your review agent — all running on the same machine, all perfectly capable — cannot talk to each other. So **you** end up being the messenger: copying conclusions from one terminal, pasting instructions into another, relaying status updates by hand.

File-based workarounds (a shared markdown "log", a `dropped-notes/` folder) decay into an unreadable transcript. Cron-and-scan workarounds burn tokens on empty polls. Cloud relays put your workflow data behind someone else's API.

## The fix

A mailbox that is **just another tool**:

| Tool | What it does |
|---|---|
| `mailbox_register` | Claim your mailbox. Idempotent. |
| `mailbox_send` | Deliver to one agent, a list, or `"all"` for broadcast. |
| `mailbox_check` | Fetch pending messages — they auto-ack on read. |
| `mailbox_reply` | Reply inside a thread, auto-routed to the sender. |
| `mailbox_list` | Browse by status (`pending` / `acked` / `done`). |
| `mailbox_done` | Mark handled; done messages archive automatically. |
| `mailbox_broadcast` | One call, every registered agent. |
| `mailbox_whoami` | Who's registered, where the mail root is. |

Messages are plain JSON with a tiny lifecycle: `pending → acked → done`. A message that arrives while the recipient is offline simply waits — mail, like mail should.

## Quick start

**Hermes** (`~/.hermes/config.yaml`):

```yaml
mcp:
  servers:
    agent-mailbox:
      command: uvx
      args: ["--from", "git+https://github.com/polaris-smart/agent-mailbox", "agent-mailbox"]
```

**Claude Code** (`~/.claude/settings.json`):

```json
{ "mcpServers": { "agent-mailbox": { "command": "uvx", "args": ["--from", "git+https://github.com/polaris-smart/agent-mailbox", "agent-mailbox"] } } }
```

**Any MCP client** (stdio):

```bash
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox
```

**Remote agents** (e.g. an agent on another server):

```bash
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox --http 8642   # on the mail host
```

```json
{ "mcpServers": { "agent-mailbox": { "url": "http://your-host:8642/mcp" } } }
```


## Waiting for mail (no polling)

Agents don't need to poll. `mailbox_wait` blocks (long-poll) until a message
arrives — call it as the last action of a turn and the next message wakes your
agent immediately:

```json
{ "tool": "mailbox_wait", "arguments": { "timeout_seconds": 25 } }
```

For humans and dashboards, a companion watcher prints every new message as a
JSON line and can fire macOS notifications for chosen agents:

```bash
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox-watch --notify boss      # macOS notification center
agent-mailbox-watch --once                     # single scan (cron-friendly)
```

## Design

- **Local-first** — plain JSON files under `~/.agent-mail/`. No SMTP, no IMAP, no domain, no cloud relay, no network by default.
- **Register-once addressing** — `mailbox_register("WB")` is all it takes; every registered agent is immediately addressable by everyone.
- **Zero external dependencies** — only `mcp`. The store is one Python file with `flock`-guarded atomic writes; multiple MCP host processes share one mail root safely.
- **Human-readable** — every message is a small JSON file you can `cat`. The boss can read the inbox directly.
- **Honors existing identities** — set `AGENT_MAIL_ID` in each agent's environment and its tools become self-addressed.

### When you outgrow it

Local mailboxes solve same-machine and trusted-LAN coordination. The message lifecycle (`pending → acked → done`) is designed to carry over unchanged when an agent's threads need to reach other machines and organizations over real email infrastructure.

## Security notes

- Mail root lives in your home directory; messages never leave the machine unless you opt into HTTP transport on a trusted network.
- Agent ids are strictly validated (`[A-Za-z0-9_-]`, ≤64 chars) — no path traversal.
- The store is append-oriented with atomic writes and file locks; a crashed writer cannot corrupt the registry.
- For tamper-evidence, signed receipts (ed25519) are on the roadmap.


## Roadmap

- **v0.1.0** (current) — same-machine agent mailboxes over stdio MCP. Zero infrastructure. Includes long-poll `mailbox_wait` and a companion watcher — no polling daemons needed.
- **v0.2.0** — federation: streamable HTTP transport for agents on other machines (Tailscale/LAN friendly).
- **v0.3.0** — signed receipts (ed25519) for tamper-evident delivery.
- **v1.0.0** — cross-organization bridge: local threads reach agents on other machines and organizations over standard email infrastructure, with the same mailbox lifecycle.

Sister project: [dsh-devices](https://github.com/polaris-smart/dsh-devices) manages your devices; agent-mailbox manages the conversation between the agents on them.

## Development

```bash
git clone https://github.com/polaris-smart/agent-mailbox && cd agent-mailbox
pip install -e ".[dev]"
pytest
```

## License

MIT — see [LICENSE](LICENSE).
