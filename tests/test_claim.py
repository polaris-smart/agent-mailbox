"""Claim semantics for ``mailbox_wait`` (v0.6.2 T3, P1–P4 收口).

``wait`` used to list pending mail and then consume it via ``check()`` in a
second unlocked step, so two waiters on one mailbox could both observe the
same pending batch and race over it. ``MailStore.claim`` is the locked
single-step fix: returned letters are atomically ``acked`` + ``claimed_by``
in the same pass, and the existing stale-acked reap round stays the
fail-open way back (claim 失败不丢信, 铁1 reap_ttl < dedup_ttl unchanged).
"""

import time

import pytest

from agent_mailbox import server
from agent_mailbox.store import MailStore


@pytest.fixture()
def fresh_server(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MAIL_HOME", str(tmp_path / "mail"))
    monkeypatch.delenv("AGENT_MAIL_TOKEN", raising=False)
    monkeypatch.setattr(server, "_store", None)
    monkeypatch.setattr(server, "_binding", None)
    yield server
    monkeypatch.setattr(server, "_store", None)
    monkeypatch.setattr(server, "_binding", None)


# ------------------------------------------------------------ store.claim

def test_second_claim_never_sees_claimed_mail(tmp_path):
    store = MailStore(root=tmp_path / "mail")
    store.register("A")
    store.register("B")
    sent = []
    for i in range(3):
        sent += store.send("A", "B", f"m{i}", "b")
    ids = {e["id"] for e in sent}

    first = store.claim("B")
    assert {m["id"] for m in first} == ids
    assert store.claim("B") == []  # second waiter: nothing left to claim

    for m in first:
        assert m["status"] == "acked"
        assert m["claimed_by"] == "B"
        assert m["acked_at"]
        assert m["handled_log"][-1]["action"] == "claimed"
    assert store.list_messages("B", status="pending") == []


def test_claim_empty_box_returns_empty(tmp_path):
    store = MailStore(root=tmp_path / "mail")
    store.register("B")
    assert store.claim("B") == []


def test_reclaim_round_restores_claimed_mail(tmp_path):
    """Fail-open: a claimed letter whose waiter died comes back via reap."""
    store = MailStore(root=tmp_path / "mail")
    store.register("A")
    store.register("B")
    ref = store.send("A", "B", "work item", "b")[0]

    store.claim("B")  # waiter dies before handling
    assert store.reap_stale_acked("B", ttl_seconds=0) == [ref["id"]]

    m = store.list_messages("B")[0]
    assert m["status"] == "pending"
    assert "claimed_by" not in m  # stale claim dies with the ack
    assert "acked_at" not in m
    assert [e["action"] for e in m["handled_log"]] == ["claimed", "reclaimed"]

    again = store.claim("B")  # consumable again
    assert [x["id"] for x in again] == [ref["id"]]


# ------------------------------------------------------- mailbox_wait tool

def test_wait_single_waiter_behavior_unchanged(fresh_server):
    fresh_server.mailbox_register("B")
    fresh_server.mailbox_send(to="B", subject="wake", body="b", from_id="A")
    t0 = time.monotonic()
    out = fresh_server.mailbox_wait("B", timeout_seconds=5)
    assert time.monotonic() - t0 < 3  # immediate on pending mail, as before
    assert out["received"] == 1
    assert out["messages"][0]["subject"] == "wake"
    assert out["messages"][0]["status"] == "acked"
    assert out["messages"][0]["claimed_by"] == "B"


def test_wait_second_waiter_gets_nothing_already_claimed(fresh_server):
    fresh_server.mailbox_register("B")
    fresh_server.mailbox_send(to="B", subject="t", body="b", from_id="A")

    first = fresh_server.mailbox_wait("B", timeout_seconds=5)
    assert first["received"] == 1

    second = fresh_server.mailbox_wait("B", timeout_seconds=1)
    assert second["messages"] == []  # no re-consumption of the claimed batch
    assert second.get("timeout") is True  # sleeps to timeout instead of
    # the old early "saw pending, checked, got nothing" empty wake-up


def test_wait_delivers_letters_landing_while_polling(fresh_server):
    """A letter that lands mid-poll is claimed on a later loop iteration."""
    fresh_server.mailbox_register("B")
    fresh_server.mailbox_register("A")
    # no mail yet: the wait polls in the background of this thread
    import threading

    def late_send():
        time.sleep(1.0)
        fresh_server.mailbox_send(to="B", subject="late", body="b", from_id="A")

    t = threading.Thread(target=late_send)
    t.start()
    out = fresh_server.mailbox_wait("B", timeout_seconds=10)
    t.join()
    assert out["received"] == 1
    assert out["messages"][0]["subject"] == "late"
