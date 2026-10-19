"""Owner plan boundary: browser identifiers never become host paths or grants."""

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agent_mailbox import workbench_hermes_catalog as catalog
from agent_mailbox import workbench_hermes_installer as installer
from agent_mailbox.workbench import WorkbenchHTTP
from agent_mailbox.workbench_mail_sessions import create_session
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


@pytest.fixture
def case(tmp_path, monkeypatch):
    home = tmp_path.resolve()
    user = home / "user"
    user.mkdir(mode=0o700)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user))
    monkeypatch.setattr(installer, "SOCKET_ROOT", user)
    profile = user / ".hermes"
    profile.mkdir(mode=0o700)
    python = profile / "hermes-agent/.venv/bin/python"
    python.parent.mkdir(parents=True, mode=0o700)
    python.write_text("synthetic")
    store = WorkbenchStore(home / "data")
    project = store.create_project("Synthetic", home)
    employee = store.create_employee("Synthetic", "hermes", project["id"])
    (home / "foreign").mkdir(mode=0o700)
    foreign = store.create_project("Foreign", home / "foreign")
    monkeypatch.setattr(
        catalog,
        "_discover",
        lambda: (
            profile,
            python,
            {
                "state": "profile_candidates",
                "profiles": [{"home": str(profile), "profile_id": "default"}],
            },
        ),
    )
    profile_id = catalog.choices(store, employee["id"], project["id"])["profiles"][0]["id"]
    calls = []
    native_id = "plan_" + "a" * 32

    def child(runtime, selected, request, **kwargs):
        calls.append((runtime, selected, request, kwargs))
        assert runtime == python and selected == profile
        assert request["profile"] == str(profile) and request["version"] == 1
        if request["operation"] == "preview_discovery":
            assert set(request["options"]) == {"socket_path"}
            assert request["options"]["socket_path"].startswith(str(user / "ambh-"))
            return {
                "state": "preview",
                "plan_id": native_id,
                "requires_confirmation": True,
                "will_activate": False,
                "will_write_injection_grant": False,
                "session_verified": False,
                "delivery_verified": False,
            }
        assert request["plan_id"] == native_id
        if request["operation"] in {"apply", "remove"}:
            assert request["confirmed"] is True
        return {
            "state": {
                "apply": "prepared_not_activated",
                "status": "prepared_not_activated",
                "remove": "preparation_removed",
            }[request["operation"]],
            "plan_id": native_id,
            "activation_requested": False,
            "session_verified": False,
            "delivery_verified": False,
        }

    monkeypatch.setattr(installer.process, "run", child)
    return store, project, foreign, employee, profile_id, calls


def test_plan_uses_opaque_browser_id_and_project_scoped_record(case):
    store, project, foreign, employee, profile_id, calls = case
    empty = installer.installation(
        store, employee["id"], project["id"], "lookup", profile_id=profile_id
    )
    assert empty["state"] == "not_prepared_here" and not calls
    preview = installer.installation(
        store, employee["id"], project["id"], "preview", profile_id=profile_id
    )
    assert len(preview["plan_id"]) == 32 and preview["plan_id"] != "a" * 32
    assert preview["state"] == "preview" and preview["will_write_injection_grant"] is False
    with pytest.raises(WorkbenchError):
        installer.installation(
            store,
            employee["id"],
            foreign["id"],
            "apply",
            plan_id=preview["plan_id"],
            confirmed=True,
        )
    with pytest.raises(WorkbenchError):
        installer.installation(
            store, employee["id"], project["id"], "apply", plan_id="0" * 32, confirmed=True
        )
    assert len(calls) == 1
    with pytest.raises(WorkbenchError):
        installer.installation(
            store, employee["id"], project["id"], "apply", plan_id=preview["plan_id"]
        )
    assert len(calls) == 1
    applied = installer.installation(
        store, employee["id"], project["id"], "apply", plan_id=preview["plan_id"], confirmed=True
    )
    assert applied["state"] == "prepared_not_activated"
    looked_up = installer.installation(
        store, employee["id"], project["id"], "lookup", profile_id=profile_id
    )
    assert looked_up["plan_id"] == preview["plan_id"]
    assert looked_up["state"] == "prepared_not_activated"
    assert (
        installer.installation(
            store, employee["id"], project["id"], "status", plan_id=preview["plan_id"]
        )["state"]
        == "prepared_not_activated"
    )
    assert (
        installer.installation(
            store,
            employee["id"],
            project["id"],
            "remove",
            plan_id=preview["plan_id"],
            confirmed=True,
        )["state"]
        == "preparation_removed"
    )
    assert len(calls) == 5
    assert not any(str(profile_id) in json.dumps(v) for v in calls[-1][2:3])


def test_wrong_profile_and_permission_claim_refuse_before_record(case, monkeypatch):
    store, project, _, employee, profile_id, calls = case
    with pytest.raises(WorkbenchError):
        installer.installation(
            store, employee["id"], project["id"], "preview", profile_id="hermes_profile_" + "0" * 64
        )
    assert not calls

    def grant(*args, **kwargs):
        return {
            "plan_id": "plan_" + "b" * 32,
            "state": "preview",
            "will_activate": False,
            "will_write_injection_grant": True,
            "session_verified": False,
            "delivery_verified": False,
        }

    monkeypatch.setattr(installer.process, "run", grant)
    with pytest.raises(WorkbenchError):
        installer.installation(
            store, employee["id"], project["id"], "preview", profile_id=profile_id
        )
    assert list((store.directory / "hermes-discovery-plans").glob("*.json")) == []


def test_lookup_prefers_prepared_plan_over_newer_unapplied_preview(case, monkeypatch):
    store, project, _, employee, profile_id, _ = case
    states = {}

    def child(_runtime, _profile, request, **_kwargs):
        operation = request["operation"]
        if operation == "preview_discovery":
            native_id = "plan_" + format(len(states) + 1, "032x")
            states[native_id] = "preview"
            return {
                "plan_id": native_id,
                "state": "preview",
                "requires_confirmation": True,
                "will_activate": False,
                "will_write_injection_grant": False,
                "session_verified": False,
                "delivery_verified": False,
            }
        native_id = request["plan_id"]
        if operation == "apply":
            states[native_id] = "prepared_not_activated"
        return {
            "plan_id": native_id,
            "state": states[native_id],
            "session_verified": False,
            "delivery_verified": False,
        }

    monkeypatch.setattr(installer.process, "run", child)
    older = installer.installation(
        store, employee["id"], project["id"], "preview", profile_id=profile_id
    )
    installer.installation(store, employee["id"], project["id"], "preview", profile_id=profile_id)
    installer.installation(
        store, employee["id"], project["id"], "apply", plan_id=older["plan_id"], confirmed=True
    )
    recovered = installer.installation(
        store, employee["id"], project["id"], "lookup", profile_id=profile_id
    )
    assert recovered["state"] == "prepared_not_activated"
    assert recovered["plan_id"] == older["plan_id"]


def test_http_owner_only_rejects_paths_and_employee_token(case):
    store, project, _, employee, profile_id, calls = case
    session = create_session(store, employee["id"], project["id"], "Synthetic")
    server = WorkbenchHTTP(store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = server.endpoint + "/api/workbench/mailbox/installers/hermes/diagnostic"

    def post(token, data):
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(data).encode(),
            method="POST",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    data = {
        "employee_id": employee["id"],
        "project_id": project["id"],
        "action": "preview",
        "profile_id": profile_id,
    }
    try:
        for token in ("", session["token"]):
            with pytest.raises(urllib.error.HTTPError) as error:
                post(token, data)
            assert error.value.code == 401
        assert not calls
        with pytest.raises(urllib.error.HTTPError) as error:
            post(server.token, {**data, "path": "/tmp/foreign"})
        assert error.value.code == 400 and not calls
        result = post(server.token, data)
        assert result["state"] == "preview" and "profile" not in result
        assert len(calls) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        server.instance_lock.close()
