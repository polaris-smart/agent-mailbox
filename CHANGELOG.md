# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.7.2] — 2026-09-26

Close out every open 0.7 finding (HS review P2 SKILL + P4 items) before any version bump.

### Added
- **Per-agent sampling kill switch (SEP-2577 hardening)** — MCP deprecated the sampling capability on 2026-07-28; a per-agent `"sampling": {"enabled": false}` section in `wake.json` now disables the path outright (no `createMessage` attempts, letters land via the fallback chain). Malformed values fail loudly into a `sampling.log` error audit — the letter still lands, the misconfiguration is visible. Sampling remains an accelerator, never a delivery guarantee.

### Fixed
- **`__version__` tracks the release** — `src/agent_mailbox/__init__.py` had stayed at 0.6.2 through the 0.7.0/0.7.1 releases; both version locations now match (release-checklist §1 "two places, no exceptions").
- **Tool-count consistency** — the six READMEs claimed "17 MCP tools" (counting four built-in MCP primitives `get_prompt`/`list_prompts`/`list_resources`/`read_resource` that `list_tools` never returns); the verified count is 13 registered tools, now stated consistently across README ×6, roadmap, and SKILL.md.
- **SKILL.md brought current** — in-repo `skills/agent-mailbox/SKILL.md` now documents v0.7: sampling wake, the wake-policy schema (`identity` / `forbidden` / `max_concurrent` / `sampling.enabled`), the local-command adapter, `wake run --adapter`, and the wake delivery chain (sampling → wake-daemon / webhook / next check).

## [0.7.1] — 2026-09-26

### Fixed
- **sdist hygiene** — AOCI cognition assets (`.aoci/`, `aoci*.txt`) and local runtime config no longer ship in the sdist (hatchling exclude + `.gitignore`); in-repo `aoci.code.txt` entries normalized to repo-relative paths (was: absolute local paths).

## [0.7.0] — 2026-09-26

### Added
- **Sampling wake (MCP `createMessage` reverse call)** — letters sent through the MCP server now wake the recipient host over its existing stdio connection when the host declares `capabilities.sampling`; per-connection capability negotiation, wake-policy injection (identity / task / forbidden actions / require-receipt, force-injected into every sampling request and audited to `sampling.log`), 60s configurable timeout, per-`msg_id` dedup (in-process + `handled_log` persisted), fail-open to the mailbox when the host is offline or undeclared.
- **Per-agent execution lock** — one in-flight sampling per agent (`max_concurrent`, default 1), FIFO by arrival, released on success/failure/timeout, restart degrades the queued requests to the mailbox fallback (nothing stranded in memory).
- **`local-command` wake adapter** — for on-demand CLI agents (no resident process): wake triggers a configured argv command (e.g. `codex exec`), argv-array only (no shell), letter content passed via `AGENT_MAIL_*` env vars, 300s timeout with process-group kill.
- **`wake run --adapter` CLI override** — multi-agent installs share one `wake.json`; each plist picks its own wake path (`--agent codex --adapter local-command`).
- **AOCI cognition layer** — repository cognition index (baseline / drift / governed entries) established and maintained under AOCI governance.

### Fixed
- **`mailbox_list` inbox-only blindness** — handled letters live in `archive/`; the unified `list_all_messages` view now spans both (HS: five status queries returned 0 against 877 on-disk letters).
- **Cross-agent reply thread inheritance** — `reply_to` resolution fell back to a whole-root scan; previously every cross-agent reply minted a fresh thread (v0.6 F2 regression).
- **Wake drain legacy-letter poisoning** — a stale-format letter (no `id`) raised on the bare `m["id"]` and the fail-open handler skipped every letter after it; the filename stem is now back-filled on read.
- **`ServerSession` anchoring** — the SDK rebuilds the session per request; the sampling registry now anchors the stable `Connection` object so identity binding survives across requests.

## [0.6.2] — 2026-09-23

Security hardening + closing three long-open v0.5.x items. Default behavior is unchanged: every new capability is opt-in or additive until configured.

### Security

- **SECURITY.md**: vulnerability reporting channel (GitHub Security Advisories), supported versions, and the security model stated in the open — local trust by default, `identity_binding` as the opt-in hardening mode.
- **Identity binding (opt-in)**: `identity_binding` in `<mail-root>/config.json` (`{"enabled": true, "<agent_id>": "<sha256(token) hex>"}`) pins agent ids to tokens. Semantics pinned down: **not enabled → allow (local trust remains the default); token mismatch → reject with `identity mismatch`; malformed config → fail loudly at startup**. Bound callers present `AGENT_MAIL_TOKEN` — sha256, compared in constant time (`hmac.compare_digest`, per the web.py precedent). Unbound agents and the disabled default keep today's local-trust behavior; a malformed block fails loudly at server startup, never silently degrading to "disabled".

### Added

- **`unread_count` in webhook payloads**: the recipient's pending-letter count at notification time, top-level on the POSTed event (pure addition — callers that pass no count, like the wake adapters, emit payloads without the field).
- **Claim semantics for `mailbox_wait`** (P1–P4 from the 2026-09-13 forensics): a new locked single-pass `MailStore.claim` returns pending letters already flipped to `acked`, stamped `claimed_by` with a `claimed` handled_log entry, so two waiters on one mailbox can never consume the same batch (the old list-then-check gap let them race and wake empty). Fail-open unchanged: a claimed-but-unhandled letter stays ordinary acked mail for the stale-acked reap round, which now also drops the stale `claimed_by`.

## [0.6.1] — 2026-09-23

Docs-only hotfix: v0.6.0 updated only the English README; every other doc surface was still up to three versions behind.

- **Added** — this `CHANGELOG.md` (Keep a Changelog format, back-filled 0.3.1 → 0.6.0).
- **Fixed** — `README.zh-CN.md` header anchor raised v0.3.0 → v0.6.0, with new Wake daemon and Threads sections, the 13-tool table, and a synced roadmap.
- **Fixed** — `README.es.md` / `README.pt-BR.md` / `README.fr.md` / `README.ru.md` header anchors raised to v0.6.0 (one-line summary + link to the English README for details).
- **Added** — `docs/release-checklist.md`: a per-file version-bump checklist (pyproject / six READMEs / CHANGELOG / SKILL.md) so multilingual docs stop lagging releases.

## [0.6.0] — 2026-09-23

### Added

- **Wake daemon (信必达)**: `agent-mailbox wake install / uninstall / status / run` installs an OS file-watcher (launchd `WatchPaths` on macOS / systemd `PathChanged=` path units on Linux) over `~/.agent-mail/inbox/<agent>/`; every change fires one drain round (`python -m agent_mailbox.wake run --once`). Adapters: `hermes` (POST the gateway webhook; signature style auto-adapts github/generic/slack), `generic-webhook` (your URL + secret), `claude-code` (terminal bell + desktop toast; unknown values fall back to hermes — waking beats silence).
- **Wake reliability trio** (productized from the 2026-09-22 incident review): a failed POST retries 5× at 60s inside one round, then leaves the letter unmarked so the next file-watcher trigger re-drains it — the wake side never drops mail; a `wake` entry in the letter's `handled_log` makes every letter wake **at most once**; count semantics v2 wakes on all `pending` plus `acked` letters older than 600s while freshly-acked mail stays quiet.
- **Fail-open iron law**: the daemon is a separate process that only reads letter files and appends through the store's locked APIs — if wake dies, mail still lands and the next `mailbox_check` still delivers.
- **First-class threads**: every letter carries a `thread_id` (minted on send, inherited on reply); new `mailbox_thread` MCP tool replays a whole conversation oldest-first across every agent (inbox + archive); `mailbox_list` accepts a `thread` filter; legacy letters are grouped by normalized subject with stacked `Re:`/`Fwd:` stripped (a 12-deep chain resolves to one thread, unmatchable singles stay `null`); `mailbox_check` returns a `ghost_warning` past 5 open letters in one thread.
- **Jev routing (optional, default OFF)**: an async scoring router gates what is worth waking the agent for (Noul gate; urgency 0–10; sub-threshold scores batch into a daily digest) without blocking the wake path. Any Jev timeout/error falls back to wake-on-any-mail. Decisions are logged with scores to `<mail-root>/wake-jev.log`; the `api_key` never appears in logs. Pure stdlib behind the `jev` optional extra (reserved for a native client later).
- Server: `agent-mailbox wake` subcommand dispatch (`--home` passthrough), `mailbox_thread` tool (12 → 13 tools), `mailbox_list` `thread` parameter, `mailbox_check` ghost reminder. PyPI package renamed `agent-mailbox-mcp` → `agent-mailbox` with a Trusted Publisher workflow.

## [0.5.0] — 2026-09-22

### Added

- **Duplicate suppression (delivery-side)**: every letter stores a `semantic_hash` — sha256 over normalized subject + prose + code-fence regions hashed raw in original order. `mailbox_send` / `mailbox_broadcast` dedupe by default: a same-hash non-terminal (`pending`/`acked`) letter in the target inbox within the 24h window blocks the send with zero side effects, and the caller sees `{"deduped": true, "existing_id"}`; `count` only counts letters that actually landed; `dedupe: false` exempts; replies are exempt by design; scope is inbox-only, never archives; legacy letters without the field never match.
- **Two-phase `handled_log`**: `MailStore.record_handled(agent_id, msg_id, action)` records `intent` / `outcome`; the store is the single writer (mail-root flock), keeping `handled_log` append-only and auditable across sessions.
- **Compensation table**: `MailStore.resume_plan(agent_id, msg_id)` reads only the log and returns `process` / `replay` / `finalize` / `skip` — a half-done claim is recoverable without guessing.
- **Stale-`acked` reclaim**: `python -m agent_mailbox.reap --agent ID --ttl 7200` flips `acked` mail older than the TTL back to `pending` with a `reclaimed` entry (acked → reclaimed → done stays auditable end to end); letters whose newest `intent` is fresher than 30 minutes are deferred; manual maintenance only.
- **Wake circuit breaker**: N consecutive no-progress drain rounds latch a breaker file (auto-expires after 6h) and stop launching turns — backoff stretches the interval, the breaker stops the bleeding.

### Changed

- Wake wiring (`scripts/wake-zc.sh`) reaps **before** counting pending on every loop iteration (flock-idempotent, fail-open with an explicit log line).

### Hard rules

- 铁1: `reap_ttl` (default 3600s) must stay strictly below `dedup_ttl` (default 86400s); violating configs fail loudly (`MailboxError`) at load time.
- 铁2: deduped perception contract — `deduped` / `existing_id` in the per-recipient result, `count` counts only real landings, deduped sends append nothing anywhere.
- 铁3: periodic letters whose bodies embed timestamps naturally hash differently — dedup does **not** stop them (by design; replay protection relies on the B compensation flow plus `dedupe: false`).

## [0.4.0] — 2026-09-13

### Added

- Configurable webhook signature style via `AGENT_MAIL_SIGNATURE_STYLE`: `github` (default, `X-Hub-Signature-256: sha256=<hex>`) / `generic` (`X-Webhook-Signature: <hex>`, bare hex) / `slack` (`X-Slack-Signature: v0=<hex>`; emits no `ts` field, so receivers must not verify against the full Slack base string).
- Self-echo protection: notifications whose sender equals the notify target are dropped by default (the letter still lands); the drop is audited as `echo_suppressed: true` in `sent.log`; `notify_self_echo` in `<mail-root>/config.json` or env `AGENT_MAIL_NOTIFY_SELF_ECHO` restores delivery with an `[echo] ` subject prefix on the notification (the stored letter keeps its original subject).
- `python -m agent_mailbox.cleanup --dry-run` maintenance command: scans the mail root for suspected test residue (unregistered `inbox/`/`archive/` directories, `NEWBIE`/`WBTEST`-style test-named directories, orphan letters, `*.tmp` leftovers), lists path + size + reason without deleting; `--yes` deletes for real but requires typing `yes` to confirm; registered-but-test-named directories are review-only.

## [0.3.1] — 2026-09-13

### Fixed

- Reply subjects no longer pile up `Re: Re:` — first replies, re-replies, and mixed-case prefixes all normalize to a single `Re:` (reply path only).
- Web board tokens use constant-time comparison (`hmac.compare_digest`, both sites) and persist across reboots (`~/.agent-mail/web_token`, mode 0600; `AGENT_MAIL_WEB_TOKEN` env always wins).
- `sent.log` auto-rotates one generation past 10 MB (`os.replace` → `sent.log.1`).

[Unreleased]: https://github.com/polaris-smart/agent-mailbox/compare/v0.7.2...HEAD
[0.7.2]: https://github.com/polaris-smart/agent-mailbox/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/polaris-smart/agent-mailbox/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/polaris-smart/agent-mailbox/compare/v0.6.2...v0.7.0
[0.6.2]: https://github.com/polaris-smart/agent-mailbox/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/polaris-smart/agent-mailbox/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/polaris-smart/agent-mailbox/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/polaris-smart/agent-mailbox/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/polaris-smart/agent-mailbox/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/polaris-smart/agent-mailbox/compare/v0.3.0...v0.3.1
