"""Activity is project scoped and does not mutate messages or task states."""

import pytest

from agent_mailbox.workbench_activity import project_activity
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


def test_activity_scope_read_only_and_no_model_transcripts(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("One", str(tmp_path))
    other = store.create_project("Two", str(tmp_path))
    employee = store.create_employee("Worker", "codex", project["id"])
    task = store.create_task(project["id"], "Actual task", "Work", employee["id"])
    store.add_event(task["id"], "event", "Private transcript", {"thought": "private"})
    store.add_memory(project["id"], "Decision", "Confirmed", source="human")
    store.send_message(project["id"], "Project message", "No ACK", recipient_id=employee["id"])
    store.add_memory(other["id"], "Unrelated", "must not leak")
    before = store.snapshot()
    result = project_activity(store, project["id"])
    text = str(result)
    assert "Actual task" in text and "Project message" in text and "Decision" in text
    assert "must not leak" not in text and "Private transcript" not in text
    assert store.snapshot() == before
    assert not result["has_older"]
    assert project_activity(store, project["id"], 1)["has_older"]
    with pytest.raises(WorkbenchError):
        project_activity(store, other["id"], True)
    with pytest.raises(WorkbenchError):
        project_activity(store, "missing")
