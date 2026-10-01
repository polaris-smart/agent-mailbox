"""Exercise real HTTP requests and child processes across the workbench boundary."""

import json
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from agent_mailbox.workbench import WorkbenchHTTP
from agent_mailbox.workbench_engine import WorkbenchEngine
from agent_mailbox.workbench_runtime import BridgeExecution
from agent_mailbox.workbench_store import WorkbenchStore

FAKE = r"""
import sys,json,time
r=json.loads(sys.stdin.readline())
def send(t,**v):
 print(json.dumps(dict(protocol=1,run_id=r['run_id'],session_id=r['session_id'],type=t,**v)),flush=True)
if r['prompt']=='exit': sys.exit(0)
send('started')
if r['prompt']=='permission':
 send('permission_required',request_id='approval',options=[{'optionId':'once','kind':'allow_once','name':'Allow once'}],tool_call={'title':'Read project'})
 answer=json.loads(sys.stdin.readline())
 if answer.get('decision')!='allow_once':
  send('result',status='failed',error={'code':'PERMISSION_DENIED','message':'Denied'});sys.exit(0)
if r['prompt']=='wait':
 json.loads(sys.stdin.readline());send('result',status='cancelled');sys.exit(0)
send('result',status='completed',output_text='Verified deliverable: '+r['prompt'])
json.loads(sys.stdin.readline())
"""


@pytest.fixture
def bench(tmp_path):
    store = WorkbenchStore(tmp_path / "state")
    project = store.create_project("Test project", str(tmp_path))
    employee = store.create_employee("Fixture employee", "codex", project["id"])
    bridge = tmp_path / "fake.py"
    bridge.write_text(FAKE)
    engine = WorkbenchEngine(store, [sys.executable, str(bridge)], task_timeout=3)
    server = WorkbenchHTTP(store, engine=engine, token="test-owner-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    engine.start()
    yield server, project, employee
    server.shutdown()
    server.close()
    thread.join(timeout=2)


def request(server, path, body=None, method=None, token="test-owner-token", origin=None):
    headers = {"Authorization": "Bearer " + token}
    if origin:
        headers["Origin"] = origin
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        server.endpoint + "/api/workbench/" + path, data=data, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def dispatch(bench, prompt="simple", employee=None):
    server, project, person = bench
    status, task = request(
        server,
        "tasks",
        {
            "project_id": project["id"],
            "title": prompt,
            "prompt": prompt,
            "assignee_id": (employee or person)["id"],
        },
    )
    assert status == 200
    return task


def await_state(server, task_id, status):
    end = time.monotonic() + 6
    while time.monotonic() < end:
        task = server.store.get_task(task_id)
        if task["status"] == status:
            return task
        time.sleep(0.03)
    pytest.fail(f"Expected {status}; got {task['status']}: {task.get('error')}")


def test_execution_and_human_acceptance(bench):
    server, _, _ = bench
    task = dispatch(bench)
    result = await_state(server, task["id"], "review")
    assert result["result"] == "Verified deliverable: simple"
    assert request(server, "tasks/" + task["id"])[1]["events"]
    assert (
        request(server, "tasks/" + task["id"] + "/review", {"decision": "accept"})[1]["status"]
        == "done"
    )


def test_exit_zero_without_result_is_failure(bench):
    server, _, _ = bench
    task = dispatch(bench, "exit")
    assert await_state(server, task["id"], "failed")["error"]["code"] == "EXECUTION_INTERRUPTED"


def test_owner_approval_and_late_answer(bench):
    server, _, _ = bench
    task = dispatch(bench, "permission")
    await_state(server, task["id"], "waiting_approval")
    detail = request(server, "tasks/" + task["id"])[1]
    assert detail["permissions"][0]["status"] == "pending"
    path = "tasks/" + task["id"] + "/permissions/approval"
    assert request(server, path, {"decision": "allow_once"})[0] == 200
    await_state(server, task["id"], "review")
    assert request(server, path, {"decision": "allow_once"})[0] == 400


def test_cancel_stops_only_assigned_run_and_releases_queue(bench):
    server, _, _ = bench
    task = dispatch(bench, "wait")
    await_state(server, task["id"], "running")
    queued = dispatch(bench)
    request(server, "tasks/" + task["id"] + "/cancel", {})
    await_state(server, task["id"], "cancelled")
    await_state(server, queued["id"], "review")


def test_different_employees_execute_concurrently(bench):
    server, project, _ = bench
    task = dispatch(bench, "wait")
    await_state(server, task["id"], "running")
    colleague = server.store.create_employee("Other fixture", "codex", project["id"])
    next_task = dispatch(bench, employee=colleague)
    await_state(server, next_task["id"], "review")
    assert server.store.get_task(task["id"])["status"] == "running"
    request(server, "tasks/" + task["id"] + "/cancel", {})


def test_auth_origin_and_employee_scope(bench, tmp_path):
    server, project, employee = bench
    assert request(server, "bootstrap", token="wrong")[0] == 401
    assert (
        request(
            server,
            "projects",
            {"name": "evil", "path": str(tmp_path)},
            origin="https://evil.example",
        )[0]
        == 403
    )
    credentials = server.store.employee_credentials(employee["id"], project["id"])
    assert request(server, "bootstrap", token=credentials["token"])[0] == 401
    assert (
        request(
            server,
            "notify",
            {"employee_id": employee["id"], "project_id": project["id"]},
            token=credentials["token"],
        )[0]
        == 401
    )
    other = server.store.create_project("Other", str(tmp_path / "state"))
    assert (
        request(
            server,
            "notify",
            {"employee_id": employee["id"], "project_id": other["id"]},
            token=credentials["token"],
        )[0]
        == 401
    )
    snapshot = request(server, "bootstrap")[1]
    assert credentials["token"] not in json.dumps(snapshot)


def test_static_assets_and_path_traversal(bench):
    server, _, _ = bench
    with urllib.request.urlopen(server.endpoint + "/workbench") as response:
        assert response.status == 200
        assert "script-src 'self'" in response.headers["Content-Security-Policy"]
    with urllib.request.urlopen(server.endpoint + "/workbench-assets/workbench.css") as response:
        assert response.status == 200
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(server.endpoint + "/../workbench_store.py")
    assert exc.value.code == 404


def test_employee_first_discovered_apps_members_and_messages(tmp_path, monkeypatch):
    import agent_mailbox.workbench as web

    discoveries = [
        {
            "kind": "codex",
            "name": "Codex",
            "connection_type": "cli",
            "entrypoint": "/test/codex",
            "status": "installed",
            "detail": "Not execution verified.",
        },
        {
            "kind": "codex",
            "name": "Codex",
            "connection_type": "app",
            "entrypoint": "/test/Codex.app",
            "status": "installed",
            "detail": "App adapter unavailable.",
        },
        {
            "kind": "hermes",
            "name": "Hermes",
            "connection_type": "cli",
            "entrypoint": "/test/hermes",
            "status": "installed",
            "detail": "Adapter unavailable.",
        },
    ]
    monkeypatch.setattr(web, "discover_employees", lambda: discoveries)
    store = WorkbenchStore(tmp_path / "home")
    server = WorkbenchHTTP(store, token="test-owner-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert request(server, "bootstrap")[1]["projects"] == []
        people = []
        for candidate in discoveries:
            status, person = request(
                server,
                "employees",
                {
                    "name": candidate["name"] + " " + candidate["connection_type"],
                    "kind": candidate["kind"],
                    "connection_type": candidate["connection_type"],
                    "entrypoint": candidate["entrypoint"],
                    "execution_supported": True,
                },
            )
            assert status == 200
            assert person["project_ids"] == []
            people.append(person)
        assert people[0]["execution_supported"] is True
        assert people[1]["execution_supported"] is False
        assert people[2]["execution_supported"] is False
        status, checked = request(server, f"employees/{people[0]['id']}/check", {})
        assert status == 200
        assert checked["execution_verified"] is False
        assert (
            request(
                server, "employees", {"name": "Fake", "kind": "codex", "entrypoint": "/unknown"}
            )[0]
            == 400
        )
        _, project = request(server, "projects", {"name": "Group", "path": str(tmp_path)})
        for person in people:
            assert (
                request(server, f"projects/{project['id']}/members", {"employee_id": person["id"]})[
                    0
                ]
                == 200
            )
        prefix = f"projects/{project['id']}/messages"
        _, normal = request(
            server, prefix, {"title": "Hello", "body": "Shared progress", "request_id": "human-1"}
        )
        assert not normal.get("task_id")
        assert store.snapshot()["tasks"] == []
        assert (
            request(
                server, prefix, {"title": "Fake sender", "body": "No", "sender_id": people[0]["id"]}
            )[0]
            == 400
        )
        for person in people[1:]:
            status, error = request(
                server,
                prefix,
                {
                    "title": "Work",
                    "body": "Read",
                    "recipient_id": person["id"],
                    "request_work": True,
                },
            )
            assert status == 400
            assert error["error"]["code"] == "ADAPTER_UNSUPPORTED"
        payload = {
            "title": "Work",
            "body": "Read",
            "recipient_id": people[0]["id"],
            "request_work": True,
            "request_id": "human-work-1",
        }
        status, first = request(server, prefix, payload)
        assert status == 200
        replay = request(server, prefix, payload)[1]
        assert replay["id"] == first["id"]
        assert replay["task_id"] == first["task_id"]
        assert len(store.snapshot()["tasks"]) == 1
        reply = request(
            server, prefix, {"title": "Reply", "body": "Recorded", "reply_to": normal["id"]}
        )[1]
        assert reply["thread_id"] == normal["thread_id"]
        assert len(request(server, prefix)[1]["messages"]) == 3
        assert (
            request(server, f"projects/{project['id']}/members/{people[1]['id']}", method="DELETE")[
                0
            ]
            == 200
        )
        assert people[1]["id"] in {p["id"] for p in store.snapshot()["employees"]}
    finally:
        server.shutdown()
        server.close()
        thread.join(timeout=2)


def test_bridge_timeout_and_wrong_session(tmp_path):
    project = {"path": str(tmp_path)}
    task = {
        "kind": "codex",
        "run_id": "run",
        "session_id": "session",
        "prompt": "test",
        "permission_mode": "read-only",
    }
    script = tmp_path / "bad.py"
    script.write_text("import time\ntime.sleep(8)\n")
    execution = BridgeExecution(tmp_path, [sys.executable, str(script)])
    start = time.monotonic()
    result = execution.run(
        task, project, [], lambda e: None, lambda e: "deny", lambda: False, timeout=0.1
    )
    assert result["error"]["code"] == "TIMEOUT"
    assert time.monotonic() - start < 5
    script.write_text(
        "import json\nprint(json.dumps({'protocol':1,'run_id':'run','session_id':'other','type':'result','status':'completed','output_text':'wrong'}),flush=True)\n"
    )
    assert (
        execution.run(task, project, [], lambda e: None, lambda e: "deny", lambda: False)["error"][
            "code"
        ]
        == "RUNTIME_PROTOCOL_ERROR"
    )


def test_push_updates_require_owner_auth_and_do_not_poll(bench):
    server, _, _ = bench
    assert request(server, "changes", token="wrong")[0] == 401
    req = urllib.request.Request(
        server.endpoint + "/api/workbench/changes",
        headers={"Authorization": "Bearer test-owner-token"},
    )
    with urllib.request.urlopen(req, timeout=3) as response:
        first = json.loads(response.readline())
        server.ui_notify()
        second = json.loads(response.readline())
        assert second["revision"] > first["revision"]


def test_second_frontend_cannot_interrupt_running_owner(bench):
    from agent_mailbox.workbench_store import WorkbenchError

    server, _, _ = bench
    task = dispatch(bench, "wait")
    await_state(server, task["id"], "running")
    with pytest.raises(WorkbenchError) as error:
        WorkbenchHTTP(server.store)
    assert error.value.code == "ALREADY_RUNNING"
    assert server.store.get_task(task["id"])["status"] == "running"
    request(server, "tasks/" + task["id"] + "/cancel", {})


def test_cancelled_before_launch_does_not_start_a_process(tmp_path):
    flag = tmp_path / "unexpected"
    command = [sys.executable, "-c", f"from pathlib import Path;Path({str(flag)!r}).touch()"]
    execution = BridgeExecution(tmp_path, command)
    result = execution.run({}, {}, [], lambda e: None, lambda e: "deny", lambda: True)
    assert result["status"] == "cancelled"
    assert not flag.exists()


def test_owner_lifecycle_api_revokes_access_and_stops_owned_child(bench):
    server, project, employee = bench
    creds = server.store.employee_credentials(employee["id"], project["id"])
    path = "employees/" + employee["id"] + "/lifecycle"
    assert request(server, path, {"status": "paused"}, token=creds["token"])[0] == 401
    assert request(server, path, {"status": "paused", "reason": "休息"})[0] == 200
    denied, _ = request(
        server,
        "tasks",
        {
            "project_id": project["id"],
            "title": "Test",
            "prompt": "simple",
            "assignee_id": employee["id"],
        },
    )
    assert denied == 400
    request(server, path, {"status": "active"})
    task = dispatch(bench, "wait")
    await_state(server, task["id"], "running")
    assert request(server, path, {"status": "retired", "reason": "项目结束"})[0] == 200
    await_state(server, task["id"], "cancelled")
    assert (
        request(
            server,
            "notify",
            {"employee_id": employee["id"], "project_id": project["id"]},
            token=creds["token"],
        )[0]
        == 401
    )
    code, ledger = request(server, "governance?project_id=" + project["id"])
    assert code == 200
    assert any(event["type"] == "employee_lifecycle" for event in ledger["events"])
    assert creds["token"] not in json.dumps(ledger)


def test_application_quit_is_owner_only_and_main_cleans_up(tmp_path):
    import subprocess

    root = tmp_path / "app-home"
    process = subprocess.Popen(
        [sys.executable, "-m", "agent_mailbox.workbench", "--home", str(root), "--no-browser"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        url = process.stdout.readline().strip()
        endpoint, token = url.split("/#token=")
        request_url = endpoint + "/api/workbench/application/quit"
        denied = urllib.request.Request(
            request_url, data=b"{}", headers={"Authorization": "Bearer wrong"}
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(denied, timeout=3)
        assert error.value.code == 401
        allowed = urllib.request.Request(
            request_url, data=b"{}", headers={"Authorization": "Bearer " + token}
        )
        with urllib.request.urlopen(allowed, timeout=3) as response:
            assert json.load(response) == {"stopping": True}
        assert process.wait(timeout=5) == 0
        assert not (root / "workbench/instance.json").exists()
        # The lock is released and external state survives a replacement launch.
        from agent_mailbox.workbench_lock import WorkbenchLock

        lock = WorkbenchLock(root)
        try:
            assert (root / "workbench/state.db").is_file()
        finally:
            lock.close()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_quit_main_stops_owned_execution_and_preserves_external_state(tmp_path):
    import subprocess

    root = tmp_path / "active-home"
    store = WorkbenchStore(root)
    project = store.create_project("Quit check", str(tmp_path))
    person = store.create_employee("Fixture", "codex", project["id"])
    task = store.create_task(project["id"], "Wait", "wait", person["id"])
    bridge = tmp_path / "fixture.py"
    bridge.write_text(FAKE)
    script = (
        "from agent_mailbox import workbench\n"
        "from agent_mailbox.workbench_engine import WorkbenchEngine\n"
        f"workbench.WorkbenchEngine=lambda store: WorkbenchEngine(store, {[sys.executable, str(bridge)]!r},task_timeout=10)\n"
        f"workbench.main(['--home',{str(root)!r},'--no-browser'])\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        endpoint, token = process.stdout.readline().strip().split("/#token=")
        deadline = time.monotonic() + 5
        while store.get_task(task["id"])["status"] != "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert store.get_task(task["id"])["status"] == "running"
        req = urllib.request.Request(
            endpoint + "/api/workbench/application/quit",
            data=b"{}",
            headers={"Authorization": "Bearer " + token},
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            assert json.load(response)["stopping"]
        assert process.wait(timeout=8) == 0
        assert store.get_task(task["id"])["status"] == "cancelled"
        assert store.snapshot()["projects"][0]["id"] == project["id"]
        assert not (store.directory / "instance.json").exists()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=8)


def test_distribution_notices_are_available_without_owner_credentials(bench):
    server, _, _ = bench
    for name, expected in (
        ("LICENSE.txt", "Version 2.0, January 2004"),
        ("NOTICE.txt", "NoFox and contributors"),
        ("MIT-Legacy.txt", "polaris-smart contributors"),
    ):
        with urllib.request.urlopen(server.endpoint + "/workbench-assets/" + name) as response:
            assert response.headers["Content-Type"] == "text/plain; charset=utf-8"
            assert expected in response.read().decode()


def test_resource_preview_capability_and_live_provenance(bench, tmp_path):
    import hashlib

    server, project, _ = bench
    source = tmp_path / "diagram.html"
    source.write_text("<h1>Diagram</h1><script>window.fixture=1</script>")
    resource = server.store.add_resource(project["id"], "Diagram", "architecture", source)
    route = f"projects/{project['id']}/resources/{resource['id']}/read"
    assert request(server, route, token="wrong")[0] == 401
    status, result = request(server, route)
    assert status == 200
    assert result["provenance"]["mode"] == "live"
    assert (
        result["provenance"]["content_sha256"]
        == hashlib.sha256(result["content"].encode()).hexdigest()
    )
    other = server.store.create_project("Other", str(tmp_path))
    assert request(server, f"projects/{other['id']}/resources/{resource['id']}/read")[0] == 400
    with urllib.request.urlopen(server.endpoint + result["preview_url"]) as response:
        csp = response.headers["Content-Security-Policy"]
        assert "sandbox allow-scripts;" in csp
        assert "connect-src 'none'" in csp
        assert "allow-same-origin" not in csp
        assert response.read().decode() == result["content"]
    nonce = result["preview_url"].rsplit("/", 1)[1]
    with server.preview_lock:
        server.previews[nonce] = (0, result["content"])
    with pytest.raises(urllib.error.HTTPError) as expired:
        urllib.request.urlopen(server.endpoint + result["preview_url"])
    assert expired.value.code == 404
    assert request(server, "bootstrap", token=nonce)[0] == 401
    for _ in range(40):
        server.resource_preview("x")
    assert len(server.previews) == 32
    source.write_text("Changed live file")
    assert request(server, route)[1]["content"] == "Changed live file"


def test_knowledge_http_is_project_bound_and_authenticated(bench, monkeypatch, tmp_path):
    from agent_mailbox import workbench_knowledge

    server, project, _ = bench
    calls = []

    def query(path, value):
        calls.append((path, value))
        return {
            "symbols": [{"name": "Example"}],
            "mode": "symbols_snapshot",
            "freshness": "unknown",
        }

    monkeypatch.setattr(workbench_knowledge, "knowledge_query", query)
    route = f"projects/{project['id']}/knowledge/query"
    assert request(server, route, {"query": "Example"}, token="wrong")[0] == 401
    assert calls == []
    revision = server.ui_revision
    status, result = request(server, route, {"query": "Example", "path": "/arbitrary"})
    assert server.ui_revision == revision
    assert status == 200
    assert calls == [(str(tmp_path), "Example")]
    assert result["provenance"]["freshness"] == "unknown"
    assert request(server, "projects/missing/knowledge/query", {"query": "Example"})[0] == 404
    assert len(calls) == 1


def test_resource_versions_owner_routes_and_activity(bench, tmp_path):
    server, project, employee = bench
    source = tmp_path / "Architecture.html"
    source.write_text("<h1>Old report</h1>")
    resource = server.store.add_resource(project["id"], "Architecture", "archify", source)
    route = f"projects/{project['id']}/resources/{resource['id']}/versions"
    assert request(server, route)[1] == {"versions": []}
    assert request(server, route, {"summary": "Baseline"}, token="wrong")[0] == 401
    status, version = request(server, route, {"summary": "Baseline"})
    assert status == 200 and version["status"] == "approved"
    source.write_text("<h1>New report</h1>")
    status, read = request(server, route + "/" + version["id"] + "/read")
    assert status == 200 and read["content"] == "<h1>Old report</h1>"
    assert read["preview_url"].startswith("/workbench-preview/")
    proposal = server.store.capture_resource_version(
        project["id"], resource["id"], "New", employee["id"]
    )
    assert proposal["status"] == "proposed"
    assert request(server, route + "/" + proposal["id"] + "/approve", {})[1]["status"] == "approved"
    other = server.store.create_project("Other", str(tmp_path))
    assert request(server, f"projects/{other['id']}/resources/{resource['id']}/versions")[0] == 400
    activity = request(server, f"projects/{project['id']}/activity")[1]
    assert any(e["type"] == "resource_version_approved" for e in activity["events"])
    assert request(server, f"projects/{project['id']}/activity", token="wrong")[0] == 401


def test_loopback_startup_does_not_require_reverse_dns(tmp_path, monkeypatch):
    import socket

    def forbidden_lookup(*args):
        pytest.fail("Loopback binding must not perform reverse DNS")

    monkeypatch.setattr(socket, "getfqdn", forbidden_lookup)
    store = WorkbenchStore(tmp_path / "home")
    server = WorkbenchHTTP(store, token="test-owner-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert server.server_name == "127.0.0.1"
        assert request(server, "bootstrap")[0] == 200
    finally:
        server.shutdown()
        server.close()
        thread.join(timeout=3)
