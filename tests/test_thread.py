"""Tests for v0.6 F2 — thread as a first-class citizen.

thread_id minting/inheritance on send/reply, legacy back-fill by subject
key (the 12-deep Re: chain collapsing to one thread), cross-agent thread
replay, --thread list filtering, and the ghost-thread warning.
"""

import json

import pytest

import agent_mailbox.server as server_mod
from agent_mailbox.server import mailbox_check, mailbox_list, mailbox_thread
from agent_mailbox.store import MailboxError, MailStore, thread_key


@pytest.fixture()
def store(tmp_path, monkeypatch):
    root = tmp_path / "mail"
    monkeypatch.setenv("AGENT_MAIL_HOME", str(root))
    server_mod._store = None  # the server caches one store — reset per test
    return MailStore(root=root)


# --------------------------------------------------------------- mint/inherit


def test_send_mints_shared_thread_id(store):
    store.register("HS")
    store.register("ZC")
    out = store.send("ZC", ["HS", "boss"], "kickoff", "agenda inside")
    ids = {m["thread_id"] for m in out}
    assert len(ids) == 1 and next(iter(ids)).startswith("th-")
    for m in out:
        letter = json.loads(
            (store.root / "inbox" / m["to"] / f"{m['id']}.json").read_text(encoding="utf-8")
        )
        assert letter["thread_id"] == out[0]["thread_id"]


def test_new_send_gets_fresh_thread(store):
    store.register("HS")
    a = store.send("ZC", "HS", "topic one", "a")[0]["thread_id"]
    b = store.send("ZC", "HS", "topic one", "b", dedupe=False)[0]["thread_id"]
    assert a != b


def test_reply_inherits_thread_id(store):
    store.register("HS")
    original = store.send("ZC", "HS", "design review", "please review")[0]
    reply = store.send("HS", "ZC", "Re: design review", "looks good", reply_to=original["id"])[0]
    assert reply["thread_id"] == original["thread_id"]


def test_reply_to_legacy_original_backfills_original(store):
    """A legacy letter carries no thread_id; the first reply back-fills the
    original with the reply's thread so the pair stays linked (向后兼容)."""
    store.register("HS")
    original = store.send("ZC", "HS", "legacy thread", "old body")[0]
    path = store.root / "inbox" / "HS" / f"{original['id']}.json"
    legacy = json.loads(path.read_text(encoding="utf-8"))
    del legacy["thread_id"]  # simulate a pre-v0.6 letter
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    reply = store.send("HS", "ZC", "Re: legacy thread", "done", reply_to=original["id"])[0]
    back = json.loads(path.read_text(encoding="utf-8"))
    assert back["thread_id"] == reply["thread_id"]


# ------------------------------------------------------------------- key


def test_thread_key_strips_stacked_prefixes():
    assert thread_key("Re: Re: Fwd: 发票确认") == thread_key("发票确认")
    assert thread_key("RE: X") == thread_key("x")
    assert thread_key("") == ""  # empty never groups


# ---------------------------------------------------------------- backfill


def _write_legacy(store, agent, subject, created):
    mid = f"legacy-{created}-{agent}".replace(":", "")
    msg = {
        "id": mid,
        "from": "ZC",
        "to": agent,
        "subject": subject,
        "body": "b",
        "priority": "normal",
        "status": "done",
        "reply_to": None,
        "created_at": created,
    }
    (store.root / "inbox" / agent).mkdir(parents=True, exist_ok=True)
    (store.root / "inbox" / agent / f"{mid}.json").write_text(
        json.dumps(msg, ensure_ascii=False), encoding="utf-8"
    )
    return mid


def test_backfill_collapses_twelve_re_chain(store):
    """验收判据: 12 连 Re: 的历史信回填后归并为 1 个 thread。"""
    store.register("HS")
    subjects = ["方案评审"]
    for i in range(12):
        subjects.append("Re: " * (i + 1) + "方案评审")
    for i, s in enumerate(subjects):
        _write_legacy(store, "HS", s, f"2026-09-01T00:{i:02d}:00Z")
    out = store.backfill_threads()
    assert out["groups"] == 1 and out["backfilled"] == 13
    tids = {x["thread_id"] for x in store.list_messages("HS")}
    assert len(tids) == 1 and None not in tids
    # idempotent: a second pass finds nothing left to group
    assert store.backfill_threads()["groups"] == 0


def test_backfill_singleton_stays_null(store):
    store.register("HS")
    _write_legacy(store, "HS", "one lonely letter", "2026-09-01T00:00:00Z")
    out = store.backfill_threads()
    assert out == {"groups": 0, "backfilled": 0}
    assert store.list_messages("HS")[0].get("thread_id") is None


# ------------------------------------------------------------------ replay


def test_thread_messages_cross_agent_time_order(store):
    store.register("HS")
    store.register("ZC")
    o = store.send("ZC", "HS", "handoff", "1")[0]
    r = store.send("HS", "ZC", "Re: handoff", "2", reply_to=o["id"])[0]
    store.send("ZC", "HS", "Re: Re: handoff", "3", reply_to=r["id"])
    out = store.thread_messages(o["thread_id"])
    assert out["count"] == 3
    assert [m["body"] for m in out["messages"]] == ["1", "2", "3"]  # oldest first
    assert {m["to"] for m in out["messages"]} == {"HS", "ZC"}  # cross-agent view


def test_thread_messages_accepts_message_id(store):
    store.register("HS")
    o = store.send("ZC", "HS", "by id", "x")[0]
    out = store.thread_messages(o["id"])
    assert out["count"] == 1 and out["messages"][0]["id"] == o["id"]


def test_thread_messages_unknown_ref_raises(store):
    with pytest.raises(MailboxError):
        store.thread_messages("th-nonexistent")


# ------------------------------------------------------------------ filter


def test_list_messages_thread_filter(store):
    store.register("HS")
    t1 = store.send("ZC", "HS", "alpha", "1")[0]["thread_id"]
    t2 = store.send("ZC", "HS", "beta", "2")[0]["thread_id"]
    got1 = store.list_messages("HS", thread=t1)
    got2 = mailbox_list(agent_id="HS", thread=t2)
    assert [m["thread_id"] for m in got1] == [t1]
    assert got2["count"] == 1 and got2["messages"][0]["thread_id"] == t2


def test_list_filter_matches_legacy_by_subject(store):
    store.register("HS")
    _write_legacy(store, "HS", "Re: Re: old thread", "2026-09-02T00:00:00Z")
    got = store.list_messages("HS", thread="old thread")
    assert len(got) == 1


# ------------------------------------------------------------------ ghosts


def test_ghost_threads_and_check_warning(store, monkeypatch):
    monkeypatch.setenv("AGENT_MAIL_ID", "HS")
    store.register("HS")
    tid = None
    for i in range(6):  # 6 open letters on one thread — over the limit of 5
        out = store.send("ZC", "HS", f"ghost {i}", "x", dedupe=False)
        tid = out[0]["thread_id"]
    # force one shared thread to accumulate: send() mints fresh ids, so
    # instead pin the letters onto one thread by back-filling manually.
    inbox = store.root / "inbox" / "HS"
    for p in sorted(inbox.glob("*.json")):
        m = json.loads(p.read_text(encoding="utf-8"))
        m["thread_id"] = tid
        p.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")

    ghosts = store.ghost_threads()
    assert len(ghosts) == 1 and ghosts[0]["open_count"] == 6

    out = mailbox_check(agent_id="HS")  # all 6 pending -> acked, still open
    assert out["unread"] == 6
    assert out["ghosts"][0]["open_count"] == 6
    assert "thread" in out["ghost_warning"].lower()


def test_no_ghost_warning_below_threshold(store, monkeypatch):
    monkeypatch.setenv("AGENT_MAIL_ID", "HS")
    store.register("HS")
    store.send("ZC", "HS", "small", "x")
    out = mailbox_check(agent_id="HS")
    assert "ghosts" not in out and "ghost_warning" not in out


def test_server_mailbox_thread_tool(store, monkeypatch):
    monkeypatch.setenv("AGENT_MAIL_ID", "HS")
    store.register("HS")
    o = store.send("ZC", "HS", "mcp replay", "1")[0]
    store.send("HS", "ZC", "Re: mcp replay", "2", reply_to=o["id"])
    out = mailbox_thread(o["thread_id"])
    assert out["count"] == 2
    assert out["messages"][0]["created_at"] <= out["messages"][1]["created_at"]
