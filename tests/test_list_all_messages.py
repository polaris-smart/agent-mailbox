"""Regression tests for the inbox+archive unified listing (HS 09-24 incident).

``mailbox_list`` only ever read the inbox, but handled letters live in
``archive/`` (``archive_done``/cleanup move them there) — an agent that
archives regularly saw zero letters in every status query (HS: five
queries, all 0, against 877 on-disk letters). ``list_all_messages`` is the
addressable whole mailbox these tests pin down, plus the structured miss
for ``mailbox_thread`` probes.
"""

import pytest

from agent_mailbox import server as srv
from agent_mailbox.store import MailboxError, MailStore


def _make_store(tmp_path):
    store = MailStore(root=tmp_path / "mail")
    store.register("A")
    store.register("B")
    return store


def test_list_all_spans_inbox_and_archive(tmp_path):
    store = _make_store(tmp_path)
    store.send("A", "B", "work item", "please handle")
    assert [m["subject"] for m in store.list_messages("B")] == ["work item"]

    got = store.check("B")  # pending -> acked, handed to caller
    assert [m["status"] for m in got] == ["acked"]
    store.set_status("B", got[0]["id"], "done")
    moved = store.archive_done("B")  # explicit archive sweep, as HS runs it
    assert moved == 1

    # inbox-only view is blind once archived...
    assert store.list_messages("B") == []
    # ...but the unified view still addresses the letter.
    all_msgs = store.list_all_messages("B")
    assert [m["subject"] for m in all_msgs] == ["work item"]
    assert all_msgs[0]["status"] == "done"


def test_list_all_status_filter(tmp_path):
    store = _make_store(tmp_path)
    store.send("A", "B", "first", "m1")
    store.send("A", "B", "second", "m2")
    inbox = store.list_messages("B")
    first_id = next(m["id"] for m in inbox if m["subject"] == "first")
    store.check("B")  # both -> acked
    store.set_status("B", first_id, "done")
    store.archive_done("B")

    done_only = store.list_all_messages("B", status="done")
    assert [m["subject"] for m in done_only] == ["first"]
    # the second letter stays in the inbox as acked
    acked_in_inbox = store.list_messages("B", status="acked")
    assert [m["subject"] for m in acked_in_inbox] == ["second"]


def test_list_all_thread_filter(tmp_path):
    store = _make_store(tmp_path)
    m1 = store.send("A", "B", "kickoff", "m1")
    store.send("A", "B", "Re: kickoff", "m2", reply_to=m1[0]["id"])
    store.check("B")  # both -> acked
    store.set_status("B", m1[0]["id"], "done")
    store.archive_done("B")  # kickoff archived, Re: kickoff stays in inbox

    thread_msgs = store.list_all_messages("B", thread=m1[0]["thread_id"])
    assert [m["subject"] for m in thread_msgs] == ["Re: kickoff", "kickoff"]


def test_mailbox_thread_structured_miss(tmp_path, monkeypatch):
    """A probe that matches nothing returns a structured miss, not a raise.

    HS 09-24 probed by commit hash and session id; the raw MailboxError
    bubbled as an MCP "Error executing tool" with no way to branch on it.
    """
    store = _make_store(tmp_path)
    monkeypatch.setattr(srv, "_store", store)
    monkeypatch.setenv("AGENT_MAIL_ID", "B")

    result = srv.mailbox_thread("not-a-real-thread-ref")
    assert result["count"] == 0
    assert result["matched_by"] == "miss"
    assert "not-a-real-thread-ref" in result["error"]

    with pytest.raises(MailboxError):
        store.thread_messages("not-a-real-thread-ref")
