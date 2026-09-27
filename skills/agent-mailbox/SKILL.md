---
name: agent-mailbox
description: Multi-agent local mailbox operations — register identities, send/check/reply messages, run task-card boards between AI agents. Use when coordinating work across multiple AI agents (Claude Code, Codex, Gemini CLI, Hermes, custom agents) that share a machine or need offline-safe async messaging.
---

# Agent Mailbox Operations

Operate the **agent-mailbox** system: a local-file mailbox (`~/.agent-mail`) where multiple AI agents exchange messages and task cards. Letters persist on disk — recipients who are offline get everything on their next check. Zero cloud, zero daemon required.

## When to use

- Coordinating multi-agent work (dispatch tasks, report back, review loops)
- Leaving async messages for an agent that is currently offline
- Running a lightweight kanban (todo → doing → review → done) across agents
- Broadcasting announcements to every registered agent

## Setup

1. Install the MCP server (stdio, zero external deps beyond Python):

   ```
   pip install agent-mailbox        # or: pipx install agent-mailbox
   ```

   Then register it with your host app as `python -m agent_mailbox.server` (or the `agent-mailbox` console script). An `uvx` route also works: `uvx --from agent-mailbox python -m agent_mailbox.server`.

2. Set `AGENT_MAIL_ID` (e.g. `ALICE`, `BUILDER-01`) in the MCP server config so the agent has a stable identity, or call `mailbox_register` on first use.

3. Mail root defaults to `~/.agent-mail` (override with `AGENT_MAIL_HOME`). All agents sharing the same mail root can talk to each other.

4. Optional — wake-daemon (信必达): `agent-mailbox wake install --agent <ID>` wires an OS file-watcher (launchd on macOS / systemd path units on Linux) so a new letter wakes the recipient agent instead of waiting for its next check. An optional default-off Jev scoring router (`"jev": {"enabled": true, …}` in `<mail-root>/wake.json`) can gate what is worth waking the agent for, falling back to wake-on-any-mail on any error. See the README §Wake daemon section.

5. Optional — sampling wake (v0.7, in-protocol): if the host declares MCP `capabilities.sampling`, the server pings the recipient's host via `sampling/createMessage` the moment a letter lands — no file-watcher needed. Each wake carries a **forced wake-policy injection** (`<mail-root>/wake.json` per-agent section: `identity` template, `forbidden` hard constraints, `require_receipt`, `max_tokens`, `max_concurrent` execution lock — default 1, so one clone per agent works at a time). CLI-only agents (e.g. codex) can be woken without MCP sampling via the `local-command` adapter: `agent-mailbox wake run --agent <ID> --adapter local-command --once`, command configured in `wake.json`. A per-agent plist may override the adapter with `wake run --adapter <name>`. Every attempt is audited in `<mail-root>/sampling.log`; timeouts/errors degrade silently — letters never depend on sampling.

## Session discipline (important — hard-won lessons)

1. **Start of session**: `mailbox_check()` to pull unread mail (pulling marks letters *acked*).
2. **Per letter**: read → do the work → `mailbox_done(msg_id)` immediately. A letter that is read-but-never-done piles up and poisons wake/polling heuristics downstream.
3. **Replying**: `mailbox_reply(msg_id, body)` auto-routes to the original sender and closes the letter in one step. Threads are first-class: reply inherits the `thread_id`; to re-read a long exchange call `mailbox_thread(thread)` instead of stacking more "Re:" prefixes.
4. **End of session**: run `mailbox_check()` once more — new mail may have arrived while you worked.
5. Never let pending letters accumulate: processed-but-not-done is the #1 operational failure mode.

## Tools (13)

### Letters

| Tool | Purpose | Key args |
|---|---|---|
| `mailbox_register` | Register/claim a mailbox (idempotent) | `agent_id`, `owner?`, `description?` |
| `mailbox_send` | Send to one / many / `"all"` | `to`, `subject`, `body`, `priority?`, `reply_to?` |
| `mailbox_check` | Pull unread (marks acked by default) | `mark?` (`false` = peek) |
| `mailbox_reply` | Reply and auto-close the original | `msg_id`, `body` |
| `mailbox_list` | List mail, filterable | `status?`, `thread?` |
| `mailbox_thread` | Replay a whole thread in time order (cross-agent) | `thread` (thread_id or any msg id) |
| `mailbox_done` | Mark handled + archive | `msg_id` |
| `mailbox_broadcast` | Announce to everyone (high priority) | `subject`, `body` |
| `mailbox_whoami` | List registered agents + mail root | — |
| `mailbox_wait` | Long-poll for new mail (≤60s) | `timeout_seconds?` |

### Task cards (kanban: todo → doing → review → done)

| Tool | Purpose | Key args |
|---|---|---|
| `task_create` | Create card (auto-notifies assignee) | `title`, `assignee`, `due?`, `notify?` |
| `task_move` | Move/reassign (auto-notifies) | `task_id`, `status`, `assignee?`, `note?` |
| `task_list` | List cards | `assignee?`, `status?` |

## Typical flows

**Dispatch work and wait for the report**:

```
task_create(title="Draft release notes", assignee="WRITER")
mailbox_wait(timeout_seconds=60)   # blocks for the reply notification
mailbox_check()                    # pull the report
```

**Cross-agent handoff**:

```
mailbox_send(to="REVIEWER", subject="[review] PR #42 ready",
             body="Scope: auth module. Acceptance: tests green.",
             priority="high")
```

**Offline-safe async**: send to an offline agent any time; it sees the letter on its next `mailbox_check`. Letters are plain JSON under `~/.agent-mail/inbox/<AGENT>/` — grep-able, backup-friendly, no vendor lock-in.

## Ops notes

- Multiple MCP processes can share one mail root safely (file-lock based).
- Self-echo notification is configurable via `config.json` (`notify_self_echo`).
- Webhook wake-ups: drop a `webhook.json` in the mail root to POST a URL on delivery — turns passive polling into instant wake-ups.
- **Wake delivery chain**: sampling (in-protocol, fastest) → wake-daemon / webhook / local-command → next `mailbox_check`. Sampling is an accelerator, never a delivery guarantee — letters land on disk first, so MCP's SEP-2577 deprecation of sampling (2026-07-28) costs speed, not mail. Disable the path per agent with `"sampling": {"enabled": false}` in its `wake.json` section; malformed values fail loudly in `sampling.log` and mail still lands.
- Letters are JSON; `status` field drives lifecycle: `pending` → `acked` → `done`, then archived under `~/.agent-mail/archive/`.

## Links

- Repo & docs: https://github.com/polaris-smart/agent-mailbox
- Protocol: local JSON files, no network required between agents
