---
name: agent-mailbox
description: Collaborate through the project-scoped agent-mailbox v0.8 tools supplied by a managed employee execution session.
---

# Project collaboration in agent-mailbox v0.8

The Human discovers employees, creates a project group, adds members and assigns work in the local workbench. Use only the project tools supplied to your current execution session. Do not install the old v0.7 global MCP server or edit the Human's agent configuration. The historical skill is preserved in [the v0.7 archive](../../docs/archive/v07/skills/agent-mailbox/SKILL.md).

1. Call `project_context` first to read the assigned project, task, permissions and shared resources.
2. Read relevant project resources before acting. Report incomplete context or unavailable tools explicitly.
3. `project_messages` is read-only: use inbox, sent or group as appropriate. Reading is not acknowledgement or completion.
4. Ordinary project messages exchange information. `team_message` requests read-only colleague work. Send or delegate only within explicit Human authorization and current task permissions. A queued or delivered receipt proves only that status.
5. Write proposals with `project_note`; use `project_delivery` to read a colleague's fixed delivery for review. Return your own result in the assigned task session so the workbench can capture its delivery. Human acceptance and applying a workspace patch are separate actions. Never infer approval from a successful tool call.
6. Session credentials and tools bind your employee identity. Do not pass them to subagents. Subagents report to you; you remain responsible for your reply. Never impersonate another employee.

Client tool discovery and an execution wrapper are allowed when needed to reach these project MCP tools. This does not authorize shell, filesystem or unrelated operations inside that wrapper. Follow any stricter Human instruction for the current task.

See [the workbench guide](../../docs/WORKBENCH.md) for the Human workflow and [node setup](../../docs/NODES.md) for optional remote devices.
