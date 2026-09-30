# Security

This document covers the independent v0.8 alpha, not v0.7's mail server.

The human workbench listens on loopback and uses an owner bearer token plus same-origin/host checks. It must not be exposed to a LAN or public reverse proxy. Browser extensions and processes running as the same OS account remain within the local trust boundary; these credentials are not protection from a compromised host.

SQLite, credential files and device identity files are created privately. POSIX mode checks apply on macOS/Linux; Windows access is governed by the user's filesystem ACL and still requires physical-platform verification. Secrets are redacted from public state and remote activity payloads; prompts, deliverables and saved notes can still contain user-provided sensitive content.

An employee task gets project-scoped tools. Employee names are organizational identities, not OS sandboxes or independent provider accounts. Native agent login remains local; actual tool execution authority depends on the agent and its native sandbox. Human permissions default to deny on timeout or loss of control, and a worker cannot approve its own output.

Optional devices use TLS 1.2+ with a pinned SHA-256 certificate identity checked before invitations or bearer credentials are sent. Pairing scopes projects and binds device/run receipts. Invitations and pairing recovery proofs are secrets: deliver them through a trusted channel, store private files and revoke compromised devices. Registration and revocation are local records, not SPIFFE or short-lived identity infrastructure.

The coordinator accepts an explicitly selected private/loopback/CGNAT address for the device listener. Use a private network or SSH tunnel for remote servers; do not expose the owner API. Project files and provider credentials are not transported by pairing. Persistent terminal receipts retry identical results, while uncertain executions are interrupted without automatic replay.

Task/governance history is useful local evidence, not tamper-proof audit storage. Full cost accounting and enterprise policy enforcement are not implemented. The source/wheel/app alpha has no public release assurance; the optional macOS build currently has only a local ad-hoc seal, with no Developer ID notarization.

Report security issues privately to the repository maintainer using GitHub's private vulnerability reporting when available; otherwise contact the maintainer before publishing exploit details.
