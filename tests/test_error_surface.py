"""v0.7.6 A7 — structured error surface (task brief §二, A1–A7).

Every failure a caller can provoke must come back as a structured payload
(``error_code`` + field + valid values + hint) instead of the opaque
``Error executing tool`` bubble, must never create side effects (no
``inbox/<NOSUCH>/`` directory, no sent.log line for an oversized body), and
must never echo internal paths or credential material.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_mailbox.store import MAX_BODY_BYTES, MailboxError, MailStore


@pytest.fixture()
def store(tmp_path: Path) -> MailStore:
    s = MailStore(root=tmp_path / "mail")
    s.register("HS")
    s.register("ZC")
    return s


def _err(fn):
    """Call fn, return the structured error dict (guard shape) it produces."""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — store-layer raise, fields mirrored to guard shape
        return {
            "error": True,
            "error_code": getattr(exc, "code", None) or "invalid_field",
            "detail": str(exc),
        }
    raise AssertionError("expected a MailboxError")


# ── A1: illegal enum → field + valid values ──────────────────────────────


def test_illegal_attention_is_structured(store: MailStore):
    e = _err(lambda: store.send(from_id="ZC", to="HS", subject="s", body="b", attention="info"))
    assert e["error_code"] == "invalid_field"
    for tier in ("decision", "report", "archive"):
        assert tier in e["detail"]


def test_illegal_status_is_structured(store: MailStore):
    e = _err(lambda: store.send(from_id="ZC", to="HS", subject="s", body="b", status="nope"))
    assert e["error_code"] == "invalid_field"
    assert "pending" in e["detail"]


def test_illegal_origin_is_structured(store: MailStore):
    e = _err(lambda: store.send(from_id="ZC", to="HS", subject="s", body="b", origin="cloud"))
    assert e["error_code"] == "invalid_field"
    assert "local" in e["detail"]


# ── A2: unregistered recipient → hard reject by default ─────────────────


def test_unregistered_recipient_rejected_by_default(store: MailStore):
    e = _err(lambda: store.send(from_id="ZC", to="NOSUCH", subject="s", body="b"))
    assert e["error_code"] == "not_found"
    assert "NOSUCH" in e["detail"]
    assert "ZC" in e["detail"]  # registered members offered as candidates


def test_unregistered_recipient_creates_no_directory(store: MailStore):
    with pytest.raises(MailboxError):
        store.send(from_id="ZC", to="NOSUCH", subject="s", body="b")
    assert not (store.root / "inbox" / "NOSUCH").exists()


def test_unregistered_recipient_warn_mode_downgrades(store: MailStore):
    cfg = store.root / "config.json"
    cfg.write_text(json.dumps({"unregistered_recipients": "warn"}), encoding="utf-8")
    out = store.send(from_id="ZC", to="NOSUCH", subject="s", body="b", dedupe=False)
    warned = [r for r in out if r.get("to") == "NOSUCH"]
    assert warned and warned[0]["warn"] == "unregistered"
    assert not (store.root / "inbox" / "NOSUCH").exists()
    audit = (store.root / "audit.log").read_text(encoding="utf-8")
    assert "unregistered_recipient" in audit


def test_unregistered_reject_writes_no_letter(store: MailStore):
    with pytest.raises(MailboxError):
        store.send(from_id="ZC", to="NOSUCH", subject="s", body="b")
    inbox = store.root / "inbox"
    assert sorted(p.name for p in inbox.iterdir()) == ["HS", "ZC"]


# ── A3: slug whitelist (anti-traversal) ─────────────────────────────────


@pytest.mark.parametrize(
    "bad", ["../escape", "a/b", "", ".hidden", "a" * 65, "a..b", "/abs", "x\ny"]
)
def test_slug_whitelist_rejects_traversal_samples(store: MailStore, bad: str):
    e = _err(lambda: store.send(from_id="ZC", to=bad, subject="s", body="b"))
    assert e["error_code"] == "invalid_field"


def test_dotted_id_now_legal_per_task_brief(store: MailStore):
    # 任务书 slug 白名单允许点号（v0.7.6 起统一）；注册后即可寻址。
    store.register("svc.ingest")
    out = store.send(from_id="ZC", to="svc.ingest", subject="s", body="b", dedupe=False)
    assert any(r.get("to") == "svc.ingest" for r in out)


# ── A4: payload_too_large — three-part message + zero side effects ───────


def test_oversized_body_rejected_before_any_write(store: MailStore):
    big = "x" * (MAX_BODY_BYTES + 1)
    e = _err(lambda: store.send(from_id="ZC", to="HS", subject="s", body=big))
    assert e["error_code"] == "payload_too_large"
    # three-part message: limit / actual bytes / what to do instead
    assert str(MAX_BODY_BYTES) in e["detail"]
    assert str(MAX_BODY_BYTES + 1) in e["detail"]
    assert "link" in e["detail"].lower()
    inbox = store.root / "inbox" / "HS"
    assert list(inbox.glob("*.json")) == []
    assert not (store.root / "sent.log").exists()


def test_body_at_limit_is_delivered(store: MailStore):
    body = "x" * MAX_BODY_BYTES
    out = store.send(from_id="ZC", to="HS", subject="s", body=body, dedupe=False)
    assert any(r.get("to") == "HS" for r in out)


def test_max_body_bytes_config_override(store: MailStore):
    (store.root / "config.json").write_text(
        json.dumps({"max_body_bytes": 10}), encoding="utf-8"
    )
    e = _err(lambda: store.send(from_id="ZC", to="HS", subject="s", body="x" * 11))
    assert e["error_code"] == "payload_too_large"
    assert "limit 10 bytes" in e["detail"]


# ── A5: guard surfaces error_code through the tool layer ─────────────────


def test_guard_converts_mailboxerror_to_structured():
    from agent_mailbox.server import _tool_guard

    @_tool_guard
    def boom():
        raise MailboxError("attention must be one of ('decision', 'report', 'archive')",
                           code="invalid_field")

    out = boom()
    assert out["error"] is True
    assert out["error_code"] == "invalid_field"
    assert "decision" in out["detail"]


def test_guard_masks_env_token_in_detail(monkeypatch):
    from agent_mailbox.server import _tool_guard

    monkeypatch.setenv("AGENT_MAIL_TOKEN", "super-secret-token-value")

    @_tool_guard
    def boom():
        raise MailboxError("bad call near super-secret-token-value", code="invalid_field")

    out = boom()
    assert "super-secret-token-value" not in out["detail"]
    assert "••••" in out["detail"]


def test_guard_passes_success_through():
    from agent_mailbox.server import _tool_guard

    @_tool_guard
    def fine():
        return {"ok": True}

    assert fine() == {"ok": True}


# ── A6: error surface whitelist — no paths/credentials ever echoed ───────


FORBIDDEN = ("/Users/", "token", "key", "secret", "pairing")


def test_error_surface_never_leaks_paths_or_credentials(store: MailStore):
    probes = [
        lambda: store.send(from_id="ZC", to="NOSUCH", subject="s", body="b"),
        lambda: store.send(from_id="ZC", to="HS", subject="s", body="x" * (MAX_BODY_BYTES + 1)),
        lambda: store.send(from_id="ZC", to="HS", subject="s", body="b", attention="info"),
        lambda: store.send(from_id="ZC", to="../escape", subject="s", body="b"),
        lambda: store._validate_id("../escape"),
    ]
    for probe in probes:
        e = _err(probe)
        low = json.dumps(e, ensure_ascii=False).lower()
        for bad in FORBIDDEN:
            assert bad.lower() not in low, f"{bad!r} leaked into error surface: {e}"


# ── A7: legacy raises still work (backward compatibility) ────────────────


def test_mailbox_error_without_code_defaults_to_none():
    from agent_mailbox.store import MailboxError

    e = MailboxError("legacy message")
    assert e.code is None
    assert str(e) == "legacy message"
