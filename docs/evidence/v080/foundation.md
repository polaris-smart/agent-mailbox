# v0.8.0 foundation smoke comparison — 2026-09-30

## Decision
Keep the independent Python product layer and v0.7.6 protocol/data compatibility. Use pinned acpx 0.19.3 as an execution component behind an adapter boundary. Do not embed/fork the complete Paperclip or AionUi product for v0.8.0. Borrow product/workflow patterns and validate any reused code license at the exact source revision.

This is a scoped readiness and packaging decision, not proof that the existing mailbox already implements the management domain or is globally superior. Full model task execution, authentication reuse, real-device federation and upgrade recovery were intentionally not exercised.

## Actual isolated checks
All paths are under /tmp/v080-foundation-smoke. No background service was installed; no global config was changed; no credential content was read or printed; no model was invoked. Child launch environments were explicit small maps, not inherited shell credentials. No agents/companies/tasks were created in Paperclip.

### Paperclip 2026.916.1
Official release has feature-catalog.json only; supported managed/npm installer. npm metadata engines >=24.11.0; host Node22.23.1 is insufficient.

Command:
`npm install --prefix /tmp/v080-foundation-smoke/paperclip node@24 paperclipai@2026.916.1 --no-audit --no-fund --cache /tmp/v080-foundation-smoke/npm-cache`

Success: 389 packages in 2m. Installed Node24.21.0 locally. node_modules approximately 1.6GiB including Node194MiB, bundled Codex326MiB, Claude193MiB and embedded Postgres147MiB. Some package directory totals include nested/bundled duplication.

`/tmp/v080-foundation-smoke/paperclip/node_modules/node/bin/node /tmp/v080-foundation-smoke/paperclip/node_modules/paperclipai/dist/index.js --help`
Success. Exposes project, issue, agent, company, approval, budget, run, activity, skills and update operations.

`.../node .../paperclipai/dist/index.js onboard --yes --no-install-service --data-dir /tmp/v080-foundation-smoke/paperclip/data`
With explicit PAPERCLIP_HOME=temp data, PORT=35929, PATH=temp node bin:/usr/bin:/bin. This configured AND started foreground server despite doc's configure-only language. It was stopped by a 30s subprocess deadline; no service registration. Never created the default CEO/test-drive agent.

`.../node .../paperclipai/dist/index.js run --no-repair --data-dir /tmp/v080-foundation-smoke/paperclip/data`
Controlled foreground start with process-group cleanup. /api/health returned HTTP200 and startupRecovery ready in 5.46s. / returned HTTP200 text/html (5973 bytes). /api/companies returned [] with HTTP200. Process tree20, summed RSS913680KiB; this double counts PostgreSQL shared pages and is not unique physical RAM. Data directory82260KiB. Foreground exited0 on group SIGTERM. No PostgreSQL remains.

Architecture tradeoff: most complete management product, but company/budget/deployment models and PG/native/execution dependencies become our migration and release boundary. Current remote runner remains explicitly experimental. Reusing it does not remove device-node/auth/path-mapping work. A thin branded skin is possible, but legacy MCP/SQLite migration and company mappings still need design; a deep fork would create parallel UI/domain upkeep.

### AionCore v0.2.2 / AionUi
`gh release download v0.2.2 -R iOfficeAI/AionCore -p '*aarch64-apple-darwin.tar.gz' -p aioncore-checksums.txt -D /tmp/v080-foundation-smoke/aioncore`
Official arm64 tar36154246B; extracted binary ~88MiB; SHA256 verified70c50b84be9e17ba574a2c74370e6d57a267f44bdd6ecf641740cf220737d5cb against release checksum file.

`aioncore --help`
Success; supports data/work dirs, local identity, bundled/download managed resources, team/session CLI and doctor.

`/tmp/v080-foundation-smoke/aioncore/aioncore --host 127.0.0.1 --port 35928 --data-dir /tmp/v080-foundation-smoke/aioncore/data --work-dir /tmp/v080-foundation-smoke/aioncore/work --local --managed-resources-mode bundled`
PATH=/usr/bin:/bin, XDG_CONFIG_HOME/cache under temporary root. /health HTTP200 in5.89s (version0.2.2). RSS43792KiB; data3260KiB. / and guessed /api endpoints404. Startup log explicitly reports BOOTSTRAP_DEGRADED_MANAGED_RUNTIME_PREPARE: bundled Node24.11 runtime missing. Thus API process health does not prove agents can run.

AionUi latest GitHub release v2.2.2 and previous two release records contain no assets. Main package defines Node>=22<25,104 production dependencies,52 dev dependencies, fixed Corev0.2.2. scripts/webui.ts needs out/renderer and backend; default path calls frontend package build. Did not download/install full AionUi or test UI; Core alone is not a ready user product.

Architecture tradeoff: valuable automatic discovery/team/ACP/MCP design; adopting full Core introduces Rust API contracts and new SQLite/domain ownership. Existing same-backend team runtime is not proven multi-device federation. A full desktop fork adds frontend/Electron/release work rather than removing it.

### acpx0.19.3 component
`npm install --prefix /tmp/v080-foundation-smoke/acpx acpx@0.19.3 --no-audit --no-fund --cache /tmp/v080-foundation-smoke/npm-cache`
Success.43MiB node_modules; Node>=22.13.0. `node .../acpx/dist/cli.js --help` succeeds. `import(.../acpx/dist/runtime.js)` succeeds and exports createAcpRuntime, createFileSessionStore, createAgentRegistry, createSharedAcpRuntime etc. No prompt or session creation invoked.

43MiB is component footprint, not complete shipping footprint: Node and selected ACP adapter/CLI dependencies must be accounted for. Pin versions, resolve explicit executables, own process lifecycle, and record request/run/session IDs in product DB. ACP filesystem/terminal flags are not OS sandbox guarantees. Prefer one in-process runtime owner; shared runtime has exact cwd/agent/name identity and non-serializable host callback limitations.

## Consequences for v0.8.0
- Existing076 MCP message/task clients continue against unchanged compatibility interface; new project/run/log/memory tables and APIs are additive. Preserve employee identity independent of acpx session identity.
- Separate product data from replaceable program payload. Installer must bundle/manage Python and Node/selected adapters; keep credential authorization on each device and configure explicit runtime paths. Do not market 43MiB acpx as complete installer size.
- Finish one end-to-end task plus review and restart recovery in our product before claiming parity. Message delivery, runtime success and human acceptance are different facts.
- Paperclip patterns to copy conceptually: issue checkout ownership, heartbeat outcome/blocked reporting, audit/activity, code/data split, update/rollback boundaries. Aion patterns: agent discovery, session MCP injection and team tasks.
- No full-system merge or remote experimental runner in the first implementation. Device node/outbox/auth/path mapping must be independently verified regardless of chosen foundation.

## Cleanup
After all checks, urllib probes for35928/35929 refused connection. ps inspection found no command containing these temporary server paths. Files retained for inspection; temporary dependency directories can later be removed by the parent if unneeded. Logs: paperclip/install.log, onboard.log, run.log; aioncore/startup.log.
