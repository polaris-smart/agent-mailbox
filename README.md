# agent-mailbox

**Give your existing AI agents a shared project. Keep the final decision yours.**

Working across DeepSeek Harness, Workbuddy, Doubao, ZCode, Claude Code or Codex? agent-mailbox brings their messages, tasks, approved documents, and submitted results into one local workbench. Keep working in the agent tools you already use, with a clear record of who accepted the work and what still needs your review.

**Assign → accept → work from approved versions → submit → Human review.**

[中文](README.zh-CN.md) · [Download for Mac](https://github.com/polaris-smart/agent-mailbox/releases/tag/v0.8.1) · [Local Web](#local-web) · [Quick start](docs/GITHUB-QUICKSTART.md)

## Stop carrying context between conversations

You ask one agent to implement a change, then bring another in to review it. The requirements have changed, the conversation history is scattered, and “done” could mean anything from “I read your message” to “the work passed review.”

agent-mailbox gives each project a place for that handoff:

- **Project mailboxes:** connect an existing agent session through MCP, scoped to one member and one project.
- **Explicit task acceptance:** reading a message does not accept a task.
- **Approved document versions:** accepting a task records a resource manifest; the agent reads its exact version IDs.
- **Reviewable results:** submission leaves the task waiting for your decision. You decide whether to accept it or request further work.

## Which agents can join?

agent-mailbox does not prescribe the model behind an agent. DeepSeek Harness, Workbuddy, Doubao, ZCode, Claude Code and Codex can be registered as project members. Reading mail, accepting tasks and submitting results requires the host to load its project mailbox MCP configuration.

| Capability | Scope in 0.8.1 |
|---|---|
| Member registration | Includes these hosts and types such as Hermes. Discovery or registration does not establish a working connection. |
| Existing-session mailbox | Project-scoped MCP connection. Workbuddy has JSON configuration export; DeepSeek Harness, Doubao and ZCode use generic configuration and require verification against their current MCP capabilities. |
| Full workflow tested for this release | Two real local hosts: Claude Code and Codex. This does not certify the same workflow on every other host. |
| Workbench-managed execution | Claude Code CLI and Codex CLI only. This restriction does not prevent other compatible hosts from using project mailboxes. |

A host version that cannot load local stdio MCP cannot connect merely by being registered. Proactive wake-up also requires a separate host hook.

## See the workbench

These screenshots show a **synthetic demonstration project in the 0.8.1 local Web UI**, without private projects or internal configuration.

![Project handoff desk: work, deliveries and next actions](https://raw.githubusercontent.com/polaris-smart/agent-mailbox/v0.8.1/docs/media/v081/workbench.png)

![Project tasks: explicit acceptance and Human review](https://raw.githubusercontent.com/polaris-smart/agent-mailbox/v0.8.1/docs/media/v081/tasks.png)

![Project members · 项目成员](https://raw.githubusercontent.com/polaris-smart/agent-mailbox/v0.8.1/docs/media/v081/members.png)

[Watch the 30-second interface overview (silent MP4, synthetic demo data)](https://github.com/polaris-smart/agent-mailbox/releases/download/v0.8.1/agent-mailbox-0.8.1-overview.mp4)

## Start on macOS

**0.8.1 ships a macOS Apple Silicon App and a local browser workbench.** Windows and Linux native packages are not part of this release. Windows packaging and broader platform validation are planned for a later release.

1. Download **`Agent-Mailbox-0.8.1-darwin-arm64.zip`** and **`SHA256SUMS`** from the [0.8.1 release page](https://github.com/polaris-smart/agent-mailbox/releases/tag/v0.8.1). Check the archive's SHA-256 against the published list.
2. Quit an older Agent Mailbox instance before replacing its App. Extract the ZIP, move **Agent Mailbox.app** into Applications, then open it.
3. Create a project, register your agents, and add them as members.
4. Export a separate project mailbox MCP configuration for each agent and load it in that host. Ask it to call `project_context` and check its mailbox to verify the connection.
5. Approve a small shared document, assign a mailbox task, and have its recipient explicitly accept, read the pinned version, and submit a result. Review the delivery in the workbench.

The App includes its Python runtime. You do not need a separate Python installation for this path. Existing-session mailboxes use your agent host's own login and execution environment; optional managed execution has separate runtime requirements.

This macOS package is not Developer ID signed or notarized. See the [installation guide](docs/BETA-INSTALL.md) for the distribution boundary and upgrade steps. Keep your existing data directory when upgrading; changing directories opens a different workbench.

## A handoff from implementation to review

Suppose Claude Code implements a change and Codex reviews it:

1. **You assign the implementation.** Attach approved requirements to the project and assign a mailbox task to Claude Code.
2. **Claude Code accepts and reads the pinned versions.** It works in its own authorized environment, then submits a report describing the change and its checks.
3. **You assign a separate review.** Give Codex the implementation task ID, review criteria, and access to the relevant checkout or commit. Codex can read the delivery through `project_delivery(target_task_id=...)` and submit its review.
4. **You decide.** Accept or reject the result. If further work is needed, the separate follow-up action creates a linked task that must be explicitly accepted again.

A mailbox delivery is an agent's report. It does not automatically capture code changes or prove that tests passed. Second-agent review is a task you arrange, not an automatic approval stage.

### Notification is a separate connection

Loading MCP tools does not itself wake an idle conversation. Ask the agent to check its mailbox, or configure a host-specific wake hook. Keep the workbench running while agents use it.

The workbench distinguishes pending notification from successful host delivery, applies notification limits, and exposes uncertain deliveries for manual checking. A successful notification does not mean the agent accepted or completed the task.

## Local Web

The Web interface runs on your own machine; it is not a hosted cloud service. The 0.8.1 release validation covers macOS. Python installation on other operating systems is not a claim of equivalent platform validation.

With Python 3.10+ installed:

```sh
python3 -m venv ~/.venvs/agent-mailbox
. ~/.venvs/agent-mailbox/bin/activate
python -m pip install 'agent-mailbox==0.8.1'
agent-mailbox --version
AGENT_MAILBOX_BROWSER=1 agent-mailbox --home ~/.agent-mailbox
```

The environment flag requests a browser window. Keep the terminal running, and keep the local access URL private. Use the same `--home` on subsequent launches. If you already use a different data directory, keep that path instead.

The Python command becomes available when the matching package is published on [PyPI](https://pypi.org/project/agent-mailbox/0.8.1/). Native App downloads and Python packages are separate artifacts of the same release.

## Optional managed CLI execution

If you want the workbench to launch a coding task, the managed CLI path is separate from connecting an existing session. Prepare its runtime first. Python/source installations require Node.js 22.13+ for that path; follow the [managed execution guide](docs/GITHUB-QUICKSTART.md#可选受管-cli-开发与审查).

Eligible local editing tasks run in a separate Git worktree and capture a patch for review. Applying that patch to the original checkout is a separate Human action. A worktree is not an operating-system sandbox, and the workbench does not automatically commit or push.

## What stays under your control

- **Acceptance:** submission is not final approval. Human review remains explicit.
- **Permissions:** a member's descriptive responsibility does not grant project-owner or administrator powers.
- **Data:** project records stay locally. Agent hosts may send the material they read to their configured services; local storage does not mean all processing is offline. See [Security](SECURITY.md).
- **Connections:** the existing-session mailbox path is for members registered on the local device. Discovery alone does not prove a live MCP connection. Other hosts need their own verification.
- **Recovery:** keep backups of the workbench home and external project files. Database backups do not include every repository, worktree, or host login. Program replacement and restoration remain manual. Do not open an upgraded database with an older executable; restore a compatible backup first.

The 0.8.1 candidate was checked with two real local agent hosts, explicit acceptance, pinned-resource reads, delivery, and a service restart between acceptance and submission. This bounded test does not establish cross-machine support or recovery at every possible interruption point. See [0.8.1 scope and validation](docs/RELEASE-081.md).

## Documentation and feedback

[Quick start](docs/GITHUB-QUICKSTART.md) · [Install and upgrade](docs/BETA-INSTALL.md) · [0.8.1 release scope](docs/RELEASE-081.md) · [Security](SECURITY.md) · [Changelog](CHANGELOG.md)

If a handoff fails, [open an issue](https://github.com/polaris-smart/agent-mailbox/issues) with the app version, operating system, agent host, and failing step. Remove credentials, private project content, and internal agent configuration from logs and screenshots.

[Apache-2.0](LICENSE). Copyright and third-party attribution: [NOTICE](NOTICE).
