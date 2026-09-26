"""v0.5.0 design A: delivery-side semantic-hash duplicate suppression.

Covers the HS review hard requirements (docs/v0.5.0-实施任务书.md):
铁1 config coupling pin, 铁2 send-contract E2E on all three paths, 缺口1
hash 口径, 缺口2 inbox-only scope + index, 微点2 TTL 放行, and the
null-hash legacy rule (旧信不回填).
"""

import json
import re
from pathlib import Path

import pytest

from agent_mailbox import store as store_mod
from agent_mailbox.store import (
    DEDUP_TTL_DEFAULT,
    REAP_TTL_DEFAULT,
    MailboxError,
    MailStore,
    load_window_config,
    semantic_hash,
)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    notified: list[list[dict]] = []
    monkeypatch.setattr(
        store_mod, "notify_new_messages", lambda msgs, **kw: notified.append(list(msgs))
    )
    st = MailStore(root=tmp_path / "mail")
    st.notified = notified  # type: ignore[attr-defined]
    st.register("A")
    st.register("B")
    return st


# ------------------------------------------------------------------ 缺口1 hash


def test_hash_pinned_formula():
    h = semantic_hash("s", "b")
    assert re.fullmatch(r"[0-9a-f]{64}", h)  # full 64 hex, stored in full
    assert h == semantic_hash(" s \n ", "b\t")  # whitespace folds
    assert h == semantic_hash("ｓ", "b")  # NFKC folds fullwidth
    assert h != semantic_hash("different", "b")
    assert h != semantic_hash("s", "different")


def test_hash_code_regions_raw_and_ordered():
    """缺口1: fenced regions hash raw, in original order — folding prose must
    not make different code orderings collide."""
    a = semantic_hash("t", "```\nx = 1\ny = 2\n```\nprose")
    b = semantic_hash("t", "```\ny = 2\nx = 1\n```\nprose")
    assert a != b  # order preserved, zero folding
    assert a == semantic_hash("t", "```\nx = 1\ny = 2\n```\n  prose\t")  # prose folds, code raw
    # unterminated fence still hashes raw
    c = semantic_hash("t", "prose\n```\nopen chunk")
    assert c != semantic_hash("t", "prose")
    assert c == semantic_hash("t", "prose\n```\nopen chunk")


def test_hash_changes_when_code_body_changes():
    assert semantic_hash("t", "```\na\n```") != semantic_hash("t", "```\nb\n```")


# --------------------------------------------------------------- 铁2 three paths


def test_dupe_blocked_with_zero_side_effects(store):
    """铁2 ①: hit -> blocked, caller sees deduped/existing_id, zero side effects."""
    first = store.send("A", "B", "wake", "please look")
    assert first[0]["id"]
    before = sorted(p.name for p in (store.root / "inbox" / "B").glob("*.json"))

    second = store.send("A", "B", "wake", "  please\nlook ")  # same semantics, folded
    assert second == [{"to": "B", "deduped": True, "existing_id": first[0]["id"]}]

    after = sorted(p.name for p in (store.root / "inbox" / "B").glob("*.json"))
    assert after == before  # no letter landed
    assert len(store.notified) == 1  # webhook fired for the first send only
    sent_log = store.root / "sent.log"
    lines = sent_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1  # no sent.log line for the deduped send (零副作用)


def test_dedupe_false_exempts(store):
    """铁2 ②: dedupe=False delivers despite an identical pending letter."""
    first = store.send("A", "B", "cron", "run it")
    second = store.send("A", "B", "cron", "run it", dedupe=False)
    assert second[0]["id"] != first[0]["id"]
    assert "deduped" not in second[0]
    assert len(store.list_messages("B", status="pending")) == 2


def test_same_hash_terminal_passes_through(store):
    """铁2 ③: same hash but done is delivered normally (缺口2: done 在箱内
    可查状态放行；归档场景由 test_only_target_inbox_scanned_never_archive 覆盖)."""
    mid = store.send("A", "B", "task", "do it")[0]["id"]
    store.set_status("B", mid, "done")
    again = store.send("A", "B", "task", "do it")
    assert again[0]["id"] != mid and "deduped" not in again[0]


# ------------------------------------------------------------------ window/范围


def test_ttl_release_leaves_two_same_hash_letters(store):
    """微点2: after the dedup window a re-send passes — the queue may then
    legitimately hold two same-hash non-terminal letters."""
    first = store.send("A", "B", "old", "mail")[0]
    p = store.root / "inbox" / "B" / f"{first['id']}.json"
    m = json.loads(p.read_text(encoding="utf-8"))
    m["created_at"] = "2026-09-01T00:00:00Z"  # older than the 24h window
    p.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")

    second = store.send("A", "B", "old", "mail")
    assert "deduped" not in second[0]
    same = [x for x in store.list_messages("B") if x.get("semantic_hash")]
    assert len(same) == 2 and len({x["semantic_hash"] for x in same}) == 1


def test_only_target_inbox_scanned_never_archive(store):
    """缺口2: a same-hash acked letter in the archive must not block — the
    dedup query never leaves the inbox."""
    mid = store.send("A", "B", "drift", "body")[0]["id"]
    store.set_status("B", mid, "done")
    store.archive_done("B")
    p = store.root / "archive" / "B" / f"{mid}.json"
    m = json.loads(p.read_text(encoding="utf-8"))
    m["status"] = "acked"  # worst case: an archived acked orphan
    p.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")

    assert "deduped" not in store.send("A", "B", "drift", "body")[0]


def test_legacy_letter_without_hash_never_blocks(store):
    """旧信 null hash 不回填：pre-v0.5 letters cannot match a new hash."""
    p = store.root / "inbox" / "B"
    legacy = {
        "id": "20260901000000-legacy-b",
        "from": "A",
        "to": "B",
        "subject": "legacy",
        "body": "legacy",
        "priority": "normal",
        "status": "pending",
        "reply_to": None,
        "created_at": "2026-09-01T00:00:00Z",
    }
    (p / f"{legacy['id']}.json").write_text(json.dumps(legacy), encoding="utf-8")
    out = store.send("A", "B", "legacy", "legacy")
    assert "deduped" not in out[0]


def test_message_stores_full_hash(store):
    mid = store.send("A", "B", "hash me", "body")[0]["id"]
    m = json.loads((store.root / "inbox" / "B" / f"{mid}.json").read_text(encoding="utf-8"))
    assert m["semantic_hash"] == semantic_hash("hash me", "body")


def test_broadcast_dedupes_per_recipient(store):
    store.register("C")
    first = store.send("A", "all", "standup", "now")
    assert len(first) == 3  # A, B, C
    second = store.send("A", "all", "standup", "now")
    assert len(second) == 3
    assert all(e.get("deduped") for e in second)


# ------------------------------------------------------------------ 铁1 pin


def test_iron1_defaults_satisfy_reap_below_dedup():
    assert REAP_TTL_DEFAULT < DEDUP_TTL_DEFAULT
    assert load_window_config(Path("/nonexistent-root-for-test")) == {
        "dedup_ttl": DEDUP_TTL_DEFAULT,
        "reap_ttl": REAP_TTL_DEFAULT,
    }


@pytest.mark.parametrize(
    "cfg",
    [
        '{"reap_ttl": 86400}',  # reap >= default dedup
        '{"dedup_ttl": 3600, "reap_ttl": 3600}',  # equal — must fail loudly
        '{"dedup_ttl": 3600, "reap_ttl": 7200}',
        '{"reap_ttl": 0}',
        "not json at all",
    ],
)
def test_iron1_config_violation_fails_loudly(tmp_path, cfg):
    """铁1: a config that breaks reap_ttl < dedup_ttl must fail at load time
    (loud MailboxError), not silently distort the windows."""
    root = tmp_path / "mail"
    root.mkdir()
    (root / "config.json").write_text(cfg, encoding="utf-8")
    with pytest.raises(MailboxError):
        load_window_config(root)
    st = MailStore(root=root)
    st.register("A")
    with pytest.raises(MailboxError):
        st.send("A", "B", "s", "b")  # the send path gates on the same config
    with pytest.raises(MailboxError):
        st.reap_stale_acked("A")  # so does the reap default


def test_iron1_valid_override_accepted(tmp_path):
    root = tmp_path / "mail"
    root.mkdir()
    (root / "config.json").write_text('{"dedup_ttl": 7200, "reap_ttl": 1800}', encoding="utf-8")
    assert load_window_config(root) == {"dedup_ttl": 7200.0, "reap_ttl": 1800.0}


# ------------------------------------------------------------------ index


def test_index_sees_letters_from_other_instances(tmp_path):
    """缺口2: two MailStore instances on one root dedupe against each other —
    the snapshot refreshes off the inbox directory mtime."""
    s1 = MailStore(root=tmp_path / "mail")
    s2 = MailStore(root=tmp_path / "mail")
    for s in (s1, s2):
        s.register("A")
        s.register("B")
    s1.send("A", "B", "cross", "process")
    assert s2.send("A", "B", "cross", "process")[0].get("deduped")
    # and after the letter is done'd by the other instance, s2 lets it through
    s2.set_status("B", s2.list_messages("B")[0]["id"], "done")
    assert "deduped" not in s2.send("A", "B", "cross", "process")[0]


# ------------------------------------------------------- t-38② 重复件不重发 wake


def test_dedupe_false_resend_suppresses_wake_for_dup(store):
    """t-38②：dedupe=False 落箱的重复件照常落盘留痕，但不再发 wake 通知
    （webhook notify 面剔除 + 信体带 wake_suppressed_dup 标记）——双 wake
    = 收件人白跑一轮（HS 第10会话实证）。"""
    first = store.send("A", "B", "sync", "same content")
    assert len(store.notified) == 1  # 原信发一次 wake
    before = len(store.notified[-1])

    second = store.send("A", "B", "sync", "same content", dedupe=False)
    # 信照常落箱（调用侧显式豁免）
    assert "deduped" not in second[0]
    letters = store.list_messages("B", status="pending")
    dups = [m for m in letters if m.get("wake_suppressed_dup")]
    assert len(dups) == 1 and dups[0]["wake_suppressed_dup"] == first[0]["id"]
    # 但不产生新的 wake 通知
    assert len(store.notified) == 1
    assert len(store.notified[-1]) == before


def test_dedupe_false_resend_fires_wake_when_no_prior(store):
    """dedupe=False 豁免只在箱内确有同 hash 非终态信时才抑制 wake；箱内无
    原信（首投/原信已终态）照常唤醒。"""
    store.send("A", "B", "fresh", "once")
    assert len(store.notified) == 1
    # 原信 done（终态）→ 同文再发不算重复件，wake 照发
    store.set_status("B", store.list_messages("B")[0]["id"], "done")
    store.send("A", "B", "fresh", "once", dedupe=False)
    assert len(store.notified) == 2
