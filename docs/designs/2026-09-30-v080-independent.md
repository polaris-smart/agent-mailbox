# Independent v0.8 foundation

Human instruction, 2026-09-30: v0.8 starts completely independent. This supersedes the earlier alpha design's legacy-compatibility/runtime decision. Keep the product name and GitHub version line; old Git history and the separate v0.7 worktrees remain historical evidence, not an executable dependency.

## Scope approved in this conversation

Default: one person managing supported employees and project work on one computer. Optional: paired LAN devices and Ubuntu execution nodes on external servers, including Hong Kong and Silicon Valley. Management itself needs no LLM. Supported agent accounts and authentication remain native to each device.

## Independent application boundary

The v0.8 distribution contains the workbench domain, execution bridge, scoped employee tools and optional device transport. It does not ship the old mail store, MCP mail server, watch/belt/launchd installation paths, legacy web board, legacy CLI, or their test fixtures. Default CLI and `python -m agent_mailbox` open the workbench. Frozen desktop entry and explicitly scoped MCP processes use the same product modules. Data remain in the v0.8 application home; no automatic old-mailbox import or modification.

Preserve the tested v0.8 task state machine, employee/project credentials, single-owner lock and private SQLite store. Use one dependency lock and an explicit runtime preparation step. Do not add distributed databases, mandatory vector memory, a management LLM, a cloud relay, or a second task scheduler.

## Optional nodes

A device is an execution node, not a second human control plane. A headless node must pair explicitly, map each authorized project to its own existing directory, register supported employees and recover uncertain execution without replaying it. Provider credentials never travel through pairing.

LAN and private overlay networks share the existing pinned HTTPS protocol. Public-server use should initially run over a user-established private network or SSH tunnel, retaining the same certificate pin and project/device authorization. The browser management listener stays on loopback. Do not expose the human owner API on public IPs. No actual Hong Kong/Silicon Valley server deployment is authorized by this source implementation request.

The computer holding the project control plane must be awake and reachable. Always-on Ubuntu control-plane hosting is an optional topology, not silently promised by adding a remote worker. SSH/overlay connection and filesystem synchronization remain distinct concerns.

## Review and verification

Audit old and alpha code before deciding what to keep. Remove superseded runtime paths rather than add compatibility switches. Fix concrete source-bound defects with targeted regression checks; avoid extracting helpers solely to reduce line count. Each employee/server capability must have an actual consumer and failure state.

Verify an isolated independent wheel imports/runs without old modules; actual local HTTP, scoped stdio tools, fake owned execution and human acceptance; optional headless node over loopback pinned HTTPS; restored identity after restart; inactive/revoked scopes rejected; no accidental model requests. Existing alpha model evidence does not prove new physical-device or public-WAN operation. Platform CI must report executed/skipped checks honestly.

## Outside this foundation

Drag-and-drop workflow, isolated parallel edit workspaces, universal agent App control, broad independent-account configuration, automatic release/update rollback, physical Windows/Linux/LAN validation, audit-grade storage and AI ERP remain separate work. This document does not claim those features implemented.
