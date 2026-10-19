"""Owner HTTP assignment -> employee API delivery -> directed native intent."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from agent_mailbox.workbench import WorkbenchHTTP
from agent_mailbox.workbench_mail_notifications import pending_ids
from agent_mailbox.workbench_mail_sessions import create_session
from agent_mailbox.workbench_store import WorkbenchStore


def test_http_reviewer_routing_is_owner_only_and_delivery_reaches_reviewer(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Synthetic", tmp_path)
    worker = store.create_employee("Worker", "codex", project["id"])
    reviewer = store.create_employee("Reviewer", "deepseek", project["id"])
    session = create_session(store, worker["id"], project["id"], "Test")
    server = WorkbenchHTTP(store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def post(path, data, token):
        request = urllib.request.Request(
            server.endpoint + "/api/workbench/" + path,
            data=json.dumps(data).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    data = {
        "project_id": project["id"],
        "assignee_id": worker["id"],
        "review_recipient_id": reviewer["id"],
        "title": "Synthetic",
        "prompt": "Read",
    }
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            post("mail-tasks", data, session["token"])
        assert error.value.code == 401
        mail_request = {**data, "request_id": "http-mail-task-001"}
        task = post("mail-tasks", mail_request, server.token)
        assert post("mail-tasks", mail_request, server.token)["id"] == task["id"]
        assert len(store.list_messages(project["id"])) == 1
        assert task["review_recipient_id"] == reviewer["id"]
        post(
            "mailbox/tools",
            {"tool": "task_accept", "args": {"task_id": task["id"]}},
            session["token"],
        )
        post(
            "mailbox/tools",
            {"tool": "task_submit", "args": {"task_id": task["id"], "result": "Synthetic result"}},
            session["token"],
        )
        assert len(pending_ids(store, reviewer["id"])) == 1
        assert store.get_task(task["id"])["status"] == "review"

        managed_request = {**data, "request_id": "http-managed-task-001"}
        managed = post("tasks", managed_request, server.token)
        assert post("tasks", managed_request, server.token)["id"] == managed["id"]
        assert managed["review_recipient_id"] == reviewer["id"]
        claimed = store.claim_task(store.local_node()["id"])
        assert claimed["id"] == managed["id"]
        store.set_status(managed["id"], "running")
        store.finish_task(managed["id"], "review", "Managed result")
        delivery = [
            message
            for message in store.list_messages(project["id"])
            if message["source_task_id"] == managed["id"]
        ]
        assert len(delivery) == 1
        assert delivery[0]["recipient_id"] == reviewer["id"]
        assert delivery[0]["id"] in pending_ids(store, reviewer["id"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        server.instance_lock.close()
