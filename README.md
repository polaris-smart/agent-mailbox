# agent-mailbox

**Give every local AI agent its own mailbox.**

One MCP server. Register once, message any agent on this machine. No cron. No polling daemons. No shared markdown files. No cloud.

```bash
uvx agent-mailbox          # stdio transport, ready for any MCP host
uvx agent-mailbox --http 8642   # or expose it over HTTP for remote agents
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
      args: ["agent-mailbox-mcp"]
```

**Claude Code** (`~/.claude/settings.json`):

```json
{ "mcpServers": { "agent-mailbox": { "command": "uvx", "args": ["agent-mailbox-mcp"] } } }
```

**Any MCP client** (stdio):

```bash
uvx agent-mailbox-mcp
```

**Remote agents** (e.g. an agent on another server):

```bash
uvx agent-mailbox-mcp --http 8642   # on the mail host
```

```json
{ "mcpServers": { "agent-mailbox": { "url": "http://your-host:8642/mcp" } } }
```

## Design

- **Local-first** — plain JSON files under `~/.agent-mail/`. No SMTP, no IMAP, no domain, no cloud relay, no network by default.
- **Register-once addressing** — `mailbox_register("WB")` is all it takes; every registered agent is immediately addressable by everyone.
- **Zero external dependencies** — only `mcp`. The store is one Python file with `flock`-guarded atomic writes; multiple MCP host processes share one mail root safely.
- **Human-readable** — every message is a small JSON file you can `cat`. The boss can read the inbox directly.
- **Honors existing identities** — set `AGENT_MAIL_ID` in each agent's environment and its tools become self-addressed.

### When you outgrow it

Local mailboxes solve same-machine and trusted-LAN coordination. When you need cross-organization delivery over real email infrastructure, graduate your protocol semantics to [AAMP](https://github.com/larksuite/aamp) (Agent Asynchronous Messaging Protocol) — agent-mailbox's message lifecycle is designed to map onto it cleanly.

## Comparison with neighbors

| | agent-mailbox | [cc2cc](https://github.com/non4me/cc2cc) | [agent-talk](https://github.com/xhluca/agent-talk) | email-based kits |
|---|---|---|---|---|
| External service | **none** | Claude Code channels | retalk relay | SMTP / IMAP / cloud |
| Works offline | **yes** | yes | relay required | no |
| Persistent identity | **register once** | session-bound | invite codes | per-provider |
| Any MCP client | **yes** | Claude Code only | six CLIs, plugin-only | yes |
| Human-readable store | **plain JSON** | JSON | relay-side | mailbox export |

## Security notes

- Mail root lives in your home directory; messages never leave the machine unless you opt into HTTP transport on a trusted network.
- Agent ids are strictly validated (`[A-Za-z0-9_-]`, ≤64 chars) — no path traversal.
- The store is append-oriented with atomic writes and file locks; a crashed writer cannot corrupt the registry.
- For tamper-evidence, signed receipts (ed25519) are on the roadmap — see [agenttransfer](https://github.com/shehryarsaroya/agenttransfer) for the pattern.

## Development

```bash
git clone https://github.com/polaris-smart/agent-mailbox && cd agent-mailbox
pip install -e ".[dev]"
pytest
```

## License

MIT — see [LICENSE](LICENSE).
