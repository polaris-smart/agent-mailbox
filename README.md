# agent-mailbox

[![polaris-smart/agent-mailbox MCP server](https://glama.ai/mcp/servers/polaris-smart/agent-mailbox/badges/score.svg)](https://glama.ai/mcp/servers/polaris-smart/agent-mailbox)

**Give every AI agent its own mailbox.** One stdio MCP server. Zero daemons. One JSON file per message. Plus a built-in task board: cards wake their assignee when they move, and a zero-dependency web kanban for the human.

Other docs: [中文](README.zh-CN.md) · [Español](README.es.md) · [Português](README.pt-BR.md) · [Français](README.fr.md) · [Русский](README.ru.md)

> 🆕 **v0.3.0 — Task board**: agents now share a task surface on the same mail root. 3 new MCP tools (12 total), a zero-dependency drag-and-drop board (`--web`), and every move messages the assignee. ⚠️ **Upgrade note:** restart your agent session to pick up the new tools. → [Task board](#task-board)

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
  tasks.json                   the task board ({"next_id", "tasks": {id: card}})
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

## Task board

Task cards live in `<mail-root>/tasks.json` (plain JSON, same file lock as the mail). The state machine is strict: `todo→doing→review→done`, non-adjacent moves rejected unless `force=True`; `done` is terminal. Creating or moving a card sends the assignee a normal mailbox message (`[task#t-12 → review] …`) — so board motion wakes the owning agent through the existing inbox, no polling, no webhooks. Self-assigned moves stay silent, and `notify=False` opts out.

**Web board (for the human).** `agent-mailbox --web 8643` serves a zero-dependency kanban (stdlib `http.server` + one embedded HTML page, no framework) on `127.0.0.1`. Four lanes mirror the state machine; drag a card between adjacent lanes to move it, or create cards from the form; one toggle switches light/dark themes. Auth is a bearer token — set `AGENT_MAIL_WEB_TOKEN` for a stable one, or a fresh token is printed at boot (open `http://127.0.0.1:8643/?token=…`). The board acts as agent `boss`: every card you create or drag still auto-messages the assignee, so even human drag-and-drop wakes the right agent. The page auto-refreshes every 5 seconds.

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
- The config file is resolved **per mail root** (the store's own `webhook.json`), so a `MailStore(root=…)` built on a scratch root can never wake the production gateway. `webhook.json` in the default home still covers normal use.
- Every delivered mail is also appended to `<mail-root>/sent.log` (one JSONL line: id/from/to/subject/created_at) under the same lock as the write — a webhook notification with no matching `sent.log` line never was a mail.

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
| `task_create(title, assignee, due?)` | create a task card (starts `todo`); assignee auto-messaged |
| `task_move(task_id, status, assignee?, note?, force?)` | move along `todo→doing→review→done` (skips need `force`); moving a card auto-messages its owner |
| `task_list(assignee?, status?)` | list task cards, optional filters |

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

## Upgrading

Upgrade with `uv tool upgrade agent-mailbox` (or re-pull however you installed it).

⚠️ **Restart your agent session (or reconnect the MCP client) after upgrading** — MCP tool lists are enumerated at session start, so new tools (12 now, was 9) only appear after a restart. No config changes needed; `tasks.json` is created automatically on first use.

## Roadmap

- **v0.3.1** (current) — patch batch: reply subjects no longer pile up `Re: Re:` (first reply, re-replies, and mixed-case prefixes all normalize to a single `Re:`); web board tokens use constant-time comparison (`hmac.compare_digest`) and persist across reboots (`~/.agent-mail/web_token`, mode 0600, `AGENT_MAIL_WEB_TOKEN` env always wins); `sent.log` auto-rotates one generation past 10 MB (to `sent.log.1`).
- **v0.3.0** — task board + web kanban: `task_create` / `task_move` / `task_list` with a strict todo→doing→review→done state machine; creating or moving a card auto-messages the assignee, so board motion wakes agents with zero polling. `--web 8643` serves a token-protected zero-dependency kanban UI where human drag-and-drop goes through the same wake-up path. Messages + tasks + wake-up + board, still zero dependencies.
- **v0.4.0** — maybe: deeper kanban integrations (Kaneo as reference/competitor). Under discussion.
- **Next** — federation: streamable HTTP transport for agents on other machines (Tailscale/LAN friendly); signed receipts (ed25519) for tamper-evident delivery.
- **v1.0.0** — cross-organization bridge: local threads reach agents on other machines and organizations over standard email infrastructure, with the same mailbox lifecycle.

## License

MIT
