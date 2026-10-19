# agent-mailbox 0.8.3a1 preview

0.8.3a1 is a **pre-release for review**, not the completed 0.8.3 release. The
initial distribution target is the macOS Apple Silicon App with its local Web
workbench. Keep a backup of an existing home and use a separate test home for
the first trial. Do not treat a successful package check as approval to replace
an active team's installation.

This candidate adds host notification and receiver work, a Human-facing access
surface, explicit mailbox and project connection controls, and Fleet LAN
building blocks. Existing project mail, task acceptance, delivery and separate
Human review remain available. Host capabilities vary: an MCP mailbox can be
used without a native wake receiver, and a notification or first-open record
does not mean an Agent accepted or completed a task.

The extracted macOS arm64 candidate passed a synthetic-home startup and
HTTP/MCP package check. It verified the Human assets, owner authentication,
mail exchange, explicit task acceptance and submission, and separate Human
review without provider calls. The Python 3.13 regression suite and the pinned
Codex receiver Node tests passed in an isolated source snapshot. This evidence
does **not** establish Gatekeeper/notarization behavior, every real host's
notification path, or a two-device continuation.

Known preview boundaries:

- The pinned Hermes private-work overlay is opt-in and is not yet connected to
  the workbench's normal start/stop flow. Do not enable it against a live
  profile solely because its isolated probe passes.
- Existing held mail remains held until the owner reviews and releases each
  message. The alpha does not bulk-send previously held messages.
- Cross-device transfer, offline relay, family permissions and controller
  recovery need separate end-to-end acceptance before the final 0.8.3 release.
- Python and npm package registries are not part of this first alpha App
  distribution. The Python package uses version `0.8.3a1`; an eventual npm
  pre-release would use the SemVer form `0.8.3-alpha.1`.

Only publish this preview after a clean commit, a rebuild whose provenance is
`dirty=false`, platform CI, archive checksum verification, and Human review of
the exact artifact. Public release notes must not expose private accounts,
model choices, sessions, credentials or raw logs.
