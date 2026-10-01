# agent-mailbox

**Local-first AI agent team workbench for Codex and Claude Code.**

Turn the AI agents you already use into a project team. Give them shared context, let them hand off work through a project mailbox, and review their delivery before applying changes. Your project records stay on your computer; the workbench needs no additional management LLM account.

[中文](https://github.com/polaris-smart/agent-mailbox/blob/main/README.zh-CN.md) · [Quick start](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/GITHUB-QUICKSTART.md) · [Release channels](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/RELEASE-CHANNELS.md) · [Product baseline](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/PRD.md)

## One project, two agents, one review

Ask **Codex** to fix a bug in an isolated Git worktree. Ask **Claude Code** to review Codex's captured diff through the project tools. Read their findings, return the work with instructions if needed, then accept the delivery. Applying the patch to your original repository is a separate confirmation; agent-mailbox never commits or pushes it automatically.

This is multi-agent collaboration with a clear human decision point. You choose who does each task; there is no required workflow diagram to learn.

## Discover → join → collaborate → review

1. **Discover your agents.** Register existing CLI tools and desktop apps. The workbench distinguishes discovered tools, native sign-in, and verified task execution. Codex and Claude Code CLI adapters currently execute managed tasks.
2. **Make a project team.** Select a project directory, add employees, and share PRD, todo, daily updates, notes, and architecture documents. Each task pins the approved resource versions it starts with.
3. **Give the team work.** Run an explicit connection test, assign a task, and approve operations when requested. Project messages keep context; an explicit collaboration request starts work. The runner supplies project-scoped MCP tools without a separate MCP setup for each managed employee.
4. **Review the delivery.** Inspect the fixed files, diff, and activity. Accept it, return it as a linked follow-up, or separately confirm applying a safe text patch to the original repository.

## Install the v0.8 beta

**Beta 4: `0.8.0b4`.** Check the [GitHub release](https://github.com/polaris-smart/agent-mailbox/releases/tag/v0.8.0b4) for available files and checksums. A release is available only after its checks pass and its files are uploaded. [Channel status and release gates](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/RELEASE-CHANNELS.md) distinguish release preparation from publication.

| Your computer | Native download |
| --- | --- |
| Apple Silicon Mac | `Agent-Mailbox-0.8.0b4-darwin-arm64.zip` |
| Windows x64 | `Agent-Mailbox-0.8.0b4-win32-x64.zip` |
| Linux x64 | `Agent-Mailbox-0.8.0b4-linux-x64.tar.gz` |

Extract the entire archive, then start **Agent Mailbox**. Native downloads include Python, Node and the locked task runtime. Keep their folders intact. macOS builds have no Developer ID signature or notarization; Windows downloads may show a reputation warning. See [installation and upgrade steps](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/BETA-INSTALL.md). Intel Mac and Windows ARM native packages are not provided by this beta.

Prefer Python? Use Python 3.10+ and a dedicated virtual environment. Once the matching PyPI prerelease is available:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install 'agent-mailbox==0.8.0b4'
agent-mailbox --home ~/.agent-mailbox-v08
```

On Windows use `py -m venv .venv` and `.venv\Scripts\Activate.ps1`. PyPI beta installation needs an explicit version or `--pre`; an ordinary stable install does not select a beta. Python/source task execution needs Node.js 22.13+ and **Prepare runtime** in the UI. Our existing package is [PyPI agent-mailbox](https://pypi.org/project/agent-mailbox/). We have no TestPyPI, Homebrew or npm distribution; the unscoped npm name belongs to another project.

Sign into Codex or Claude Code using its native login. Viewing connection checks does not run a model; starting a connection test uses native model quota. If a default model is unavailable, explicitly select a model advertised by the execution service. See the [quick start](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/GITHUB-QUICKSTART.md) for source installation and your first collaboration.

## Built around your existing tools

- **Local-first project history:** private database, project mailbox, notes, shared resource proposals and approvals, and a work log of actual events.
- **Human control:** scoped operations, explicit permissions, employee pause/retirement, acceptance separate from applying code, and visible failure reasons.
- **Safer code handoffs:** clean, committed Git roots for local editing tasks; independent worktrees and immutable delivery records. A worktree isolates changes, but is not an operating-system sandbox.
- **Optional devices:** explicitly paired HTTPS nodes for read-only collaboration. Each device keeps its own agent login and project checkout; pairing does not synchronize files.
- **Optional knowledge tools:** existing CodeGraph index lookup and isolated preview of self-contained archify HTML. Structured local memory uses text search, without a required vector database.

Several employee names of the same tool share that device's native login by default. Discovering a desktop app does not make it automatically controllable. v0.8 replaces the software distribution, but does not automatically migrate a v0.7 database, background service, or configuration; old MCP configuration is not compatible with the new project-tool entry points. Keep the old data backed up; do not point v0.8 at a v0.7 data home.

## Learn more

[Detailed quick start](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/GITHUB-QUICKSTART.md) · [Beta acceptance and platform boundaries](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/BETA-ACCEPTANCE.md) · [Beta 3 verification evidence](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/evidence/v080/beta3-collaboration.md) · [Optional device setup](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/NODES.md) · [Security](https://github.com/polaris-smart/agent-mailbox/blob/main/SECURITY.md)

Updates currently offer explicit checks, maintenance pause, and private backups; program replacement and recovery remain manual. Keep project repositories and retained task worktrees backed up separately from the database backup. Cross-device editing isolation, automatic updates, universal app control, a drag-and-drop workflow editor, and AI ERP are outside this beta's implemented scope.

© 2026 NoFox and contributors · [Apache-2.0](https://github.com/polaris-smart/agent-mailbox/blob/main/LICENSE) · [NOTICE](https://github.com/polaris-smart/agent-mailbox/blob/main/NOTICE) · [Legacy MIT notices](https://github.com/polaris-smart/agent-mailbox/blob/main/LICENSES/MIT-Legacy.txt)
