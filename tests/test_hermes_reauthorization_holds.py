"""Old mail stays separate from a newly authorized Hermes session."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_mailbox import workbench_reauthorization_holds as holds
from agent_mailbox import workbench_wake as wake
from agent_mailbox.workbench_host_bindings import resolve_routes, set_binding
from agent_mailbox.workbench_mail_notifications import mark_host_received, pending_ids, ready_ids
from agent_mailbox.workbench_mail_tasks import create_mail_task
from agent_mailbox.workbench_reauthorization_holds import (
    arm_binding_gate,
    cutoff_path,
    epoch_path,
    gate_path,
    gated,
    held,
    hold_prior_pending,
    release_binding_gate,
    required_path,
)
from agent_mailbox.workbench_store import WorkbenchStore


@pytest.fixture
def case(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Synthetic", tmp_path)
    employee = store.create_employee("Worker", "hermes", project["id"])
    binding = set_binding(
        store,
        employee["id"],
        project["id"],
        host_kind="hermes",
        host_session_id="agent:main:feishu:dm:synthetic",
    )
    first = create_mail_task(store, project["id"], "Old unreceived", "Synthetic", employee["id"])
    second = create_mail_task(store, project["id"], "Old unknown", "Synthetic", employee["id"])
    return (
        store,
        project,
        employee,
        binding,
        first["request_message_id"],
        second["request_message_id"],
    )


def test_old_pending_is_held_but_unknown_remains_receipt_only(case, monkeypatch):
    store, project, employee, binding, old, unknown = case
    claims = store.root / "wake" / "claims"
    claims.mkdir(mode=0o700)
    (claims / f"{old}.pending").write_text(employee["id"])
    (claims / f"{unknown}.unknown").write_text("existing delivery id")
    plan = "plan_" + "a" * 32
    arm_binding_gate(store, binding, plan)
    result = hold_prior_pending(store, binding, plan)
    assert result == {
        "state": "old_pending_held",
        "held_count": 1,
        "unknown_count": 1,
        "delivery_verified": False,
    }
    assert held(store.root, old) is True
    assert held(store.root, unknown) is False
    assert pending_ids(store, employee["id"]) == [unknown]
    with store._transaction(readonly=True) as db:
        statuses = {
            row["state_hash"]: row["status"]
            for row in db.execute(
                "SELECT state_hash,status FROM notifications WHERE state_hash IN (?,?)",
                (old, unknown),
            )
        }
    assert statuses == {old: "reauthorization_held", unknown: "pending"}
    assert wake._pending_for(store.root, employee["id"]) == []
    assert gated(store.root, binding["id"])
    assert (
        release_binding_gate(store, binding, plan, route_ready=lambda: True)["state"]
        == "new_route_released"
    )

    sent = []
    monkeypatch.setattr(wake, "hook_command", lambda _home: ["/bin/true"])

    def runner(command, **_kwargs):
        delivery_id = command[-1]
        payload = json.loads((store.root / "wake/wake-outbox" / f"{delivery_id}.json").read_text())
        sent.append(payload["fresh"])
        receipts = store.root / "wake" / "receipts"
        receipts.mkdir(exist_ok=True)
        (receipts / delivery_id).write_text(
            json.dumps(
                {
                    "protocol": "agent-mailbox.host-receipt.v1",
                    "delivery_id": delivery_id,
                    "employee_id": employee["id"],
                    "message_ids": payload["fresh"],
                    "binding_id": binding["id"],
                    "project_id": project["id"],
                    "host_kind": "hermes",
                    "host_session_id": binding["host_session_id"],
                    "status": "received",
                }
            )
        )
        return SimpleNamespace(returncode=0)

    fresh = create_mail_task(store, project["id"], "New", "Synthetic", employee["id"])[
        "request_message_id"
    ]
    deliver = wake.hook_deliver(
        store.root,
        runner=runner,
        require_receipt=True,
        include_pending=False,
        binding_resolver=lambda eid, mids: resolve_routes(store, eid, mids),
    )
    decision = {"fresh": [old, fresh], "reason": "synthetic"}
    assert deliver(employee["id"], decision) is False  # Held old mail keeps the batch unconfirmed.
    assert sent == [[fresh]]  # The new letter is independent and really reached the adapter.
    assert decision["delivered_ids"] == [fresh]
    mark_host_received(store, employee["id"], decision["delivered_ids"])
    assert (claims / f"{old}.reauth-hold").stat().st_mode & 0o077 == 0

    cli_fresh = create_mail_task(store, project["id"], "CLI new", "Synthetic", employee["id"])[
        "request_message_id"
    ]
    direct = wake.hook_deliver(
        store.root,
        runner=runner,
        require_receipt=True,
        expected_binding=binding,
        include_pending=True,
    )
    direct_decision = {"fresh": [old, cli_fresh], "reason": "synthetic_cli"}
    assert direct(employee["id"], direct_decision) is False  # Direct path lacks cutoff proof.
    assert sent == [[fresh]]
    routed_decision = {"fresh": [cli_fresh], "reason": "synthetic_cli_routed"}
    assert deliver(employee["id"], routed_decision) is True
    assert routed_decision["delivered_ids"] == [cli_fresh]
    mark_host_received(store, employee["id"], routed_decision["delivered_ids"])
    assert sent == [[fresh], [cli_fresh]]

    # A late exact receipt for the old unknown effect may settle it; this is
    # reconciliation, not a second host delivery or release of the held mail.
    delivery_id = "wake-synthetic-prior"
    (claims / f"{unknown}.unknown").write_text(delivery_id)
    outbox = store.root / "wake/wake-outbox"
    (outbox / f"{delivery_id}.json").write_text(
        json.dumps(
            {
                "employee_id": employee["id"],
                "fresh": [unknown],
            }
        )
    )
    receipts = store.root / "wake/receipts"
    (receipts / delivery_id).write_text(
        json.dumps(
            {
                "protocol": "agent-mailbox.host-receipt.v1",
                "delivery_id": delivery_id,
                "employee_id": employee["id"],
                "message_ids": [unknown],
                "binding_id": binding["id"],
                "project_id": project["id"],
                "host_kind": "hermes",
                "host_session_id": binding["host_session_id"],
                "status": "received",
            }
        )
    )
    assert ready_ids(store, employee["id"]) == [unknown]
    decision = {"fresh": [unknown], "reason": "strict_receipt_reconciliation"}
    assert deliver(employee["id"], decision) is True
    assert decision["delivered_ids"] == [unknown]
    assert sent == [[fresh], [cli_fresh]]
    assert held(store.root, old) is True

    # A lost sidecar alone must not reopen the product's routed delivery path.
    hold_marker = claims / f"{old}.reauth-hold"
    hold_marker.unlink()
    routes, missing = resolve_routes(store, employee["id"], [old])
    assert routes == [] and missing == [old]
    assert pending_ids(store, employee["id"]) == [unknown]


def test_inflight_or_changed_binding_refuses_hold_without_writing(case):
    store, _, employee, binding, old, _ = case
    claims = store.root / "wake" / "claims"
    claims.mkdir(mode=0o700)
    (claims / old).write_text("delivery already in flight")
    arm_binding_gate(store, binding, "plan_" + "b" * 32)
    with pytest.raises(ValueError, match="inflight_delivery"):
        hold_prior_pending(store, binding, "plan_" + "b" * 32)
    assert not held(store.root, old)
    (claims / old).unlink()
    with pytest.raises(ValueError, match="binding_changed"):
        hold_prior_pending(store, {**binding, "id": "binding_" + "f" * 32}, "plan_" + "b" * 32)
    assert pending_ids(store, employee["id"])
    assert not held(store.root, old)


def test_binding_gate_blocks_new_mail_until_a_cutoff_protocol_exists(case):
    store, project, employee, binding, old, _ = case
    plan = "plan_" + "c" * 32
    arm_binding_gate(store, binding, plan)
    routes, missing = resolve_routes(store, employee["id"], [old])
    assert routes == [] and missing == [old]
    new_during_gate = create_mail_task(
        store, project["id"], "Arrived during reauthorization", "Synthetic", employee["id"]
    )["request_message_id"]
    assert resolve_routes(store, employee["id"], [new_during_gate])[0] == []
    assert gated(store.root, binding["id"])
    with pytest.raises(ValueError, match="verified_new_route"):
        release_binding_gate(store, binding, plan, route_ready=lambda: False)
    assert gated(store.root, binding["id"])
    release_binding_gate(store, binding, plan, route_ready=lambda: True)
    assert not gated(store.root, binding["id"])
    routes, missing = resolve_routes(store, employee["id"], [old, new_during_gate])
    assert routes == [] and missing == [old, new_during_gate]
    later = create_mail_task(store, project["id"], "Post release", "Synthetic", employee["id"])
    routes, missing = resolve_routes(store, employee["id"], [later["request_message_id"]])
    assert missing == [] and len(routes) == 1
    assert gate_path(store.root, binding["id"]).exists() is False
    required = required_path(store.root, binding["id"])
    cutoff = cutoff_path(store.root, binding["id"])
    assert required.stat().st_mode & 0o077 == 0
    assert cutoff.stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError, match="existing_reauthorization"):
        arm_binding_gate(store, binding, plan)  # A completed cutover cannot be rearmed.
    backup = required.with_name(required.name + ".test-backup")
    required.rename(backup)
    assert resolve_routes(store, employee["id"], [later["request_message_id"]])[0] == []
    backup.rename(required)
    cutoff.rename(backup)
    assert resolve_routes(store, employee["id"], [later["request_message_id"]])[0] == []


def test_second_epoch_intent_blocks_mail_even_if_gate_write_crashes(case, monkeypatch):
    store, project, employee, binding, _old, _unknown = case
    first, second = "plan_" + "a" * 32, "plan_" + "b" * 32
    arm_binding_gate(store, binding, first)
    hold_prior_pending(store, binding, first)
    release_binding_gate(store, binding, first, route_ready=lambda: True)
    between = create_mail_task(store, project["id"], "Between", "Synthetic", employee["id"])[
        "request_message_id"
    ]
    assert len(resolve_routes(store, employee["id"], [between])[0]) == 1
    target = gate_path(store.root, binding["id"])
    original = holds._write_or_verify_hold

    def fail_gate(path, payload):
        if path == target:
            raise OSError("synthetic crash after epoch intent")
        return original(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(holds, "_write_or_verify_hold", fail_gate)
        with pytest.raises(OSError, match="synthetic crash"):
            arm_binding_gate(store, binding, second)
    assert epoch_path(store.root, binding["id"], second).is_file()
    assert not target.exists()
    assert resolve_routes(store, employee["id"], [between])[0] == []
    assert arm_binding_gate(store, binding, second)["state"] == "binding_delivery_gated"
    assert target.exists()


def test_second_epoch_completion_crash_keeps_gate_then_recovers(case, monkeypatch):
    store, project, employee, binding, _old, _unknown = case
    first, second = "plan_" + "a" * 32, "plan_" + "b" * 32
    arm_binding_gate(store, binding, first)
    hold_prior_pending(store, binding, first)
    release_binding_gate(store, binding, first, route_ready=lambda: True)
    between = create_mail_task(store, project["id"], "Between", "Synthetic", employee["id"])[
        "request_message_id"
    ]
    arm_binding_gate(store, binding, second)
    hold_prior_pending(store, binding, second)
    gate = gate_path(store.root, binding["id"])
    original = Path.unlink

    def fail_unlink(path, *args, **kwargs):
        if path == gate:
            raise OSError("synthetic crash after cutoff")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_unlink)
        with pytest.raises(OSError, match="synthetic crash"):
            release_binding_gate(store, binding, second, route_ready=lambda: True)
    record = holds._private_json(epoch_path(store.root, binding["id"], second))
    assert record["phase"] == "complete" and gate.exists()
    assert resolve_routes(store, employee["id"], [between])[0] == []
    assert (
        release_binding_gate(store, binding, second, route_ready=lambda: True)["state"]
        == "new_route_released"
    )
    assert not gate.exists()
    assert resolve_routes(store, employee["id"], [between])[0] == []
    fresh = create_mail_task(store, project["id"], "After second", "Synthetic", employee["id"])[
        "request_message_id"
    ]
    assert len(resolve_routes(store, employee["id"], [fresh])[0]) == 1


def test_malformed_hidden_epoch_name_still_refuses_delivery(case):
    store, _project, _employee, binding, _old, _unknown = case
    first, second = "plan_" + "a" * 32, "plan_" + "b" * 32
    arm_binding_gate(store, binding, first)
    release_binding_gate(store, binding, first, route_ready=lambda: True)
    suspicious = epoch_path(store.root, binding["id"], second).with_name(
        "." + binding["id"] + "." + second + ".reauth-epoch.bad.tmp"
    )
    suspicious.write_text("partial")
    suspicious.chmod(0o600)
    assert gated(store.root, binding["id"])
    with pytest.raises(ValueError, match="reauthorization_epoch_unconfirmed"):
        arm_binding_gate(store, binding, second)


def test_second_epoch_partial_write_orphan_does_not_block_retry(case, monkeypatch):
    store, _project, _employee, binding, _old, _unknown = case
    first, second = "plan_" + "a" * 32, "plan_" + "b" * 32
    arm_binding_gate(store, binding, first)
    release_binding_gate(store, binding, first, route_ready=lambda: True)
    arm_binding_gate(store, binding, second)
    target = epoch_path(store.root, binding["id"], second)
    gate = gate_path(store.root, binding["id"])
    actual_unlink = Path.unlink

    def interrupt_replace(source, _destination):
        assert source.is_file() and source != target
        raise OSError("synthetic process interruption before rename")

    def leave_partial(path, *args, **kwargs):
        if path.name.endswith(".tmp"):
            return None  # A terminated process does not run its finally block.
        return actual_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(holds.os, "replace", interrupt_replace)
        patch.setattr(Path, "unlink", leave_partial)
        with pytest.raises(OSError, match="synthetic process interruption"):
            release_binding_gate(store, binding, second, route_ready=lambda: True)
    assert gate.exists()
    assert holds._private_json(target)["phase"] == "pending"
    assert list(target.parent.glob("*.tmp"))
    assert (
        release_binding_gate(store, binding, second, route_ready=lambda: True)["state"]
        == "new_route_released"
    )
    assert not gate.exists()
    assert not list(target.parent.glob("*.tmp"))


def test_legacy_epoch_temp_from_older_candidate_does_not_wedge_retry(case):
    store, _project, _employee, binding, _old, _unknown = case
    first, second = "plan_" + "a" * 32, "plan_" + "b" * 32
    arm_binding_gate(store, binding, first)
    release_binding_gate(store, binding, first, route_ready=lambda: True)
    target = epoch_path(store.root, binding["id"], second)
    legacy = target.with_name(target.name + "." + "1" * 16 + ".tmp")
    legacy.write_text("partial")
    legacy.chmod(0o644)
    with pytest.raises(ValueError, match="reauthorization_epoch_unconfirmed"):
        arm_binding_gate(store, binding, second)
    legacy.chmod(0o600)
    with pytest.raises(ValueError, match="reauthorization_epoch_unconfirmed"):
        arm_binding_gate(store, binding, second)
    legacy.unlink()
    arm_binding_gate(store, binding, second)
    legacy.write_text("partial")
    legacy.chmod(0o600)
    assert holds._private_json(target)["phase"] == "pending"
    assert (
        release_binding_gate(store, binding, second, route_ready=lambda: True)["state"]
        == "new_route_released"
    )
    assert holds._private_json(target)["phase"] == "complete"
    assert not legacy.exists()


def test_release_recovers_cutoff_written_before_gate_unlink(case, monkeypatch):
    store, project, employee, binding, old, _ = case
    plan = "plan_" + "d" * 32
    arm_binding_gate(store, binding, plan)
    gate = gate_path(store.root, binding["id"])
    original = Path.unlink

    def fail_gate_unlink(path, *args, **kwargs):
        if path == gate:
            raise OSError("synthetic crash before gate removal")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_gate_unlink)
        with pytest.raises(OSError, match="synthetic crash"):
            release_binding_gate(store, binding, plan, route_ready=lambda: True)
    assert gate.exists() and cutoff_path(store.root, binding["id"]).exists()
    assert resolve_routes(store, employee["id"], [old])[0] == []
    release_binding_gate(store, binding, plan, route_ready=lambda: True)
    assert not gate.exists()
    assert resolve_routes(store, employee["id"], [old])[0] == []
    fresh = create_mail_task(store, project["id"], "After recovery", "Synthetic", employee["id"])
    assert resolve_routes(store, employee["id"], [fresh["request_message_id"]])[1] == []


def test_arm_recovers_same_plan_after_required_marker_only(case, monkeypatch):
    store, _, employee, binding, old, _ = case
    plan = "plan_" + "e" * 32
    gate = gate_path(store.root, binding["id"])
    original = holds._write_hold

    def fail_gate(path, payload):
        if path == gate:
            raise OSError("synthetic crash before gate creation")
        return original(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(holds, "_write_hold", fail_gate)
        with pytest.raises(OSError, match="synthetic crash"):
            arm_binding_gate(store, binding, plan)
    assert required_path(store.root, binding["id"]).exists()
    assert resolve_routes(store, employee["id"], [old])[0] == []
    assert arm_binding_gate(store, binding, plan)["state"] == "binding_delivery_gated"
    assert gated(store.root, binding["id"])
    assert arm_binding_gate(store, binding, plan)["state"] == "binding_delivery_gated"
    with pytest.raises(ValueError, match="existing_reauthorization"):
        arm_binding_gate(store, binding, "plan_" + "f" * 32)


def test_old_pending_hold_recovers_partial_marker_write(case, monkeypatch):
    store, project, employee, binding, old, _ = case
    another = create_mail_task(store, project["id"], "Another old", "Synthetic", employee["id"])
    plan = "plan_" + "e" * 32
    arm_binding_gate(store, binding, plan)
    marker = holds.hold_path(store.root, another["request_message_id"])
    original = holds._write_hold

    def fail_second(path, payload):
        if path == marker:
            raise OSError("synthetic crash before second hold")
        return original(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(holds, "_write_hold", fail_second)
        with pytest.raises(OSError, match="synthetic crash"):
            hold_prior_pending(store, binding, plan)
    assert held(store.root, old)
    assert resolve_routes(store, employee["id"], [old, another["request_message_id"]])[0] == []
    assert hold_prior_pending(store, binding, plan)["held_count"] == 3
    assert pending_ids(store, employee["id"]) == []
