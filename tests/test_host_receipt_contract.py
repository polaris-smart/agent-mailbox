"""Real wake entry boundaries; synthetic adapters never call actual hosts/models."""

import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from agent_mailbox import workbench_wake as wake


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    monkeypatch.setattr(wake, "hook_command", lambda home: ["synthetic-adapter"])
    return tmp_path, {"fresh": ["message_a", "message_b"], "reason": "test"}


def receipt(home, command, **overrides):
    base = home / "wake"
    payload = json.loads((base / "wake-outbox" / f"{command[-1]}.json").read_text())
    value = {
        "protocol": "agent-mailbox.host-receipt.v1",
        "delivery_id": command[-1],
        "employee_id": command[-2],
        "message_ids": payload["fresh"],
        "status": "received",
        "host_session_id": "synthetic-bound-session",
    }
    value.update(overrides)
    (base / "receipts").mkdir(exist_ok=True)
    (base / "receipts" / command[-1]).write_text(json.dumps(value))


@pytest.mark.parametrize(
    "outcome",
    [
        "zero",
        "nonzero",
        "timeout",
        "malformed",
        "wrong_employee",
        "wrong_batch",
        "wrong_delivery",
        "no_session",
    ],
)
def test_unconfirmed_host_never_acknowledges_or_retries(delivery, outcome):
    home, decision = delivery
    calls = []

    def host(command, **kwargs):
        calls.append(command)
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, 30)
        if outcome == "malformed":
            receipt(home, command)
            (home / "wake/receipts" / command[-1]).write_text("not json")
        elif outcome.startswith("wrong_") or outcome == "no_session":
            field, value = {
                "wrong_employee": ("employee_id", "other"),
                "wrong_batch": ("message_ids", ["message_a"]),
                "wrong_delivery": ("delivery_id", "wake-other"),
                "no_session": ("host_session_id", ""),
            }[outcome]
            receipt(home, command, **{field: value})
        return SimpleNamespace(returncode=1 if outcome == "nonzero" else 0)

    send = wake.hook_deliver(home, runner=host, require_receipt=True)
    for _ in range(3):
        assert send("employee_a", decision) is False
    assert len(calls) == 1
    for mid in decision["fresh"]:
        assert (home / "wake/claims" / f"{mid}.unknown").exists()
        assert not (home / "wake/claims" / f"{mid}.ok").exists()


@pytest.mark.parametrize("crash", [False, True])
def test_valid_durable_receipt_survives_hook_timeout_and_deduplicates(delivery, crash):
    home, decision = delivery
    calls = []

    def host(command, **kwargs):
        calls.append(command)
        receipt(home, command)
        if crash:
            raise subprocess.TimeoutExpired(command, 30)
        return SimpleNamespace(returncode=0)

    send = wake.hook_deliver(home, runner=host, require_receipt=True)
    assert send("employee_a", decision)
    assert send("employee_a", decision)
    assert len(calls) == 1


def test_explicit_not_received_can_retry(delivery):
    home, decision = delivery
    calls = []

    def host(command, **kwargs):
        calls.append(command)
        receipt(home, command, status="not_received" if len(calls) == 1 else "received")
        return SimpleNamespace(returncode=1 if len(calls) == 1 else 0)

    send = wake.hook_deliver(home, runner=host, require_receipt=True)
    assert not send("employee_a", decision)
    assert send("employee_a", decision)
    assert len(calls) == 2


def test_stale_claim_recovers_only_its_bound_receipt(delivery):
    home, decision = delivery
    calls = []

    def interrupted(command, **kwargs):
        calls.append(command)
        receipt(home, command)
        raise KeyboardInterrupt("simulate worker crash after receipt")

    send = wake.hook_deliver(home, runner=interrupted, require_receipt=True)
    with pytest.raises(KeyboardInterrupt):
        send("employee_a", decision)
    for mid in decision["fresh"]:
        os.utime(home / "wake/claims" / mid, (1, 1))
    assert send("employee_a", decision)
    assert send("employee_a", decision)
    assert len(calls) == 1


def test_legacy_empty_ok_is_not_a_native_receipt(delivery):
    home, decision = delivery
    claims = home / "wake/claims"
    claims.mkdir(parents=True)
    (claims / "message_a.ok").touch()
    assert not wake.hook_deliver(home, require_receipt=True)("employee_a", decision)
    assert (claims / "message_a.unknown").exists()
