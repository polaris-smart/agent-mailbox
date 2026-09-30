# Changelog

## 0.8.0b2 — local prerelease (2026-10-01)

- Explicit stable/Beta GitHub release checks with numeric version ordering; no automatic polling, downloads or replacement. Public releases may be older than this local Beta.
- Persistent update maintenance pause blocks local/remote new claims while existing tasks finish. Queued work survives; uncertain claims and unconfirmed receipts block update readiness.
- Private, verified SQLite and identity/configuration backup before manual upgrade. Errors keep claims paused; explicit resume remains available. No automatic rollback.
- Installation-specific guidance and authenticated node version/protocol reports; incompatible protocols block new claims while preserving terminal receipts. Legacy unreported nodes remain unknown.
- About/update and device UI supports Chinese/English, mobile, waiting/ready/error states and a persistent pause banner. Schema 8 preserves existing business data.

## 0.8.0b1 — local Beta 1 (not publicly released)

- Separate global employees, current-project navigation and management in the sidebar; add a real project work log and accessible mobile navigation.
- Add immutable resource revisions, explicit human approval and task-run manifests. Default employee reads use pinned versions; explicit remote text proposals cannot overwrite coordinator files.
- Enforce active task identity on remote reads as well as writes. Finished, cancelled or revoked sessions are rejected.
- Verify real Codex-to-Claude mailbox handoff and approved resource retrieval on macOS; verify Mac-to-Ubuntu Docker pinned HTTPS, version consistency, recovery and revocation with a deterministic worker.
- Preserve existing data with private schema migration backups. Optional CodeGraph retrieval and isolated self-contained HTML previews remain supported; Windows CodeGraph is explicitly unavailable in this beta.
- Public GitHub/PyPI release, Windows physical validation, server deployment and automatic updates are separate work.


## 0.8.0a3 — independent foundation (unpublished)

- Remove v0.7 mail runtime, background services, installation scripts and legacy tests from the v0.8 distribution; default CLI and Python module open the workbench.
- Add explicit runtime preparation and optional headless device join/map/employee/run commands; private/CGNAT binding and SSH dial override.
- Persist terminal device receipts and acknowledge exact same-run replay without re-execution or overwriting human review; recover a lost pairing response safely.
- Align runtime/model discovery with the dependency manifest and managed Codex pair; synchronize permission summaries with same-run tool events.
- Preserve task drafts during setup, support named local employees, and separate local runtime readiness from remote assignment.
- Replace active PRD, quick start, security and handover with the independent product baseline; use one shared cross-platform CI test definition.

Earlier alpha and v0.7 notes below are historical, not capabilities of this build.


All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.6] — 2026-09-29

权限基座 + 状态可信 + 投递修复版（A 组 11 条基座 + E/F/PR2/claim-first 五件；四平台同版本发布：PyPI + tag `v0.7.6` + GitHub Release + MCP Registry republish）。

### Added

- **权限基座（A 组，11 条）**: tool-guard 全量 `ToolError` 化（`MailboxError` 统一出口带 `MBE|code` 前缀，mcp SDK 客户端不再只见 "Error executing tool"）；`MAX_BODY_BYTES` 1 MiB 落盘前三段报错（`BODY_TOO_LARGE`，不留半成品/死箱痕迹）；未注册收件人默认硬拒（`unregistered_recipients: reject|warn` 一键降级，warn 模式不再污染 `inbox/<NOSUCH>/`）；agent slug 白名单（`..` 显式拒，任何路径拼接前先过注册表解析）；identity binding 常时校验（`AGENT_MAIL_TOKEN` sha256 恒时比对）；读侧同形防存在性 oracle；成员三源归一化单函数（registry 优先、投递用原始形式）。
- **版本检查 + 点击更新（E）**: `fetch_latest_version()` 打 PyPI 版本级端点取 `info.version`；CLI `agent-mailbox upgrade`（uv tool/pipx/pip 自动探测，`--check` 只查、默认 dry-run 显完整命令+确认、`--yes` 直执行）+ web `/api/version`、`/api/upgrade` 与看板一键入口。安全三硬约束：owner/本机限定（token 门 + 回环对端核验）、执行前显示将运行的完整命令、`<root>/audit.log` 审计留痕（dry_run/start/result 含退出码）。**禁 self-update**——唯一形态是 spawn 外部命令。非强制查询走 `<root>/version_check.json` 24h 限流；断网/坏 JSON 一律 fail-open 静默跳过，绝不影响信件功能。
- **状态可信（F）**: `mailbox_check` 的 `unread` 改为「真待办」口径（仓内 pending 计数，acked 永不计入——修掉"18 封全 acked 仍报 unread=18"），新增 `total`（pending+acked 积压）；web 侧栏「待处理 N」可点下钻（`?folder=pending` 白名单深链）。**禁自动 ack/自动已读**：`mailbox_check` 默认变纯读（`mark=False`），消费正路 = `claim()`（恰好一次）或显式 `mark=True`（旧路径保留）——⚠️ 行为变更：依赖 check 默认消费的外部调用方需改走 claim 或传 `mark=True`。
- **信件 links 字段（PR2）**: `send()` 可选 `links=[{title, uri, kind?, sha256?, note?}]`；校验态 `ok/stale/denied`+拒因在读路径现算（check/list/thread/archived 四处），老信零影响；`file://` 走 allowed-roots 安全模型（config 显式列、不默认放行 `$HOME`、fail-closed、realpath/symlink 归一后前缀匹配、禁 `..` 穿越禁通配）；`http(s)/git` v1 只做 scheme 策略校验零网络请求；sealed 信 links 同权剥除；未知 scheme 发信即结构化拒（A7 同族码）。格式与样例见 `docs/links.md`。
- **claim-first 投递修复**: belt（`scripts/wake-zc.sh`）与 wake daemon（`agent-mailbox wake run`）三路由改「先认领再投」——投递前 `reap_stale_acked`(600s)→`claim()`，只投认领所得的信，认领 0 封不拉会话；投递全失败 `release_claim` 放回 pending（信不丢，带 label 归属校验防误放他人认领）；第二路撞在途信落 `claim_denied` 审计（每信去重一次）；`handled_log.by` 带 `belt:<pid>`/`wake:<runid>` 路由会话标识。多窗重复回信（一信双 done、一 thread 四回信）就此根治；J1–J4 活体判据 17 测入 CI。webhook 消费端（本地网关）对齐原语已备（`wake claim/release` + docstring 对齐点），网关侧接线随部署同步。

## [0.7.5] — 2026-09-26

The trust-model release (PR A brand/CLI + PR B identity & permissions + PR C wizard/mailbox/visibility), merged with the 0.7.4 wake-hardening line (`v07-sampling-wake`) onto `release/v0.7.5`. **Ships together with [0.7.4] below** — 0.7.4 was built, gated and verified locally but never uploaded, so PyPI jumps 0.7.3 → 0.7.5 (legal skip; both entries kept with their own anchors). Tool count: 13 → 14 registered MCP tools (`mailbox_confirm_external` joins).

### Added

- **Identity & permission model (v0.7.5 §3.3)**: members now carry a `kind` (`owner` 人 / `agent` 本机 agent / `guest` 外部来源) on their registry card — legacy cards resolve to the historical behavior (`boss` → owner, everything else → agent). Tool-layer enforcement: `agent`/`guest` callers reading another member's mailbox get a structured `permission denied` (never a silent empty result); owners read all traffic; an unset `AGENT_MAIL_ID` keeps local trust so existing scripts are unchanged.
- **Sealed letters (密封信)**: `mailbox_send(..., sealed=True)` marks a letter whose body is readable **only** through the recipient agent's own tools. Every other reader — owner tools, other agents, the new human-view web endpoints — gets metadata (who/when/status/subject) with `redacted: "sealed"`, so "owner sees all" can never double as a credential leak channel.
- **External-origin gate (外部来源执行门)**: letters may carry `origin` (`local`/`external`). External mail lands normally but triggers nothing — the wake drain skips it (`skipped_external`) and the webhook POST is dropped — until an owner flips it back with the new `mailbox_confirm_external` tool (appends to `handled_log` and the audit log, greppable by name).
- **Human mail view**: `GET /api/messages` (list) and `GET /api/messages/<id>` (detail) on the web board expose every letter under the root to the token holder as the owner — sealed bodies metadata-only.
- **Audit trail**: visibility-relevant decisions append JSONL to `<root>/audit.log` (`member_kind` changes, `confirm_external`) with who/when/what.
- **Pairing-token validation point**: `guest` senders must present a `pairing_token` whose sha256 matches the `pairing_tokens` block of `config.json` (constant-time compare; no block configured → guests cannot send). Pairing/rotation flows remain 0.7.6 scope.
- **Attention tiers (打扰三档 §3.4)**: letters carry `attention` (`decision` 需你拍板 / `report` 报备 / `archive` 存档), default `decision`; reminder presentation lands with the mailbox UI.
- **3-step setup wizard (`/setup`, v0.7.5 §4.1)**: ① discover — the member table (member / kind / wake channels with auditable sources / live status) straight off the discover engine, with the §5⑩ support-list fallback and §5⑨ fingerprint-staleness notes; ② test — a real test letter per member with **result + next step always paired** (a broken member shows ❌ *and* the way out); ③ done — service status, connected count, pending fixes, and the model line ("不自动处理来信（可选）" — the model is an optional feature, never a required question).
- **Three-pane mailbox (`/mail`, §4.2)**: the human mailbox — folders (收件箱/星标/已发送/草稿/已归档), a read-only **monitor** section (agent↔agent traffic, 未送达/失败 via the cached discovery report, sealed letters as metadata), the roster with channel-status dots, and per-letter actions (回复 / 转发 / 派成任务卡 / 归档 / 标未读 / 确认执行外部信) with thread history and sender-channel warnings. Keyboard shortcuts `j/k e r t /`. The §3.6 empty-mailbox call-to-action ("给你的 agent 写第一封信") posts a real letter — no blank pages. 人 vs agent 的界线在界面上讲清：信是往来，任务卡是要人动手的活 (§5⑬).
- **Visibility page (`/visibility`, §4.3)**: the four §3.3 switches with their defaults rendered as-is (`owner_sees_all` on, `agent_cross_read` / `sealed_in_human_view` / `external_auto_execute` off), 发信权限 note, and the audit trail shown inline. Changes persist to `config.json` (`visibility` block), append a `visibility_change` audit line, and are wired for real: `agent_cross_read` opens the tool-layer cross-read, `external_auto_execute` opens the wake gate (fail-closed on any config error); `sealed_in_human_view` is hard-locked off.
- **Owner mailbox APIs**: `GET /api/mail` (decorated letters + roster + counts), `POST /api/mail/send|drafts|drafts/delete`, `POST /api/mail/<id>/read|unread|archive|star|unstar|task|confirm-external`, `POST /api/members`, plus the wizard endpoints `GET /api/status`, `POST /api/discover|test`, `GET /api/setup-summary` — the discover engine calls are injectable so tests stay hermetic. Star/read/draft state lives in `<root>/web_state.json` so the human never edits other members' letters.
- **Wake-attempts anchor + failure alerts (锚A + U2)**: every wake attempt — belt (`scripts/wake-zc.sh`) and daemon (`agent_mailbox.wake`) alike — appends one 8-field JSON row (`ts`/`agent`/`route`/`attempt`/`outcome`/`error_class`/`executor`/`latency_ms`) to `<root>/wake-attempts.jsonl` (0600, append-only, success and failure both written), so wake health is judged from data, not process logs. When every delivery attempt for a letter fails, a `[wake-fail]` alert letter goes to the registered sender plus the boss box (once per letter via a `handled_log` marker; belt alerts dedup through the 24h semantic window) — a broken wake is visible from the mailbox face alone, no shell needed.

## [0.7.4] — 2026-09-26

HS dispatch (0.7.4 repo-write window): close t-37 remnant + same-root-cause sweep + t-38, then gate. **Built and verified locally; upload held pending HS re-review + boss go.** 随 0.7.5 首发合入（merged into `release/v0.7.5` — this entry ships to PyPI as part of the 0.7.5 release, not as a standalone 0.7.4 upload）.

### Fixed
- **`wake install` no longer silently wipes unknown `wake.json` keys (t-37, P0)** — `WakeConfig` round-trips only the keys it knows; the sampling per-agent `agents` policy section (identity / forbidden / `max_concurrent` / `sampling.enabled`) and any future top-level key were being dropped on every `load→save` (install/uninstall paths). Unknown top-level keys are now preserved verbatim. Same root cause at the nested level: unknown keys inside the `webhook` / `jev` sections survive too.
- **`install()` double `cfg.save()`** collapsed to a single write (t-37 remnant).
- **Duplicate letters no longer re-wake (t-38②)** — a `dedupe=False` re-send that lands while a same-`semantic_hash` non-terminal letter is already in the recipient's inbox is delivered and audited, but marked `wake_suppressed_dup` and excluded from the wake face (webhook notification AND sampling). Behavior change: `dedupe=False` same-content re-sends each got their own sampling wake before; now only the first does. Fresh content (different hash) always wakes.
- **Zero-notification sends post no webhook** — when every landed letter is a duplicate, no empty webhook round-trip happens.
- **Sampling wake prompt carries the real pending count (t-38③)** — `unread` in the wake prompt is counted at wake time under the store, not the send-time snapshot (parallel windows under-reported: notification said 1, recipient found 2).

### Added
- **`scripts/verify_release.sh`** (t-35③) — post-release artifact sweep with HS-pinned criteria (sdist content: `/U[s]ers/|inte[r]ia|/h[o]me/` = 0 and machine-specific wrappers = 0; wheel: no `scripts/` at manifest or content level; self-test: the 0.7.2 artifacts must FAIL and 0.7.3 must PASS). §6 of the release checklist wires it in.

## [0.7.3] — 2026-09-26

### Fixed
- **sdist hygiene, round two** — the post-release artifact sweep (every release re-checks the published sdist from now on) caught `scripts/wake-zc.sh`, a machine-specific ops wrapper with hardcoded local paths, shipping in the 0.7.0–0.7.2 sdists (the 0.7.1 sweep only covered the AOCI assets it was written for). Now excluded via hatchling `exclude` (the wheel was never affected — it never carried `scripts/`); the repo copy stays, launchd wiring untouched.

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

[0.7.6]: https://github.com/polaris-smart/agent-mailbox/compare/v0.7.4...v0.7.6
[0.7.4]: https://github.com/polaris-smart/agent-mailbox/compare/v0.7.3...v0.7.4
[0.7.3]: https://github.com/polaris-smart/agent-mailbox/compare/v0.7.2...v0.7.3
[0.7.2]: https://github.com/polaris-smart/agent-mailbox/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/polaris-smart/agent-mailbox/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/polaris-smart/agent-mailbox/compare/v0.6.2...v0.7.0
[0.6.2]: https://github.com/polaris-smart/agent-mailbox/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/polaris-smart/agent-mailbox/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/polaris-smart/agent-mailbox/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/polaris-smart/agent-mailbox/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/polaris-smart/agent-mailbox/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/polaris-smart/agent-mailbox/compare/v0.3.0...v0.3.1
