#!/usr/bin/env python3
"""Launch the extracted native archive and test HTTP/MCP without provider calls."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_mailbox import __version__
from agent_mailbox.workbench_onboarding import create_probe
from agent_mailbox.workbench_private import private_access
from agent_mailbox.workbench_store import WorkbenchStore


def check(manifest_path, output_path):
    manifest = json.loads(manifest_path.read_text())
    assert manifest["version"] == __version__
    app = Path(manifest["app"])
    archive_dir = manifest_path.parent / "archives"
    archive_dir.mkdir(exist_ok=True)
    platform = manifest["platform"]
    arch = manifest["architecture"].lower().replace("amd64", "x64").replace("x86_64", "x64")
    name = f"Agent-Mailbox-{__version__}-{platform}-{arch}"
    if platform == "darwin":
        archive = archive_dir / (name + ".zip")
        subprocess.run(["ditto", "-c", "-k", "--keepParent", str(app), str(archive)], check=True)
    elif platform == "win32":
        archive = Path(shutil.make_archive(str(archive_dir / name), "zip", app.parent, app.name))
    else:
        archive = archive_dir / (name + ".tar.gz")
        with tarfile.open(archive, "w:gz") as target:
            target.add(app, arcname=app.name)
    with tempfile.TemporaryDirectory(prefix="agent-mailbox-package-") as temporary:
        root = Path(temporary)
        unpack = root / "unpack"
        unpack.mkdir()
        if platform == "darwin":
            subprocess.run(["ditto", "-x", "-k", str(archive), str(unpack)], check=True)
        elif platform == "win32":
            with zipfile.ZipFile(archive) as source:
                assert source.testzip() is None
                source.extractall(unpack)
        else:
            with tarfile.open(archive) as source:
                source.extractall(unpack, filter="data")
        relative = Path(manifest["executable"]).relative_to(app)
        executable = unpack / app.name / relative
        home = root / "fresh-home"
        # Registered fixtures exercise protocol capabilities, not vendor discovery/authentication.
        fixture = WorkbenchStore(home)
        project_root = root / "project"
        project_root.mkdir()
        mail_project = fixture.create_project("Existing sessions fixture", project_root)
        mail_employees = [
            fixture.create_employee(
                "Fixture App", "zcode", mail_project["id"], connection_type="app"
            ),
            fixture.create_employee("Fixture CLI", "deepseek", mail_project["id"]),
        ]
        document = project_root / "PRD.md"
        document.write_bytes(b"Approved package fixture specification\n")
        resource = fixture.add_resource(mail_project["id"], "Fixture PRD", "prd", document)
        revision = fixture.capture_resource_version(mail_project["id"], resource["id"], "Fixture")
        fixture.approve_resource_version(mail_project["id"], resource["id"], revision["id"])
        with (root / "launch.log").open("wb") as log:
            process = subprocess.Popen(
                [str(executable), "--home", str(home), "--no-browser"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
            )
            try:
                instance = home / "workbench/instance.json"
                end = time.monotonic() + 40
                while not instance.exists() and time.monotonic() < end:
                    if process.poll() is not None:
                        raise RuntimeError("Packaged launcher exited before becoming ready")
                    time.sleep(0.1)
                owner = json.loads(instance.read_text())
                assert private_access(instance, 0o600)

                def api(route, data=None, token=None):
                    request = urllib.request.Request(
                        owner["endpoint"] + "/api/workbench/" + route,
                        data=json.dumps(data).encode() if data is not None else None,
                        headers={
                            "Authorization": "Bearer " + (token or owner["token"]),
                            "Content-Type": "application/json",
                        },
                    )
                    return json.load(urllib.request.urlopen(request, timeout=10))

                state = api("bootstrap")
                assert (
                    state["version"] == __version__
                    and not state["tasks"]
                    and len(state["projects"]) == 1
                    and len(state["employees"]) == 2
                )
                assert api("updates/status")["installation"]["kind"] == "app"
                try:
                    api("bootstrap", token="wrong-owner")
                except urllib.error.HTTPError as error:
                    assert error.code == 401
                else:
                    raise AssertionError("Packaged API accepted a wrong owner credential")
                for filename, marker in [("LICENSE.txt", "Version 2.0"), ("NOTICE.txt", "NoFox")]:
                    with urllib.request.urlopen(
                        owner["endpoint"] + "/workbench-assets/" + filename, timeout=10
                    ) as response:
                        assert marker in response.read().decode()
                api(
                    f"projects/{mail_project['id']}/members/{mail_employees[1]['id']}/role",
                    {"role": "Fixture reviewer"},
                )
                configurations = [
                    api(
                        "mailbox/sessions",
                        {
                            "project_id": mail_project["id"],
                            "employee_id": employee["id"],
                            "label": "Native package fixture",
                        },
                    )
                    for employee in mail_employees
                ]
                mail_task = api(
                    "mail-tasks",
                    {
                        "project_id": mail_project["id"],
                        "title": "Review fixture PRD",
                        "prompt": "Read approved PRD and report",
                        "assignee_id": mail_employees[1]["id"],
                    },
                )
                assert mail_task["status"] == "queued" and mail_task["execution_mode"] == "mailbox"
                for config in configurations:
                    server = config["config"]["mcpServers"]["agent-mailbox-project"]
                    assert Path(server["command"]).resolve() == executable.resolve()
                    assert server["args"][:2] == ["mailbox-mcp", "--session-file"]
                    assert private_access(Path(server["args"][2]), 0o600)
                    assert "token" not in config

                async def existing_sessions_mcp():
                    async def call(session, tool, arguments):
                        value = await session.call_tool(tool, arguments)
                        assert not value.is_error, f"Packaged mailbox tool failed: {tool}"
                        if isinstance(value.structured_content, dict):
                            return value.structured_content
                        return json.loads(
                            next(item.text for item in value.content if item.type == "text")
                        )

                    async def connect(config, action):
                        server = config["config"]["mcpServers"]["agent-mailbox-project"]
                        async with (
                            stdio_client(
                                StdioServerParameters(command=str(executable), args=server["args"])
                            ) as (reader, writer),
                            ClientSession(reader, writer) as session,
                        ):
                            await session.initialize()
                            tools = await session.list_tools()
                            assert len({tool.name for tool in tools.tools}) == len(tools.tools)
                            assert {
                                "project_tasks",
                                "project_task_accept",
                                "project_task_submit",
                            } <= {tool.name for tool in tools.tools}
                            return await action(session, call)

                    async def sender(session, call):
                        context = await call(session, "project_context", {})
                        assert context["project"]["id"] == mail_project["id"]
                        assert context["mailbox_policy"]["managed_execution"] is False
                        denial = await session.call_tool(
                            "project_task_accept", {"task_id": mail_task["id"]}
                        )
                        assert denial.is_error
                        assert fixture.get_task(mail_task["id"])["status"] == "queued"
                        return await call(
                            session,
                            "project_message",
                            {
                                "title": "Review handoff",
                                "body": "Please check approved PRD",
                                "recipient_id": mail_employees[1]["id"],
                                "request_id": "native-handoff",
                            },
                        )

                    outgoing = await connect(configurations[0], sender)
                    assert outgoing["source_session_id"] == configurations[0]["id"]
                    assert not outgoing["request_work"] and outgoing["task_id"] is None

                    async def recipient(session, call):
                        context = await call(session, "project_context", {})
                        reviewer = next(
                            employee
                            for employee in context["employees"]
                            if employee["id"] == mail_employees[1]["id"]
                        )
                        assert reviewer["project_role"] == "Fixture reviewer"
                        inbox = await call(session, "project_messages", {})
                        assert not inbox["viewing_acknowledges"]
                        assert outgoing["id"] in {message["id"] for message in inbox["messages"]}
                        approved = await call(
                            session, "project_resource_read", {"resource_id": resource["id"]}
                        )
                        assert approved["content"] == "Approved package fixture specification\n"
                        reply = await call(
                            session,
                            "project_message",
                            {
                                "title": "Reviewed",
                                "body": "Read approved revision",
                                "recipient_id": mail_employees[0]["id"],
                                "reply_to": outgoing["id"],
                            },
                        )
                        assert reply["source_session_id"] == configurations[1]["id"]
                        assert reply["thread_id"] == outgoing["thread_id"]
                        assigned = await call(session, "project_tasks", {})
                        assert assigned["tasks"][0]["status"] == "queued"
                        assert fixture.get_task(mail_task["id"])["status"] == "queued"
                        accepted = await call(
                            session, "project_task_accept", {"task_id": mail_task["id"]}
                        )
                        assert accepted["status"] == "running" and not accepted["result"]
                        submitted = await call(
                            session,
                            "project_task_submit",
                            {
                                "task_id": mail_task["id"],
                                "result": "Approved specification reviewed by existing session",
                            },
                        )
                        assert submitted["status"] == "review"

                    await connect(configurations[1], recipient)

                async def bounded_mailbox_check():
                    await asyncio.wait_for(existing_sessions_mcp(), timeout=30)

                asyncio.run(bounded_mailbox_check())
                assert fixture.get_task(mail_task["id"])["status"] == "review"
                assert fixture.claim_task(fixture.local_node()["id"]) is None
                assert (
                    api("tasks/" + mail_task["id"] + "/review", {"decision": "accept"})["status"]
                    == "done"
                )
                assert not any(
                    employee["execution_verified"] for employee in api("bootstrap")["employees"]
                )
                api("application/quit", {})
                process.wait(timeout=15)
                assert process.returncode == 0
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=15)
        store = WorkbenchStore(root / "mcp-home")
        project = store.create_project("Package fixture", root)
        employee = store.create_employee("Fixture", "codex", project["id"])
        create_probe(store, employee["id"], project["id"])
        task = store.claim_task(store.local_node()["id"])
        store.set_status(task["id"], "running")
        credentials = store.execution_credentials(task["id"])
        with store._connection() as db:
            marker = db.execute(
                "SELECT marker FROM employee_probes WHERE task_id=?", (task["id"],)
            ).fetchone()[0]
        env = {
            **os.environ,
            "AGENT_MAIL_HOME": str(store.root),
            "AGENT_MAIL_EMPLOYEE": employee["id"],
            "AGENT_MAIL_PROJECT": project["id"],
            "AGENT_MAIL_PROJECT_TOKEN": credentials["token"],
            "AGENT_MAIL_TASK": task["id"],
            "AGENT_MAIL_RUN": task["run_id"],
        }

        async def mcp():
            async with (
                stdio_client(
                    StdioServerParameters(
                        command=str(executable), args=["--workspace-mcp"], env=env
                    )
                ) as (reader, writer),
                ClientSession(reader, writer) as session,
            ):
                await session.initialize()
                tools = await session.list_tools()
                assert len({tool.name for tool in tools.tools}) == len(tools.tools)
                assert {"project_context", "project_note", "project_delivery"} <= {
                    tool.name for tool in tools.tools
                }
                for name, body in [
                    ("project_context", {}),
                    ("project_note", {"title": "Agent connectivity verification", "body": marker}),
                    ("project_delivery", {"task_id": task["id"]}),
                ]:
                    assert not (await session.call_tool(name, body)).is_error

        asyncio.run(mcp())
        store.finish_task(task["id"], "review", "Real packaged tool receipts checked")
        assert store.snapshot()["employees"][0]["execution_verified"]
    result = {
        "version": __version__,
        "platform": platform,
        "architecture": arch,
        "archive": str(archive),
        "archive_extracted_and_launched": True,
        "owner_authentication": True,
        "private_permissions": True,
        "frozen_required_mcp_tools_verified": True,
        "context_note_probe_verified": True,
        "existing_session_required_mcp_tools_verified": True,
        "existing_session_fixture_employees": 2,
        "approved_resource_read": True,
        "employee_mail_exchange_session_attributed": True,
        "mail_task_explicit_accept_submit": True,
        "mail_task_human_review_separate": True,
        "mail_task_wrong_assignee_denied": True,
        "project_responsibility_visible": True,
        "mail_task_not_claimed_by_execution_engine": True,
        "clean_exit": True,
        "provider_calls": 0,
        "gatekeeper_or_smartscreen_manual_tested": False,
    }
    output_path.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    check(args.manifest, args.output)
