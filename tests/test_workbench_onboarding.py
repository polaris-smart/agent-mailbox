"""A probe is verified by actual project calls, not an employee's success text."""

import asyncio
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_mailbox.workbench_onboarding import create_probe, onboarding_status, record_probe_step
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


@pytest.fixture
def probe_fixture(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Project", tmp_path)
    employee = store.create_employee("Codex", "codex", project["id"])
    return store, project, employee


def start(store, project, employee):
    create_probe(store, employee["id"], project["id"])
    assert create_probe(store, employee["id"], project["id"])["reused"]
    task = store.claim_task(store.local_node()["id"])
    store.set_status(task["id"], "running")
    return task


def marker(store, task):
    with store._connection() as db:
        return db.execute(
            "SELECT marker FROM employee_probes WHERE task_id=?", (task["id"],)
        ).fetchone()[0]


def test_no_probe_or_plain_success_is_not_verification(probe_fixture, monkeypatch):
    store, project, employee = probe_fixture
    monkeypatch.setattr(
        "agent_mailbox.workbench_onboarding.discover_employees",
        lambda: [{"kind": "codex", "connection_type": "cli", "auth_status": "authenticated"}],
    )
    monkeypatch.setattr(
        "agent_mailbox.workbench_onboarding.runtime_status",
        lambda root: {"installed": True, "node_available": True},
    )
    result = onboarding_status(store, employee["id"], project["id"])
    assert result["can_verify"] and result["probe"] is None
    assert store.snapshot()["tasks"] == []
    task = start(store, project, employee)
    store.finish_task(task["id"], "review", "I verified everything")
    assert store.get_task(task["id"])["error"]["code"] == "PROBE_INCOMPLETE"
    assert not store.snapshot()["employees"][0]["execution_verified"]


def test_order_marker_and_cancel_guards(probe_fixture):
    store, project, employee = probe_fixture
    task = start(store, project, employee)
    token = marker(store, task)
    record_probe_step(store, task["id"], task["run_id"], "note", token)
    record_probe_step(store, task["id"], task["run_id"], "context")
    record_probe_step(store, task["id"], task["run_id"], "note", "wrong")
    with store._connection() as db:
        assert not db.execute("SELECT note_seen FROM employee_probes").fetchone()[0]
    store.cancel_task(task["id"])
    with pytest.raises(WorkbenchError):
        record_probe_step(store, task["id"], task["run_id"], "note", token)
    store.finish_task(task["id"], "review", "complete")
    assert store.get_task(task["id"])["status"] == "cancelled"


def test_stdio_mcp_context_note_receipt_verified(probe_fixture):
    store, project, employee = probe_fixture
    task = start(store, project, employee)
    creds = store.execution_credentials(task["id"])
    env = {
        **os.environ,
        "AGENT_MAIL_HOME": str(store.root),
        "AGENT_MAIL_EMPLOYEE": employee["id"],
        "AGENT_MAIL_PROJECT": project["id"],
        "AGENT_MAIL_PROJECT_TOKEN": creds["token"],
        "AGENT_MAIL_TASK": task["id"],
        "AGENT_MAIL_RUN": task["run_id"],
    }

    async def check():
        async with (
            stdio_client(
                StdioServerParameters(
                    command=sys.executable, args=["-m", "agent_mailbox.workspace_mcp"], env=env
                )
            ) as (r, w),
            ClientSession(r, w) as s,
        ):
            await s.initialize()
            assert not (await s.call_tool("project_context", {})).is_error
            assert not (
                await s.call_tool(
                    "project_note",
                    {"title": "Agent connectivity verification", "body": marker(store, task)},
                )
            ).is_error
            tools = await s.list_tools()
            assert "project_delivery" in [t.name for t in tools.tools]

    asyncio.run(check())
    store.finish_task(task["id"], "review", "Complete")
    with store._connection() as db:
        row = db.execute("SELECT context_seen,note_seen FROM employee_probes").fetchone()
        assert row[0] and row[1]
    assert store.snapshot()["employees"][0]["execution_verified"]


def test_probe_unsupported_and_maintenance_do_not_queue(probe_fixture):
    store, project, employee = probe_fixture
    app = store.create_employee("App", "codex", project["id"], connection_type="app")
    with pytest.raises(WorkbenchError):
        create_probe(store, app["id"], project["id"])
    store.pause_updates(True)
    with pytest.raises(WorkbenchError):
        create_probe(store, employee["id"], project["id"])
    assert store.snapshot()["tasks"] == []
