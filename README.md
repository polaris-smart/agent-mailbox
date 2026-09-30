# agent-mailbox

A local workbench for managing AI employees around projects: assign work, share project knowledge, inspect progress, approve operations and accept results.

**v0.8 is an independent implementation.** It keeps the product name and release line, with its own runtime, private database and project tools. v0.7 mail commands and background services are not part of the v0.8 package. The current build is **0.8.0a6, a local alpha**; no v0.8 release has been published to GitHub or PyPI yet.

[中文](README.zh-CN.md) · [Product baseline](docs/PRD.md) · [Quick start](docs/GITHUB-QUICKSTART.md) · [Optional devices](docs/NODES.md) · [Security](SECURITY.md)

## Start locally

From this v0.8 checkout, using Python 3.10+:

```sh
python -m venv .venv
# macOS/Linux:
. .venv/bin/activate
# Windows PowerShell instead: .venv\Scripts\Activate.ps1
python -m pip install .
agent-mailbox --home ~/.agent-mailbox-v08
```

The command opens a local browser workbench. A dedicated home keeps this alpha separate from any previous installation. The packaged macOS ARM64 app optionally bundles Python, Node and execution dependencies; source installation works without a desktop app. Windows/Linux distribution and physical-platform acceptance are still pending. Do not use the public `pip install agent-mailbox` as a way to obtain this unpublished alpha.

1. Discover and register existing CLI tools or desktop apps in the global employee directory.
2. Create a project group and add registered employees as members. Automated task execution currently supports **Codex and Claude** through managed CLI adapters; detected desktop apps are not automatically controllable.
3. For source installs, install Node.js 22.13+ and choose **Prepare runtime** in the workbench. Log into the agent on this computer using its native login. No separate management LLM or API key is required.
4. Give the employee a task with a clear deliverable. Review any requested operation permission. Finished execution enters **human review**, rather than declaring itself accepted.

Several employee names of the same kind share that device's native agent login by default. This is organizational identity, not isolated provider accounts. Each employee receives project-scoped tools for context, notes, resources and messages; the task runner supplies them without requiring a manual MCP setup for every employee.

## What this alpha provides

- Global employee directory, project membership, persistent message threads, explicit collaboration requests, task execution, human approvals and acceptance.
- Project notes, resource links, work history, basic employee lifecycle and health. Memory is structured local data and text search, with no vector database or separate memory service.
- Shared project tools for supported managed task sessions. Existing desktop conversations are not injected or awakened automatically.
- Optional devices over explicitly paired, certificate-pinned HTTPS. A headless node can map an authorized project to its own local checkout and run supported employees. Files, logins and repositories are not synchronized automatically.
- One execution owner per data home. Lost final replies retain a local receipt for retry; uncertain executions are not run again automatically.

The workbench normally runs on one computer. Remote Ubuntu workers are optional and require an existing private network or SSH tunnel. The coordinator must remain awake and reachable. Loopback tests establish protocol behavior; they do not establish real Hong Kong/Silicon Valley, LAN or Ubuntu operation.

## Updating and limitations

Application files and the selected data home are separate. Replacing a compatible v0.8 build with the same home preserves projects, employee identities, scopes and device identity; back up the home while stopped. Automatic download/update/rollback, general workflow editing, isolated edit workspaces, universal app control and AI ERP are not implemented.

Apache-2.0 licensed, by NoFox and contributors. Prior MIT notices are preserved in LICENSES/MIT-Legacy.txt. macOS public distribution still requires a signing/notarization decision; GitHub hosting does not remove operating-system checks.
