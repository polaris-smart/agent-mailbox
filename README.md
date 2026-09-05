# agent-mailbox

**Give every AI agent its own mailbox.** One stdio MCP server. Zero daemons. One JSON file per message.

Other docs: [中文](README.zh-CN.md) · [Español](README.es.md) · [Português](README.pt-BR.md) · [Français](README.fr.md) · [Русский](README.ru.md)

---

## The problem

Run several AI agents on one machine — Claude Code, Hermes, your own scripts — and they have no way to leave each other messages. Agents overlap, wait on each other, or you end up copy-pasting between their windows like a human switchboard.

## The fix

A mailbox is a directory of plain JSON files:

```
~/.agent-mail/
  registry.json                agent_id → {owner, description, created_at}
  inbox/HS/20260905-….json     one file per message
  archive/HS/…
```

Agents read and write it through a small stdio MCP server. No broker process, no ports, no database, no network by default. Any number of MCP host processes share one mail root safely (file-lock guarded).

![agent-mailbox architecture](docs/architecture.png)

## Quick start

**Prerequisites** — one-time: install [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh` on macOS/Linux, or `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` on Windows). `uvx` runs everything else; nothing else to install.

### 1 · Register the server with your MCP host

Claude Code:

```bash
claude mcp add agent-mailbox -- uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox
```

Any MCP host (generic JSON):

```json
{
  "mcpServers": {
    "agent-mailbox": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/polaris-smart/agent-mailbox", "agent-mailbox"]
    }
  }
}
```

Tip: set `AGENT_MAIL_ID=HS` (or whichever id) in the agent's environment and every tool becomes self-addressed — no need to pass `agent_id` on each call.

### 2 · Agents register once

```json
{ "tool": "mailbox_register", "arguments": { "agent_id": "HS", "owner": "Hermes", "description": "PM & QA" } }
```

Registration is idempotent. Every registered agent is immediately addressable by everyone — including a human `boss` id you can read yourself.

### 3 · Send, check, reply

```json
{ "tool": "mailbox_send", "arguments": { "to": "HS", "subject": "deploy ready", "body": "v0.1.0 is staged, please verify." } }
{ "tool": "mailbox_check", "arguments": {} }
{ "tool": "mailbox_reply", "arguments": { "msg_id": "20260905-…-hs", "body": "verified, marked done." } }
```

`mailbox_check` fetches pending messages and marks them `acked`. Lifecycle: `pending → acked → done`, then optionally archived. A message is one JSON file you can `cat` — the boss reads the inbox directly.

### 4 · Wait instead of poll

`mailbox_wait` blocks (long-poll) until a message arrives — call it as the last action of a turn:

```json
{ "tool": "mailbox_wait", "arguments": { "timeout_seconds": 25 } }
```

## For AI agents (hand this section to your agent)

If you are an AI agent reading this because your user said "install agent-mailbox": do exactly this, no questions needed.

1. Check `uvx --version`; if missing, install uv with the Prerequisites command above for the OS you are running on.
2. Register the MCP server with your host app — use the generic JSON above (step 1) or your host's native mechanism.
3. Pick an identity: any id matching `[A-Za-z0-9_-]` up to 64 chars. Set `AGENT_MAIL_ID=<id>` in your environment, then call `mailbox_register` once (it is idempotent).
4. Verify: call `mailbox_whoami` — you should appear in the registry. You are now addressable by every other agent on this machine.
5. Report your id back to your user so they can send you mail at `~/.agent-mail/inbox/<id>/`.

Read English even if your user speaks another language — the commands are identical everywhere.

## Works with your agent CLI

Anything that speaks MCP stdio can host a mailbox — which is all of them. Register per your CLI's config:

| Agent CLI | How to register |
|-----------|-----------------|
| Claude Code | `claude mcp add agent-mailbox -- uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox` |
| Gemini CLI | `~/.gemini/settings.json` → `"mcpServers": { … }` (same JSON as Quick start) |
| Qwen Code | same as Gemini CLI (`~/.qwen/settings.json`) |
| Codex CLI | `~/.codex/config.toml` → `[mcp_servers.agent-mailbox]` with `command` / `args` |
| OpenCode | `opencode.json` → `"mcp": { "agent-mailbox": { "type": "local", "command": ["uvx", "--from", "git+https://github.com/polaris-smart/agent-mailbox", "agent-mailbox"] } }` |
| Hermes / Ark CLI / veCLI / OpenClaw / any MCP host | same generic JSON — point `command` at the `uvx` line above |

Then set `AGENT_MAIL_ID` for that CLI's sessions and `mailbox_register` once. Agents on the same machine can now message each other **across different CLIs** — a Claude Code agent and a Gemini CLI agent share the same mail root with zero extra setup.

## Waking a sleeping agent (one config line)

If the receiving agent isn't even running, `mailbox_send` itself can POST every new message to a webhook the moment it lands — no daemon, no polling, no extra process:

```json
// ~/.agent-mail/webhook.json   (chmod 600)
{ "url": "http://localhost:8644/webhooks/agent-mailbox", "secret": "…" }
```

Generate the secret yourself once: `openssl rand -hex 32`. Omit it for unsigned posts (fine for local testing; your receiver decides whether to require it).

Your host's webhook handler receives:

```json
{ "event": "agent_mailbox_new_message", "event_type": "agent_mailbox_new_message", "message": { "id": "…", "from": "ZC", "to": "HS", "subject": "…", "body": "…" } }
```

…wakes the agent, and the agent calls `mailbox_check` on arrival. That is the whole integration.

- Signed `X-Hub-Signature-256: sha256=<hmac>` (GitHub scheme — accepted by Hermes gateway and most webhook consumers).
- The target is pinned: http/https only, loopback/private addresses by default, redirects refused, system proxy bypassed.
- Env vars `AGENT_MAIL_WEBHOOK_URL` / `AGENT_MAIL_WEBHOOK_SECRET` override the file. Unset → fully offline.

## The tools

| Tool | Notes |
|------|-------|
| `mailbox_register(agent_id, owner?, description?)` | claim a mailbox; idempotent |
| `mailbox_send(to, subject, body, priority?)` | `to` = one id, a list, or `"all"` |
| `mailbox_check(agent_id?, mark?)` | fetch pending (→ `acked`) |
| `mailbox_reply(msg_id, body)` | routes back to the original sender |
| `mailbox_list(agent_id?, status?)` | list messages, optional status filter |
| `mailbox_done(msg_id)` | mark handled |
| `mailbox_broadcast(subject, body)` | to every registered agent |
| `mailbox_whoami()` | directory of agents + mail root |
| `mailbox_wait(agent_id?, timeout_seconds?)` | long-poll for new mail |

Identity: pass `agent_id` explicitly, or set `AGENT_MAIL_ID` once per agent.

## Optional: desktop notifications for humans

A companion watcher prints every new message as a JSON line and fires desktop notifications (macOS / Linux / Windows). It is never on the agent wake-up path — agents don't need it:

```bash
uvx --from git+https://github.com/polaris-smart/agent-mailbox agent-mailbox-watch --notify boss
```

Run it as a service on your platform:

| Platform | Install | Verify |
|----------|---------|--------|
| macOS (launchd) | `scripts/install-watch-macos.sh --notify boss` | `tail -f ~/.agent-mail/watch.log` |
| Linux (systemd user) | `scripts/install-watch-linux.sh …` | `journalctl --user -u agent-mailbox-watch -f` |
| Windows (schtasks) | `scripts\install-watch-windows.ps1` | `schtasks /Query /TN AgentMailboxWatch /V` |

## Design

- **Local-first** — plain JSON files under `~/.agent-mail/`. No SMTP, no IMAP, no domain, no cloud relay, no network by default.
- **Register-once addressing** — `mailbox_register("HS")` is all it takes; every registered agent is immediately addressable by everyone.
- **Zero external dependencies** — only `mcp`. The store is one Python file with `flock`-guarded atomic writes; multiple MCP host processes share one mail root safely.
- **Human-readable** — every message is a small JSON file you can `cat`. The boss reads the inbox directly.
- **Honors existing identities** — set `AGENT_MAIL_ID` in each agent's environment and its tools become self-addressed.

## Security notes

- Mail root lives in your home directory; messages never leave the machine unless you opt into the webhook, which is pinned to loopback/private targets by default.
- Agent ids are strictly validated (`[A-Za-z0-9_-]`, ≤64 chars) — no path traversal.
- The store is append-oriented with atomic writes and file locks; a crashed writer cannot corrupt the registry.
- Webhook payloads are HMAC-signed; verifiers should compare with a constant-time function.
- For tamper-evidence, signed receipts (ed25519) are on the roadmap.

## Development

```bash
git clone https://github.com/polaris-smart/agent-mailbox && cd agent-mailbox
uv venv && uv pip install -e ".[dev]"
pytest
```

## Roadmap

- **v0.1.0** (current) — same-machine agent mailboxes over stdio MCP. Zero infrastructure. Long-poll `mailbox_wait`, built-in webhook wake-up on `mailbox_send`, optional watcher.
- **v0.2.0** — federation: streamable HTTP transport for agents on other machines (Tailscale/LAN friendly).
- **v0.3.0** — signed receipts (ed25519) for tamper-evident delivery.
- **v1.0.0** — cross-organization bridge: local threads reach agents on other machines and organizations over standard email infrastructure, with the same mailbox lifecycle.

## License

MIT
