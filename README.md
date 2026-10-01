# agent-mailbox

**Local-first AI agent team workbench for Codex and Claude Code.**

Turn the AI agents you already use into a project team. Give them shared context, let them hand off work through a project mailbox, and review their delivery before applying changes. Your project records stay on your computer; the workbench needs no additional management LLM account.

[中文](README.zh-CN.md) · [Quick start](docs/GITHUB-QUICKSTART.md) · [Release channels](docs/RELEASE-CHANNELS.md) · [Product baseline](docs/PRD.md)

## One project, two agents, one review

Ask **Codex** to fix a bug in an isolated Git worktree. Ask **Claude Code** to review Codex's captured diff through the project tools. Read their findings, return the work with instructions if needed, then accept the delivery. Applying the patch to your original repository is a separate confirmation; agent-mailbox never commits or pushes it automatically.

This is multi-agent collaboration with a clear human decision point. You choose who does each task; there is no required workflow diagram to learn.

## Discover → join → collaborate → review

1. **Discover your agents.** Register existing CLI tools and desktop apps. The workbench distinguishes discovered tools, native sign-in, and verified task execution. Codex and Claude Code CLI adapters currently execute managed tasks.
2. **Make a project team.** Select a project directory, add employees, and share PRD, todo, daily updates, notes, and architecture documents. Each task pins the approved resource versions it starts with.
3. **Give the team work.** Run an explicit connection test, assign a task, and approve operations when requested. Project messages keep context; an explicit collaboration request starts work. The runner supplies project-scoped MCP tools without a separate MCP setup for each managed employee.
4. **Review the delivery.** Inspect the fixed files, diff, and activity. Accept it, return it as a linked follow-up, or separately confirm applying a safe text patch to the original repository.

## Try the v0.8 beta from source

**Release target: `0.8.0b4` (Beta 4, in preparation).** The source branch is public; Beta 4 release preparation is in progress. There is no published Beta 4 GitHub Release or package-channel release yet. Our PyPI package currently contains v0.7.6. We have not published TestPyPI, Homebrew, or npm distributions. The unscoped npm package named `agent-mailbox` belongs to another project and is not an installation source for this workbench. This release targets GitHub Beta and PyPI at the same product version. See [channel status and release gates](docs/RELEASE-CHANNELS.md).

Use Python 3.10+ and a dedicated virtual environment. The product, repository, Python package, and CLI keep the name `agent-mailbox`. v0.8 will replace the earlier distribution across all channels. Until the release passes validation, keep source testing separate from an older installation:

```sh
git clone --branch feat/v080-workbench --single-branch https://github.com/polaris-smart/agent-mailbox.git agent-mailbox-v08
cd agent-mailbox-v08
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
agent-mailbox --home ~/.agent-mailbox-v08
```

The command opens your local browser workbench. Source task execution needs Node.js 22.13+ and **Prepare runtime** in the UI. Sign into Codex or Claude Code using its native login. Viewing connection checks does not run a model; starting a connection test uses native model quota. If a default model is unavailable, explicitly select a model advertised by the execution service.

macOS ARM64 has local packaged-app verification. Windows PowerShell uses `.venv\Scripts\Activate.ps1` instead of the activation line, but Windows is not yet a verified release target. GitHub CI also has unresolved platform failures; local Mac verification is not a claim that all platform CI passes.

## Built around your existing tools

- **Local-first project history:** private database, project mailbox, notes, shared resource proposals and approvals, and a work log of actual events.
- **Human control:** scoped operations, explicit permissions, employee pause/retirement, acceptance separate from applying code, and visible failure reasons.
- **Safer code handoffs:** clean, committed Git roots for local editing tasks; independent worktrees and immutable delivery records. A worktree isolates changes, but is not an operating-system sandbox.
- **Optional devices:** explicitly paired HTTPS nodes for read-only collaboration. Each device keeps its own agent login and project checkout; pairing does not synchronize files.
- **Optional knowledge tools:** existing CodeGraph index lookup and isolated preview of self-contained archify HTML. Structured local memory uses text search, without a required vector database.

Several employee names of the same tool share that device's native login by default. Discovering a desktop app does not make it automatically controllable. v0.8 replaces the software distribution, but does not automatically migrate a v0.7 database, background service, or configuration; old MCP configuration is not compatible with the new project-tool entry points. Keep the old data backed up; do not point v0.8 at a v0.7 data home.

## Learn more

[Detailed quick start](docs/GITHUB-QUICKSTART.md) · [Beta acceptance and platform boundaries](docs/BETA-ACCEPTANCE.md) · [Beta 3 verification evidence](docs/evidence/v080/beta3-collaboration.md) · [Optional device setup](docs/NODES.md) · [Security](SECURITY.md)

Updates currently offer explicit checks, maintenance pause, and private backups; program replacement and recovery remain manual. Keep project repositories and retained task worktrees backed up separately from the database backup. Cross-device editing isolation, automatic updates, universal app control, a drag-and-drop workflow editor, and AI ERP are outside this beta's implemented scope.

© 2026 NoFox and contributors · [Apache-2.0](LICENSE) · [NOTICE](NOTICE) · [Legacy MIT notices](LICENSES/MIT-Legacy.txt)
