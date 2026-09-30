# v0.8.0a1 verification — 2026-09-30

- Baseline at a7e1094: **557 passed, 1 skipped**.
- Integrated suite: **660 passed, 1 skipped**, 143.12 s. The 22 existing warnings concern deprecated MCP sampling. Ruff and Git whitespace checks passed.
- The first integrated rerun exposed an existing wire-test race: sampling result log and per-letter handled log are distinct files. The test now waits for both bounded durable receipts. Sampling implementation was not changed; its entire suite passed (31 passed, 1 skipped).
- ACP bridge: 29 process-level checks with an actual NDJSON child and a fake ACP peer. This is protocol evidence, not provider authentication evidence.
- Fleet: 24 actual loopback HTTPS protocol checks; remote execution: 8 integration/recovery checks with fake owned bridge children. Owner HTTP, device HTTPS, frozen/shared MCP and human acceptance are exercised. No physical LAN is claimed.
- Browser: 25 assertions against actual HTTP/SQLite/TLS, with clearly labelled discovery/runtime/execution fixtures. Console errors empty. Sources: ui-basic.json, ui-fleet.json, ui-final.json.
- Final packaged artifact: bundled runtime ready, HTML/JS/CSS served, 5 frozen local tools, 5 frozen remote tools, two app instances paired over TLS, authenticated push updates, graceful SIGTERM cleanup, persistent project/memory/TLS endpoint+fingerprint after restart. **No model invocation in packaging checks**. See packaged-check.json.

## Actual model evidence

Three narrowly scoped native Codex probes used separate temporary projects and mailbox roots, without editing global config or source files:

1. Existing CLI setting gpt-6.1-sol was rejected with provider invalid_request_error. Adapter initially reported completed text containing the provider JSON error; fixed to return failed and covered by bridge regression tests.
2. Explicit gpt-6-luna / low was applied and recorded in the session event. Model requested injected MCP context but reported tool runner failure; no successful native tool call was evidenced. This remains a release gate. The adapter's /mcp local diagnostic was also checked without a model call and did not establish target tool readiness.
3. Explicit gpt-6-luna / low, native read-only mode, automatically supplied scoped project briefing: actual result **MAILBOX_CONTEXT_OK**, task **review**. No automatic human acceptance, no file edits. This proves actual employee activation and supplied project context, not successful native MCP tool execution.

Raw model session databases contain local membership credentials and are intentionally excluded from Git/evidence archives. Only these sanitized findings are recorded.

## Remaining gates

Native MCP interoperability; actual second physical device; remote approval round trip; graceful startup/force-quit behavior under real side effects; distributable signatures/notarization and third-party notices; real update/rollback release channel; more agent adapters. No tag, push, Release, production daemon replacement or false v0.8.0 completion claim.

## Cognition

Closing aoci_maintain(scope=all) returned index_invalid/code_object_path_unresolved, matching initial reads. CodeGraph was initialized and refreshed. Formal PRD/HANDOVER were not rewritten with unmerged implementation facts.
