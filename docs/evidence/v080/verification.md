# v0.8.0 verification history — 2026-09-30

- Baseline at a7e1094: **557 passed, 1 skipped**.
- Integrated suite: **660 passed, 1 skipped**, 143.12 s. The 22 existing warnings concern deprecated MCP sampling. Ruff and Git whitespace checks passed.
- The first integrated rerun exposed an existing wire-test race: sampling result log and per-letter handled log are distinct files. The test now waits for both bounded durable receipts. Sampling implementation was not changed; its entire suite passed (31 passed, 1 skipped).
- ACP bridge: 29 process-level checks with an actual NDJSON child and a fake ACP peer. This is protocol evidence, not provider authentication evidence.
- Fleet: 24 actual loopback HTTPS protocol checks; remote execution: 8 integration/recovery checks with fake owned bridge children. Owner HTTP, device HTTPS, frozen/shared MCP and human acceptance are exercised. No physical LAN is claimed.
- Browser: 25 assertions against actual HTTP/SQLite/TLS, with clearly labelled discovery/runtime/execution fixtures. Console errors empty. Sources: ui-basic.json, ui-fleet.json, ui-final.json.
- Final packaged artifact: bundled runtime ready, HTML/JS/CSS served, 5 frozen local tools, 5 frozen remote tools, two app instances paired over TLS, authenticated push updates, graceful SIGTERM cleanup, persistent project/memory/TLS endpoint+fingerprint after restart. **No model invocation in packaging checks**. See packaged-check.json.

## Actual model evidence

Three narrowly scoped native Codex probes used separate temporary projects and mailbox roots, without editing global config or source files:

1. Existing CLI model setting was rejected with provider invalid_request_error. Adapter initially reported completed text containing the provider JSON error; fixed to return failed and covered by bridge regression tests.
2. Explicit an explicitly selected compatible model was applied and recorded in the session event. Model requested injected MCP context but reported tool runner failure; no successful native tool call was evidenced. This was an alpha1 release gate; the alpha2 section below records the successful fix. The adapter's /mcp local diagnostic was also checked without a model call and did not establish target tool readiness.
3. Explicit an explicitly selected compatible model, native read-only mode, automatically supplied scoped project briefing: actual result **MAILBOX_CONTEXT_OK**, task **review**. No automatic human acceptance, no file edits. This proves actual employee activation and supplied project context, not successful native MCP tool execution.

Raw model session databases contain local membership credentials and are intentionally excluded from Git/evidence archives. Only these sanitized findings are recorded.

## Alpha1 remaining gates (historical)

Native MCP interoperability; actual second physical device; remote approval round trip; graceful startup/force-quit behavior under real side effects; distributable signatures/notarization and third-party notices; real update/rollback release channel; more agent adapters. No tag, push, Release, production daemon replacement or false v0.8.0 completion claim.

## Cognition

Closing aoci_maintain(scope=all) returned index_invalid/code_object_path_unresolved, matching initial reads. CodeGraph was initialized and refreshed. Formal PRD/HANDOVER were not rewritten with unmerged implementation facts.

## Alpha2 verification — 2026-09-30

- Code artifact source: `74f23e8`, version `0.8.0a2`; subsequent evidence/document commits do not change packaged code.
- Full suite with `AGENT_MAILBOX_TEST_RUNTIME_DIR=/tmp/v080-full-runtime`: **693 passed, 2 skipped**, 695 collected, exit 0. The skips are the existing sampling case and a negative ACPX-only test incompatible with the full runtime fixture. An earlier run without that environment had 658 passed / 37 skipped; it is not the final result. Existing 22 MCP sampling deprecation warnings remain. Ruff and whitespace checks passed.
- Runtime bridge: 36 process checks; the complete runtime run executes 35 and skips the ACPX-only negative check. Separate specialist fixture executed all 36. Fleet + remote: **47 passed**, actual loopback HTTPS and owned fake execution children. Remote owner approval, expiry, timeout, cancellation and device revocation are exercised; no physical second device claimed.
- UI: **12 new browser groups** (7 lifecycle, 5 quit), real HTTP/SQLite and real owned application shutdown; execution/discovery fixtures labelled. No console errors; 390 px lifecycle layout has no horizontal overflow. Existing 25 alpha1 assertions remain separate evidence.
- Actual native model seam: managed matching Codex CLI/host 0.158.0, an explicitly selected compatible model, read-only. `project_context` and `project_note` both completed; the exact note persisted with employee source. Root independently read native tool-call statuses and the stored note. Two narrowly matched allow-once test decisions were made; this is not automatic approval in production. Sanitized evidence: native-mcp-alpha2.json. Raw session state and credentials excluded.
- Independently installed wheel: no editable/source import, correct version, HTTP assets, five actual stdio tools, context read, employee note attribution, pause/resume, retired identity losing MCP access, management ledger, owner quit and state preserved after restart. No model calls. wheel-alpha2-check.json.
- Final macOS App: numeric bundle version 0.8.0, explicit prerelease metadata 0.8.0a2, 74 native files, ad-hoc integrity verification passed. Zero Developer ID identities; no notarization or uploads. A printed release plan is not signing evidence.

Remaining gates: physical second-device validation, actual automatic update/rollback release channel, platform distribution/third-party review, wider native employee coverage and force-quit recovery under real external side effects. No GitHub push, tag or Release. Canonical PRD/HANDOVER unchanged.

- Final frozen App additionally passed alpha1 → alpha2 replacement with the same isolated home, preserved project membership credential and TLS endpoint/fingerprint, five local and five remote frozen MCP tools, two local App instances paired over HTTPS, authenticated push, owner quit and restart preservation. No model calls or physical device. packaged-alpha2-check.json.
