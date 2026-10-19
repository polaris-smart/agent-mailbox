"""Owner HTTP route cannot turn an employee capability into MCP config control."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from agent_mailbox import workbench_hermes_mcp_cutover as cutover
from agent_mailbox.workbench import WorkbenchHTTP
from agent_mailbox.workbench_store import WorkbenchStore


def test_hermes_mcp_cutover_route_requires_owner_and_bounded_arguments(monkeypatch, tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Synthetic", tmp_path)
    employee = store.create_employee("Hermes", "hermes", project["id"])
    server = WorkbenchHTTP(store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    calls = []

    def bounded_operation(*args, **kwargs):
        calls.append((args, kwargs))
        return {
            "state": "preview",
            "runtime_verified": False,
            "session_verified": False,
            "delivery_verified": False,
        }

    monkeypatch.setattr(cutover, "operation", bounded_operation)

    def post(payload, token):
        request = urllib.request.Request(
            server.endpoint + "/api/workbench/mailbox/receivers/hermes/mcp-command",
            data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=5)

    payload = {
        "employee_id": employee["id"],
        "project_id": project["id"],
        "profile_id": "hermes_profile_synthetic",
        "action": "preview",
    }
    try:
        with post(payload, server.token) as response:
            assert json.load(response)["state"] == "preview"
        assert calls == [
            (
                (store, employee["id"], project["id"], "hermes_profile_synthetic", "preview"),
                {"plan_id": None, "confirmed": False},
            )
        ]
        with pytest.raises(urllib.error.HTTPError) as denied:
            post(payload, "employee-capability")
        assert denied.value.code == 401
        assert len(calls) == 1
        malformed = {**payload, "new_command": "/tmp/attacker"}
        with pytest.raises(urllib.error.HTTPError) as invalid:
            post(malformed, server.token)
        assert invalid.value.code == 400
        assert len(calls) == 1
    finally:
        server.shutdown()
        thread.join(5)
        server.close()
