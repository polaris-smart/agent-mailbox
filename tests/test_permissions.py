"""v0.7.5 PR B — 身份/权限模型 + 密封信 + 外部来源门 (任务书 §3.3/§3.4).

Hermetic acceptance fixtures (tmp mail roots only, no real state):
- member kinds (owner/agent/guest) with legacy-registry compatibility;
- agent members cannot read another member's mailbox (structured rejection);
  the owner can — 验收判据 4;
- sealed bodies appear only through the recipient agent's tools; the owner's
  tools and every human view (web API list + detail) get metadata only —
  验收判据 5;
- external-origin letters land but never wake and never POST the webhook,
  until an owner confirms them (greppable confirm_external) — 验收判据 6;
- visibility changes append to audit.log — 验收判据 7;
- guest senders need a valid pairing token; attention 打扰三档 rides letters.
"""

import hashlib
import json
import threading

import pytest

from agent_mailbox import server
from agent_mailbox import store as store_mod
from agent_mailbox.store import (
    ATTENTION_TIERS,
    MailboxError,
    MailStore,
    load_pairing_tokens,
    redact_sealed,
)
from agent_mailbox.wake import WakeConfig, run_once
from agent_mailbox.web import LoopbackServer, _BoardHandler

TOKEN = "perm-token-1"


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@pytest.fixture()
def root(tmp_path, monkeypatch):
    """Isolated mail root + reset server-side caches between tests."""
    r = tmp_path / "mail"
    monkeypatch.setenv("AGENT_MAIL_HOME", str(r))
    monkeypatch.delenv("AGENT_MAIL_ID", raising=False)
    monkeypatch.setattr(server, "_store", None)
    monkeypatch.setattr(server, "_binding", None)
    yield r
    monkeypatch.setattr(server, "_store", None)
    monkeypatch.setattr(server, "_binding", None)


@pytest.fixture()
def store(root):
    st = MailStore(root=root)
    for aid in ("A", "B", "boss"):
        st.register(aid)
    return st


@pytest.fixture()
def board(root):
    handler = type("H", (_BoardHandler,), {"store": MailStore(root=root), "token": TOKEN})
    srv = LoopbackServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield port
    srv.shutdown()
    srv.server_close()


def _req(port: int, path: str, *, token: str | None = TOKEN):
    import http.client

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


class _Recorder:
    def __init__(self):
        self.calls = []

    def deliver(self, msg):
        self.calls.append(msg["id"])
        return True


def _wake_cfg(root):
    return WakeConfig(
        {
            "agent_id": "B",
            "adapter": "generic-webhook",
            "webhook": {"url": "http://127.0.0.1:9/hook", "secret": "s"},
            "retry_interval": 0,
        },
        root,
    )


# ------------------------------------------------------- member kinds (§3.3)


def test_kind_defaults_owner_ids_and_explicit(store):
    assert store.kind_of("boss") == "owner"  # legacy human id
    assert store.kind_of("A") == "agent"  # local agent default
    store.register("G1", kind="guest")
    assert store.kind_of("G1") == "guest"
    card = store.register("G1")  # idempotent re-confirm keeps the kind
    assert card["kind"] == "guest"
    with pytest.raises(MailboxError, match="kind must be one of"):
        store.register("X", kind="superuser")


def test_legacy_registry_card_without_kind_still_works(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "registry.json").write_text(
        json.dumps({"agents": {"OLD1": {"created_at": "2026-01-01T00:00:00Z"}}}),
        encoding="utf-8",
    )
    st = MailStore(root=root)
    assert st.kind_of("OLD1") == "agent"
    assert st.kind_of("never-seen") == "agent"
    card = st.register("OLD1", owner="legacy")  # old-style call, no kind
    assert card["kind"] == "agent" and card["owner"] == "legacy"


# ------------------------------------------- 判据: agent 看不到他人信件 (API 拒绝)


def test_agent_reading_another_mailbox_is_rejected(store, monkeypatch):
    store.send("A", "B", "for b", "b-body")
    monkeypatch.setenv("AGENT_MAIL_ID", "A")
    with pytest.raises(MailboxError, match="permission denied"):
        server.mailbox_check(agent_id="B")
    with pytest.raises(MailboxError, match="permission denied"):
        server.mailbox_list(agent_id="B")
    with pytest.raises(MailboxError, match="permission denied"):
        server.mailbox_wait(agent_id="B")
    with pytest.raises(MailboxError, match="permission denied"):
        server.mailbox_done(msg_id="whatever", agent_id="B")
    with pytest.raises(MailboxError, match="permission denied"):
        server.mailbox_reply(msg_id="whatever", body="x", agent_id="B")
    # own mailbox keeps working
    monkeypatch.setenv("AGENT_MAIL_ID", "B")
    assert server.mailbox_check("B")["unread"] == 1


def test_owner_identity_reads_any_mailbox(store, monkeypatch):
    store.send("A", "B", "for b", "b-body")
    monkeypatch.setenv("AGENT_MAIL_ID", "boss")
    out = server.mailbox_check(agent_id="B")
    assert out["unread"] == 1
    assert out["messages"][0]["body"] == "b-body"


def test_unset_caller_id_keeps_legacy_local_trust(store, monkeypatch):
    monkeypatch.delenv("AGENT_MAIL_ID", raising=False)
    store.send("A", "B", "for b", "b-body")
    assert server.mailbox_check("B")["unread"] == 1


# ---------------------------------------------------- 密封信 (sealed, §3.3)


def test_sealed_body_readable_only_by_recipient_tool(store, monkeypatch):
    sent = store.send("A", "B", "credentials", "SECRET-BODY", sealed=True)
    mid = sent[0]["id"]
    # recipient agent's own tools: full content
    monkeypatch.setenv("AGENT_MAIL_ID", "B")
    listed = server.mailbox_list("B")["messages"]
    assert listed[0]["body"] == "SECRET-BODY"
    # sender is NOT the recipient: metadata only
    monkeypatch.setenv("AGENT_MAIL_ID", "A")
    seen = server.mailbox_thread(mid)["messages"]
    assert all("SECRET-BODY" != m.get("body") for m in seen)
    assert any(m.get("redacted") == "sealed" for m in seen)
    # owner's tool: metadata only too — 全可见不等于密封信可见
    monkeypatch.setenv("AGENT_MAIL_ID", "boss")
    out = server.mailbox_list(agent_id="B")["messages"]
    assert "body" not in out[0] and out[0]["redacted"] == "sealed"
    assert out[0]["subject"] == "credentials"  # envelope metadata stays
    # redact_sealed unit: recipient passes through untouched
    assert redact_sealed(listed[0], reader="B") is listed[0]


def test_sealed_metadata_only_in_web_human_view(store, board):
    sent = store.send("A", "B", "credentials", "SECRET-BODY", sealed=True)
    store.send("A", "B", "plain", "normal-body")
    status, body = _req(board, "/api/messages")
    assert status == 200
    text = body.decode("utf-8")
    assert "SECRET-BODY" not in text  # sealed content never enters the human view
    assert "normal-body" in text  # unsealed letters display as before
    msgs = json.loads(body)["messages"]
    sealed_view = next(m for m in msgs if m["id"] == sent[0]["id"])
    assert "body" not in sealed_view and sealed_view["redacted"] == "sealed"
    assert sealed_view["from"] == "A" and sealed_view["to"] == "B"  # who/when stay
    # detail endpoint: same redaction, addressable by id
    status, body = _req(board, f"/api/messages/{sent[0]['id']}")
    assert status == 200
    detail = json.loads(body)["message"]
    assert "body" not in detail and detail["redacted"] == "sealed"


def test_web_messages_endpoint_requires_token(board):
    assert _req(board, "/api/messages", token=None)[0] == 401


def test_web_unknown_letter_is_structured_404(store, board):
    status, body = _req(board, "/api/messages/nope")
    assert status == 404
    assert "nope" in json.loads(body)["error"]


# --------------------------------- 判据: 外部来源信不触发动作 (origin=external)


def test_external_letter_lands_but_no_webhook_no_wake(root, monkeypatch):
    st = MailStore(root=root)
    st.register("B")
    notified = []
    monkeypatch.setattr(store_mod, "notify_new_messages", lambda msgs, **kw: notified.extend(msgs))
    ext = st.send("OUTSIDE", "B", "external ping", "stranger body", origin="external")
    loc = st.send("A", "B", "local ping", "local body")  # control: local still POSTs
    assert [m["subject"] for m in notified] == ["local ping"]
    # the external letter landed normally — 只落箱
    landed = {m["id"]: m for m in st.list_messages("B")}
    assert landed[ext[0]["id"]]["origin"] == "external"
    rec = _Recorder()
    stats = run_once(root, _wake_cfg(root), adapter=rec)
    assert stats["woke"] == 1 and rec.calls == [loc[0]["id"]]
    assert stats["skipped_external"] == 1  # sampling/wake path skipped it


def test_confirm_external_is_owner_only_and_greppable(root, monkeypatch):
    st = MailStore(root=root)
    st.register("B")
    ext = st.send("OUTSIDE", "B", "external", "x", origin="external")
    mid = ext[0]["id"]
    monkeypatch.setenv("AGENT_MAIL_ID", "B")
    with pytest.raises(MailboxError, match="permission denied"):
        server.mailbox_confirm_external(msg_id=mid)
    monkeypatch.setenv("AGENT_MAIL_ID", "boss")
    out = server.mailbox_confirm_external(msg_id=mid)
    assert out == {"message": mid, "origin": "local", "confirmed_by": "boss"}
    letter = st.find_letter(mid)
    assert letter["origin"] == "local"
    assert any(e["action"] == "confirmed_external" for e in letter["handled_log"])
    # after the human confirmation the wake path may act on it
    rec = _Recorder()
    assert run_once(root, _wake_cfg(root), adapter=rec)["woke"] == 1


def test_send_rejects_unknown_origin_and_attention(store):
    with pytest.raises(MailboxError, match="origin must be one of"):
        store.send("A", "B", "s", "b", origin="mars")
    with pytest.raises(MailboxError, match="attention must be one of"):
        store.send("A", "B", "s", "b", attention="loud")


# ------------------------------------------------------- 配对令牌 (guest 发信)


def test_guest_send_requires_valid_pairing_token(root):
    st = MailStore(root=root)
    st.register("G1", kind="guest")
    st.register("B")
    with pytest.raises(MailboxError, match="pairing token required"):
        st.send("G1", "B", "hi", "x")  # no token configured at all
    (root / "config.json").write_text(
        json.dumps({"pairing_tokens": {"dev": _sha("tok123")}}), encoding="utf-8"
    )
    assert load_pairing_tokens(root) == {"dev": _sha("tok123")}
    with pytest.raises(MailboxError, match="pairing token required"):
        st.send("G1", "B", "hi", "x", pairing_token="wrong")
    out = st.send("G1", "B", "hi", "x", pairing_token="tok123")
    assert out[0]["to"] == "B"
    # local members are never gated
    assert st.send("B", "G1", "re", "y")[0]["to"] == "G1"


def test_pairing_tokens_malformed_fails_loudly(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(
        json.dumps({"pairing_tokens": {"dev": "zz"}}), encoding="utf-8"
    )
    with pytest.raises(MailboxError, match="sha256"):
        load_pairing_tokens(root)


def test_guest_cannot_broadcast(root, monkeypatch):
    MailStore(root=root).register("G1", kind="guest")
    monkeypatch.setenv("AGENT_MAIL_ID", "G1")
    with pytest.raises(MailboxError, match="guest members cannot broadcast"):
        server.mailbox_broadcast(subject="s", body="b")


# ---------------------------------------------------- 可见性变更留痕 (audit)


def test_visibility_changes_are_audited(root):
    st = MailStore(root=root)
    st.register("B")
    st.register("B", kind="guest")  # visibility change 1
    st.register("B", kind="agent")  # visibility change 2
    lines = [json.loads(l) for l in (root / "audit.log").read_text("utf-8").splitlines()]
    entries = [e for e in lines if e["action"] == "member_kind"]
    assert [e["kind"] for e in entries] == ["guest", "agent"]
    assert all(e["by"] == "B" and e["member"] == "B" and e["at"] for e in entries)


def test_register_without_kind_writes_no_audit(root):
    st = MailStore(root=root)
    st.register("B")
    assert not (root / "audit.log").exists()


# ---------------------------------------------------- 打扰三档 (§3.4 元数据)


def test_attention_tiers_default_and_explicit(store):
    assert ATTENTION_TIERS == ("decision", "report", "archive")
    normal = store.send("A", "B", "t1", "x")[0]
    assert store.find_letter(normal["id"])["attention"] == "decision"  # 默认第一档
    quiet = store.send("A", "B", "t2", "x", attention="archive")[0]
    assert store.find_letter(quiet["id"])["attention"] == "archive"


# --------------------------------------------------------- agent 线程可见范围


def test_agent_thread_view_scoped_to_own_letters(store, monkeypatch):
    t1 = store.send("A", "B", "team", "1")[0]["thread_id"]
    t2 = store.send("A", "boss", "private", "2")[0]["thread_id"]
    monkeypatch.setenv("AGENT_MAIL_ID", "B")
    own = server.mailbox_thread(t1)
    assert own["count"] == 1 and own["messages"][0]["to"] == "B"
    foreign = server.mailbox_thread(t2)
    assert foreign["count"] == 0
    assert "permission denied" in foreign["error"]  # structured, not silent


# ------------------------------------------------------- 旧信兼容 (无新字段)


def test_legacy_letter_without_new_fields_flows_as_before(root, monkeypatch):
    st = MailStore(root=root)
    st.register("B")
    inbox = root / "inbox" / "B"
    inbox.mkdir(parents=True, exist_ok=True)
    legacy = {
        "id": "20260101000000-legacy",
        "from": "A",
        "to": "B",
        "subject": "old letter",
        "body": "old body",
        "status": "pending",
        "created_at": "2026-01-01T00:00:00Z",
    }
    (inbox / f"{legacy['id']}.json").write_text(json.dumps(legacy), encoding="utf-8")
    m = st.list_messages("B")[0]
    assert "origin" not in m and "sealed" not in m and "attention" not in m
    assert redact_sealed(m, reader="boss") is m  # redaction is a no-op
    rec = _Recorder()  # the wake path treats it as local and wakes it
    assert run_once(root, _wake_cfg(root), adapter=rec)["woke"] == 1
    monkeypatch.setenv("AGENT_MAIL_ID", "B")
    assert server.mailbox_check("B")["messages"][0]["body"] == "old body"
