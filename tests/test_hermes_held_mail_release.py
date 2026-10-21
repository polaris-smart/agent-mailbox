"""One exact old ordinary letter requires an Owner preview and confirmation."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from agent_mailbox import workbench_held_mail_release as release
from agent_mailbox import workbench_hermes_project_link as link
from agent_mailbox.workbench import WorkbenchHTTP
from agent_mailbox.workbench_host_bindings import resolve_routes, set_binding
from agent_mailbox.workbench_mail_sessions import create_session
from agent_mailbox.workbench_mail_tasks import create_mail_task
from agent_mailbox.workbench_message_reads import open_in
from agent_mailbox.workbench_reauthorization_holds import (
    arm_binding_gate,
    held,
    hold_prior_pending,
    release_binding_gate,
)
from agent_mailbox.workbench_store import WorkbenchStore


@pytest.fixture
def linked(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    first = store.create_project("First", tmp_path)
    second = store.create_project("Second", tmp_path)
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
                        "socket_path": "/private/tmp/held-release-test.sock",
                    }
                ],
            }
        )
    )
    endpoint.chmod(0o600)
    return store, first, second, employee


def _link(store, project, employee):
    preview = link.operation(store, employee["id"], project["id"], "preview")
    link.operation(
        store,
        employee["id"],
        project["id"],
        "apply",
        expected_fingerprint=preview["fingerprint"],
        confirmed=True,
    )


def test_exact_ordinary_held_mail_requires_confirmation_and_then_routes(linked):
    store, _, second, employee = linked
    old = store.send_message(second["id"], "One old letter", "Synthetic", employee["id"])
    _link(store, second, employee)
    assert held(store.root, old["id"])
    assert resolve_routes(store, employee["id"], [old["id"]])[1] == [old["id"]]

    preview = release.operation(store, employee["id"], second["id"], old["id"], "preview")
    assert preview["state"] == "confirmation_required"
    assert preview["title"] == "One old letter"
    assert preview["sender_id"] is None and preview["sender_name"] is None
    assert "original-session" not in json.dumps(preview)
    assert "held-release-test.sock" not in json.dumps(preview)
    assert held(store.root, old["id"])

    with pytest.raises(ValueError, match="confirmation_required"):
        release.operation(
            store,
            employee["id"],
            second["id"],
            old["id"],
            "apply",
            expected_fingerprint=preview["fingerprint"],
        )
    result = release.operation(
        store,
        employee["id"],
        second["id"],
        old["id"],
        "apply",
        expected_fingerprint=preview["fingerprint"],
        confirmed=True,
    )
    assert result == {"state": "old_mail_pending", "delivery_verified": False}
    assert not held(store.root, old["id"])
    with store._transaction(readonly=True) as db:
        note = db.execute(
            "SELECT status,attempts FROM notifications WHERE state_hash=?", (old["id"],)
        ).fetchone()
    assert (note["status"], note["attempts"]) == ("pending", 0)
    routes, missing = resolve_routes(store, employee["id"], [old["id"]])
    assert missing == [] and routes[0]["messages"] == [old["id"]]


def test_held_mail_review_shows_registered_sender_before_owner_can_release(linked):
    store, _, second, employee = linked
    sender = store.create_employee("WorkBuddy", "workbuddy", second["id"])
    session = create_session(store, sender["id"], second["id"], "Synthetic sender")
    old = store.send_message(
        second["id"], "Old agent letter", "Synthetic content", recipient_id=employee["id"],
        sender_id=sender["id"], mailbox_token=session["token"],
    )
    _link(store, second, employee)
    listed = release.list_held(store, employee["id"], second["id"])["items"]
    assert listed == [{
        "id": old["id"], "title": old["title"], "created_at": old["created_at"],
        "sender_id": sender["id"], "sender_name": "WorkBuddy",
    }]
    preview = release.operation(store, employee["id"], second["id"], old["id"], "preview")
    assert preview["sender_id"] == sender["id"]
    assert preview["sender_name"] == "WorkBuddy"


def test_recipient_inbox_identifies_exact_held_letter_without_opening_it(linked):
    store, first, second, employee = linked
    other = store.send_message(first["id"], "Other project", "Synthetic", employee["id"])
    old = store.send_message(second["id"], "Held here", "Synthetic", employee["id"])
    _link(store, second, employee)

    before = store.snapshot()
    inbox = store.employee_messages(second["id"], employee["id"])
    assert [item["id"] for item in inbox["messages"]] == [old["id"]]
    assert inbox["messages"][0]["delivery_alert_status"] == "reauthorization_held"
    assert inbox["messages"][0]["read_receipt"] is None
    assert store.snapshot() == before
    assert store.employee_messages(first["id"], employee["id"])["messages"][0]["id"] == other["id"]


def test_two_project_backlog_does_not_burst_when_routes_are_linked(linked):
    store, first, second, employee = linked
    third = store.create_project("Third", first["path"])
    store.add_project_member(third["id"], employee["id"])
    backlog = {
        second["id"]: [
            store.send_message(second["id"], f"Old {i}", "Synthetic", employee["id"])["id"]
            for i in range(27)
        ],
        third["id"]: [
            store.send_message(third["id"], f"Old {i}", "Synthetic", employee["id"])["id"]
            for i in range(2)
        ],
    }

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
        assert result["old_alerts_held"] == len(backlog[project["id"]])
        assert [
            item["id"] for item in release.list_held(store, employee["id"], project["id"])["items"]
        ] == backlog[project["id"]]

    all_old = [*backlog[second["id"]], *backlog[third["id"]]]
    routes, missing = resolve_routes(store, employee["id"], all_old)
    assert routes == []
    assert set(missing) == set(all_old)

    selected = backlog[second["id"]][0]
    preview = release.operation(store, employee["id"], second["id"], selected, "preview")
    release.operation(
        store,
        employee["id"],
        second["id"],
        selected,
        "apply",
        expected_fingerprint=preview["fingerprint"],
        confirmed=True,
    )
    routes, missing = resolve_routes(store, employee["id"], all_old)
    assert [mid for route in routes for mid in route["messages"]] == [selected]
    assert set(missing) == set(all_old) - {selected}
    assert len(release.list_held(store, employee["id"], second["id"])["items"]) == 26
    assert len(release.list_held(store, employee["id"], third["id"])["items"]) == 2


def test_one_employee_cannot_preview_or_release_another_projects_held_mail(linked):
    store, first, second, employee = linked
    third = store.create_project("Third", first["path"])
    store.add_project_member(third["id"], employee["id"])
    second_mail = store.send_message(second["id"], "Second only", "Second body", employee["id"])
    third_mail = store.send_message(third["id"], "Third only", "Third body", employee["id"])
    _link(store, second, employee)
    _link(store, third, employee)

    assert [item["id"] for item in release.list_held(store, employee["id"], second["id"])["items"]] == [
        second_mail["id"]
    ]
    assert [item["id"] for item in release.list_held(store, employee["id"], third["id"])["items"]] == [
        third_mail["id"]
    ]
    second_preview = release.operation(
        store, employee["id"], second["id"], second_mail["id"], "preview"
    )
    assert second_preview["body"] == "Second body"

    # A valid review token for one project must not authorize that message
    # through another project, even when the employee and host session match.
    with pytest.raises(ValueError, match="held_mail_unavailable"):
        release.operation(store, employee["id"], third["id"], second_mail["id"], "preview")
    with pytest.raises(ValueError, match="held_mail_unavailable"):
        release.operation(
            store,
            employee["id"],
            third["id"],
            second_mail["id"],
            "apply",
            expected_fingerprint=second_preview["fingerprint"],
            confirmed=True,
        )
    assert held(store.root, second_mail["id"])
    assert held(store.root, third_mail["id"])
    routes, missing = resolve_routes(store, employee["id"], [second_mail["id"], third_mail["id"]])
    assert routes == []
    assert set(missing) == {second_mail["id"], third_mail["id"]}


def test_work_request_and_wrong_scope_cannot_be_released(linked):
    store, first, second, employee = linked
    task_message = create_mail_task(store, second["id"], "Task", "Synthetic", employee["id"])[
        "request_message_id"
    ]
    _link(store, second, employee)
    with pytest.raises(ValueError, match="ordinary_mail_required"):
        release.operation(store, employee["id"], second["id"], task_message, "preview")
    with pytest.raises(ValueError, match="held_mail_route_unconfirmed"):
        release.operation(store, employee["id"], first["id"], task_message, "preview")


def test_interrupted_release_remains_held_and_can_be_reconfirmed(linked, monkeypatch):
    store, _, second, employee = linked
    old = store.send_message(second["id"], "Old", "Synthetic", employee["id"])
    _link(store, second, employee)
    preview = release.operation(store, employee["id"], second["id"], old["id"], "preview")
    original = release._publish_release
    monkeypatch.setattr(
        release, "_publish_release", lambda *_: (_ for _ in ()).throw(OSError("disk"))
    )
    with pytest.raises(OSError, match="disk"):
        release.operation(
            store,
            employee["id"],
            second["id"],
            old["id"],
            "apply",
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
        )
    assert held(store.root, old["id"])
    visible = release.list_held(store, employee["id"], second["id"])["items"]
    assert [item["id"] for item in visible] == [old["id"]]
    assert resolve_routes(store, employee["id"], [old["id"]])[1] == [old["id"]]
    monkeypatch.setattr(release, "_publish_release", original)
    retry = release.operation(store, employee["id"], second["id"], old["id"], "preview")
    release.operation(
        store,
        employee["id"],
        second["id"],
        old["id"],
        "apply",
        expected_fingerprint=retry["fingerprint"],
        confirmed=True,
    )
    assert not held(store.root, old["id"])


def test_owner_http_lists_and_confirms_only_one_exact_old_letter(linked):
    store, _, second, employee = linked
    old = store.send_message(second["id"], "Old", "Synthetic", employee["id"])
    task_message = create_mail_task(store, second["id"], "Task", "Synthetic", employee["id"])[
        "request_message_id"
    ]
    _link(store, second, employee)
    employee_token = create_session(store, employee["id"], second["id"], "Synthetic")["token"]
    server = WorkbenchHTTP(store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = server.endpoint + "/api/workbench/mailbox/receivers/hermes/held-mail"

    def post(token, body):
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    scope = {"employee_id": employee["id"], "project_id": second["id"]}
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            post(employee_token, {**scope, "action": "list"})
        assert error.value.code == 401
        listing = post(server.token, {**scope, "action": "list"})
        assert [item["id"] for item in listing["items"]] == [old["id"]]
        assert task_message not in json.dumps(listing)
        preview = post(server.token, {**scope, "message_id": old["id"], "action": "preview"})
        with pytest.raises(urllib.error.HTTPError) as error:
            post(
                server.token,
                {
                    **scope,
                    "message_id": old["id"],
                    "action": "apply",
                    "fingerprint": preview["fingerprint"],
                },
            )
        assert error.value.code == 400
        outcome = post(
            server.token,
            {
                **scope,
                "message_id": old["id"],
                "action": "apply",
                "fingerprint": preview["fingerprint"],
                "confirmed": True,
            },
        )
        assert outcome == {"state": "old_mail_pending", "delivery_verified": False}
        assert post(server.token, {**scope, "action": "list"})["items"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        server.instance_lock.close()


def test_later_session_cutover_revokes_prior_old_mail_exception(linked):
    store, _, second, employee = linked
    old = store.send_message(second["id"], "Old", "Synthetic", employee["id"])
    _link(store, second, employee)
    preview = release.operation(store, employee["id"], second["id"], old["id"], "preview")
    release.operation(
        store,
        employee["id"],
        second["id"],
        old["id"],
        "apply",
        expected_fingerprint=preview["fingerprint"],
        confirmed=True,
    )
    assert resolve_routes(store, employee["id"], [old["id"]])[1] == []
    binding = next(
        row for row in release.load_bindings(store.root) if row["project_id"] == second["id"]
    )
    next_plan = "plan_" + "d" * 32
    arm_binding_gate(store, binding, next_plan)
    hold_prior_pending(store, binding, next_plan)
    release_binding_gate(store, binding, next_plan, route_ready=lambda: True)
    assert held(store.root, old["id"])
    assert resolve_routes(store, employee["id"], [old["id"]])[1] == [old["id"]]
    next_preview = release.operation(store, employee["id"], second["id"], old["id"], "preview")
    release.operation(
        store,
        employee["id"],
        second["id"],
        old["id"],
        "apply",
        expected_fingerprint=next_preview["fingerprint"],
        confirmed=True,
    )
    assert resolve_routes(store, employee["id"], [old["id"]])[1] == []


def test_stale_route_and_unknown_effect_do_not_release_old_mail(linked):
    store, _, second, employee = linked
    old = store.send_message(second["id"], "Old", "Synthetic", employee["id"])
    _link(store, second, employee)
    preview = release.operation(store, employee["id"], second["id"], old["id"], "preview")
    endpoint = store.root / "wake" / "host-endpoints.json"
    value = json.loads(endpoint.read_text())
    value["endpoints"][-1]["socket_path"] = "/private/tmp/other-session.sock"
    endpoint.write_text(json.dumps(value))
    endpoint.chmod(0o600)
    with pytest.raises(ValueError, match="configuration_changed"):
        release.operation(
            store,
            employee["id"],
            second["id"],
            old["id"],
            "apply",
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
        )
    assert held(store.root, old["id"])
    (store.root / "wake" / "claims" / f"{old['id']}.unknown").write_text("synthetic")
    with pytest.raises(ValueError, match="held_mail_effect_unconfirmed"):
        release.operation(store, employee["id"], second["id"], old["id"], "preview")


def test_explicitly_opened_held_mail_is_no_longer_eligible(linked):
    store, _, second, employee = linked
    old = store.send_message(second["id"], "Old", "Synthetic", employee["id"])
    _link(store, second, employee)
    with store._transaction() as db:
        open_in(
            store,
            db,
            {"id": "synthetic-session", "employee_id": employee["id"], "project_id": second["id"]},
            old["id"],
        )
        # Older databases may retain a held row even though an open receipt exists.
        db.execute(
            "UPDATE notifications SET status='reauthorization_held' WHERE state_hash=?",
            (old["id"],),
        )
    assert release.list_held(store, employee["id"], second["id"])["items"] == []
    with pytest.raises(ValueError, match="held_mail_unavailable"):
        release.operation(store, employee["id"], second["id"], old["id"], "preview")


def test_content_change_invalidates_old_confirmation(linked):
    store, _, second, employee = linked
    old = store.send_message(second["id"], "Old", "Original body", employee["id"])
    _link(store, second, employee)
    preview = release.operation(store, employee["id"], second["id"], old["id"], "preview")
    assert preview["body"] == "Original body"
    with store._transaction() as db:
        db.execute("UPDATE messages SET body='Changed body' WHERE id=?", (old["id"],))
    with pytest.raises(ValueError, match="configuration_changed"):
        release.operation(
            store,
            employee["id"],
            second["id"],
            old["id"],
            "apply",
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
        )
    assert held(store.root, old["id"])


def test_sender_change_invalidates_old_confirmation(linked):
    store, _, second, employee = linked
    original = store.create_employee("Original", "workbuddy", second["id"])
    replacement = store.create_employee("Replacement", "workbuddy", second["id"])
    session = create_session(store, original["id"], second["id"], "Synthetic sender")
    old = store.send_message(
        second["id"], "Old", "Synthetic", recipient_id=employee["id"],
        sender_id=original["id"], mailbox_token=session["token"],
    )
    _link(store, second, employee)
    preview = release.operation(store, employee["id"], second["id"], old["id"], "preview")
    with store._transaction() as db:
        db.execute("UPDATE messages SET sender_id=? WHERE id=?", (replacement["id"], old["id"]))
    with pytest.raises(ValueError, match="configuration_changed"):
        release.operation(
            store,
            employee["id"],
            second["id"],
            old["id"],
            "apply",
            expected_fingerprint=preview["fingerprint"],
            confirmed=True,
        )
    assert held(store.root, old["id"])
