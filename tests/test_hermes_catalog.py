"""Owner HTTP and membership boundary; synthetic profile child projections."""

import hashlib
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from agent_mailbox import workbench_hermes_catalog as catalog
from agent_mailbox.workbench import WorkbenchHTTP
from agent_mailbox.workbench_mail_sessions import create_session
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


@pytest.fixture
def case(tmp_path, monkeypatch):
    store = WorkbenchStore(tmp_path / "data")
    project = store.create_project("Synthetic", tmp_path)
    employee = store.create_employee("Synthetic", "hermes", project["id"])
    user = tmp_path / "user"
    python = user / ".hermes/hermes-agent/.venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("synthetic")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user))
    calls = []

    def child(runtime, root, request, **kwargs):
        calls.append((runtime, root, request))
        assert runtime == python and request == {
            "version": 1,
            "operation": "profiles",
            "profile": str(root),
        }
        return {
            "state": "profile_candidates",
            "profiles": [{"profile_id": "default", "home": str(root)}],
            "session_verified": False,
            "delivery_verified": False,
        }

    monkeypatch.setattr(catalog.process, "run", child)
    return store, project, employee, calls, python


def test_owner_projection_has_no_paths_or_runtime_claim(case):
    store, project, employee, calls, _ = case
    value = catalog.choices(store, employee["id"], project["id"])
    assert value["state"] == "profile_candidates"
    assert value["profiles"][0]["id"].startswith("hermes_profile_")
    assert value["profiles"][0]["candidate_only"] is True
    assert value["session_verified"] is value["delivery_verified"] is False
    assert str(Path.home()) not in json.dumps(value)
    assert len(calls) == 1


def test_foreign_membership_does_not_discover(case, tmp_path):
    store, _, employee, calls, _ = case
    (tmp_path / "other").mkdir()
    other = store.create_project("Other", tmp_path / "other")
    with pytest.raises(WorkbenchError, match="active local Hermes"):
        catalog.choices(store, employee["id"], other["id"])
    assert not calls


def test_wrong_host_does_not_discover(case):
    store, project, _, calls, _ = case
    worker = store.create_employee("Other", "codex", project["id"])
    with pytest.raises(WorkbenchError, match="active local Hermes"):
        catalog.choices(store, worker["id"], project["id"])
    assert not calls


def test_missing_runtime_is_not_absence_of_all_installations(case):
    store, project, employee, calls, python = case
    python.unlink()
    value = catalog.choices(store, employee["id"], project["id"])
    assert value["state"] == "runtime_not_discovered"
    assert value["discovery_scope"] == "local_default_installation"
    assert not calls


def test_published_pm_launcher_uses_only_its_committed_generation(case, monkeypatch):
    store, project, employee, calls, old_python = case
    root = Path.home() / ".hermes"
    install = root / "hermes-agent"
    launcher = install / ".hermes/bin/hermes"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    key = hashlib.sha256(str(install).encode()).hexdigest()[:16]
    venv = root / "installs" / key / "environments" / ("a" * 32) / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("version = 3.14\n")
    pm_python = venv / "bin/python"
    pm_python.write_text("#!/bin/sh\n")
    pm_python.chmod(0o755)
    facts = root / "installs" / key / "facts.json"
    facts.write_text(json.dumps({"packages": {"venv": {"environment": str(venv)}}}))
    facts.chmod(0o600)

    def child(runtime, home, request, **kwargs):
        calls.append((runtime, home, request))
        assert runtime == pm_python and runtime != old_python
        return {
            "state": "profile_candidates",
            "profiles": [{"profile_id": "default", "home": str(home)}],
        }

    monkeypatch.setattr(catalog.process, "run", child)
    assert catalog.choices(store, employee["id"], project["id"])["state"] == "profile_candidates"
    assert len(calls) == 1

    # A published launcher with a broken/misdirected record never uses the
    # still-existing legacy venv as a silent replacement.
    facts.write_text(json.dumps({"packages": {"venv": {"environment": str(root / "escape")}}}))
    with pytest.raises(WorkbenchError, match="runtime selection could not be verified"):
        catalog.choices(store, employee["id"], project["id"])
    facts.write_text("{")
    with pytest.raises(WorkbenchError, match="runtime selection could not be verified"):
        catalog.choices(store, employee["id"], project["id"])
    facts.unlink()
    with pytest.raises(WorkbenchError, match="runtime selection could not be verified"):
        catalog.choices(store, employee["id"], project["id"])
    launcher.unlink()
    with pytest.raises(WorkbenchError, match="runtime selection could not be verified"):
        catalog.choices(store, employee["id"], project["id"])
    assert len(calls) == 1


def test_child_error_does_not_expose_config(case, monkeypatch):
    def refuse(*a, **k):
        raise ValueError("PRIVATE_CONFIG")

    monkeypatch.setattr(catalog.process, "run", refuse)
    store, project, employee, *_ = case
    with pytest.raises(WorkbenchError) as error:
        catalog.choices(store, employee["id"], project["id"])
    assert "PRIVATE_CONFIG" not in str(error.value)


def test_http_owner_only_and_rejects_path_and_duplicate_inputs(case):
    store, project, employee, calls, _ = case
    session = create_session(store, employee["id"], project["id"], "Synthetic")
    server = WorkbenchHTTP(store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = server.endpoint + "/api/workbench/mailbox/receivers/hermes/profiles?"
    query = urllib.parse.urlencode({"employee_id": employee["id"], "project_id": project["id"]})

    def get(token, tail=query):
        request = urllib.request.Request(base + tail, headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    try:
        for token in ("", session["token"]):
            with pytest.raises(urllib.error.HTTPError) as error:
                get(token)
            assert error.value.code == 401
        assert not calls
        assert get(server.token)["state"] == "profile_candidates"
        for tail in (query + "&path=/etc", query + "&project_id=" + project["id"]):
            with pytest.raises(urllib.error.HTTPError) as error:
                get(server.token, tail)
            assert error.value.code == 400
        assert len(calls) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        server.instance_lock.close()
