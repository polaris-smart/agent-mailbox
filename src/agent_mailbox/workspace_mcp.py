"""Project-scoped tools injected into managed employees; no user MCP setup."""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .workbench_store import WorkbenchError, WorkbenchStore


def build_server(store, employee_id, project_id, token, endpoint=""):
    def guard():
        if not store.validate_employee(token, employee_id, project_id):
            raise WorkbenchError("UNAUTHORIZED", "This employee has no access to this project.")

    guard()
    server = MCPServer(
        "agent-mailbox-project",
        instructions="Use project_context to understand your assigned project. Team messages become queued tasks for a selected colleague. Delivery is not completion. Notes are proposals, not approved PRD changes.",
    )

    @server.tool()
    def project_context() -> dict:
        """Read this project's shared resources, memory and employee membership."""
        guard()
        return store.project_context(project_id)

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
    def project_note(title: str, body: str) -> dict:
        """Record a project note attributed to this employee; human decisions stay separate."""
        guard()
        return store.add_memory(project_id, title, body, source=f"employee:{employee_id}")

    @server.tool()
    def team_message(recipient_id: str, title: str, message: str) -> dict:
        """Queue work for another employee in this project. A human reviews the result."""
        guard()
        if recipient_id == employee_id:
            raise WorkbenchError("SELF_MESSAGE", "Choose another employee.")
        if not endpoint:
            raise WorkbenchError(
                "WAKE_UNAVAILABLE", "Open the project workbench before sending team work."
            )
        task = store.create_task(project_id, title, message, recipient_id, "read-only")
        store.add_event(
            task["id"], "team_message", "A colleague requested this work.", {"from_id": employee_id}
        )
        body = json.dumps({"employee_id": employee_id, "project_id": project_id}).encode()
        request = urllib.request.Request(
            endpoint + "/api/workbench/notify",
            data=body,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                response.read()
        except OSError:
            store.add_event(
                task["id"],
                "wake_unconfirmed",
                "The workbench did not acknowledge this queued request. It remains saved for recovery.",
                {"code": "WAKE_UNAVAILABLE"},
            )
            return {
                "task_id": task["id"],
                "status": "delivery_unconfirmed",
                "error": {
                    "code": "WAKE_UNAVAILABLE",
                    "message": "The request is saved, but employee triggering is unconfirmed.",
                },
            }
        return {"task_id": task["id"], "status": "queued"}

    return server


def main():
    if os.environ.get("AGENT_MAIL_FLEET") == "1":
        from .workbench_fleet import FleetClient

        root = Path(os.environ["AGENT_MAIL_HOME"])
        saved = json.loads((root / "workbench/fleet/client.json").read_text())
        client = FleetClient(root, saved)
        build_remote_server(
            client, os.environ["AGENT_MAIL_EMPLOYEE"], os.environ["AGENT_MAIL_PROJECT"]
        ).run()
        return
    store = WorkbenchStore(Path(os.environ["AGENT_MAIL_HOME"]))
    server = build_server(
        store,
        os.environ["AGENT_MAIL_EMPLOYEE"],
        os.environ["AGENT_MAIL_PROJECT"],
        os.environ["AGENT_MAIL_PROJECT_TOKEN"],
        os.environ.get("AGENT_MAIL_WORKBENCH_URL", ""),
    )
    server.run()


def build_remote_server(client, employee_id, project_id):
    server = MCPServer(
        "agent-mailbox-project",
        instructions="You are a paired employee working in your mapped local project. Shared notes and resources come from the coordinator. Messages queue work for colleagues; human acceptance remains separate.",
    )

    def call(tool, args):
        return client.project_tool(project_id, employee_id, tool, args)

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
    def project_note(title: str, body: str) -> dict:
        """Propose a project note attributed to this employee."""
        return call("note", {"title": title, "body": body})

    @server.tool()
    def team_message(recipient_id: str, title: str, message: str) -> dict:
        """Queue read-only work for a colleague on this project."""
        return call(
            "team_message", {"recipient_id": recipient_id, "title": title, "message": message}
        )

    return server


if __name__ == "__main__":
    main()
