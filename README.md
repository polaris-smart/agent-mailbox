# agent-mailbox

**Local-first team workbench for your existing AI agent apps and CLIs.**

Turn the AI agents you already use into a project team. Give them shared context, let them hand off work through a project mailbox, and review their delivery before applying changes. Your project records stay on your computer; the workbench needs no additional management LLM account.

[中文](https://github.com/polaris-smart/agent-mailbox/blob/main/README.zh-CN.md) · [Quick start](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/GITHUB-QUICKSTART.md) · [Release channels](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/RELEASE-CHANNELS.md) · [Product baseline](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/PRD.md)

## Discover → join → connect → collaborate → review

**v0.8.0 connects existing agent sessions through project mailbox MCP. Download availability and checksums are determined by the actual release page.**

1. **Discover your employees.** Register existing desktop apps and CLI agents, then create a project and add its members. Record each member's responsibility; a role description does not grant extra permissions.
2. **Connect an existing conversation.** Generate its project mailbox MCP configuration in the workbench and import it into that agent's MCP settings. Each connection is bound to one employee, project and session. This does not require a new provider key or a workbench-started CLI; support depends on the host accepting the exported stdio MCP configuration.
3. **Share documents and exchange mail.** Agents read approved PRD, todo, daily updates and architecture documents, send project messages and reply in their existing environment. Ask them to check their mailbox: MCP access alone does not notify or wake an idle conversation. Reading a message does not accept work.
4. **Assign and review a mailbox task.** Mailbox tasks are the default path. The assigned employee explicitly accepts, which pins approved document revisions, then submits a result for Human review. Submission is not completion or acceptance. You accept the result or return it as a linked follow-up; no CLI is launched and no changes are automatically applied.

Keep each employee's project connection with its main session. Subagents report to their responsible agent; sharing that connection with subagents would attribute their calls to the same registered session. The system does not independently verify the internal author.

**Managed CLI execution remains optional.** Codex and Claude Code CLI adapters can run workbench-started tasks with project tools supplied by the runner. Local editing tasks use isolated Git worktrees and captured diffs; applying a patch to the original repository requires separate confirmation. agent-mailbox never commits or pushes automatically. This managed path is separate from connecting an existing App or CLI mailbox.

Existing-session mailbox connections currently require employees registered on the local device. Optional remote execution nodes use their existing protocol; they do not provide the new remote App mailbox connection.

## Install v0.8.0

**Version: `0.8.0`.** Check the [GitHub release](https://github.com/polaris-smart/agent-mailbox/releases/tag/v0.8.0) for available files and checksums. A release is available only after its checks pass and its files are uploaded. [Channel status and release gates](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/RELEASE-CHANNELS.md) distinguish release preparation from publication.

| Your computer | Native download |
| --- | --- |
| Apple Silicon Mac | [DMG](https://github.com/polaris-smart/agent-mailbox/releases/download/v0.8.0/Agent-Mailbox-0.8.0-darwin-arm64.dmg) · [ZIP](https://github.com/polaris-smart/agent-mailbox/releases/download/v0.8.0/Agent-Mailbox-0.8.0-darwin-arm64.zip) |
| Windows x64 | [Agent-Mailbox-0.8.0-win32-x64.zip](https://github.com/polaris-smart/agent-mailbox/releases/download/v0.8.0/Agent-Mailbox-0.8.0-win32-x64.zip) |
| Linux x64 | [Agent-Mailbox-0.8.0-linux-x64.tar.gz](https://github.com/polaris-smart/agent-mailbox/releases/download/v0.8.0/Agent-Mailbox-0.8.0-linux-x64.tar.gz) |

Extract the entire archive, then start **Agent Mailbox**. Native downloads include Python, Node and the locked task runtime. Keep their folders intact. macOS builds have no Developer ID signature or notarization; Windows downloads may show a reputation warning. See [installation and upgrade steps](https://github.com/polaris-smart/agent-mailbox/blob/main/docs/BETA-INSTALL.md). Intel Mac and Windows ARM native packages are not provided by this release.

Prefer Python? Use Python 3.10+ and a dedicated virtual environment. Use the exact version below; availability is determined by the registry:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install 'agent-mailbox==0.8.0'
agent-mailbox --home ~/.agent-mailbox-v08
```

On Windows use `py -m venv .venv` and `.venv\Scripts\Activate.ps1`. Package availability is determined by the registry; use the exact version pin to verify installation. Python/source task execution needs Node.js 22.13+ and **Prepare runtime** in the UI. Our existing package is [PyPI agent-mailbox](https://pypi.org/project/agent-mailbox/). There is no TestPyPI or Homebrew distribution. The existing [dsh-agent-mailbox](https://www.npmjs.com/package/dsh-agent-mailbox) npm package is a separate DeepSeek Harness plugin, not a workbench installer. The matching plugin `dsh-agent-mailbox@0.8.0` is published on the official npm registry and has passed an independent installation check; the unscoped npm name `agent-mailbox` belongs to another project.

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

Updates currently offer explicit checks, maintenance pause, and private backups; program replacement and recovery remain manual. Keep project repositories and retained task worktrees backed up separately from the database backup. Cross-device editing isolation, automatic updates, universal app control, a drag-and-drop workflow editor, and AI ERP are outside this version's implemented scope.

© 2026 NoFox and contributors · [Apache-2.0](https://github.com/polaris-smart/agent-mailbox/blob/main/LICENSE) · [NOTICE](https://github.com/polaris-smart/agent-mailbox/blob/main/NOTICE) · [Legacy MIT notices](https://github.com/polaris-smart/agent-mailbox/blob/main/LICENSES/MIT-Legacy.txt)
