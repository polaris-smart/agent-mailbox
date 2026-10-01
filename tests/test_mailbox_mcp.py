"""Existing-session MCP: real local HTTP and stdio, with no providers or SQLite."""

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.mcpserver.exceptions import ToolError

from agent_mailbox.mailbox_mcp import build_server
from agent_mailbox.workbench_private import private_mode


@pytest.fixture
def connection(tmp_path):
    (tmp_path / "workbench").mkdir()
    manifest = tmp_path / "session.json"
    manifest.write_text(
        json.dumps(
            {
                "token": "employee-secret",
                "home": str(tmp_path),
                "employee_id": "alice",
                "project_id": "project",
                "session_id": "session",
            }
        )
    )
    private_mode(manifest, 0o600)
    servers = []
    calls = []
    state = {"status": 200}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, self.headers.get("Authorization"), data))
            self.send_response(state["status"])
            if state["status"] == 302:
                self.send_header("Location", state["redirect"])
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    state.get("response", {"tool": data["tool"], "args": data["args"]})
                ).encode()
            )

    class LoopbackServer(ThreadingHTTPServer):
        def server_bind(self):
            # A literal loopback test listener does not need reverse DNS.
            TCPServer.server_bind(self)
            self.server_name, self.server_port = self.server_address[:2]

    def start():
        server = LoopbackServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        instance = tmp_path / "workbench/instance.json"
        instance.write_text(
            json.dumps(
                {"endpoint": f"http://127.0.0.1:{server.server_port}", "token": "ADMIN-NEVER-USE"}
            )
        )
        private_mode(instance, 0o600)
        return server

    start()
    yield manifest, calls, state, start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_real_stdio_port_change_duplicate_request_and_revocation(connection, monkeypatch):
    manifest, calls, state, start = connection
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")

    async def client():
        async with (
            stdio_client(
                StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "agent_mailbox.mailbox_mcp", "--session-file", str(manifest)],
                    env=dict(os.environ),
                )
            ) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = (await session.list_tools()).tools
            assert {tool.name for tool in tools} == {
                "project_context",
                "project_messages",
                "project_message",
                "project_note",
                "project_memory_search",
                "project_resource_read",
                "project_resource_versions",
                "project_resource_propose",
                "project_delivery",
                "project_tasks",
                "project_task_accept",
                "project_task_submit",
            }
            assert all(
                "employee_id" not in tool.input_schema.get("properties", {}) for tool in tools
            )
            proposal = next(tool for tool in tools if tool.name == "project_resource_propose")
            assert "content" in proposal.input_schema["required"]
            assert proposal.input_schema["properties"]["content"]["type"] == "string"
            assert not (await session.call_tool("project_context", {})).is_error
            start()  # Same installed connection follows a new workbench port.
            assert not (await session.call_tool("project_messages", {})).is_error
            args = {"title": "review", "body": "hello", "recipient_id": "bob", "request_id": "same"}
            for _ in range(2):
                assert not (await session.call_tool("project_message", args)).is_error
            state["status"] = 401
            result = await session.call_tool("project_context", {})
            assert result.is_error
            serialized = json.dumps(result.model_dump())
            assert "UNAUTHORIZED" in serialized
            assert "employee-secret" not in serialized and "ADMIN-NEVER-USE" not in serialized
            state["status"] = 302
            state["redirect"] = f"http://127.0.0.1:{start().server_port}/stolen"
            before = len(calls)
            result = await session.call_tool("project_context", {})
            assert result.is_error
            assert len(calls) == before + 1  # No authenticated redirect request.

    asyncio.run(client())
    assert all(path == "/api/workbench/mailbox/tools" for path, _, _ in calls)
    assert all(auth == "Bearer employee-secret" for _, auth, _ in calls)
    messages = [body for _, _, body in calls if body["tool"] == "message"]
    assert len(messages) == 2 and messages[0] == messages[1]
    assert messages[0]["args"]["request_id"] == "same"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:1234",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:80/",
        "http://user@127.0.0.1:80",
        "https://127.0.0.1:80",
        "http://127.0.0.1:80?token=secret",
    ],
)
def test_reject_untrusted_endpoint(connection, endpoint):
    manifest, calls, _, _ = connection
    instance = manifest.parent / "workbench/instance.json"
    instance.write_text(json.dumps({"endpoint": endpoint}))
    server = build_server(manifest)
    with pytest.raises(ToolError, match="MAILBOX_CONFIG_INVALID"):
        asyncio.run(server.call_tool("project_context", {}))
    assert not calls


def test_private_manifest_and_instance_required(connection):
    manifest, calls, _, _ = connection
    if os.name != "nt":
        manifest.chmod(0o644)
        with pytest.raises(ToolError, match="MAILBOX_CONFIG_INVALID"):
            build_server(manifest)
        private_mode(manifest, 0o600)
        server = build_server(manifest)
        (manifest.parent / "workbench/instance.json").chmod(0o644)
        with pytest.raises(ToolError, match="MAILBOX_CONFIG_INVALID"):
            asyncio.run(server.call_tool("project_context", {}))
        assert not calls


def test_no_database_dependency_and_resource_default(connection):
    manifest, calls, _, _ = connection
    server = build_server(manifest)
    result = asyncio.run(server.call_tool("project_resource_read", {"resource_id": "prd"}))
    assert "resource_read" in str(result)
    assert calls[-1][2]["args"] == {"resource_id": "prd", "version_id": ""}
    assert not list(manifest.parent.rglob("*.sqlite*"))


def test_manifest_validation_never_echoes_secret(connection):
    manifest, _, _, _ = connection
    manifest.write_text(
        json.dumps(
            {
                "token": "private-secret\nheader",
                "home": str(manifest.parent),
                "employee_id": "alice",
                "project_id": "project",
                "session_id": "session",
            }
        )
    )
    with pytest.raises(ToolError) as caught:
        build_server(manifest)
    assert "private-secret" not in str(caught.value)
    manifest.write_text("{invalid private-secret")
    with pytest.raises(ToolError) as caught:
        build_server(manifest)
    assert "private-secret" not in str(caught.value)


def test_private_file_checks_fail_closed_on_all_platforms(connection, monkeypatch):
    from agent_mailbox import mailbox_mcp

    manifest, _, _, _ = connection
    monkeypatch.setattr(mailbox_mcp, "private_access", lambda *_args: False)
    with pytest.raises(ToolError, match="MAILBOX_CONFIG_INVALID"):
        build_server(manifest)


def test_delivery_contract_and_safe_known_error(connection):
    manifest, calls, state, _ = connection
    server = build_server(manifest)
    asyncio.run(server.call_tool("project_delivery", {"task_id": "task-1"}))
    assert calls[-1][2] == {"tool": "delivery", "args": {"target_task_id": "task-1"}}
    state["status"] = 400
    state["response"] = {"error": {"code": "RESOURCE_NOT_APPROVED", "message": "employee-secret"}}
    with pytest.raises(ToolError, match="RESOURCE_NOT_APPROVED") as caught:
        asyncio.run(server.call_tool("project_resource_read", {"resource_id": "prd"}))
    assert "employee-secret" not in str(caught.value)


def test_live_resource_denied_without_http(connection):
    manifest, calls, _, _ = connection
    server = build_server(manifest)
    with pytest.raises(ToolError, match="MAILBOX_ARGUMENT_DENIED"):
        asyncio.run(server.call_tool("project_resource_read", {"resource_id": "prd", "live": True}))
    assert not calls
