"""Use a real stdio MCP client against membership-scoped project tools."""

import asyncio
import json
import os
import sys
import threading
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_mailbox.workbench import WorkbenchHTTP
from agent_mailbox.workbench_engine import WorkbenchEngine
from agent_mailbox.workbench_store import WorkbenchStore


def test_project_mcp_context_message_wakes_colleague(tmp_path):
    store = WorkbenchStore(tmp_path / "state")
    project = store.create_project("Project One", str(tmp_path))
    other = store.create_project("Project Two", str(tmp_path / "state"))
    alice = store.create_employee("Alice", "codex", project["id"])
    bob = store.create_employee("Bob", "codex", project["id"])
    store.add_memory(project["id"], "Shared marker", "only-project-one")
    store.add_memory(other["id"], "Private marker", "only-project-two")
    bridge = tmp_path / "fake.py"
    bridge.write_text(
        "import sys,json\nr=json.loads(sys.stdin.readline())\nfor t,v in [('started',{}),('result',{'status':'completed','output_text':'Colleague responded'})]:\n print(json.dumps(dict(protocol=1,run_id=r['run_id'],session_id=r['session_id'],type=t,**v)),flush=True)\njson.loads(sys.stdin.readline())\n"
    )
    engine = WorkbenchEngine(store, [sys.executable, str(bridge)])
    server = WorkbenchHTTP(store, engine=engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    engine.start()
    source = store.create_task(project["id"], "Source", "Ask a colleague", alice["id"])
    source = store.claim_task(store.local_node()["id"])
    creds = store.execution_credentials(source["id"])
    env = {
        **os.environ,
        "AGENT_MAIL_HOME": str(store.root),
        "AGENT_MAIL_EMPLOYEE": alice["id"],
        "AGENT_MAIL_PROJECT": project["id"],
        "AGENT_MAIL_PROJECT_TOKEN": creds["token"],
        "AGENT_MAIL_WORKBENCH_URL": server.endpoint,
        "AGENT_MAIL_TASK": source["id"],
        "AGENT_MAIL_RUN": source["run_id"],
    }

    async def client():
        async with (
            stdio_client(
                StdioServerParameters(
                    command=sys.executable, args=["-m", "agent_mailbox.workspace_mcp"], env=env
                )
            ) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {
                "project_context",
                "project_memory_search",
                "project_resource_read",
                "project_note",
                "team_message",
                "project_message",
                "project_messages",
            }
            context = await session.call_tool("project_context", {})
            assert not context.is_error
            serialized = json.dumps(context.model_dump())
            assert "only-project-one" in serialized
            assert bob["id"] in serialized
            assert "only-project-two" not in serialized
            assert creds["token"] not in serialized
            note = await session.call_tool(
                "project_note", {"title": "Proposed", "body": "Agent observation"}
            )
            assert not note.is_error
            ordinary = await session.call_tool(
                "project_message",
                {
                    "title": "Progress",
                    "body": "Reading shared material",
                    "request_id": "ordinary-1",
                },
            )
            assert not ordinary.is_error
            assert len(store.snapshot()["tasks"]) == 1
            result = await session.call_tool(
                "team_message",
                {
                    "recipient_id": bob["id"],
                    "title": "Help",
                    "message": "Review shared project",
                    "request_id": "help-1",
                },
            )
            assert not result.is_error
            replay = await session.call_tool(
                "team_message",
                {
                    "recipient_id": bob["id"],
                    "title": "Help",
                    "message": "Review shared project",
                    "request_id": "help-1",
                },
            )
            assert not replay.is_error
            assert len(store.snapshot()["tasks"]) == 2
            messages = await session.call_tool("project_messages", {})
            assert not messages.is_error
            assert "employee_session" in json.dumps(messages.model_dump())
            # Cross-project recipient membership is validated by the domain store.
            outsider = store.create_employee("Outside", "codex", other["id"])
            refused = await session.call_tool(
                "team_message",
                {"recipient_id": outsider["id"], "title": "Leak", "message": "Private"},
            )
            assert refused.is_error

    try:
        asyncio.run(client())
        end = time.monotonic() + 5
        while time.monotonic() < end:
            tasks = [t for t in store.snapshot()["tasks"] if t["assignee_id"] == bob["id"]]
            if tasks and tasks[0]["status"] == "review":
                break
            time.sleep(0.03)
        assert tasks[0]["status"] == "review"
        assert tasks[0]["result"] == "Colleague responded"
        assert store.snapshot()["memories"][-1]["source"].startswith("employee:") or any(
            m["source"].startswith("employee:") for m in store.snapshot()["memories"]
        )
    finally:
        store.finish_task(source["id"], "cancelled")
        assert not store.validate_execution(
            creds["token"], alice["id"], project["id"], source["id"], source["run_id"]
        )
        server.shutdown()
        server.close()
        thread.join(timeout=2)
