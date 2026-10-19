"""DSH profile discovery is owner-only metadata, never receiver activation."""

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from agent_mailbox import workbench_dsh_catalog as catalog
from agent_mailbox.workbench import WorkbenchHTTP
from agent_mailbox.workbench_mail_sessions import create_session
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


@pytest.fixture
def case(tmp_path, monkeypatch):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Synthetic", tmp_path)
    employee = store.create_employee("DSH", "deepseek", project["id"], connection_type="app")
    user = tmp_path / "user"
    profiles = user / ".dsh/profiles"
    profiles.mkdir(parents=True)
    (user / ".dsh").chmod(0o700)
    profile = profiles / "desktop"
    profile.mkdir(mode=0o700)
    (profile / "package.json").write_text("{}")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user))
    return store, project, employee, profiles


def test_metadata_only_and_missing_installation(case):
    store, project, employee, profiles = case
    value = catalog.choices(store, employee["id"], project["id"])
    assert value["state"] == "profile_candidates"
    assert value["host_kind"] == "deepseek"
    assert value["session_verified"] is value["delivery_verified"] is False
    assert value["profiles"] == [
        {
            "id": catalog._identifier(employee["id"], project["id"], profiles / "desktop"),
            "label": "desktop",
            "candidate_only": True,
        }
    ]
    assert str(profiles) not in json.dumps(value)
    (profiles / "desktop/package.json").unlink()
    assert catalog.choices(store, employee["id"], project["id"])["profiles"] == []
    (profiles / "desktop").rmdir()
    profiles.rmdir()
    assert (
        catalog.choices(store, employee["id"], project["id"])["state"] == "runtime_not_discovered"
    )


def test_membership_and_unsafe_profile_fail_closed(case, tmp_path):
    store, project, employee, profiles = case
    (tmp_path / "other").mkdir()
    other = store.create_project("Other", tmp_path / "other")
    with pytest.raises(WorkbenchError, match="local DSH member"):
        catalog.choices(store, employee["id"], other["id"])
    (profiles / "escape").symlink_to(profiles / "desktop")
    with pytest.raises(WorkbenchError, match="could not be verified"):
        catalog.choices(store, employee["id"], project["id"])


def test_owner_http_only_and_no_browser_paths(case):
    store, project, employee, profiles = case
    session = create_session(store, employee["id"], project["id"], "Synthetic")
    server = WorkbenchHTTP(store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = server.endpoint + "/api/workbench/mailbox/receivers/dsh/profiles?"
    query = urllib.parse.urlencode({"employee_id": employee["id"], "project_id": project["id"]})

    def get(token, suffix=query):
        request = urllib.request.Request(
            base + suffix, headers={"Authorization": "Bearer " + token}
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    try:
        for token in ("", session["token"]):
            with pytest.raises(urllib.error.HTTPError) as error:
                get(token)
            assert error.value.code == 401
        value = get(server.token)
        assert value["state"] == "profile_candidates" and str(profiles) not in json.dumps(value)
        for suffix in (query + "&path=/etc", query + "&project_id=" + project["id"]):
            with pytest.raises(urllib.error.HTTPError) as error:
                get(server.token, suffix)
            assert error.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        server.instance_lock.close()
