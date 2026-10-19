#!/usr/bin/env python3
"""Launch the extracted native archive and test HTTP/MCP without provider calls."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import stat
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


def _verify_package_notices(unpacked_app: Path, manifest: dict) -> int:
    """Verify the extracted distribution contains every declared text notice."""
    resources = (
        unpacked_app / "Contents/Resources" if manifest["platform"] == "darwin"
        else unpacked_app / "_internal"
    )
    root = resources / "licenses/python-dependencies"
    inventory_file = root / "inventory.json"
    if not inventory_file.is_file():
        raise RuntimeError("package notice inventory missing")
    expected = manifest.get("python_dependency_notice_inventory")
    actual = json.loads(inventory_file.read_text(encoding="utf-8"))
    if not isinstance(expected, list) or not expected or actual != expected:
        raise RuntimeError("package notice inventory mismatch")
    count = 0
    resolved_root = root.resolve()
    for distribution in expected:
        paths = distribution.get("notices")
        if not isinstance(paths, list) or not paths:
            raise RuntimeError("package notice inventory has an empty distribution")
        for name in paths:
            relative = Path(name)
            path = (root / relative).resolve()
            if relative.is_absolute() or not path.is_relative_to(resolved_root) or not path.is_file():
                raise RuntimeError(f"package notice file missing: {name}")
            with path.open("rb") as stream:
                sample = stream.read(8192)
            if not sample or b"\0" in sample or sample.startswith(
                (b"\x7fELF", b"MZ", b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
                 b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe")
            ):
                raise RuntimeError(f"package contains binary notice: {name}")
            count += 1
    return count


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
        subprocess.run(
            ["ditto", "-c", "-k", "--norsrc", "--keepParent", str(app), str(archive)],
            check=True,
        )
        with zipfile.ZipFile(archive) as source:
            assert source.testzip() is None
            assert not any(
                "__MACOSX" in Path(name).parts or Path(name).name.startswith("._")
                for name in source.namelist()
            ), "The macOS archive contains AppleDouble metadata files"
    elif platform == "win32":
        archive = Path(shutil.make_archive(str(archive_dir / name), "zip", app.parent, app.name))
    else:
        archive = archive_dir / (name + ".tar.gz")
        with tarfile.open(archive, "w:gz") as target:
            target.add(app, arcname=app.name)
    # Unix-domain sockets on macOS have a short pathname limit. Keep the
    # receiver's isolated HOME short even when the system TMPDIR is deep.
    with (tempfile.TemporaryDirectory(prefix="agent-mailbox-package-") as temporary,
          tempfile.TemporaryDirectory(prefix="amh-", dir="/private/tmp" if platform == "darwin" else None)
          as host_home_name):
        root = Path(temporary)
        unpack = root / "unpack"
        unpack.mkdir()
        if platform == "darwin":
            subprocess.run(["ditto", "-x", "-k", str(archive), str(unpack)], check=True)
            verified = subprocess.run(
                ["codesign", "--verify", "--deep", "--strict", str(unpack / app.name)],
                capture_output=True, text=True, check=False,
            )
            if verified.returncode:
                raise RuntimeError(
                    "Extracted macOS App has an invalid signature: "
                    + verified.stderr.strip()[:400]
                )
        elif platform == "win32":
            with zipfile.ZipFile(archive) as source:
                assert source.testzip() is None
                source.extractall(unpack)
        else:
            with tarfile.open(archive) as source:
                source.extractall(unpack, filter="data")
        notice_count = _verify_package_notices(unpack / app.name, manifest)
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
            # The receiver IPC root follows Path.home(), independently of
            # --home. Keep package probes out of the operator's real HOME.
            host_home = Path(host_home_name)
            host_home.chmod(0o700)
            process = subprocess.Popen(
                [str(executable), "--home", str(home), "--no-browser"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                env={**os.environ, "HOME": str(host_home)},
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
                if platform == "darwin":
                    key = hashlib.sha256(os.fsencode(home.resolve())).hexdigest()[:16]
                    activity_socket = host_home / ".agent-mailbox-hosts" / key / "idle.sock"
                    end = time.monotonic() + 10
                    while not activity_socket.exists() and time.monotonic() < end:
                        if process.poll() is not None:
                            raise RuntimeError("Packaged host-activity listener exited")
                        time.sleep(0.05)
                    info = activity_socket.lstat()
                    assert stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid()
                    assert not info.st_mode & 0o077, "Packaged host-activity socket is not private"

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

                # instance.json is written while WorkbenchHTTP is still restoring
                # its connections. On slower hosts (notably Windows CI), the
                # listener may not have entered serve_forever yet.
                ready_deadline = time.monotonic() + 30
                while True:
                    try:
                        state = api("bootstrap")
                        break
                    except urllib.error.HTTPError:
                        raise
                    except urllib.error.URLError as exc:
                        exit_code = process.poll()
                        if exit_code is not None or time.monotonic() >= ready_deadline:
                            launch_tail = "\n".join(
                                (root / "launch.log")
                                .read_text(encoding="utf-8", errors="replace")
                                .splitlines()[-24:]
                            )
                            raise RuntimeError(
                                "Packaged API did not become ready "
                                f"(exit_code={exit_code}, error={type(exc.reason).__name__})\n"
                                f"Packaged launcher output:\n{launch_tail}"
                            ) from exc
                        time.sleep(0.1)
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
                # This must exercise the extracted package, not the source tree:
                # a previous installed App predates the Human portal assets.
                for route, marker, content_type in (
                    ("/human", b'id="enroll-form"', "text/html"),
                    ("/workbench-assets/human.css", b".entry", "text/css"),
                    ("/workbench-assets/human.js", b"enroll-form", "text/javascript"),
                ):
                    with urllib.request.urlopen(owner["endpoint"] + route, timeout=10) as response:
                        assert response.status == 200
                        assert content_type in response.headers["Content-Type"]
                        assert marker in response.read(), f"Missing packaged Human asset: {route}"
                forwarded = api("human-forward/start", {})
                assert forwarded["enabled"] and 0 < forwarded["port"] <= 65535
                human_endpoint = f"http://127.0.0.1:{forwarded['port']}"
                try:
                    with urllib.request.urlopen(human_endpoint + "/human", timeout=10) as response:
                        assert response.status == 200 and b'id="enroll-form"' in response.read()
                    try:
                        urllib.request.urlopen(
                            urllib.request.Request(
                                human_endpoint + "/api/workbench/bootstrap",
                                headers={"Authorization": "Bearer " + owner["token"]},
                            ),
                            timeout=10,
                        )
                    except urllib.error.HTTPError as error:
                        assert error.code == 404
                    else:
                        raise AssertionError("Human forward exposed the owner API")
                finally:
                    assert api("human-forward/stop", {}) == {"stopped": True}
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
                                "project_message_open",
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
                        for message_id in (outgoing["id"], mail_task["request_message_id"]):
                            opened = await call(
                                session, "project_message_open", {"message_id": message_id}
                            )
                            assert opened["accepts_task"] is False
                            assert opened["message"]["read_receipt"]["reader_id"] == mail_employees[1]["id"]
                        with fixture._transaction(readonly=True) as db:
                            for message_id in (outgoing["id"], mail_task["request_message_id"]):
                                alert = db.execute(
                                    "SELECT status,attempts FROM notifications "
                                    "WHERE kind='mailbox_wake' AND state_hash=?",
                                    (message_id,),
                                ).fetchone()
                                assert (alert["status"], alert["attempts"]) == (
                                    "recipient_opened", 0,
                                )
                        assert fixture.get_task(mail_task["id"])["status"] == "queued"
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

                    cancelled_task = api(
                        "mail-tasks",
                        {
                            "project_id": mail_project["id"],
                            "title": "Cancellation fixture",
                            "prompt": "Open this request, then wait for cancellation",
                            "assignee_id": mail_employees[1]["id"],
                        },
                    )

                    async def open_before_cancel(session, call):
                        opened = await call(
                            session,
                            "project_message_open",
                            {"message_id": cancelled_task["request_message_id"]},
                        )
                        assert opened["accepts_task"] is False

                    await connect(configurations[1], open_before_cancel)
                    assert api("tasks/" + cancelled_task["id"] + "/cancel", {})["status"] == "cancelled"

                    async def receive_cancel(session, call):
                        inbox = await call(session, "project_messages", {})
                        notices = [
                            message for message in inbox["messages"]
                            if message["request_id"] == "mailbox_cancel:" + cancelled_task["id"]
                        ]
                        assert len(notices) == 1
                        opened = await call(
                            session, "project_message_open", {"message_id": notices[0]["id"]}
                        )
                        assert opened["accepts_task"] is False
                        assert opened["message"]["read_receipt"]["reader_id"] == mail_employees[1]["id"]

                    await connect(configurations[1], receive_cancel)
                    assert fixture.get_task(cancelled_task["id"])["status"] == "cancelled"

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
        "python_dependency_notice_files_verified": notice_count,
        "owner_authentication": True,
        "human_assets_http_verified": True,
        "human_forward_owner_surface_denied": True,
        "private_permissions": True,
        "frozen_required_mcp_tools_verified": True,
        "context_note_probe_verified": True,
        "existing_session_required_mcp_tools_verified": True,
        "existing_session_fixture_employees": 2,
        "approved_resource_read": True,
        "employee_mail_exchange_session_attributed": True,
        "mail_task_explicit_accept_submit": True,
        "mail_task_cancel_after_open_notice": True,
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
