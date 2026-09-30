"""Project-scoped tools injected into managed employees; no user MCP setup."""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .workbench_store import WorkbenchError, WorkbenchStore


def build_server(store, employee_id, project_id, token, endpoint="", task_id="", run_id=""):
    def guard(*, write=False):
        valid = (
            store.validate_execution(token, employee_id, project_id, task_id, run_id)
            if task_id or run_id
            else store.validate_employee(token, employee_id, project_id)
        )
        if not valid or write and not (task_id and run_id):
            raise WorkbenchError("UNAUTHORIZED", "This employee has no access to this project.")

    guard()
    server = MCPServer(
        "agent-mailbox-project",
        instructions="Use project_context to understand your assigned project. Ordinary project messages do not start work; team_message explicitly requests read-only work. Delivery is not completion. Notes are proposals. Credentials belong to this execution session: do not give them or these tools to subagents. Subagents report to you. Messages are attributed to the registered employee session; internal authorship is not independently verified. Never impersonate another member.",
    )

    @server.tool()
    def project_context() -> dict:
        """Read this project's shared resources, memory and employee membership."""
        guard()
        return store.project_context(project_id, employee_id=employee_id)

    @server.tool()
    def project_memory_search(query: str) -> dict:
        """Search the assigned project's durable notes."""
        guard()
        return {"memories": store.search_memory(project_id, query)}

    @server.tool()
    def project_resource_read(resource_id: str) -> dict:
        """Read a registered text artifact in the assigned project."""
        guard()
        return store.read_resource(project_id, resource_id)

    @server.tool()
    def project_code_search(query: str) -> dict:
        """Search this project's optional CodeGraph symbol index. No index rebuild or LLM call."""
        guard()
        try:
            return store.query_knowledge(project_id, query)
        except WorkbenchError as exc:
            raise ToolError(f"{exc.code}: {exc}") from None

    @server.tool()
    def project_note(title: str, body: str) -> dict:
        """Record a project note attributed to this employee; human decisions stay separate."""
        guard(write=True)
        return store.add_memory(project_id, title, body, source=f"employee:{employee_id}")

    @server.tool()
    def project_messages(folder: str = "inbox", limit: int = 100) -> dict:
        """Read my inbox, sent mail or project group. Viewing is not ACK or completion."""
        guard()
        return store.employee_messages(project_id, employee_id, folder, limit)

    def send(title, body, recipient_id=None, reply_to=None, request_work=False, request_id=None):
        guard(write=True)
        if request_work and not endpoint:
            raise WorkbenchError(
                "WAKE_UNAVAILABLE", "Open the project workbench before sending team work."
            )
        saved = store.send_message(
            project_id,
            title,
            body,
            recipient_id=recipient_id or None,
            sender_id=employee_id,
            reply_to=reply_to,
            request_work=request_work,
            request_id=request_id,
            source_task_id=task_id,
        )
        if not endpoint:
            return saved
        body = json.dumps(
            {
                "employee_id": employee_id,
                "project_id": project_id,
                "task_id": task_id,
                "run_id": run_id,
            }
        ).encode()
        request = urllib.request.Request(
            endpoint + "/api/workbench/notify",
            data=body,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                response.read()
        except OSError:
            if not saved.get("task_id"):
                return {**saved, "notification_confirmed": False}
            store.add_event(
                saved["task_id"],
                "wake_unconfirmed",
                "The workbench did not acknowledge this queued request. It remains saved for recovery.",
                {"code": "WAKE_UNAVAILABLE"},
            )
            return {
                **saved,
                "status": "delivery_unconfirmed",
                "error": {
                    "code": "WAKE_UNAVAILABLE",
                    "message": "The request is saved, but employee triggering is unconfirmed.",
                },
            }
        return {**saved, "status": "queued" if saved.get("task_id") else "delivered"}

    @server.tool()
    def project_message(
        title: str, body: str, recipient_id: str = "", reply_to: str = "", request_id: str = ""
    ) -> dict:
        """Send or reply without triggering work. Identity comes from the bound execution session."""
        return send(title, body, recipient_id, reply_to or None, request_id=request_id or None)

    @server.tool()
    def team_message(recipient_id: str, title: str, message: str, request_id: str = "") -> dict:
        """Explicitly request bounded read-only work from a colleague; human reviews the result."""
        return send(title, message, recipient_id, request_work=True, request_id=request_id or None)

    return server


def main():
    if os.environ.get("AGENT_MAIL_FLEET") == "1":
        from .workbench_fleet import FleetClient

        root = Path(os.environ["AGENT_MAIL_HOME"])
        saved = json.loads((root / "workbench/fleet/client.json").read_text())
        client = FleetClient(root, saved)
        build_remote_server(
            client,
            os.environ["AGENT_MAIL_EMPLOYEE"],
            os.environ["AGENT_MAIL_PROJECT"],
            os.environ.get("AGENT_MAIL_TASK", ""),
            os.environ.get("AGENT_MAIL_RUN", ""),
        ).run()
        return
    store = WorkbenchStore(Path(os.environ["AGENT_MAIL_HOME"]))
    server = build_server(
        store,
        os.environ["AGENT_MAIL_EMPLOYEE"],
        os.environ["AGENT_MAIL_PROJECT"],
        os.environ["AGENT_MAIL_PROJECT_TOKEN"],
        os.environ.get("AGENT_MAIL_WORKBENCH_URL", ""),
        os.environ.get("AGENT_MAIL_TASK", ""),
        os.environ.get("AGENT_MAIL_RUN", ""),
    )
    server.run()


def build_remote_server(client, employee_id, project_id, task_id="", run_id=""):
    server = MCPServer(
        "agent-mailbox-project",
        instructions="You are a paired employee working in your mapped local project. Ordinary messages do not trigger work; team_message explicitly queues bounded work. Subagents report to you; do not share project tool credentials. Attribution identifies your session, not independently verified internal authorship. Human acceptance remains separate.",
    )

    def call(tool, args):
        return client.project_tool(
            project_id, employee_id, tool, args, task_id=task_id, run_id=run_id
        )

    @server.tool()
    def project_context() -> dict:
        """Read this project's shared context."""
        return call("context", {})

    @server.tool()
    def project_memory_search(query: str) -> dict:
        """Search shared project notes."""
        return call("memory_search", {"query": query})

    @server.tool()
    def project_resource_read(resource_id: str) -> dict:
        """Read a registered project resource."""
        return call("resource_read", {"resource_id": resource_id})

    @server.tool()
    def project_code_search(query: str) -> dict:
        """Search the coordinator's authorized project index; freshness is explicit."""
        try:
            return call("code_search", {"query": query})
        except WorkbenchError as exc:
            raise ToolError(f"{exc.code}: {exc}") from None

    @server.tool()
    def project_note(title: str, body: str) -> dict:
        """Propose a project note attributed to this employee."""
        return call("note", {"title": title, "body": body})

    @server.tool()
    def project_messages(folder: str = "inbox", limit: int = 100) -> dict:
        """Read my inbox, sent mail or project group without ACK or triggering work."""
        return call("messages", {"folder": folder, "limit": limit})

    @server.tool()
    def project_message(
        title: str, body: str, recipient_id: str = "", reply_to: str = "", request_id: str = ""
    ) -> dict:
        """Send or reply to a project message without starting work."""
        return call(
            "message",
            {
                "title": title,
                "body": body,
                "recipient_id": recipient_id or None,
                "reply_to": reply_to or None,
                "request_id": request_id or None,
            },
        )

    @server.tool()
    def team_message(recipient_id: str, title: str, message: str, request_id: str = "") -> dict:
        """Queue read-only work for a colleague on this project."""
        return call(
            "team_message",
            {
                "recipient_id": recipient_id,
                "title": title,
                "message": message,
                "request_id": request_id or None,
            },
        )

    return server


if __name__ == "__main__":
    main()
