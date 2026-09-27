"""Regression tests for orphaned ``acked`` mail (task t-6, 2026-09-13 incident).

``check()`` flips pending -> acked and hands the mail to the caller. Any
caller that never finishes (crash, cancelled turn, or a foreign-identity
check, see the 09-13 B9 report) leaves the letter acked forever, and a drain
that only scans ``pending`` never sees it again. These tests pin the
reclaim path and the full reply -> check -> done chain around it.
"""

import json
import os
import time

from agent_mailbox.store import MailStore


def _msgs(store, agent, status):
    return store.list_messages(agent, status=status)


def test_orphaned_ack_is_reclaimed_and_replayable(tmp_path):
    store = MailStore(root=tmp_path / "mail")
    store.register("A")
    store.register("B")
    store.send("A", "B", "work item", "please handle")

    got = store.check("B")  # reserve it...
    assert [m["status"] for m in got] == ["acked"]
    assert _msgs(store, "B", "pending") == []  # ...then the caller dies

    reaped = store.reap_stale_acked("B", ttl_seconds=0)
    assert len(reaped) == 1

    msg = store.list_messages("B")[0]
    assert msg["status"] == "pending"
    assert "acked_at" not in msg
    assert [e["action"] for e in msg["handled_log"]] == ["acked", "reclaimed"]

    # the reclaimed letter is visible to a pending-only drain again
    replay = store.check("B")
    assert [m["id"] for m in replay] == [msg["id"]]
    store.set_status("B", msg["id"], "done")
    assert _msgs(store, "B", "done")[0]["id"] == msg["id"]


def test_fresh_ack_is_left_alone(tmp_path):
    store = MailStore(root=tmp_path / "mail")
    store.register("A")
    store.register("B")
    store.send("A", "B", "work item", "please handle")
    store.check("B")

    assert store.reap_stale_acked("B", ttl_seconds=3600) == []
    assert len(_msgs(store, "B", "acked")) == 1


def test_reclaim_falls_back_to_mtime_without_acked_at(tmp_path):
    """Legacy writers (pre-handled_log) set status=acked with no timestamp."""
    store = MailStore(root=tmp_path / "mail")
    store.register("A")
    store.register("B")
    store.send("A", "B", "legacy", "body")
    p = next((store.root / "inbox" / "B").glob("*.json"))
    m = json.loads(p.read_text(encoding="utf-8"))
    m["status"] = "acked"
    m.pop("acked_at", None)
    p.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    old = time.time() - 7200
    os.utime(p, (old, old))

    assert len(store.reap_stale_acked("B", ttl_seconds=3600)) == 1
    assert store.list_messages("B")[0]["status"] == "pending"


def test_reclaim_only_touches_acked(tmp_path):
    store = MailStore(root=tmp_path / "mail")
    store.register("A")
    store.register("B")
    store.send("A", "B", "still pending", "body")
    store.send("A", "B", "will be done", "body")
    done_id = _msgs(store, "B", "pending")[1]["id"]
    store.set_status("B", done_id, "done")

    assert store.reap_stale_acked("B", ttl_seconds=0) == []
    assert len(_msgs(store, "B", "pending")) == 1
    assert len(_msgs(store, "B", "done")) == 1


def test_reclaim_is_per_mailbox(tmp_path):
    store = MailStore(root=tmp_path / "mail")
    store.register("A")
    store.register("B")
    store.register("C")
    store.send("A", "B", "for B", "body")
    store.check("B")

    assert store.reap_stale_acked("C", ttl_seconds=0) == []
    assert len(_msgs(store, "B", "acked")) == 1


def test_reply_check_done_chain_survives_orphan(tmp_path):
    """reply -> check -> done, with a reclaim round in the middle."""
    store = MailStore(root=tmp_path / "mail")
    store.register("A")
    store.register("B")
    store.register("C")
    first = store.send("A", "B", "topic", "ping")[0]["id"]

    store.send("B", "A", "Re: topic", "pong", reply_to=first)  # B answers, A gets the reply
    reply_id = _msgs(store, "A", "pending")[0]["id"]
    store.check("A")  # A reserves the reply, then dies
    assert store.reap_stale_acked("A", ttl_seconds=0) == [reply_id]

    got = store.check("A")
    assert [m["id"] for m in got] == [reply_id]
    assert got[0]["subject"] == "Re: topic"
    store.set_status("A", reply_id, "done")
    assert store.list_messages("A", status="done")[0]["id"] == reply_id
