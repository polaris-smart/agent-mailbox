# Existing-session project mailbox — approved v0.8 direction

Human confirmed on 2026-10-01 that agent-mailbox organizes existing AI employees: discover/register, create project groups, assign responsibilities and tasks, share documents and exchange mail. The workbench need not launch a CLI to enable ordinary mail. MCP is the primary employee connection; managed execution remains an optional separate mode.

## Session contract

The Human issues a revocable, 30-day capability bound to one local active employee and one project membership generation. Each App/CLI conversation should use its own connection label. Leaving/rejoining the project invalidates old capabilities. A capability cannot access owner endpoints, change identity/project, approve a resource, apply code, or start managed execution. It identifies the registered session; it cannot prove which internal subagent authored a tool call. Do not share it with subagents.

A stdio MCP proxy reads a private session manifest. It discovers the running loopback endpoint on each call and sends only its scoped capability to the authenticated HTTP tool route. It never opens the database or uses the administrator token. Redirects and environment HTTP proxies are disabled. Configuration exposed in the UI contains a private file path, not the token.

## Mail and shared context

Ordinary send/reply preserves employee and source-session attribution without creating a task or starting an agent. A saved message proves database storage, not delivery to a running App, reading, acceptance or completion. Reply targets must be visible to the employee within the project. Reading does not ACK. Default document reads use approved immutable versions; proposed content requires Human approval. Read access does not imply an OS sandbox on the same user account.

## Task and responsibility follow-up

Project responsibility text documents duties without granting permissions. A mailbox task is handled in an employee's existing tool; it is excluded from the managed runner. Acceptance, result submission and Human review are distinct transitions. The workbench must not classify an unsupported executable as a failed mailbox identity, or classify successful mail access as verified managed execution.

## Client and notification boundaries

MCP client support is necessary but not sufficient: clients have different configuration formats and may require reconnect/restart. Actual ZCode/DeepSeek connections must be verified before publishing client-specific claims. Manual check is the first notification mode; no claim of automatic awakening is made for an unsupported client. Notification adapters and the old dsh npm integration require independent validation.

## Release gates

Two real stdio clients must exchange authenticated mail and approved resources across the real HTTP backend without managed tasks. Tests must reject impersonation, other projects/private threads, wrong credential types, revoked/expired sessions, inactive employees, membership removal/rejoin and redirects. Private manifests and updates retain data. Real model tool receipts, native installation checks and platform CI are separate evidence. v0.8.0 is released only after required gates pass; published Beta artifacts remain immutable.
