"""A Human bearer has one narrow HTTP seam, never the loopback owner API."""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agent_mailbox.workbench import WorkbenchHTTP
from agent_mailbox.workbench_human_spaces import (
    add_space_member,
    create_human,
    create_shared_space,
    grant_project,
    issue_human_session,
    move_project_to_shared_space,
)
from agent_mailbox.workbench_store import WorkbenchStore


def request(server, method, route, token, body=None):
    payload = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        server.endpoint + "/api/workbench/" + route,
        data=payload,
        headers={"Authorization": "Bearer " + token},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def test_human_access_stream_filters_unrelated_changes_and_retracts_open_tab(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    project_id = store.create_project("Shared", tmp_path)["id"]
    with store._transaction(readonly=True) as db:
        owner = db.execute(
            "SELECT owner_human_id FROM human_bootstrap WHERE singleton=1"
        ).fetchone()[0]
    second = create_human(store, "Second")
    second_id = second["human_id"]
    space_id = create_shared_space(store, owner, "Shared home")["space_id"]
    add_space_member(store, owner, space_id, second_id)
    move_project_to_shared_space(store, owner, project_id, space_id)
    grant_project(store, owner, project_id, second_id, "viewer")
    owner_token = issue_human_session(store, owner)["token"]
    second_session = issue_human_session(store, second_id)
    server = WorkbenchHTTP(store, token="owner-test-only")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert request(server, "GET", "human/changes", server.token)[0] == 401
        stream_request = urllib.request.Request(
            server.endpoint + "/api/workbench/human/changes",
            headers={"Authorization": "Bearer " + second_session["token"]},
        )
        with urllib.request.urlopen(stream_request, timeout=3) as stream:
            assert stream.headers.get("Cache-Control") == "no-store"
            assert json.loads(stream.readline()) == {"ready": True}
            # An unrelated workbench update cannot disclose its revision or timing.
            server.ui_notify()
            status, result = request(
                server,
                "DELETE",
                "human/projects/" + project_id + "/grants/" + second_id,
                owner_token,
            )
            assert status == 200 and result["revoked"] is True
            assert json.loads(stream.readline()) == {"access_changed": True}
            assert (
                request(server, "GET", "human/projects", second_session["token"])[1]["projects"]
                == []
            )
            status, result = request(
                server, "DELETE", "human-sessions/" + second_session["session_id"], server.token
            )
            assert status == 200 and result["revoked"] is True
            assert json.loads(stream.readline()) == {"revoked": True}
            assert request(server, "GET", "human/self", second_session["token"])[0] == 401
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.close()


def test_human_change_stream_reports_only_visible_project_changes(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    private_id = store.create_project("Owner private", tmp_path)["id"]
    shared_id = store.create_project("Visible shared", tmp_path)["id"]
    private_employee = store.create_employee("Private agent", "codex", private_id)["id"]
    shared_employee = store.create_employee("Shared agent", "codex", shared_id)["id"]
    with store._transaction(readonly=True) as db:
        owner_id = db.execute(
            "SELECT owner_human_id FROM human_bootstrap WHERE singleton=1"
        ).fetchone()[0]
    guest_id = create_human(store, "Guest")["human_id"]
    space_id = create_shared_space(store, owner_id, "Shared space")["space_id"]
    add_space_member(store, owner_id, space_id, guest_id)
    move_project_to_shared_space(store, owner_id, shared_id, space_id)
    grant_project(store, owner_id, shared_id, guest_id, "viewer")
    guest_token = issue_human_session(store, guest_id)["token"]
    server = WorkbenchHTTP(store, token="owner-test-only")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stream_request = urllib.request.Request(
        server.endpoint + "/api/workbench/human/changes",
        headers={"Authorization": "Bearer " + guest_token},
    )
    try:
        with urllib.request.urlopen(stream_request, timeout=0.4) as stream:
            assert json.loads(stream.readline()) == {"ready": True}
            store.create_task(private_id, "Private task", "private", private_employee)
            server.ui_notify()
            with pytest.raises(TimeoutError):
                stream.readline()
        with urllib.request.urlopen(stream_request, timeout=3) as stream:
            assert json.loads(stream.readline()) == {"ready": True}
            store.create_task(shared_id, "Visible task", "shared", shared_employee)
            server.ui_notify()
            assert json.loads(stream.readline()) == {"tasks_changed": True}
        with urllib.request.urlopen(stream_request, timeout=0.4) as stream:
            assert json.loads(stream.readline()) == {"ready": True}
            store.send_message(private_id, "Private group", "private update")
            server.ui_notify()
            with pytest.raises(TimeoutError):
                stream.readline()
        with urllib.request.urlopen(stream_request, timeout=3) as stream:
            assert json.loads(stream.readline()) == {"ready": True}
            store.send_message(shared_id, "Visible group", "shared update")
            server.ui_notify()
            assert json.loads(stream.readline()) == {"messages_changed": True}
        private_file = tmp_path / "private-approved.md"
        private_file.write_text("Private approved resource")
        with urllib.request.urlopen(stream_request, timeout=0.4) as stream:
            assert json.loads(stream.readline()) == {"ready": True}
            private_resource = store.add_resource(private_id, "Private brief", "prd", private_file)
            store.capture_resource_version(private_id, private_resource["id"])
            server.ui_notify()
            with pytest.raises(TimeoutError):
                stream.readline()
        shared_file = tmp_path / "shared-approved.md"
        shared_file.write_text("Shared approved resource")
        with urllib.request.urlopen(stream_request, timeout=3) as stream:
            assert json.loads(stream.readline()) == {"ready": True}
            shared_resource = store.add_resource(shared_id, "Shared brief", "prd", shared_file)
            store.capture_resource_version(shared_id, shared_resource["id"])
            server.ui_notify()
            assert json.loads(stream.readline()) == {"resources_changed": True}
        # One wake can cover two simultaneous changes. The single signal must
        # still let the client refresh both visible collections.
        with urllib.request.urlopen(stream_request, timeout=3) as stream:
            assert json.loads(stream.readline()) == {"ready": True}
            combined_message = store.send_message(shared_id, "Combined group", "new mail")
            combined_file = tmp_path / "combined-approved.md"
            combined_file.write_text("New approved content")
            combined_resource = store.add_resource(
                shared_id, "Combined brief", "prd", combined_file
            )
            combined_version = store.capture_resource_version(shared_id, combined_resource["id"])
            server.ui_notify()
            assert json.loads(stream.readline()) == {"messages_changed": True}
            status, visible_messages = request(
                server, "GET", "human/projects/" + shared_id + "/messages", guest_token
            )
            assert status == 200
            assert combined_message["id"] in {item["id"] for item in visible_messages["messages"]}
            status, visible_resources = request(
                server, "GET", "human/projects/" + shared_id + "/resources/manifest", guest_token
            )
            assert status == 200
            assert any(
                item["resource_id"] == combined_resource["id"]
                and item["version_id"] == combined_version["id"]
                for item in visible_resources["resources"]
            )
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.close()


def test_legacy_owner_routes_fail_closed_when_project_scope_mapping_is_missing(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    project_id = store.create_project("Owner private", tmp_path)["id"]
    server = WorkbenchHTTP(store, token="owner-test-only")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for suffix, marker in (
            ("/human", b'id="enroll-form"'),
            ("/workbench-assets/human.js", b"human/enroll"),
            ("/workbench-assets/human.css", b".workspace-grid"),
        ):
            with urllib.request.urlopen(server.endpoint + suffix, timeout=3) as response:
                assert response.status == 200 and marker in response.read()
        assert request(server, "GET", "bootstrap", server.token)[0] == 200
        with store._transaction() as db:
            db.execute("DELETE FROM project_spaces WHERE project_id=?", (project_id,))
        assert request(server, "GET", "bootstrap", server.token)[0] == 409
        assert (
            request(server, "GET", "projects/" + project_id + "/messages", server.token)[0] == 409
        )
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.close()


def test_human_http_never_falls_through_to_owner_capability(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    private = store.create_project("Owner private", tmp_path)["id"]
    shared = store.create_project("Shared", tmp_path)["id"]
    private_employee = store.create_employee("Private synthetic", "codex", private)["id"]
    shared_employee = store.create_employee("Shared synthetic", "codex", shared)["id"]
    private_task = store.create_task(
        private, "Private task", "PRIVATE_PROMPT_MARKER", private_employee
    )["id"]
    store.create_task(private, "Private second", "PRIVATE_PROMPT_SECOND", private_employee)
    shared_task = store.create_task(shared, "Shared task", "SHARED_PROMPT_MARKER", shared_employee)[
        "id"
    ]
    shared_group = store.send_message(shared, "Group update", "SHARED_GROUP_MARKER")
    newer_shared_group = store.send_message(shared, "Latest update", "LATEST_GROUP_MARKER")
    store.send_message(
        shared, "Agent direct", "DIRECT_MAILBOX_MARKER", recipient_id=shared_employee
    )
    store.send_message(private, "Private group", "PRIVATE_GROUP_MARKER")
    store.add_memory(private, "Private memory", "PRIVATE_SEARCH_MARKER")
    shared_memory = store.add_memory(shared, "Shared memory", "SHARED_SEARCH_MARKER")
    private_file = tmp_path / "private-brief.md"
    shared_file = tmp_path / "shared-brief.md"
    private_file.write_text("PRIVATE_RESOURCE_MARKER")
    shared_file.write_text("SHARED_RESOURCE_MARKER")
    private_resource = store.add_resource(private, "Private brief", "prd", private_file)
    shared_resource = store.add_resource(shared, "Shared brief", "prd", shared_file)
    draft_only_file = tmp_path / "draft-only.md"
    draft_only_file.write_text("UNAPPROVED_DRAFT_ONLY_MARKER")
    draft_only_resource = store.add_resource(shared, "Draft only", "prd", draft_only_file)
    private_version = store.capture_resource_version(private, private_resource["id"])
    shared_version = store.capture_resource_version(shared, shared_resource["id"])
    shared_file.write_text("UNAPPROVED_RESOURCE_MARKER")
    unapproved_version = store.capture_resource_version(
        shared, shared_resource["id"], employee_id=shared_employee
    )
    with store._transaction(readonly=True) as db:
        owner = db.execute(
            "SELECT owner_human_id FROM human_bootstrap WHERE singleton=1"
        ).fetchone()[0]
    server = WorkbenchHTTP(store, token="owner-test-only")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert request(server, "GET", "bootstrap", server.token)[0] == 200
        status, second = request(
            server, "POST", "humans", server.token, {"display_name": "Second Human"}
        )
        assert status == 200
        second_id = second["human_id"]
        space = create_shared_space(store, owner, "Shared workspace")["space_id"]
        add_space_member(store, owner, space, second_id)
        move_project_to_shared_space(store, owner, shared, space)
        grant_project(store, owner, shared, second_id, "viewer")

        assert (
            request(server, "POST", "human-sessions", server.token, {"human_id": second_id})[0]
            == 403
        )
        status, automatic_owner = request(server, "POST", "human-sessions", server.token, {})
        assert status == 200
        assert (
            request(server, "GET", "human/self", automatic_owner["token"])[1]["human_id"] == owner
        )
        status, invitation = request(
            server, "POST", "human-invitations", server.token, {"human_id": second_id}
        )
        assert status == 200 and invitation["invitation"].startswith("ami1_")
        status, session = request(
            server, "POST", "human/enroll", "", {"invitation": invitation["invitation"]}
        )
        assert status == 200 and session["recovery_code"].startswith("amr1_")
        assert session["human_id"] == second_id
        assert (
            request(server, "POST", "human/enroll", "", {"invitation": invitation["invitation"]})[0]
            == 401
        )
        assert (
            request(server, "POST", "human-invitations", server.token, {"human_id": second_id})[0]
            == 403
        )
        bearer = session["token"]
        assert bearer.startswith("amh1_")
        status, identity = request(server, "GET", "human/self", bearer)
        assert status == 200 and identity["human_id"] == second_id
        status, own_space = request(
            server, "POST", "human/spaces", bearer, {"name": "Second's shared space"}
        )
        assert status == 200
        status, second_spaces = request(server, "GET", "human/spaces", bearer)
        assert status == 200
        assert {item["id"] for item in second_spaces["spaces"]} == {
            second["private_space_id"],
            space,
            own_space["space_id"],
        }
        assert (
            request(server, "POST", "human/spaces", server.token, {"name": "Wrong bearer"})[0]
            == 401
        )
        status, owner_session = request(
            server, "POST", "human-sessions", server.token, {"human_id": owner}
        )
        assert status == 200
        status, owner_spaces = request(server, "GET", "human/spaces", owner_session["token"])
        assert status == 200 and own_space["space_id"] not in {
            item["id"] for item in owner_spaces["spaces"]
        }
        status, shared_members = request(
            server, "GET", "human/spaces/" + space + "/members", bearer
        )
        assert status == 200 and {member["id"] for member in shared_members["members"]} == {
            owner,
            second_id,
        }
        assert all(
            set(member) == {"id", "display_name", "role"} for member in shared_members["members"]
        )
        private_members = request(
            server,
            "GET",
            "human/spaces/" + second["private_space_id"] + "/members",
            owner_session["token"],
        )
        missing_members = request(
            server, "GET", "human/spaces/space_missing/members", owner_session["token"]
        )
        assert private_members == missing_members
        assert request(server, "GET", "human/spaces/" + space + "/members", server.token)[0] == 401
        assert (
            request(
                server, "POST", "human/projects", bearer, {"name": "Escaped", "path": str(tmp_path)}
            )[0]
            == 400
        )
        status, second_private = request(
            server, "POST", "human/projects", bearer, {"name": "Second private"}
        )
        assert status == 200 and set(second_private) == {"project_id", "space_id"}
        second_private_id = second_private["project_id"]
        with store._transaction(readonly=True) as db:
            private_path = db.execute(
                "SELECT path FROM projects WHERE id=?", (second_private_id,)
            ).fetchone()[0]
        assert private_path.startswith(str(store.directory / "human-projects" / second_id) + "/")
        assert Path(private_path).stat().st_mode & 0o777 == 0o700
        assert request(server, "GET", "bootstrap", server.token)[0] == 409
        assert (
            request(server, "GET", "projects/" + second_private_id + "/messages", server.token)[0]
            == 409
        )
        assert (
            request(
                server,
                "POST",
                "projects",
                server.token,
                {"name": "Owner legacy attempt", "path": str(tmp_path)},
            )[0]
            == 409
        )
        assert (
            request(
                server,
                "POST",
                "human/projects/" + second_private_id + "/share",
                owner_session["token"],
                {"space_id": own_space["space_id"]},
            )[0]
            != 200
        )
        assert (
            request(
                server,
                "POST",
                "human/projects/" + second_private_id + "/share",
                bearer,
                {"space_id": space},
            )[0]
            != 200
        )  # target is not B-owned
        assert request(
            server,
            "POST",
            "human/projects/" + second_private_id + "/share",
            bearer,
            {"space_id": own_space["space_id"]},
        ) == (200, {"project_id": second_private_id, "space_id": own_space["space_id"]})
        status, owner_projects = request(server, "GET", "human/projects", owner_session["token"])
        assert status == 200 and second_private_id not in {
            item["id"] for item in owner_projects["projects"]
        }
        members_path = "human/spaces/" + own_space["space_id"] + "/members"
        grants_path = "human/projects/" + second_private_id + "/grants"
        assert request(server, "POST", members_path, bearer, {"human_id": owner})[0] == 200
        assert (
            request(server, "GET", "human/projects", owner_session["token"])[1]["projects"]
            == (owner_projects["projects"])
        )  # membership alone never shares a project
        assert (
            request(
                server,
                "POST",
                grants_path,
                owner_session["token"],
                {"human_id": owner, "role": "viewer"},
            )[0]
            != 200
        )
        assert (
            request(server, "POST", grants_path, bearer, {"human_id": owner, "role": "viewer"})[0]
            == 200
        )
        status, active_grants = request(server, "GET", grants_path, bearer)
        assert status == 200 and {g["human_id"]: g["role"] for g in active_grants["grants"]} == {
            second_id: "owner",
            owner: "viewer",
        }
        assert request(server, "GET", grants_path, owner_session["token"])[0] != 200
        assert request(server, "GET", "human/projects/project_missing/grants", bearer) == request(
            server, "GET", grants_path, owner_session["token"]
        )
        status, owner_granted = request(server, "GET", "human/projects", owner_session["token"])
        assert status == 200 and second_private_id in {
            item["id"] for item in owner_granted["projects"]
        }
        assert (
            request(
                server,
                "GET",
                "human/projects/" + second_private_id + "/task-counts",
                owner_session["token"],
            )[0]
            == 200
        )
        assert request(server, "DELETE", grants_path + "/" + owner, bearer)[0] == 200
        assert [
            g["human_id"] for g in request(server, "GET", grants_path, bearer)[1]["grants"]
        ] == [second_id]
        assert second_private_id not in {
            item["id"]
            for item in request(server, "GET", "human/projects", owner_session["token"])[1][
                "projects"
            ]
        }
        assert (
            request(
                server,
                "GET",
                "human/projects/" + second_private_id + "/task-counts",
                owner_session["token"],
            )[0]
            != 200
        )
        assert (
            request(server, "POST", grants_path, bearer, {"human_id": owner, "role": "viewer"})[0]
            == 200
        )
        assert request(server, "DELETE", members_path + "/" + owner, bearer)[0] == 200
        assert request(server, "GET", members_path, owner_session["token"]) == (
            request(server, "GET", "human/spaces/space_missing/members", owner_session["token"])
        )
        assert request(server, "POST", members_path, bearer, {"human_id": owner})[0] == 200
        assert second_private_id not in {
            item["id"]
            for item in request(server, "GET", "human/projects", owner_session["token"])[1][
                "projects"
            ]
        }
        assert (
            request(
                server,
                "GET",
                "human/projects/" + second_private_id + "/resources/manifest",
                owner_session["token"],
            )[0]
            != 200
        )
        logout_session = issue_human_session(store, second_id)
        assert request(server, "POST", "human/logout", logout_session["token"], {}) == (
            200,
            {"revoked": True},
        )
        assert request(server, "GET", "human/spaces", logout_session["token"])[0] == 401
        assert request(server, "GET", "human/spaces", bearer)[0] == 200
        status, projects = request(server, "GET", "human/projects", bearer)
        assert status == 200 and {p["id"] for p in projects["projects"]} == {
            shared,
            second_private_id,
        }
        project_request = urllib.request.Request(
            server.endpoint + "/api/workbench/human/projects",
            headers={"Authorization": "Bearer " + bearer},
        )
        with urllib.request.urlopen(project_request, timeout=3) as private_response:
            assert private_response.headers.get("Cache-Control") == "no-store"
        assert {p["id"]: p["role"] for p in projects["projects"]} == {
            shared: "viewer",
            second_private_id: "owner",
        }
        assert private not in json.dumps(projects)
        assert str(tmp_path) not in json.dumps(projects)  # no local file path leaks
        status, tasks = request(server, "GET", "human/projects/" + shared + "/tasks", bearer)
        assert status == 200 and {task["id"] for task in tasks["tasks"]} == {shared_task}
        assert "SHARED_PROMPT_MARKER" not in json.dumps(tasks)  # summary is deliberately narrow
        forbidden = request(server, "GET", "human/projects/" + private + "/tasks", bearer)
        missing = request(server, "GET", "human/projects/project_missing/tasks", bearer)
        assert forbidden == missing  # no existence oracle for a private project
        assert private_task not in json.dumps(tasks)
        assert request(server, "GET", "human/projects/" + shared + "/task-counts", bearer) == (
            200,
            {"counts": {"queued": 1}},
        )
        assert request(server, "GET", "human/projects/" + private + "/task-counts", bearer) == (
            request(server, "GET", "human/projects/project_missing/task-counts", bearer)
        )
        status, team_mail = request(server, "GET", "human/projects/" + shared + "/messages", bearer)
        assert status == 200 and team_mail["viewing_acknowledges"] is False
        assert [item["id"] for item in team_mail["messages"]] == [
            newer_shared_group["id"],
            shared_group["id"],
        ]
        assert set(team_mail["messages"][0]) == {
            "id",
            "title",
            "body",
            "sender_id",
            "reply_to",
            "thread_id",
            "task_id",
            "created_at",
        }
        assert "DIRECT_MAILBOX_MARKER" not in json.dumps(team_mail)
        assert "PRIVATE_GROUP_MARKER" not in json.dumps(team_mail)
        assert request(server, "GET", "human/projects/" + private + "/messages", bearer) == (
            request(server, "GET", "human/projects/project_missing/messages", bearer)
        )
        status, found = request(
            server,
            "GET",
            "human/projects/" + shared + "/memory/search?q=SHARED_SEARCH_MARKER",
            bearer,
        )
        assert status == 200 and {item["id"] for item in found["memories"]} == {shared_memory["id"]}
        assert "PRIVATE_SEARCH_MARKER" not in json.dumps(found)
        assert request(
            server,
            "GET",
            "human/projects/" + private + "/memory/search?q=PRIVATE_SEARCH_MARKER",
            bearer,
        ) == (
            request(
                server,
                "GET",
                "human/projects/project_missing/memory/search?q=PRIVATE_SEARCH_MARKER",
                bearer,
            )
        )
        status, manifest = request(
            server, "GET", "human/projects/" + shared + "/resources/manifest", bearer
        )
        assert status == 200
        assert {item["resource_id"] for item in manifest["resources"]} == {shared_resource["id"]}
        assert manifest["resources"][0]["version_id"] == shared_version["id"]
        assert set(manifest) == {"resources"}
        assert set(manifest["resources"][0]) == {
            "resource_id",
            "version_id",
            "content_sha256",
            "redacted_since_capture",
        }
        assert draft_only_resource["id"] not in json.dumps(manifest)
        assert "canonical_sha256" not in json.dumps(manifest)
        assert private_resource["id"] not in json.dumps(manifest)
        assert str(tmp_path) not in json.dumps(manifest)
        assert request(
            server, "GET", "human/projects/" + private + "/resources/manifest", bearer
        ) == (request(server, "GET", "human/projects/project_missing/resources/manifest", bearer))
        shared_read_path = (
            "human/projects/"
            + shared
            + "/resources/"
            + shared_resource["id"]
            + "/versions/"
            + shared_version["id"]
        )
        status, approved_read = request(server, "GET", shared_read_path, bearer)
        assert status == 200 and approved_read["content"] == "SHARED_RESOURCE_MARKER"
        assert str(tmp_path) not in json.dumps(approved_read)
        assert "UNAPPROVED_RESOURCE_MARKER" not in json.dumps(approved_read)
        approved_request = urllib.request.Request(
            server.endpoint + "/api/workbench/" + shared_read_path,
            headers={"Authorization": "Bearer " + bearer},
        )
        with urllib.request.urlopen(approved_request, timeout=3) as approved_response:
            assert approved_response.headers.get("Cache-Control") == "no-store"
        draft_path = (
            "human/projects/"
            + shared
            + "/resources/"
            + shared_resource["id"]
            + "/versions/"
            + unapproved_version["id"]
        )
        assert request(server, "GET", draft_path, bearer)[0] != 200
        private_read = (
            "human/projects/"
            + private
            + "/resources/"
            + private_resource["id"]
            + "/versions/"
            + private_version["id"]
        )
        missing_read = (
            "human/projects/project_missing/resources/"
            + private_resource["id"]
            + "/versions/"
            + private_version["id"]
        )
        assert request(server, "GET", private_read, bearer) == (
            request(server, "GET", missing_read, bearer)
        )

        # One live grant revocation must close every data seam for the same
        # still-valid Human session, including immutable resource snapshots.
        assert (
            request(
                server,
                "DELETE",
                "human/projects/" + shared + "/grants/" + second_id,
                owner_session["token"],
            )[0]
            == 200
        )
        for suffix in (
            "/tasks",
            "/task-counts",
            "/messages",
            "/memory/search?q=SHARED_SEARCH_MARKER",
            "/resources/manifest",
            "/resources/" + shared_resource["id"] + "/versions/" + shared_version["id"],
        ):
            route = "human/projects/" + shared + suffix
            assert request(server, "GET", route, bearer) == request(
                server, "GET", "human/projects/project_missing" + suffix, bearer
            ), route
        with pytest.raises(urllib.error.HTTPError) as revoked_resource:
            urllib.request.urlopen(approved_request, timeout=3)
        assert revoked_resource.value.code != 200
        assert revoked_resource.value.headers.get("Cache-Control") == "no-store"
        assert shared not in {
            item["id"] for item in request(server, "GET", "human/projects", bearer)[1]["projects"]
        }

        # Human credentials must not inherit the old all-project management
        # surface, its live change stream, session export or update controls.
        for method, route, payload in (
            ("GET", "bootstrap", None),
            ("GET", "changes", None),
            ("GET", "updates/status", None),
            (
                "GET",
                "mailbox/sessions?employee_id=" + shared_employee + "&project_id=" + shared,
                None,
            ),
            ("POST", "mailbox/sessions", {"employee_id": shared_employee, "project_id": shared}),
            ("GET", "projects/" + private + "/messages", None),
            ("POST", "updates/channel", {"channel": "stable"}),
            ("POST", "fleet/relocate", {"base_url": "https://localhost:8765"}),
        ):
            assert request(server, method, route, bearer, payload)[0] == 401, route
        assert (
            request(
                server, "POST", "projects", bearer, {"name": "Forbidden", "path": str(tmp_path)}
            )[0]
            == 401
        )
        assert (
            request(
                server,
                "POST",
                "mailbox/tools",
                bearer,
                {"tool": "project_context", "args": {"project_id": shared}},
            )[0]
            == 401
        )
        assert (
            request(
                server,
                "POST",
                "notify",
                bearer,
                {
                    "employee_id": shared_employee,
                    "project_id": shared,
                    "task_id": shared_task,
                    "run_id": "fake",
                },
            )[0]
            == 401
        )
        assert request(server, "GET", "human/projects", server.token)[0] == 401
        status, revoked = request(
            server, "DELETE", "human-sessions/" + session["session_id"], server.token
        )
        assert status == 200 and revoked == {"revoked": True}
        assert request(server, "GET", "human/projects", bearer)[0] == 401
        assert request(server, "GET", "human/projects/" + shared + "/task-counts", bearer)[0] == 401
        assert request(server, "GET", "human/projects/" + shared + "/messages", bearer)[0] == 401
        assert (
            request(
                server,
                "GET",
                "human/projects/" + shared + "/memory/search?q=SHARED_SEARCH_MARKER",
                bearer,
            )[0]
            == 401
        )
        assert (
            request(server, "GET", "human/projects/" + shared + "/resources/manifest", bearer)[0]
            == 401
        )
        assert request(server, "GET", shared_read_path, bearer)[0] == 401
        second_session = issue_human_session(store, second_id)
        third_session = issue_human_session(store, second_id)
        assert (
            request(
                server,
                "POST",
                "human-sessions/revoke-all",
                second_session["token"],
                {"human_id": second_id},
            )[0]
            == 401
        )
        status, revoked_all = request(
            server, "POST", "human-sessions/revoke-all", server.token, {"human_id": second_id}
        )
        assert status == 200 and revoked_all == {"revoked_count": 2}
        assert request(server, "GET", "human/projects", second_session["token"])[0] == 401
        assert request(server, "GET", "human/projects", third_session["token"])[0] == 401
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.close()


def test_human_enrollment_renewal_and_recovery_rotate_credentials(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    server = WorkbenchHTTP(store, token="owner-test-only")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, human = request(
            server, "POST", "humans", server.token, {"display_name": "Second Human"}
        )
        assert status == 200
        human_id = human["human_id"]
        status, invitation = request(
            server, "POST", "human-invitations", server.token, {"human_id": human_id}
        )
        assert status == 200
        status, claimed = request(
            server, "POST", "human/enroll", "", {"invitation": invitation["invitation"]}
        )
        assert status == 200
        assert (
            request(
                server, "POST", "human/session/renew", claimed["token"], {"human_id": human_id}
            )[0]
            == 400
        )
        status, renewed = request(server, "POST", "human/session/renew", claimed["token"], {})
        assert status == 200 and renewed["token"] != claimed["token"]
        assert request(server, "GET", "human/self", claimed["token"])[0] == 401
        assert request(server, "GET", "human/self", renewed["token"])[0] == 200
        status, recovered = request(
            server,
            "POST",
            "human/recover",
            "",
            {"human_id": human_id, "recovery_code": claimed["recovery_code"]},
        )
        assert status == 200 and recovered["recovery_code"] != claimed["recovery_code"]
        assert request(server, "GET", "human/self", renewed["token"])[0] == 401
        assert request(server, "GET", "human/self", recovered["token"])[0] == 200
        assert (
            request(
                server,
                "POST",
                "human/recover",
                "",
                {"human_id": human_id, "recovery_code": claimed["recovery_code"]},
            )[0]
            == 401
        )
        assert (
            request(server, "POST", "human-sessions", server.token, {"human_id": human_id})[0]
            == 403
        )
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.close()


def test_legacy_human_session_can_set_recovery_without_owner_reinviting(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    human_id = create_human(store, "Legacy Human")["human_id"]
    existing = issue_human_session(store, human_id)
    server = WorkbenchHTTP(store, token="owner-test-only")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert (
            request(server, "POST", "human-invitations", server.token, {"human_id": human_id})[0]
            == 403
        )
        assert request(server, "POST", "human/recovery/setup", server.token, {})[0] == 401
        status, setup = request(server, "POST", "human/recovery/setup", existing["token"], {})
        assert status == 200 and setup["recovery_code"].startswith("amr1_")
        assert request(server, "POST", "human/recovery/setup", existing["token"], {})[0] == 403
        status, recovered = request(
            server,
            "POST",
            "human/recover",
            "",
            {"human_id": human_id, "recovery_code": setup["recovery_code"]},
        )
        assert status == 200 and recovered["token"] != existing["token"]
        assert request(server, "GET", "human/self", existing["token"])[0] == 401
        assert request(server, "GET", "human/self", recovered["token"])[0] == 200
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.close()
