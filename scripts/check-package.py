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
                    state["version"] == __version__ and not state["tasks"] and not state["projects"]
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
                assert len(tools.tools) == 11
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
        "frozen_mcp_tools": 11,
        "context_note_probe_verified": True,
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
