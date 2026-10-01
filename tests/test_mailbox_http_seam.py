"""Two existing sessions exchange project mail through the real HTTP/MCP seam."""

import asyncio
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_mailbox.workbench import WorkbenchHTTP
from agent_mailbox.workbench_private import private_access
from agent_mailbox.workbench_store import WorkbenchStore


def test_owner_export_and_two_existing_sessions(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Mail team", tmp_path)
    alice = store.create_employee("ZCode", "zcode", project["id"])
    bob = store.create_employee("DeepSeek", "deepseek", project["id"])
    file = tmp_path / "PRD.md"
    file.write_text("Approved project instructions")
    resource = store.add_resource(project["id"], "PRD", "prd", file)
    store.capture_resource_version(project["id"], resource["id"], "Approved baseline")
    server = WorkbenchHTTP(store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path, data=None, token=None, method=None):
        req = urllib.request.Request(
            server.endpoint + "/api/workbench/" + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={
                "Authorization": "Bearer " + (token or server.token),
                "Content-Type": "application/json",
            },
            method=method,
        )
        return json.load(urllib.request.urlopen(req, timeout=5))

    try:
        exports = [
            request(
                "mailbox/sessions",
                {"employee_id": e["id"], "project_id": project["id"], "label": e["name"]},
            )
            for e in (alice, bob)
        ]
        paths = [e["config"]["mcpServers"]["agent-mailbox-project"]["args"][-1] for e in exports]
        assert all(private_access(path, 0o600) for path in paths)
        manifests = [json.loads(Path(path).read_text()) for path in paths]
        assert all(m["token"] not in json.dumps(exports) for m in manifests)
        try:
            request("bootstrap", token=manifests[0]["token"])
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("Employee capability gained administrator access")

        def params(path):
            return StdioServerParameters(
                command=sys.executable,
                args=["-m", "agent_mailbox", "mailbox-mcp", "--session-file", path],
                env=dict(os.environ),
            )

        async def exchange():
            async with (
                stdio_client(params(paths[0])) as (ar, aw),
                ClientSession(ar, aw) as a,
                stdio_client(params(paths[1])) as (br, bw),
                ClientSession(br, bw) as b,
            ):
                await a.initialize()
                await b.initialize()
                assert not (await a.call_tool("project_context", {})).is_error
                read = await b.call_tool("project_resource_read", {"resource_id": resource["id"]})
                assert not read.is_error and "Approved project instructions" in json.dumps(
                    read.model_dump()
                )
                send = await a.call_tool(
                    "project_message",
                    {
                        "title": "Review request",
                        "body": "Please review the PRD",
                        "recipient_id": bob["id"],
                        "request_id": "mail-1",
                    },
                )
                assert not send.is_error
                message = json.loads(send.content[0].text)
                assert message["source_session_id"] == exports[0]["id"]
                assert message["source_task_id"] is None
                inbox = await b.call_tool("project_messages", {})
                assert message["id"] in json.dumps(inbox.model_dump())
                reply = await b.call_tool(
                    "project_message",
                    {
                        "title": "Review reply",
                        "body": "Needs clearer upgrade steps",
                        "recipient_id": alice["id"],
                        "reply_to": message["id"],
                        "request_id": "mail-2",
                    },
                )
                assert not reply.is_error
                assert "Needs clearer upgrade steps" in json.dumps(
                    (await a.call_tool("project_messages", {})).model_dump()
                )
                assert store.snapshot()["tasks"] == []
                request("mailbox/sessions/" + exports[0]["id"], method="DELETE")
                assert (await a.call_tool("project_context", {})).is_error
                assert not (await b.call_tool("project_context", {})).is_error

        asyncio.run(exchange())
    finally:
        server.shutdown()
        thread.join(5)
        server.close()
