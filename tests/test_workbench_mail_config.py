"""A named App/CLI connection can be reopened without issuing more keys."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_mailbox.workbench_mail_config import export_session
from agent_mailbox.workbench_mail_sessions import create_session, list_sessions
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


@pytest.fixture
def team(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Synthetic", directory)
    employee = store.create_employee("Synthetic agent", "codex", project["id"])
    return store, project["id"], employee["id"]


def test_exporting_same_connection_reuses_its_private_manifest(team):
    store, project_id, employee_id = team
    first = export_session(store, employee_id, project_id, "Desktop conversation")
    second = export_session(store, employee_id, project_id, "Desktop conversation")

    assert second["id"] == first["id"]
    assert second["config"] == first["config"]
    assert second["reused"] is True
    assert len(list_sessions(store, employee_id, project_id)) == 1


def test_separate_conversation_keeps_its_own_key(team):
    store, project_id, employee_id = team
    desktop = export_session(store, employee_id, project_id, "Desktop conversation")
    cli = export_session(store, employee_id, project_id, "CLI conversation")

    assert desktop["id"] != cli["id"]
    assert all(row["active"] for row in list_sessions(store, employee_id, project_id))


def test_active_label_without_exported_manifest_fails_closed(team):
    store, project_id, employee_id = team
    create_session(store, employee_id, project_id, "Another host-owned session")

    with pytest.raises(WorkbenchError, match="会话名称"):
        export_session(store, employee_id, project_id, "Another host-owned session")
    assert len(list_sessions(store, employee_id, project_id)) == 1


def test_concurrent_export_of_one_connection_issues_one_key(team):
    store, project_id, employee_id = team

    def export(_):
        return export_session(store, employee_id, project_id, "One browser connection")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(export, range(2)))
    assert results[0]["id"] == results[1]["id"]
    assert len(list_sessions(store, employee_id, project_id)) == 1


def test_reused_connection_rejects_wrong_private_manifest(team):
    store, project_id, employee_id = team
    first = export_session(store, employee_id, project_id, "Desktop conversation")
    path = Path(first["config"]["mcpServers"]["agent-mailbox-project"]["args"][-1])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["token"] = "invalid-token"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(WorkbenchError, match="原接入文件不可用"):
        export_session(store, employee_id, project_id, "Desktop conversation")
    assert len(list_sessions(store, employee_id, project_id)) == 1
