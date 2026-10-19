"""An SSH-forwarded Human listener cannot expose the legacy owner surface."""

import http.client
import json
import select
import socket
import socketserver
import threading
import urllib.error
import urllib.request

import pytest

from agent_mailbox.workbench import HumanForwardHTTP, WorkbenchHTTP
from agent_mailbox.workbench_fleet import FleetClient
from agent_mailbox.workbench_human_spaces import (
    add_space_member,
    create_human,
    create_human_invitation,
    create_managed_private_project,
    create_shared_space,
    grant_project,
    issue_human_session,
    move_project_to_shared_space,
    revoke_project,
)
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


def _fetch(server, route, *, host=None, origin=None, token=None):
    headers = {}
    if host is not None:
        headers["Host"] = host
    if origin is not None:
        headers["Origin"] = origin
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(server.endpoint + route, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _post(server, route, token, payload, *, host="localhost:49001"):
    request = urllib.request.Request(
        server.endpoint + route,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Host": host,
            "Origin": "http://" + host,
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def _delete(server, route, token, *, host="localhost:49001"):
    request = urllib.request.Request(
        server.endpoint + route,
        method="DELETE",
        headers={"Host": host, "Origin": "http://" + host, "Authorization": "Bearer " + token},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def test_human_listener_behind_a_real_local_tcp_forward_uses_client_host(tmp_path):
    """Exercise the forwarded socket without hand-setting Host or Origin."""
    store = WorkbenchStore(tmp_path / "home")
    project_id = store.create_project("Shared synthetic", tmp_path)["id"]
    with store._transaction(readonly=True) as db:
        owner_id = db.execute(
            "SELECT owner_human_id FROM human_bootstrap WHERE singleton=1"
        ).fetchone()[0]
    remote_id = create_human(store, "Remote human")["human_id"]
    space_id = create_shared_space(store, owner_id, "Shared synthetic")["space_id"]
    add_space_member(store, owner_id, space_id, remote_id)
    move_project_to_shared_space(store, owner_id, project_id, space_id)
    grant_project(store, owner_id, project_id, remote_id, "viewer")
    invitation = create_human_invitation(store, remote_id)["invitation"]
    owner = WorkbenchHTTP(store, token="owner-test-only")
    forward = HumanForwardHTTP(owner)

    class Relay(socketserver.BaseRequestHandler):
        def handle(self):
            with socket.create_connection(
                ("127.0.0.1", forward.server_port), timeout=3
            ) as upstream:
                endpoints = (self.request, upstream)
                while ready := select.select(endpoints, (), (), 3)[0]:
                    for source in ready:
                        chunk = source.recv(65536)
                        if not chunk:
                            return
                        (upstream if source is self.request else self.request).sendall(chunk)

    class LocalForward(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    relay = LocalForward(("127.0.0.1", 0), Relay)
    forward_thread = threading.Thread(target=forward.serve_forever, daemon=True)
    relay_thread = threading.Thread(target=relay.serve_forever, daemon=True)
    forward_thread.start()
    relay_thread.start()
    client = f"http://127.0.0.1:{relay.server_address[1]}"

    def get(route, token=""):
        request = urllib.request.Request(
            client + route, headers={"Authorization": "Bearer " + token}
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    try:
        status, headers, page = get("/human")
        assert status == 200 and b'id="enroll-form"' in page
        assert headers["Cache-Control"] == "no-store"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert get("/workbench")[0] == 404
        enroll = urllib.request.Request(
            client + "/api/workbench/human/enroll",
            data=json.dumps({"invitation": invitation}).encode(),
            method="POST",
            headers={"Origin": client, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(enroll, timeout=3) as response:
            issued = json.load(response)
        bearer = issued["token"]
        assert issued["human_id"] == remote_id
        assert json.loads(get("/api/workbench/human/self", bearer)[2])["human_id"] == remote_id
        scoped = "/api/workbench/human/projects/" + project_id + "/task-counts"
        assert get(scoped, bearer)[0] == 200
        assert get("/api/workbench/bootstrap", owner.token)[0] == 404
        revoke_project(store, owner_id, project_id, remote_id)
        assert get(scoped, bearer)[0] != 200
    finally:
        relay.shutdown()
        forward.shutdown()
        relay_thread.join(timeout=3)
        forward_thread.join(timeout=3)
        relay.server_close()
        forward.server_close()
        owner.close()


def test_stopping_human_forward_closes_existing_keepalive_client(tmp_path):
    """A stopped forward must not keep serving through an already-open TCP socket."""
    store = WorkbenchStore(tmp_path / "home")
    with store._transaction(readonly=True) as db:
        human_id = db.execute(
            "SELECT owner_human_id FROM human_bootstrap WHERE singleton=1"
        ).fetchone()[0]
    human_token = issue_human_session(store, human_id)["token"]
    owner = WorkbenchHTTP(store, token="owner-test-only")
    owner.start_human_forward(port=0, persist=False)
    port = owner.human_forward.server_port
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    stream_client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        client.request(
            "GET", "/api/workbench/human/self", headers={"Authorization": "Bearer " + human_token}
        )
        first = client.getresponse()
        assert first.status == 200
        first.read()
        stream_client.request(
            "GET",
            "/api/workbench/human/changes",
            headers={"Authorization": "Bearer " + human_token},
        )
        stream = stream_client.getresponse()
        assert stream.status == 200
        assert json.loads(stream.readline()) == {"ready": True}
        owner.stop_human_forward(persist=False)
        with pytest.raises((OSError, http.client.HTTPException)):
            client.request(
                "GET",
                "/api/workbench/human/self",
                headers={"Authorization": "Bearer " + human_token},
            )
            client.getresponse().read()
        try:
            assert stream.read() == b""
        except (OSError, http.client.HTTPException):
            pass  # A forcibly closed chunked response may report an incomplete frame.
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.5)
    finally:
        client.close()
        stream_client.close()
        owner.close()


def test_human_forward_only_allows_human_surface_with_forwarded_loopback_origin(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    with store._transaction(readonly=True) as db:
        owner_id = db.execute(
            "SELECT owner_human_id FROM human_bootstrap WHERE singleton=1"
        ).fetchone()[0]
    project_id = store.create_project("Shared synthetic", tmp_path)["id"]
    remote_id = create_human(store, "Remote human")["human_id"]
    space_id = create_shared_space(store, owner_id, "Shared synthetic")["space_id"]
    add_space_member(store, owner_id, space_id, remote_id)
    move_project_to_shared_space(store, owner_id, project_id, space_id)
    grant_project(store, owner_id, project_id, remote_id, "viewer")
    human_token = issue_human_session(store, remote_id)["token"]
    invited_id = create_human(store, "Invited human")["human_id"]
    invitation = create_human_invitation(store, invited_id)["invitation"]
    owner = WorkbenchHTTP(store, token="owner-test-only")
    preview_path = owner.resource_preview("<p>OWNER_PRIVATE_PREVIEW_MARKER</p>")
    forward = HumanForwardHTTP(owner)
    thread = threading.Thread(target=forward.serve_forever, daemon=True)
    thread.start()
    try:
        host = (
            "localhost:49001"  # The browser's SSH local-forward port differs from the server port.
        )
        origin = "http://" + host
        status, page = _fetch(forward, "/human", host=host)
        assert status == 200 and b'id="enroll-form"' in page
        assert _fetch(forward, "/workbench-assets/human.js", host=host)[0] == 200
        status, body = _fetch(
            forward, "/api/workbench/human/self", host=host, origin=origin, token=human_token
        )
        assert status == 200 and json.loads(body)["human_id"] == remote_id
        status, enrolled = _post(
            forward, "/api/workbench/human/enroll", "", {"invitation": invitation}, host=host
        )
        assert status == 200 and enrolled["human_id"] == invited_id
        status, body = _fetch(
            forward, "/api/workbench/human/self", host=host, origin=origin, token=enrolled["token"]
        )
        assert status == 200 and json.loads(body)["human_id"] == invited_id
        scoped_route = "/api/workbench/human/projects/" + project_id + "/task-counts"
        assert _fetch(forward, scoped_route, host=host, token=human_token)[0] == 200
        assert "OWNER_PRIVATE_PREVIEW_MARKER" in owner.preview_content(
            preview_path.removeprefix("/workbench-preview/")
        )  # A real owner-side capability exists; this test serves only the Human port.
        assert _fetch(forward, preview_path, host=host, token=human_token)[0] == 404
        for route in (
            "/",
            "/workbench",
            "/workbench-assets/workbench.js",
            "/api/workbench/bootstrap",
            "/api/workbench/human-sessions",
        ):
            assert _fetch(forward, route, host=host, token=owner.token)[0] == 404
        assert _fetch(forward, "/api/workbench/human/self", host=host, token=owner.token)[0] == 401
        assert (
            _fetch(
                forward, "/api/workbench/human/self", host="attacker.test:49001", token=human_token
            )[0]
            == 403
        )
        assert (
            _fetch(
                forward,
                "/api/workbench/human/self",
                host=host,
                origin="http://attacker.test",
                token=human_token,
            )[0]
            == 403
        )
        revoke_project(store, owner_id, project_id, remote_id)
        assert _fetch(forward, scoped_route, host=host, token=human_token)[0] != 200
        status, body = _fetch(
            forward, "/api/workbench/human/projects", host=host, token=human_token
        )
        assert status == 200
        assert project_id not in {item["id"] for item in json.loads(body)["projects"]}
    finally:
        forward.shutdown()
        thread.join(timeout=3)
        forward.server_close()
        owner.close()


def test_owner_can_enable_and_stop_human_forward_but_human_bearer_cannot(tmp_path, monkeypatch):
    store = WorkbenchStore(tmp_path / "home")
    second = create_human(store, "Second")
    create_managed_private_project(store, second["human_id"], "Private to second")
    with store._transaction(readonly=True) as db:
        human_id = db.execute(
            "SELECT owner_human_id FROM human_bootstrap WHERE singleton=1"
        ).fetchone()[0]
    human_token = issue_human_session(store, human_id)["token"]
    owner = WorkbenchHTTP(store, token="owner-test-only")
    thread = threading.Thread(target=owner.serve_forever, daemon=True)
    thread.start()

    def post(route, token):
        request = urllib.request.Request(
            owner.endpoint + "/api/workbench/" + route,
            data=b"{}",
            method="POST",
            headers={"Authorization": "Bearer " + token},
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    try:
        # In multi-Human mode the legacy whole-store bootstrap is intentionally blocked.
        assert _fetch(owner, "/api/workbench/bootstrap", token=owner.token)[0] == 409
        assert _fetch(owner, "/api/workbench/human-forward/status", token=human_token)[0] == 401
        status, body = _fetch(owner, "/api/workbench/human-forward/status", token=owner.token)
        assert status == 200 and json.loads(body) == {"enabled": False, "port": None}
        assert post("human-forward/start", human_token)[0] == 401
        status, started = post("human-forward/start", owner.token)
        assert status == 200 and started["enabled"] is True
        assert owner.human_forward_status() == started
        status, body = _fetch(owner, "/api/workbench/human-forward/status", token=owner.token)
        assert status == 200 and json.loads(body) == started
        assert owner.human_forward.server_address[0] == "127.0.0.1"
        assert _fetch(owner.human_forward, "/human")[0] == 200
        assert post("human-forward/start", owner.token)[1] == started
        assert post("human-forward/stop", human_token)[0] == 401
        assert post("human-forward/stop", owner.token) == (200, {"stopped": True})
        assert owner.human_forward_status() == {"enabled": False, "port": None}
        # A physical device owner may run its listener in multi-Human mode,
        # while project invitations remain behind scoped authorization.
        assert _fetch(owner, "/api/workbench/fleet/status", token=human_token)[0] == 401
        import agent_mailbox.workbench_fleet_lan as lan

        monkeypatch.setattr(
            lan,
            "discover_lan",
            lambda: [{"base_url": "https://192.168.1.12:49001", "verified": False}],
        )
        assert _fetch(owner, "/api/workbench/fleet/discover", token=human_token)[0] == 401
        status, body = _fetch(owner, "/api/workbench/fleet/discover", token=owner.token)
        assert status == 200 and json.loads(body) == {
            "devices": [{"base_url": "https://192.168.1.12:49001", "verified": False}],
            "trusted": False,
        }
        status, body = _fetch(owner, "/api/workbench/fleet/status", token=owner.token)
        assert status == 200 and json.loads(body) == {
            "enabled": False,
            "base_url": None,
            "lan_advertised": False,
            "lan_error": None,
        }
        assert post("fleet/start", human_token)[0] == 401
        status, fleet = post("fleet/start", owner.token)
        assert status == 200 and fleet["base_url"].startswith("https://127.0.0.1:")
        status, body = _fetch(owner, "/api/workbench/fleet/status", token=owner.token)
        assert status == 200 and json.loads(body) == {
            "enabled": True,
            "base_url": fleet["base_url"],
            "lan_advertised": False,
            "lan_error": None,
        }
        assert post("fleet/invite", owner.token)[0] == 409
        assert post("fleet/stop", human_token)[0] == 401
        assert post("fleet/stop", owner.token) == (200, {"stopped": True})
        status, body = _fetch(owner, "/api/workbench/fleet/status", token=owner.token)
        assert status == 200 and json.loads(body) == {
            "enabled": False,
            "base_url": None,
            "lan_advertised": False,
            "lan_error": None,
        }
    finally:
        owner.shutdown()
        thread.join(timeout=3)
        owner.close()


def test_project_owner_scopes_device_invite_and_existing_device_access(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    first_project = store.create_project("First owner's project", tmp_path)["id"]
    with store._transaction(readonly=True) as db:
        first_human = db.execute(
            "SELECT owner_human_id FROM human_bootstrap WHERE singleton=1"
        ).fetchone()[0]
    owner = WorkbenchHTTP(store, token="device-owner-only")
    host = owner.endpoint.removeprefix("http://")
    thread = threading.Thread(target=owner.serve_forever, daemon=True)
    thread.start()
    try:
        owner.start_fleet()
        legacy_invite = owner.fleet.issue_invite([first_project])
        legacy_device = FleetClient(tmp_path / "legacy-device", legacy_invite)
        assert legacy_device.context(first_project)["project"]["id"] == first_project

        second_human = create_human(store, "Second Human")["human_id"]
        second_project = create_managed_private_project(
            store, second_human, "Second private project"
        )["project_id"]
        first_token = issue_human_session(store, first_human)["token"]
        second_token = issue_human_session(store, second_human)["token"]
        hidden = _post(
            owner,
            f"/api/workbench/human/projects/{first_project}/fleet/invites",
            second_token,
            {},
            host=host,
        )
        missing = _post(
            owner,
            "/api/workbench/human/projects/project_missing/fleet/invites",
            second_token,
            {},
            host=host,
        )
        assert hidden == missing
        hidden = _post(
            owner,
            f"/api/workbench/human/projects/{second_project}/fleet/invites",
            first_token,
            {},
            host=host,
        )
        missing = _post(
            owner,
            "/api/workbench/human/projects/project_missing/fleet/invites",
            first_token,
            {},
            host=host,
        )
        assert hidden == missing
        assert (
            _post(
                owner,
                "/api/workbench/fleet/invite",
                owner.token,
                {"project_ids": [first_project]},
                host=host,
            )[0]
            == 409
        )
        with pytest.raises(WorkbenchError) as legacy_error:
            legacy_device.context(first_project)
        assert legacy_error.value.code == "permission_denied"

        status, invite = _post(
            owner,
            f"/api/workbench/human/projects/{first_project}/fleet/invites",
            first_token,
            {},
            host=host,
        )
        assert status == 200 and invite["project_ids"] == [first_project]
        device = FleetClient(tmp_path / "scoped-device", invite)
        assert device.context(first_project)["project"]["id"] == first_project
        with pytest.raises(WorkbenchError) as wrong_project:
            device.context(second_project)
        assert wrong_project.value.code == "permission_denied"

        owner.start_human_forward()
        status, remote_invite = _post(
            owner.human_forward,
            f"/api/workbench/human/projects/{first_project}/fleet/invites",
            first_token,
            {},
        )
        assert status == 200 and remote_invite["project_ids"] == [first_project]
        forwarded_device = FleetClient(tmp_path / "forwarded-device", remote_invite)
        assert forwarded_device.context(first_project)["project"]["id"] == first_project
        listing = f"/api/workbench/human/projects/{first_project}/fleet/devices"
        status, body = _fetch(owner.human_forward, listing, token=first_token)
        assert status == 200
        devices = json.loads(body)["devices"]
        assert {row["device_id"] for row in devices} == {
            device.credentials["device_id"],
            forwarded_device.credentials["device_id"],
        }
        assert "token" not in json.dumps(devices)
        hidden = _fetch(owner.human_forward, listing, token=second_token)
        missing = _fetch(
            owner.human_forward,
            "/api/workbench/human/projects/project_missing/fleet/devices",
            token=second_token,
        )
        assert hidden == missing
        revoke_path = listing + "/" + forwarded_device.credentials["device_id"] + "/revoke"
        hidden_revoke = _post(owner.human_forward, revoke_path, second_token, {})
        missing_revoke = _post(
            owner.human_forward,
            "/api/workbench/human/projects/project_missing/fleet/devices/"
            + forwarded_device.credentials["device_id"]
            + "/revoke",
            second_token,
            {},
        )
        assert hidden_revoke == missing_revoke
        assert forwarded_device.context(first_project)["project"]["id"] == first_project
        status, revoked_project = _post(
            owner.human_forward,
            revoke_path,
            first_token,
            {},
        )
        assert status == 200 and revoked_project == {"revoked": True}
        with pytest.raises(WorkbenchError) as revoked_device:
            forwarded_device.context(first_project)
        assert revoked_device.value.code == "permission_denied"
        assert device.context(first_project)["project"]["id"] == first_project

        status, offline_invite = _post(
            owner.human_forward,
            f"/api/workbench/human/projects/{first_project}/fleet/invites",
            first_token,
            {},
        )
        assert status == 200
        offline_device = FleetClient(tmp_path / "offline-device", offline_invite)
        assert offline_device.context(first_project)["project"]["id"] == first_project
        assert _post(owner, "/api/workbench/fleet/stop", owner.token, {}, host=host)[0] == 200
        status, body = _fetch(owner.human_forward, listing, token=first_token)
        assert status == 200
        assert offline_device.credentials["device_id"] in {
            row["device_id"] for row in json.loads(body)["devices"]
        }
        status, offline_revoke = _post(
            owner.human_forward,
            listing + "/" + offline_device.credentials["device_id"] + "/revoke",
            first_token,
            {},
        )
        assert status == 200 and offline_revoke == {"revoked": True}
        owner.start_fleet()
        assert owner.fleet.state["devices"][offline_device.credentials["device_id"]]["revoked"]
        device = FleetClient(
            device.store.root, {**device.credentials, "base_url": owner.fleet.base_url}
        )
        assert device.context(first_project)["project"]["id"] == first_project

        status, pending_invite = _post(
            owner,
            f"/api/workbench/human/projects/{first_project}/fleet/invites",
            first_token,
            {},
            host=host,
        )
        assert status == 200

        with store._transaction() as db:
            db.execute(
                "UPDATE project_human_grants SET revoked_at=? WHERE project_id=? AND human_id=?",
                ("2026-10-16T00:00:00Z", first_project, first_human),
            )
        with pytest.raises(WorkbenchError) as revoked:
            device.context(first_project)
        assert revoked.value.code == "permission_denied"
        with pytest.raises(WorkbenchError) as late_pair:
            FleetClient(tmp_path / "late-device", pending_invite)
        assert late_pair.value.code == "permission_denied"
    finally:
        owner.shutdown()
        thread.join(timeout=3)
        owner.close()


def test_local_device_owner_can_join_scoped_coordinator_in_multi_human_mode(tmp_path, monkeypatch):
    coordinator_store = WorkbenchStore(tmp_path / "coordinator")
    project_id = coordinator_store.create_project("Shared work", tmp_path)["id"]
    other_project_id = coordinator_store.create_project("Not shared", tmp_path)["id"]
    with coordinator_store._transaction(readonly=True) as db:
        project_owner = db.execute(
            "SELECT owner_human_id FROM human_bootstrap WHERE singleton=1"
        ).fetchone()[0]
    coordinator = WorkbenchHTTP(coordinator_store, token="coordinator-owner")
    coordinator_thread = threading.Thread(target=coordinator.serve_forever, daemon=True)
    coordinator_thread.start()
    recipient_store = WorkbenchStore(tmp_path / "recipient")
    recipient_store.create_project("Local work", tmp_path)
    with recipient_store._transaction(readonly=True) as db:
        recipient_human = db.execute(
            "SELECT owner_human_id FROM human_bootstrap WHERE singleton=1"
        ).fetchone()[0]
    create_managed_private_project(
        recipient_store,
        create_human(recipient_store, "Second")["human_id"],
        "Second private",
    )
    recipient = WorkbenchHTTP(recipient_store, token="recipient-device-owner")
    recipient_thread = threading.Thread(target=recipient.serve_forever, daemon=True)
    recipient_thread.start()
    try:
        coordinator.start_fleet()
        invite = coordinator.issue_project_fleet_invite(project_owner, project_id)
        recipient_token = issue_human_session(recipient_store, recipient_human)["token"]
        host = recipient.endpoint.removeprefix("http://")
        assert _fetch(recipient, "/api/workbench/bootstrap", token=recipient.token)[0] == 409
        assert _fetch(recipient, "/api/workbench/fleet/connection", token=recipient_token)[0] == 401
        assert (
            _post(
                recipient,
                "/api/workbench/fleet/join",
                recipient_token,
                {"invite": invite},
                host=host,
            )[0]
            == 401
        )
        status, joined = _post(
            recipient, "/api/workbench/fleet/join", recipient.token, {"invite": invite}, host=host
        )
        assert status == 200 and joined["remote"]["paired"] is True
        status, body = _fetch(recipient, "/api/workbench/fleet/connection", token=recipient.token)
        assert status == 200 and json.loads(body) == {
            "configured": True,
            "saved_pairing": True,
            "base_url": coordinator.fleet.base_url,
            "projects": [{"id": project_id, "name": "Shared work"}],
            "mappings": {},
        }
        local_folder = tmp_path / "recipient-work"
        local_folder.mkdir()
        assert (
            _post(
                recipient,
                "/api/workbench/fleet/map",
                recipient_token,
                {"project_id": project_id, "path": str(local_folder)},
                host=host,
            )[0]
            == 401
        )
        status, mapped = _post(
            recipient,
            "/api/workbench/fleet/map",
            recipient.token,
            {"project_id": project_id, "path": str(local_folder)},
            host=host,
        )
        assert status == 200 and mapped == {"project_id": project_id, "path": str(local_folder)}
        assert recipient.remote_client.mappings[project_id] == str(local_folder)
        status, error = _post(
            recipient,
            "/api/workbench/fleet/map",
            recipient.token,
            {"project_id": other_project_id, "path": str(local_folder)},
            host=host,
        )
        assert status == 400 and error["error"]["code"] == "permission_denied"
        assert other_project_id not in recipient.remote_client.mappings
        assert (
            _post(
                recipient,
                "/api/workbench/fleet/map",
                recipient.token,
                {"project_id": project_id, "path": str(tmp_path / "missing")},
                host=host,
            )[0]
            == 400
        )
        monkeypatch.setattr(
            "agent_mailbox.workbench.discover_employees",
            lambda: [
                {"kind": "codex", "name": "Test CLI", "status": "installed", "detail": "fixture"},
                {
                    "kind": "workbuddy",
                    "name": "Other host",
                    "status": "installed",
                    "detail": "fixture",
                },
            ],
        )
        assert (
            _fetch(recipient, "/api/workbench/fleet/available-employees", token=recipient_token)[0]
            == 401
        )
        status, body = _fetch(
            recipient, "/api/workbench/fleet/available-employees", token=recipient.token
        )
        assert status == 200 and json.loads(body) == {
            "employees": [{"kind": "codex", "name": "Test CLI"}],
        }
        assert (
            _post(
                recipient,
                "/api/workbench/fleet/employee",
                recipient_token,
                {"project_id": project_id, "name": "Fixture worker", "kind": "codex"},
                host=host,
            )[0]
            == 401
        )
        status, added = _post(
            recipient,
            "/api/workbench/fleet/employee",
            recipient.token,
            {"project_id": project_id, "name": "Fixture worker", "kind": "codex"},
            host=host,
        )
        assert status == 200 and added["name"] == "Fixture worker"
        status, unsupported = _post(
            recipient,
            "/api/workbench/fleet/employee",
            recipient.token,
            {"project_id": project_id, "name": "Other host", "kind": "workbuddy"},
            host=host,
        )
        assert status == 400 and unsupported["error"]["code"] == "AGENT_UNAVAILABLE"
        status, out_of_scope = _post(
            recipient,
            "/api/workbench/fleet/employee",
            recipient.token,
            {"project_id": other_project_id, "name": "Wrong scope", "kind": "codex"},
            host=host,
        )
        assert status == 400 and out_of_scope["error"]["code"] == "permission_denied"
        moved_address = coordinator.fleet.base_url.replace("127.0.0.1", "localhost")
        assert (
            _post(
                recipient,
                "/api/workbench/fleet/relocate",
                recipient_token,
                {"base_url": moved_address},
                host=host,
            )[0]
            == 401
        )
        status, relocated = _post(
            recipient,
            "/api/workbench/fleet/relocate",
            recipient.token,
            {"base_url": moved_address},
            host=host,
        )
        assert status == 200, relocated
        assert relocated["remote"]["base_url"] == moved_address
        status, invalid = _post(
            recipient,
            "/api/workbench/fleet/relocate",
            recipient.token,
            {"base_url": moved_address.replace("https:", "http:")},
            host=host,
        )
        assert status == 400 and invalid["error"]["code"] == "invalid_field"
        assert recipient.remote_client.base_url == moved_address
        assert (
            _post(recipient, "/api/workbench/fleet/leave", recipient_token, {}, host=host)[0] == 401
        )
        pending = recipient.remote_client.directory / "terminal-receipts.json"
        pending.write_text('{"task_pending": {}}')
        status, busy = _post(
            recipient, "/api/workbench/fleet/leave", recipient.token, {}, host=host
        )
        assert status == 400 and busy["error"]["code"] == "DEVICE_BUSY"
        assert recipient.remote_client is not None
        pending.write_text("{}")
        status, left = _post(
            recipient, "/api/workbench/fleet/leave", recipient.token, {}, host=host
        )
        assert status == 200 and left == {"disconnected": True}, (status, left)
        assert recipient.remote_client is None
        assert not (recipient_store.root / "workbench/fleet/client.json").exists()
        assert not (recipient_store.root / "workbench/fleet/projects.json").exists()
        fresh_invite = coordinator.issue_project_fleet_invite(project_owner, project_id)
        status, old_identity = _post(
            recipient,
            "/api/workbench/fleet/join",
            recipient.token,
            {"invite": fresh_invite},
            host=host,
        )
        assert status == 400 and old_identity["error"]["code"] == "permission_denied"
        coordinator.revoke_project_fleet_device(
            project_owner, project_id, recipient_store.local_node()["id"]
        )
        status, rejoined = _post(
            recipient,
            "/api/workbench/fleet/join",
            recipient.token,
            {"invite": fresh_invite},
            host=host,
        )
        assert status == 200, (status, rejoined)
    finally:
        recipient.shutdown()
        recipient_thread.join(timeout=3)
        recipient.close()
        coordinator.shutdown()
        coordinator_thread.join(timeout=3)
        coordinator.close()


def test_human_forward_restores_after_restart_and_fails_closed_when_port_taken(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    first = WorkbenchHTTP(store, token="owner-test-only")
    try:
        port = first.start_human_forward()["port"]
        saved = first.human_forward_path
        assert json.loads(saved.read_text()) == {"enabled": True, "port": port}
        assert saved.stat().st_mode & 0o777 == 0o600
    finally:
        first.close()

    second = WorkbenchHTTP(store, token="owner-test-only")
    try:
        assert second.human_forward_status() == {"enabled": True, "port": port}
    finally:
        second.close()

    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", port))
    blocker.listen()
    try:
        third = WorkbenchHTTP(store, token="owner-test-only")
        try:
            status = third.human_forward_status()
            assert status["enabled"] is False and status["port"] is None
            assert status["error"]["code"] == "HUMAN_FORWARD_RESTORE_FAILED"
            assert json.loads(saved.read_text()) == {"enabled": True, "port": port}
        finally:
            third.close()
    finally:
        blocker.close()

    fourth = WorkbenchHTTP(store, token="owner-test-only")
    try:
        assert fourth.human_forward_status() == {"enabled": True, "port": port}
        fourth.stop_human_forward()
        assert json.loads(saved.read_text()) == {"enabled": False}
    finally:
        fourth.close()
    fifth = WorkbenchHTTP(store, token="owner-test-only")
    try:
        assert fifth.human_forward_status() == {"enabled": False, "port": None}
    finally:
        fifth.close()


def test_forwarded_project_owner_reviews_without_granting_viewer_final_authority(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    project_id = store.create_project("Review synthetic", tmp_path)["id"]
    employee_id = store.create_employee("Synthetic agent", "codex", project_id)["id"]
    task_id = store.create_task(project_id, "Ready for Human", "Synthetic prompt", employee_id)[
        "id"
    ]
    other_project = store.create_project("Other private", tmp_path)["id"]
    other_employee = store.create_employee("Other synthetic", "codex", other_project)["id"]
    other_task = store.create_task(other_project, "Other task", "Other prompt", other_employee)[
        "id"
    ]
    with store._transaction() as db:
        db.execute(
            "UPDATE tasks SET status='review',result=? WHERE id=?", ("Synthetic delivery", task_id)
        )
    with store._transaction(readonly=True) as db:
        owner_id = db.execute(
            "SELECT owner_human_id FROM human_bootstrap WHERE singleton=1"
        ).fetchone()[0]
    viewer_id = create_human(store, "Viewer")["human_id"]
    space_id = create_shared_space(store, owner_id, "Review synthetic")["space_id"]
    add_space_member(store, owner_id, space_id, viewer_id)
    move_project_to_shared_space(store, owner_id, project_id, space_id)
    grant_project(store, owner_id, project_id, viewer_id, "viewer")
    owner_token = issue_human_session(store, owner_id)["token"]
    viewer_token = issue_human_session(store, viewer_id)["token"]
    owner = WorkbenchHTTP(store, token="owner-test-only")
    forward = HumanForwardHTTP(owner)
    thread = threading.Thread(target=forward.serve_forever, daemon=True)
    thread.start()
    route = f"/api/workbench/human/projects/{project_id}/tasks/{task_id}/review"
    try:
        assert _fetch(forward, route, token=viewer_token)[0] == 400
        assert (
            _post(forward, route, viewer_token, {"decision": "accept", "note": "No authority"})[0]
            == 400
        )
        assert store.get_task(task_id)["status"] == "review"
        status, material = _fetch(forward, route, token=owner_token)
        assert status == 200
        assert json.loads(material) == {
            "task_id": task_id,
            "title": "Ready for Human",
            "status": "review",
            "summary": "Synthetic delivery",
            "verification": {"status": "not_verified"},
            "diff": "",
            "files": [],
            "capture_error": None,
        }
        wrong_route = f"/api/workbench/human/projects/project_missing/tasks/{task_id}/review"
        assert (
            _post(
                forward, wrong_route, owner_token, {"decision": "accept", "note": "Wrong project"}
            )[0]
            == 400
        )
        wrong_task = f"/api/workbench/human/projects/{project_id}/tasks/{other_task}/review"
        missing_task = f"/api/workbench/human/projects/{project_id}/tasks/task_missing/review"
        assert _post(
            forward, wrong_task, owner_token, {"decision": "accept", "note": "Wrong task"}
        ) == _post(
            forward, missing_task, owner_token, {"decision": "accept", "note": "Missing task"}
        )
        assert _post(
            forward, route, owner_token, {"decision": "accept", "note": "Reviewed remotely"}
        ) == (200, {"task_id": task_id, "status": "done"})
        assert store.get_task(task_id)["status"] == "done"
        events = store.governance_events(project_id)
        review = next(item for item in events if item["type"] == "task_reviewed")
        assert review["actor"] == "human:" + owner_id
        assert (
            _post(forward, route, owner_token, {"decision": "reject", "note": "Too late"})[0] == 400
        )
    finally:
        forward.shutdown()
        thread.join(timeout=3)
        forward.server_close()
        owner.close()


def test_forwarded_review_permission_is_explicit_and_revocation_reaches_open_tab(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    project_id = store.create_project("Shared synthetic", tmp_path)["id"]
    employee_id = store.create_employee("Synthetic agent", "codex", project_id)["id"]
    task_id = store.create_task(project_id, "Pending review", "Synthetic prompt", employee_id)["id"]
    second_task_id = store.create_task(project_id, "Later review", "Synthetic prompt", employee_id)[
        "id"
    ]
    with store._transaction() as db:
        db.execute(
            "UPDATE tasks SET status='review',result='Synthetic delivery' WHERE id IN (?,?)",
            (task_id, second_task_id),
        )
    with store._transaction(readonly=True) as db:
        owner_id = db.execute(
            "SELECT owner_human_id FROM human_bootstrap WHERE singleton=1"
        ).fetchone()[0]
    viewer_id = create_human(store, "Viewer")["human_id"]
    space_id = create_shared_space(store, owner_id, "Shared synthetic")["space_id"]
    add_space_member(store, owner_id, space_id, viewer_id)
    move_project_to_shared_space(store, owner_id, project_id, space_id)
    grant_project(store, owner_id, project_id, viewer_id, "viewer")
    owner_token = issue_human_session(store, owner_id)["token"]
    viewer_token = issue_human_session(store, viewer_id)["token"]
    owner = WorkbenchHTTP(store, token="owner-test-only")
    forward = HumanForwardHTTP(owner)
    thread = threading.Thread(target=forward.serve_forever, daemon=True)
    thread.start()
    base = f"/api/workbench/human/projects/{project_id}"
    review = f"{base}/tasks/{task_id}/review"
    later_review = f"{base}/tasks/{second_task_id}/review"
    reviewers = f"{base}/reviewers"
    try:
        assert _fetch(forward, review, token=viewer_token)[0] == 400
        stream_request = urllib.request.Request(
            forward.endpoint + "/api/workbench/human/changes",
            headers={"Host": "localhost:49001", "Authorization": "Bearer " + viewer_token},
        )
        with urllib.request.urlopen(stream_request, timeout=3) as stream:
            assert json.loads(stream.readline()) == {"ready": True}
            assert _post(forward, reviewers, viewer_token, {"human_id": viewer_id})[0] == 400
            assert _post(forward, reviewers, owner_token, {"human_id": viewer_id}) == (
                200,
                {"project_id": project_id, "human_id": viewer_id, "can_review": True},
            )
            assert json.loads(stream.readline()) == {"access_changed": True}
            assert _fetch(forward, review, token=viewer_token)[0] == 200
            status, projects = _fetch(forward, "/api/workbench/human/projects", token=viewer_token)
            assert status == 200 and json.loads(projects)["projects"][0]["can_review"] == 1
            assert (
                _post(
                    forward,
                    base + "/grants",
                    viewer_token,
                    {"human_id": viewer_id, "role": "editor"},
                )[0]
                == 400
            )
            assert _post(
                forward, review, viewer_token, {"decision": "accept", "note": "I reviewed this"}
            ) == (200, {"task_id": task_id, "status": "done"})
            assert store.get_task(task_id)["status"] == "done"
            assert json.loads(stream.readline()) == {"tasks_changed": True}
            review_event = next(
                item
                for item in store.governance_events(project_id)
                if item["type"] == "task_reviewed" and item["task_id"] == task_id
            )
            assert review_event["actor"] == "human:" + viewer_id
            assert _delete(forward, reviewers + "/" + viewer_id, owner_token) == (
                200,
                {"revoked": True},
            )
            assert json.loads(stream.readline()) == {"access_changed": True}
            assert _fetch(forward, later_review, token=viewer_token)[0] == 400
            assert (
                _post(
                    forward, later_review, viewer_token, {"decision": "accept", "note": "Revoked"}
                )[0]
                == 400
            )
            assert store.get_task(second_task_id)["status"] == "review"
            assert _fetch(forward, f"{base}/tasks", token=viewer_token)[0] == 200
    finally:
        forward.shutdown()
        thread.join(timeout=3)
        forward.server_close()
        owner.close()
