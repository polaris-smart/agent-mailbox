"""v0.5.0 design B: handled_log half-done compensation + reap/intent interplay.

Covers the §3 compensation table (four rows, "看 log 到哪一段"), 小项1
(acked + no handled_log at all -> same as pending + no record), 缺口4 (a
fresh handling intent defers reclaim), the two-phase intent/outcome record
API (唯一写入方), and the N1 pin: ``reap_stale_acked`` is never wired into
any library-default automatic path.
"""

import inspect
import json
import re
import time

import pytest

from agent_mailbox import cleanup, server, watch, web
from agent_mailbox import store as store_mod
from agent_mailbox.store import (
    HANDLED_INTENT,
    HANDLED_OUTCOME,
    INTENT_TTL_DEFAULT,
    MailboxError,
    MailStore,
)


@pytest.fixture()
def store(tmp_path):
    st = MailStore(root=tmp_path / "mail")
    st.register("A")
    st.register("B")
    return st


def _send_and_claim(store, subject="work item"):
    mid = store.send("A", "B", subject, "please handle")[0]["id"]
    store.check("B")  # B claims it: pending -> acked
    return mid


# ------------------------------------------------------- §3 compensation table


def test_row1_pending_no_records_is_process(store):
    mid = store.send("A", "B", "row1", "body")[0]["id"]
    plan = store.resume_plan("B", mid)
    assert plan == {
        "id": mid,
        "status": "pending",
        "intent": False,
        "outcome": False,
        "resume": "process",
    }


def test_row2_acked_no_records_is_process_too(store):
    """小项1: claimed (acked) then crashed before writing the intent — same
    treatment as pending + no record: normal handling from the intent on."""
    mid = _send_and_claim(store, "row2")
    msg = store.list_messages("B", status="acked")[0]
    assert msg["id"] == mid
    # acked itself left a handled_log entry, but no intent/outcome yet
    assert [e["action"] for e in msg["handled_log"]] == ["acked"]
    plan = store.resume_plan("B", mid)
    assert plan["resume"] == "process" and not plan["intent"] and not plan["outcome"]


def test_row3_intent_without_outcome_is_replay(store):
    mid = _send_and_claim(store, "row3")
    store.record_handled("B", mid, HANDLED_INTENT, note="started")
    plan = store.resume_plan("B", mid)
    assert plan["resume"] == "replay" and plan["intent"] and not plan["outcome"]


def test_row4_intent_outcome_not_done_is_finalize(store):
    mid = _send_and_claim(store, "row4")
    store.record_handled("B", mid, HANDLED_INTENT)
    store.record_handled("B", mid, HANDLED_OUTCOME, note="receipt sent")
    plan = store.resume_plan("B", mid)
    assert plan["resume"] == "finalize"
    store.set_status("B", mid, "done")  # the only remaining step
    assert store.resume_plan("B", mid)["resume"] == "skip"


def test_done_is_skip(store):
    mid = store.send("A", "B", "row5", "body")[0]["id"]
    store.set_status("B", mid, "done")
    assert store.resume_plan("B", mid)["resume"] == "skip"


def test_resume_plan_survives_archive(store):
    mid = store.send("A", "B", "arch", "body")[0]["id"]
    store.set_status("B", mid, "done")
    store.archive_done("B")
    assert store.resume_plan("B", mid)["resume"] == "skip"


def test_resume_plan_unknown_message_raises(store):
    with pytest.raises(MailboxError):
        store.resume_plan("B", "nope")


# ------------------------------------------------------------ record_handled


def test_record_handled_appends_two_phase_entries(store):
    mid = _send_and_claim(store, "two-phase")
    store.record_handled("B", mid, HANDLED_INTENT)
    store.record_handled("B", mid, HANDLED_OUTCOME)
    store.set_status("B", mid, "done")
    msg = store.list_messages("B", status="done")[0]
    assert [e["action"] for e in msg["handled_log"]] == ["acked", "intent", "outcome", "done"]
    assert all(e["by"] and e["at"] for e in msg["handled_log"])  # auditable: who + when
    # done only lands after the outcome: log precedes the status flip (伤①)
    assert msg["status"] == "done"


def test_record_handled_carries_extra_fields(store):
    mid = _send_and_claim(store, "fields")
    m = store.record_handled("B", mid, HANDLED_OUTCOME, note="receipt 20260913")
    entry = m["handled_log"][-1]
    assert entry["note"] == "receipt 20260913"
    assert entry["action"] == HANDLED_OUTCOME


def test_record_handled_unknown_message_raises(store):
    with pytest.raises(MailboxError):
        store.record_handled("B", "nope", HANDLED_INTENT)


# ----------------------------------------------------------------- 缺口4 reap


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + "Z"


def test_fresh_intent_defers_reclaim(store):
    """缺口4: an intent younger than the handling timeout (30m) is skipped —
    the agent is actively on it; reclaim happens once the intent goes stale."""
    mid = _send_and_claim(store, "fresh intent")
    store.record_handled("B", mid, HANDLED_INTENT)
    p = store.root / "inbox" / "B" / f"{mid}.json"
    m = json.loads(p.read_text(encoding="utf-8"))
    m["acked_at"] = _iso(time.time() - 7200)  # the ack itself is ancient...
    m["handled_log"][-1]["at"] = _iso(time.time() - 60)  # ...but the intent is fresh
    p.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")

    assert store.reap_stale_acked("B", ttl_seconds=3600) == []
    assert store.list_messages("B", status="acked")[0]["id"] == mid

    m = json.loads(p.read_text(encoding="utf-8"))
    m["handled_log"][-1]["at"] = _iso(time.time() - INTENT_TTL_DEFAULT - 1)  # intent stale
    p.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    assert store.reap_stale_acked("B", ttl_seconds=3600) == [mid]
    msg = store.list_messages("B", status="pending")[0]
    assert [e["action"] for e in msg["handled_log"]][-2:] == ["intent", "reclaimed"]


def test_no_intent_reclaims_as_before(store):
    _send_and_claim(store, "no intent")
    assert len(store.reap_stale_acked("B", ttl_seconds=0)) == 1


def test_reclaim_default_ttl_comes_from_config(tmp_path):
    root = tmp_path / "mail"
    root.mkdir()
    (root / "config.json").write_text('{"reap_ttl": 5400}', encoding="utf-8")  # 缺口3: 1–2h
    st = MailStore(root=root)
    st.register("A")
    st.register("B")
    st.send("A", "B", "cfg ttl", "body")
    st.check("B")
    # letter is 0s old: the config-sourced default TTL (5400s) must keep it acked
    assert st.reap_stale_acked("B") == []


# --------------------------------------------------------------- N1: no auto-reap


def test_n1_reap_has_no_library_call_site():
    """N1: ``reap_stale_acked`` must stay a manual maintenance operation —
    no library module may call it automatically. The wake script (outside the
    package) is the only wired caller. This pin stops an accidental auto-hook."""
    call_sites = re.compile(r"(?<!def )reap_stale_acked\(")
    for mod in (server, store_mod, web, watch, cleanup):
        src = inspect.getsource(mod)
        hit = call_sites.search(src)
        assert hit is None, f"{mod.__name__} auto-calls reap_stale_acked: {hit}"


def test_n1_normal_operations_never_reclaim_acked(store):
    """N1 behavioral: ordinary store traffic must not resurrect fresh acked mail."""
    mid = _send_and_claim(store, "stay acked")
    store.send("A", "B", "more", "traffic")  # sends, lists, checks afterwards...
    store.list_messages("B")
    store.check("B")
    statuses = {m["id"]: m["status"] for m in store.list_messages("B")}
    assert statuses[mid] == "acked"  # ...yet the orphaned letter stays acked
