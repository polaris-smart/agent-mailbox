# agent-mailbox

[![polaris-smart/agent-mailbox MCP server](https://glama.ai/mcp/servers/polaris-smart/agent-mailbox/badges/score.svg)](https://glama.ai/mcp/servers/polaris-smart/agent-mailbox)
[![npm](https://img.shields.io/npm/v/agent-mailbox)](https://www.npmjs.com/package/agent-mailbox)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Mailbox + Wake system for agent teams — agents that wake up, route, and collaborate.** One stdio MCP server. One JSON file per message. Plus a built-in task board: cards wake their assignee when they move, and a zero-dependency web kanban for the human.

> **📊 Production-proven**: 1,676 messages across 5 agents (Claude Code, Hermes, Codex-based, webhook wake) in 18 days of daily multi-agent software development — ~93 messages/day, zero data loss.

Other docs: [中文](README.zh-CN.md) · [Español](README.es.md) · [Português](README.pt-BR.md) · [Français](README.fr.md) · [Русский](README.ru.md)

## Why not just use MCP / Slack / raw files?

| Approach | Cross-CLI | Async | Wake-up | Human board | Deps |
|---|---|---|---|---|---|
| **agent-mailbox** | ✅ any MCP host | ✅ inbox persists | ✅ webhook + task cards | ✅ built-in kanban | **0** |
| Raw MCP tools | ❌ per-CLI sessions | ❌ lost on restart | ❌ | ❌ | — |
| Slack/Discord bot | ✅ | ✅ | ✅ | ❌ | API tokens, rate limits, cloud dependency |
| Shared files + conventions | ✅ | ⚠️ ad-hoc | ❌ manual | ❌ | your own locking code |

The gap agent-mailbox fills: **agents on different CLIs, on the same machine, messaging each other asynchronously — with delivery guarantees and a human-visible board — without a single dependency.**

> 🆕 **v0.6.2 — Security hardening**: optional **identity binding** pins MCP callers to agent ids via `AGENT_MAIL_TOKEN` — semantics pinned down: **not enabled → allow (local trust remains the default); token mismatch → reject with `identity mismatch`; malformed config → fail loudly at startup**. Webhook payloads now carry the recipient's `unread_count`, and `mailbox_wait` claims mail atomically (`acked` + `claimed_by` in one locked pass) so two waiters never consume the same batch. Model & reporting: [SECURITY.md](SECURITY.md). Previous: [Wake daemon](#wake-daemon-信必达-mail-arrived--agent-woken)

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
  tasks.json                   the task board ({"next


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
- The signature style is configurable via `AGENT_MAIL_SIGNATURE_STYLE`: `github` (default, `X-Hub-Signature-256: sha256=<hex>`) / `generic` (`X-Webhook-Signature: <hex>`, bare hex) / `slack` (`X-Slack-Signature: v0=<hex>`; this webhook emits no `ts` field, so receivers must NOT verify against the full Slack `v0:ts:body` base string).
- The target is pinned: http/https only, loopback/private addresses by default, redirects refused, system proxy bypassed.
- Env vars `AGENT_MAIL_WEBHOOK_URL` / `AGENT_MAIL_WEBHOOK_SECRET` override the file. Unset → fully offline.
- The config file is resolved **per mail root** (the store's own `webhook.json`), so a `MailStore(root=…)` built on a scratch root can never wake the production gateway. `webhook.json` in the default home still covers normal use.
- Every delivered mail is also appended to `<mail-root>/sent.log` (one JSONL line: id/from/to/subject/created_at) under the same lock as the write — a webhook notification with no matching `sent.log` line never was a mail.

## Wake daemon (信必达): mail arrived → agent woken

The webhook above needs a gateway that is already listening. The wake daemon closes the other half: the **recipient side**, where an agent's harness must actually be pulled into a session when mail lands. One command installs it:

```bash
agent-mailbox wake install --agent ZC            # uses webhook.json for the POST target
agent-mailbox wake install --agent ALICE --adapter claude-code    # bell + desktop toast
agent-mailbox wake install --agent BOB --adapter generic-webhook --webhook-url https://… --webhook-secret …
agent-mailbox wake status / uninstall ZC
```

- **How it fires**: launchd `WatchPaths` (macOS) / systemd `PathChanged=` path units (Linux) watch `~/.agent-mail/inbox/<agent>/`; every change launches one drain round (`python -m agent_mailbox.wake run --once`).
- **Adapters**: `hermes` (POST the gateway webhook; signature style auto-adapts github/generic/slack), `generic-webhook` (your URL + secret), `claude-code` (terminal bell + desktop notification; richer hooks reserved). Unknown values fall back to hermes — waking beats silence.
- **Reliability trio** (2026-09-22 incident review, productized): a failed POST retries 5× at 60s inside one round, then leaves the letter unmarked so the next file-watcher trigger re-drains it — mail is never dropped by the wake side; a `wake` entry in the letter's `handled_log` makes every letter wake **at most once** (idempotent); count semantics v2 wakes on all `pending` plus `acked` letters older than 600s (a handler died mid-turn) while freshly-acked mail stays quiet.
- **Fail-open iron law**: the daemon is a separate process that only reads letter files and appends through the store's locked APIs. If wake dies, mail still lands and the next `mailbox_check` still delivers — nothing in the send path depends on it.
- **Jev router (optional, default OFF)**: set `"jev": {"enabled": true, "api_key": …, "endpoint": …}` in `<mail-root>/wake.json` (or `wake install --jev --jev-api-key …`). Mail is scored asynchronously (Noul: does anyone need to act now? Score: urgency 0–10, below the threshold gets batched for a daily digest) without blocking the wake path; any Jev timeout/error falls back to "wake on any mail" immediately. Decisions are logged with their scores to `<mail-root>/wake-jev.log`; the api_key never appears in logs. Pure stdlib — `pip install agent-mailbox[jev]` works today and reserves the extra for a native client later.

> ⚠️ macOS install writes `~/Library/LaunchAgents/com.polaris-smart.agent-mailbox-wake-<agent>.plist` and runs `launchctl load`; Linux writes user units under `~/.config/systemd/user` and enables the path unit. Use `--no-activate` to generate files without loading them.

### Threads (v0.6.0, first-class)

Every letter now carries a `thread_id`: fresh sends mint one, replies inherit the original's, and `mailbox_thread(thread)` replays the whole conversation oldest-first **across every agent** (inbox + archive). `mailbox_list` accepts a `thread` filter. Legacy letters are grouped heuristically by normalized subject (stacked `Re:`/`Fwd:` stripped) — a 12-deep "Re: Re: …" chain resolves to one thread; unmatchable singles stay `null` so gaps stay visible. When a thread accumulates more than 5 open (non-done) letters, `mailbox_check` returns a `ghost_warning` — the acked-sinking failure mode, surfaced before it eats a thread.

### Self-echo protection (on by default)

A notification whose sender equals its target (self-echo, from == to) is **not delivered** by default: the letter still lands on disk, `mailbox_list` / `mailbox_check` are unaffected — the sender just isn't woken by its own send. The drop is audited in `sent.log` as `echo_suppressed: true`, so "there was a notification but no mail"-style disputes stay one grep away. Set `notify_self_echo: true` in the mail root's `config.json` (or env `AGENT_MAIL_NOTIFY_SELF_ECHO`) to restore delivery — restored self-echo notifications carry an `[echo] ` subject prefix (the stored letter keeps its original subject, so the prefix is regex-strippable).

### Duplicate suppression (v0.5.0, delivery-side)

Every letter stores a `semantic_hash` — `sha256(norm(subject) + "\0" + norm(prose) + "\0" + code_regions.join("\0"))`. Prose is NFKC/NFC normalized with whitespace folded; fenced ` ``` ` code regions are lifted out first and hashed **raw, in their original order** (zero folding — reordering code must not collapse to one hash). Legacy letters without the field never match (old mail is never back-filled).

`mailbox_send` / `mailbox_broadcast` dedupe by default: if the target inbox already holds a same-hash letter in a non-terminal state (`pending`/`acked`) inside the dedup window, no new mail is created. **How callers notice**: that recipient's entry in the result carries `"deduped": true` and `"existing_id"` — nothing lands on disk, nothing is appended to `sent.log`, and no webhook fires (zero side effects). `"count"` counts only letters that actually landed. Pass `dedupe: false` to exempt a send; replies (`mailbox_reply`) are exempt by design. Only inboxes are consulted, never archives — a same-hash letter that is `done` or archived never blocks a re-send.

- **Scope boundary (by design)**: normalization keeps timestamps verbatim, so periodic jobs whose bodies embed dates naturally hash differently — A does **not** stop them. A prevents same-semantics repeat wakes; periodic-task replay protection relies on the B compensation flow (below) plus `dedupe: false`.
- **Expected, not a bug**: once the dedup window passes, a re-send goes through, so the queue can legitimately hold two same-hash non-terminal letters (the older one is still being handled).
- **铁1 config coupling**: the reclaim window must stay strictly below the dedup window — `reap_ttl` (default 3600s) **<** `dedup_ttl` (default 86400s = 24h), both settable in `<mail-root>/config.json`. Violating configs fail loudly (`MailboxError`) at load time instead of silently distorting the windows.

### Half-done handling: compensation + reclaim (v0.5.0)

An agent that claims mail (`check` → `acked`) and dies leaves it invisible to a pending-only drain. v0.5 closes the loop three ways:

- **Two-phase handled_log**: the handling layer records `intent` when it starts and `outcome` when finished via `MailStore.record_handled(agent_id, msg_id, action)` — the store is the single writer (mail-root flock), so `handled_log` stays append-only and auditable across sessions. `set_status(done)` comes last.
- **Compensation table**: `MailStore.resume_plan(agent_id, msg_id)` reads only the log ("which segment did it reach?") and returns `resume`: `process` (pending, or acked with no records at all — claimed then crashed before the intent — same treatment: handle normally from the intent), `replay` (intent without outcome: idempotently redo the handling body), `finalize` (intent + outcome recorded but not `done` yet: only the status flip remains), `skip` (terminal).
- **Stale-`acked` reclaim**: `python -m agent_mailbox.reap --agent ID --ttl 7200` flips `acked` mail older than the TTL back to `pending` with a `reclaimed` entry in `handled_log` (acked → reclaimed → done stays auditable end to end). Defaults: TTL 3600s in the store, 7200s in the wake script. A letter whose newest `intent` is fresher than 30 minutes is deferred — someone is actively on it. Reclaim is a manual maintenance operation: nothing in the library calls it automatically; the deployed wake script is the only wired caller.
- **Wake wiring**: `scripts/wake-zc.sh` reaps **before** counting pending on every loop iteration (fail-open with an explicit log line) and carries a **circuit breaker**: after N consecutive no-progress drain rounds it latches (`~/.agent-mail/wake-zc.breaker`, auto-expires after 6h) and stops launching drain turns — backoff stretches the interval, the breaker stops the bleeding.

## The tools

| Tool | Notes |
|------|-------|
| `mailbox_register(agent_id, owner?, description?)` | claim a mailbox; idempotent |
| `mailbox_send(to, subject, body, priority?)` | `to` = one id, a list, or `"all"`; `dedupe?` (default true) suppresses same-hash repeats (see above) |
| `mailbox_check(agent_id?, mark?)` | fetch pending (→ `acked`) |
| `mailbox_reply(msg_id, body)` | routes back to the original sender (dedupe-exempt) |
| `mailbox_list(agent_id?, status?, thread?)` | list messages, optional status / thread filters |
| `mailbox_thread(thread)` | replay a whole thread oldest-first across agents (thread_id or any msg id) |
| `mailbox_done(msg_id)` | mark handled |
| `mailbox_broadcast(subject, body)` | to every registered agent; `dedupe?` per recipient |
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

## Maintenance: cleaning up test residue (cleanup)

```bash
python -m agent_mailbox.cleanup --dry-run              # list only (the default behaviour)
python -m agent_mailbox.cleanup --dry-run --root ~/.agent-mail
python -m agent_mailbox.cleanup --yes                  # actually delete: explicit --yes + interactive confirmation
```

Scans the mail root and lists **suspected test residue**: agent `inbox/` / `archive/` directories missing from registry.json, `NEWBIE` / `WBTEST`-style test-named directories, and orphan letters (stray files directly under `inbox/` / `archive/`, unparseable JSON, `*.tmp` left by an interrupted atomic write). Each finding prints path + size + reason; `--dry-run` (and the flagless default) deletes nothing; `--yes` deletes for real but requires typing `yes` to confirm. Registered-but-test-named directories are reported as review-only and never deleted. The `--yes` capability shipped with v0.4.0 — actually running it against a production mail root remains an explicit operator (boss) approval, separate from the release.

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

⚠️ **Restart your agent session (or reconnect the MCP client) after upgrading** — MCP tool lists are enumerated at session start, so new tools (13 now, was 9) only appear after a restart. No config changes needed; `tasks.json` is created automatically on first use.

## Roadmap

- **v0.6.2** (current) — security hardening + v0.5.x closeouts: **SECURITY.md** (vulnerability reporting via GitHub Security Advisories, supported versions, the local-trust model stated in the open); **identity binding** (opt-in `identity_binding` in `config.json`: bound agent ids must present `AGENT_MAIL_TOKEN` — sha256 + constant-time `hmac.compare_digest` — or calls fail with `identity mismatch`; disabled by default, unbound agents unchanged, malformed config fails loudly at startup); **`unread_count` in webhook payloads** (recipient's pending tally at notify time, top-level, pure addition); **claim semantics for `mailbox_wait`** (atomic locked `claim()`: letters come back `acked` + `claimed_by`, a second waiter never re-consumes a batch, stale claims die with the acked→pending reap).
- **v0.7.0** — the wake-up upgrade: **sampling wake** (server-driven `createMessage` over the host's own MCP connection — wake-policy injection, per-agent execution lock, 60s timeout, per-msg dedup, fail-open to the mailbox), **local-command wake adapter** (argv-only, env-injected content, killpg timeout for on-demand CLI agents like codex), `wake run --adapter` override. 17 MCP tools.
- **v0.6.0** — the 爆款 batch: **wake daemon** (`agent-mailbox wake install` — launchd WatchPaths / systemd PathChanged fire a drain round; hermes / generic-webhook / claude-code adapters; failed POSTs retry 5×60s then requeue on the next trigger, `handled_log` wake entries make each letter wake at most once, count semantics v2 = all pending + acked>600s; fail-open iron law end to end); **first-class threads** (`thread_id` minted on send and inherited on reply, `mailbox_thread` cross-agent time-ordered replay, `--thread` list filter, legacy Re:-chain subject-key back-fill, ghost-thread warning past 5 open letters); **Jev routing bypass** (optional default-off scoring plugin: Noul gates the wake, sub-threshold scores batch into a daily digest, any failure fails open to wake-on-any-mail, decisions logged with scores, pure stdlib behind the `[jev]` extra). 17 MCP tools.
- **v0.5.0** — lifecycle hardening from the 2026-09-13 incidents (task `t-6`): **duplicate suppression** (delivery-side `semantic_hash`, same-hash non-terminal repeats within a 24h window return `{"deduped": true, "existing_id"}` with zero side effects; `dedupe: false` exempts; code-fence-raw hashing, inbox-only scope, hash→inbox index); **half-done compensation** (`record_handled` two-phase intent/outcome API as the single `handled_log` writer + `resume_plan` four-row table: process / replay / finalize / skip); **stale-`acked` reclaim** shipped earlier as `reap_stale_acked` / `python -m agent_mailbox.reap` now wired into the wake loop (reap first, count second, fail-open) with the 铁1 coupling enforced — `reap_ttl` (3600s) must stay strictly below `dedup_ttl` (24h), violations fail loudly; **wake circuit breaker** — N consecutive no-progress drain rounds latch a breaker file and stop launching turns (backoff stretches the interval, the breaker stops the bleeding).
- **v0.5.x (open)** — still tracked from the t-6 reviews: `status filtering` (ask for "pending or acked" views), `wake routing` (gateway subscription `to`-filter; lives outside this repo). Shipped in v0.6.2: ~~`identity binding`~~, ~~`unread_count` in webhook payloads~~, ~~claim semantics for `mailbox_wait`~~.
- **v0.4.0** — feature batch: configurable webhook signature style (`AGENT_MAIL_SIGNATURE_STYLE`: github default / generic / slack); self-echo protection (notifications where sender == target are dropped by default, audited as `echo_suppressed` in `sent.log`; `notify_self_echo` / `AGENT_MAIL_NOTIFY_SELF_ECHO` restores delivery with an `[echo] ` subject prefix on the notification while the letter keeps its subject); new `cleanup --dry-run` maintenance command (scans for test residue, lists without deleting, `--yes` deletes after confirmation).
- **v0.3.1** — patch batch: reply subjects no longer pile up `Re: Re:` (first reply, re-replies, and mixed-case prefixes all normalize to a single `Re:`); web board tokens use constant-time comparison (`hmac.compare_digest`) and persist across reboots (`~/.agent-mail/web_token`, mode 0600, `AGENT_MAIL_WEB_TOKEN` env always wins); `sent.log` auto-rotates one generation past 10 MB (to `sent.log.1`).
- **v0.3.0** — task board + web kanban: `task_create` / `task_move` / `task_list` with a strict todo→doing→review→done state machine; creating or moving a card auto-messages the assignee, so board motion wakes agents with zero polling. `--web 8643` serves a token-protected zero-dependency kanban UI where human drag-and-drop goes through the same wake-up path. Messages + tasks + wake-up + board, still zero dependencies.
- **v0.5.0+** — maybe: deeper kanban integrations (Kaneo as reference/competitor). Under discussion.
- **Next** — federation: streamable HTTP transport for agents on other machines (Tailscale/LAN friendly); signed receipts (ed25519) for tamper-evident delivery.
- **v1.0.0** — cross-organization bridge: local threads reach agents on other machines and organizations over standard email infrastructure, with the same mailbox lifecycle.

## License

MIT
