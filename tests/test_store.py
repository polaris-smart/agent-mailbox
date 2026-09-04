"""Tests for agent-mailbox store: register, send, check, ack, broadcast, safety."""

import threading

import pytest

from agent_mailbox.store import MailboxError, MailStore


@pytest.fixture()
def store(tmp_path):
    return MailStore(root=tmp_path / "mail")


# ------------------------------------------------------------------ register

def test_register_new_agent(store):
    card = store.register("HS", owner="Hermes", description="PM & QA")
    assert card["agent_id"] == "HS"
    assert card["new"] is True
    assert (store.root / "inbox" / "HS").is_dir()


def test_register_idempotent(store):
    store.register("HS", owner="Hermes")
    card = store.register("HS", owner="Hermes", description="updated")
    assert card["new"] is False
    assert card["description"] == "updated"


@pytest.mark.parametrize("bad", ["", "../evil", "a/b", "a b", "x" * 65, "bé"])
def test_register_rejects_bad_ids(store, bad):
    with pytest.raises(MailboxError):
        store.register(bad)


# --------------------------------------------------------------------- send

def test_send_and_check_roundtrip(store):
    store.register("HS")
    store.register("WB")
    store.send("HS", "WB", "fuel ready", "1856 questions packed")
    msgs = store.check("WB")
    assert len(msgs) == 1
    assert msgs[0]["from"] == "HS"
    assert msgs[0]["subject"] == "fuel ready"
    assert msgs[0]["status"] == "acked"  # check marks as read


def test_check_only_returns_pending(store):
    store.register("HS")
    store.register("WB")
    store.send("HS", "WB", "one", "body")
    first = store.check("WB")
    second = store.check("WB")
    assert len(first) == 1 and second == []


def test_send_broadcast_all(store):
    for aid in ("HS", "WB", "ZC", "boss"):
        store.register(aid)
    sent = store.send("boss", "all", "standup", "10am")
    assert len(sent) == 4


def test_send_to_unknown_agent_still_creates_inbox(store):
    # open registration: sending creates the inbox, receiver registers later
    store.register("HS")
    store.send("HS", "NEWBIE", "hi", "welcome")
    msgs = store.check("NEWBIE")
    assert len(msgs) == 1


def test_send_rejects_bad_status(store):
    store.register("HS")
    with pytest.raises(MailboxError):
        store.send("HS", "WB", "s", "b", status="weird")


# -------------------------------------------------------------------- reply

def test_reply_chain(store):
    store.register("HS")
    store.register("WB")
    sent = store.send("HS", "WB", "review this", "please")
    msg_id = sent[0]["id"]
    store.check("WB")
    st = store
    st.set_status("WB", msg_id, "done")
    back = st.send("WB", "HS", "Re: review this", "done, all green", reply_to=msg_id)
    assert back[0]["to"] == "HS"
    got = st.check("HS")
    assert got[0]["reply_to"] == msg_id


# ------------------------------------------------------------------- status

def test_set_status_and_archive(store):
    store.register("HS")
    store.register("WB")
    sent = store.send("HS", "WB", "task", "do it")
    mid = sent[0]["id"]
    store.set_status("WB", mid, "done")
    n = store.archive_done("WB")
    assert n == 1
    assert store.list_messages("WB") == []
    assert (store.root / "archive" / "WB").is_dir()


def test_done_requires_existing_message(store):
    store.register("HS")
    with pytest.raises(MailboxError):
        store.set_status("HS", "nope", "done")


# ------------------------------------------------------------------ safety

def test_path_traversal_blocked_on_check(store):
    with pytest.raises(MailboxError):
        store.check("../../etc")


def test_corrupt_registry_raises(store):
    store.register("HS")
    (store.root / "registry.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(MailboxError):
        store.registry()


# --------------------------------------------------------------- concurrency

def test_concurrent_sends_no_corruption(store):
    for aid in ("HS", "WB"):
        store.register(aid)
    errors = []

    def worker(i):
        try:
            for j in range(10):
                store.send("HS", "WB", f"m{i}-{j}", "x")
        except (OSError, MailboxError) as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(store.list_messages("WB")) == 40
    reg = store.registry()  # registry still valid JSON
    assert "WB" in reg["agents"]
