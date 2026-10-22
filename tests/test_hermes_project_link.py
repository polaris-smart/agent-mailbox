"""Owner confirmation for reusing one Hermes session across authorized projects."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from agent_mailbox import workbench_hermes_project_link as link
from agent_mailbox.workbench import WorkbenchHTTP
from agent_mailbox.workbench_host_bindings import load_bindings, resolve_routes, set_binding
from agent_mailbox.workbench_mail_notifications import (
    pending_ids,
    ready_ids,
    settle_preexisting_opens,
)
from agent_mailbox.workbench_mail_sessions import create_session
from agent_mailbox.workbench_mail_tasks import create_mail_task
from agent_mailbox.workbench_message_reads import open_in
from agent_mailbox.workbench_reauthorization_holds import gated, held
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


@pytest.fixture
def case(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    first = store.create_project("Private", tmp_path)
    second = store.create_project("Shared", tmp_path)
    employee = store.create_employee("Hermes", "hermes", first["id"])
    store.add_project_member(second["id"], employee["id"])
    binding = set_binding(
        store, employee["id"], first["id"], host_kind="hermes", host_session_id="original-session"
    )
    endpoint = store.root / "wake" / "host-endpoints.json"
    endpoint.write_text(
        json.dumps(
            {
                "version": 1,
                "endpoints": [
                    {
                        "binding_id": binding["id"],
                        "employee_id": employee["id"],
                        "host_kind": "hermes",
                        "socket_path": "/private/tmp/hermes-link-test.sock",
                    }
                ],
            }
        )
    )
    endpoint.chmod(0o600)
    return store, first, second, employee, binding, endpoint


def test_preview_then_confirm_adds_only_target_project_route(case):
    store, _, second, employee, original, endpoint = case
    before = json.loads(endpoint.read_text())
    preview = link.operation(store, employee["id"], second["id"], "preview")
    assert preview["state"] == "confirmation_required"
    assert "original-session" not in json.dumps(preview)
    assert str(endpoint) not in json.dumps(preview)
    assert load_bindings(store.root) == [original]
    assert json.loads(endpoint.read_text()) == before

    result = link.operation(
        store,
        employee["id"],
        second["id"],
        "apply",
        expected_fingerprint=preview["fingerprint"],
        confirmed=True,
    )
    assert result["state"] == "linked_runtime_unverified"
    routes = load_bindings(store.root)
    assert routes[0] == original
    assert routes[1]["project_id"] == second["id"]
    assert routes[1]["employee_id"] == employee["id"]
    assert routes[1]["host_session_id"] == original["host_session_id"]
    rows = json.loads(endpoint.read_text())["endpoints"]
    assert rows[1]["binding_id"] == routes[1]["id"]
    assert rows[1]["socket_path"] == rows[0]["socket_path"]
    assert (
        link.operation(store, employee["id"], second["id"], "preview")["state"] == "already_linked"
    )
    assert len(load_bindings(store.root)) == 2


def test_link_holds_old_pending_and_only_new_mail_can_use_route(case):
    store, _, second, employee, _, _ = case
    old = create_mail_task(store, second["id"], "Old", "Synthetic", employee["id"])[
        "request_message_id"
    ]
    preview = link.operation(store, employee["id"], second["id"], "preview")
    result = link.operation(
        store,
        employee["id"],
        second["id"],
        "apply",
        expected_fingerprint=preview["fingerprint"],
        confirmed=True,
    )
    assert result["old_alerts_held"] == 1
    assert result["delivery_verified"] is False
    fresh = create_mail_task(store, second["id"], "Fresh", "Synthetic", employee["id"])[
        "request_message_id"
    ]
    assert held(store.root, old) is True
    routes, missing = resolve_routes(store, employee["id"], [old, fresh])
    assert missing == [old]
    assert [row["messages"] for row in routes] == [[fresh]]


def test_linking_two_projects_keeps_each_old_backlog_out_of_new_routes(case):
    store, first, second, employee, _, _ = case
    third = store.create_project("Third", first["path"])
    store.add_project_member(third["id"], employee["id"])
    old_second = create_mail_task(store, second["id"], "Old second", "Synthetic", employee["id"])[
        "request_message_id"
    ]
    old_third = create_mail_task(store, third["id"], "Old third", "Synthetic", employee["id"])[
        "request_message_id"
    ]

    for project in (second, third):
        preview = link.operation(store, employee["id"], project["id"], "preview")
        result = link.operation(
            store,
            employee["id"],
            project["id"],
            "apply",
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
        )
        assert result["old_alerts_held"] == 1
        assert result["delivery_verified"] is False

    fresh_second = create_mail_task(
        store, second["id"], "Fresh second", "Synthetic", employee["id"]
    )["request_message_id"]
    fresh_third = create_mail_task(store, third["id"], "Fresh third", "Synthetic", employee["id"])[
        "request_message_id"
    ]
    assert held(store.root, old_second) and held(store.root, old_third)
    routes, missing = resolve_routes(
        store, employee["id"], [old_second, fresh_second, old_third, fresh_third]
    )
    assert set(missing) == {old_second, old_third}
    assert {mid for route in routes for mid in route["messages"]} == {fresh_second, fresh_third}


@pytest.mark.parametrize("old_envelope", (False, True))
def test_new_project_route_never_replays_an_unknown_old_delivery(case, monkeypatch, old_envelope):
    store, _, second, employee, original_binding, _ = case
    unknown = create_mail_task(store, second["id"], "Old unknown", "Synthetic", employee["id"])[
        "request_message_id"
    ]
    claims = store.root / "wake" / "claims"
    claims.mkdir(mode=0o700, exist_ok=True)
    (claims / f"{unknown}.unknown").write_text("wake-prior-unknown")
    if old_envelope:
        outbox = store.root / "wake" / "wake-outbox"
        outbox.mkdir(mode=0o700, exist_ok=True)
        (outbox / "wake-prior-unknown.json").write_text(
            json.dumps(
                {
                    "delivery_id": "wake-prior-unknown",
                    "employee_id": employee["id"],
                    "binding": original_binding,
                    "fresh": [unknown],
                }
            )
        )

    preview = link.operation(store, employee["id"], second["id"], "preview")
    result = link.operation(
        store,
        employee["id"],
        second["id"],
        "apply",
        expected_fingerprint=preview["fingerprint"],
        confirmed=True,
    )
    assert result["unknown_alerts"] == 1
    assert result["old_alerts_held"] == 0
    fresh = create_mail_task(store, second["id"], "Fresh", "Synthetic", employee["id"])[
        "request_message_id"
    ]
    monkeypatch.setattr(
        "agent_mailbox.workbench_host_transport.configured_for_binding", lambda *_: True
    )
    monkeypatch.setattr("agent_mailbox.workbench_host_transport.receiver_online", lambda *_: True)
    assert pending_ids(store, employee["id"]) == [unknown, fresh]
    assert ready_ids(store, employee["id"]) == [fresh]


def test_recipient_open_settles_held_alert_without_host_replay(case):
    store, _, second, employee, _, _ = case
    old = create_mail_task(store, second["id"], "Old", "Synthetic", employee["id"])[
        "request_message_id"
    ]
    preview = link.operation(store, employee["id"], second["id"], "preview")
    link.operation(
        store,
        employee["id"],
        second["id"],
        "apply",
        expected_fingerprint=preview["fingerprint"],
        confirmed=True,
    )
    with store._transaction() as db:
        result = open_in(
            store,
            db,
            {"id": "synthetic-session", "project_id": second["id"], "employee_id": employee["id"]},
            old,
        )
    assert result["message"]["read_receipt"] is not None
    with store._transaction(readonly=True) as db:
        row = db.execute(
            "SELECT status,attempts FROM notifications WHERE state_hash=?", (old,)
        ).fetchone()
    assert (row["status"], row["attempts"]) == ("recipient_opened", 0)
    assert resolve_routes(store, employee["id"], [old])[0] == []


def test_recovery_scan_settles_previously_opened_held_alert(case):
    store, _, second, employee, _, _ = case
    old = create_mail_task(store, second["id"], "Old", "Synthetic", employee["id"])[
        "request_message_id"
    ]
    preview = link.operation(store, employee["id"], second["id"], "preview")
    link.operation(
        store,
        employee["id"],
        second["id"],
        "apply",
        expected_fingerprint=preview["fingerprint"],
        confirmed=True,
    )
    with store._transaction() as db:
        open_in(
            store,
            db,
            {"id": "synthetic-session", "project_id": second["id"], "employee_id": employee["id"]},
            old,
        )
        db.execute(
            "UPDATE notifications SET status='reauthorization_held' WHERE state_hash=?", (old,)
        )
    assert settle_preexisting_opens(store, employee["id"]) == [old]
    with store._transaction(readonly=True) as db:
        assert (
            db.execute("SELECT status FROM notifications WHERE state_hash=?", (old,)).fetchone()[
                "status"
            ]
            == "recipient_opened"
        )


def test_stale_confirmation_and_unrelated_employee_fail_closed(case):
    store, first, second, employee, _, endpoint = case
    preview = link.operation(store, employee["id"], second["id"], "preview")
    other = store.create_employee("Other Hermes", "hermes", second["id"])
    with pytest.raises(ValueError, match="source_route_required"):
        link.operation(store, other["id"], second["id"], "preview")
    set_binding(
        store, employee["id"], first["id"], host_kind="hermes", host_session_id="changed-session"
    )
    with pytest.raises(ValueError, match="configuration_changed"):
        link.operation(
            store,
            employee["id"],
            second["id"],
            "apply",
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
        )
    assert not any(row["project_id"] == second["id"] for row in load_bindings(store.root))
    assert len(json.loads(endpoint.read_text())["endpoints"]) == 1


def test_revoked_membership_cannot_link(case):
    store, _, second, employee, original, endpoint = case
    preview = link.operation(store, employee["id"], second["id"], "preview")
    store.remove_project_member(second["id"], employee["id"])
    with pytest.raises(WorkbenchError, match="active local Hermes member"):
        link.operation(
            store,
            employee["id"],
            second["id"],
            "apply",
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
        )
    assert load_bindings(store.root) == [original]
    assert len(json.loads(endpoint.read_text())["endpoints"]) == 1


def test_revoked_source_membership_cannot_authorize_another_project(case):
    store, first, second, employee, original, endpoint = case
    store.remove_project_member(first["id"], employee["id"])
    with pytest.raises(ValueError, match="source_route_required"):
        link.operation(store, employee["id"], second["id"], "preview")
    assert load_bindings(store.root) == [original]
    assert len(json.loads(endpoint.read_text())["endpoints"]) == 1


def test_unknown_target_route_is_not_overwritten(case):
    store, _, second, employee, _, endpoint = case
    set_binding(
        store, employee["id"], second["id"], host_kind="hermes", host_session_id="another-session"
    )
    with pytest.raises(ValueError, match="target_route_conflict"):
        link.operation(store, employee["id"], second["id"], "preview")
    assert len(load_bindings(store.root)) == 2
    assert len(json.loads(endpoint.read_text())["endpoints"]) == 1


def test_incomplete_same_session_target_is_repaired_without_replacing_binding(case):
    store, _, second, employee, original, endpoint = case
    target = set_binding(
        store,
        employee["id"],
        second["id"],
        host_kind="hermes",
        host_session_id=original["host_session_id"],
    )
    preview = link.operation(store, employee["id"], second["id"], "preview")
    assert preview["repair_required"] is True
    result = link.operation(
        store,
        employee["id"],
        second["id"],
        "apply",
        expected_fingerprint=preview["fingerprint"],
        confirmed=True,
    )
    assert result["state"] == "linked_runtime_unverified"
    assert load_bindings(store.root)[1] == target
    assert json.loads(endpoint.read_text())["endpoints"][1]["binding_id"] == target["id"]


def test_endpoint_write_failure_leaves_new_binding_fenced(case, monkeypatch):
    store, _, second, employee, original, endpoint = case
    before = endpoint.read_bytes()
    preview = link.operation(store, employee["id"], second["id"], "preview")
    monkeypatch.setattr(link, "_replace_private", lambda *_: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError, match="disk"):
        link.operation(
            store,
            employee["id"],
            second["id"],
            "apply",
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
        )
    bindings = load_bindings(store.root)
    assert bindings[0] == original
    assert len(bindings) == 2
    assert gated(store.root, bindings[1]["id"]) is True
    assert endpoint.read_bytes() == before
    with pytest.raises(ValueError, match="target_route_fenced_requires_inspection"):
        link.operation(store, employee["id"], second["id"], "preview")


def test_two_distinct_existing_sessions_are_not_guessed(case):
    store, first, second, employee, _, endpoint = case
    third = store.create_project("Third", first["path"])
    store.add_project_member(third["id"], employee["id"])
    alternate = set_binding(
        store, employee["id"], third["id"], host_kind="hermes", host_session_id="different-session"
    )
    config = json.loads(endpoint.read_text())
    config["endpoints"].append(
        {
            "binding_id": alternate["id"],
            "employee_id": employee["id"],
            "host_kind": "hermes",
            "socket_path": "/private/tmp/other-hermes.sock",
        }
    )
    endpoint.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="source_route_ambiguous"):
        link.operation(store, employee["id"], second["id"], "preview")
    assert not any(row["project_id"] == second["id"] for row in load_bindings(store.root))


def test_owner_http_does_not_accept_session_key_or_employee_token(case):
    store, _, second, employee, _, _ = case
    employee_token = create_session(store, employee["id"], second["id"], "Synthetic")["token"]
    server = WorkbenchHTTP(store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = server.endpoint + "/api/workbench/mailbox/receivers/hermes/project-link"

    def post(token, body):
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    body = {"employee_id": employee["id"], "project_id": second["id"], "action": "preview"}
    try:
        for token in ("", employee_token):
            with pytest.raises(urllib.error.HTTPError) as error:
                post(token, body)
            assert error.value.code == 401
        for key in ("session_key", "socket_path", "source_project_id"):
            with pytest.raises(urllib.error.HTTPError) as error:
                post(server.token, {**body, key: "injected"})
            assert error.value.code == 400
        preview = post(server.token, body)
        assert preview["state"] == "confirmation_required"
        assert "original-session" not in json.dumps(preview)
        assert "hermes-link-test.sock" not in json.dumps(preview)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        server.instance_lock.close()
