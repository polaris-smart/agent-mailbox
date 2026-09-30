"""v0.7.6 PR2 — letter ``links`` field + file:// allowed-roots security model.

Seven acceptance criteria (七判据): ① legacy letters without ``links`` are
untouched; ②/③ check/list/thread read paths compute ok/stale/denied + reason
fresh per call; ④ sealed letters lose ``links`` alongside ``body``; ⑤ an
unknown scheme is a structured reject at send (A7 error family); ⑥ MCP/CLI
share the single store entry point (no CLI mail subcommand exists yet — the
server tool is tested here as the surface); ⑦ the allowed_roots model is
fail-closed (no config ⇒ every file:// link denied; $HOME never implicitly
allowed; realpath normalization defeats ``..`` and symlink escapes; wildcards
rejected loudly). http(s)/git links: v1 is policy-check only — a read path
must never issue a network request (urlopen is monkeypatched to prove it).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

import agent_mailbox.server as server_mod
from agent_mailbox.server import mailbox_check, mailbox_send
from agent_mailbox.store import (
    MailboxError,
    MailStore,
    annotate_link_states,
    load_allowed_roots,
    redact_sealed,
    validate_links,
)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    root = tmp_path / "mail"
    monkeypatch.setenv("AGENT_MAIL_HOME", str(root))
    server_mod._store = None  # the server caches one store — reset per test
    st = MailStore(root=root)
    for _mid in ("HS", "ZC"):
        st.register(_mid)
    return st


# ------------------------------------------------------------- helpers


def _allow(store: MailStore, *roots: str) -> None:
    """Write an ``allowed_roots`` block into the mail root's config.json."""
    path = store.root / "config.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        cfg = {}
    cfg["allowed_roots"] = list(roots)
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


def _mkfile(base: Path, rel: str, content: bytes = b"artifact-bytes") -> Path:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _letter_on_disk(store: MailStore, to: str, msg_id: str) -> dict:
    return json.loads((store.root / "inbox" / to / f"{msg_id}.json").read_text(encoding="utf-8"))


# ------------------------------------------- ① legacy letters: zero impact


def test_legacy_letter_without_links_untouched(store: MailStore):
    sent = store.send("ZC", "HS", "legacy", "old body")[0]
    disk = _letter_on_disk(store, "HS", sent["id"])
    assert "links" not in disk  # the field simply does not exist
    for m in store.check("HS", mark=False):
        assert "links" not in m
    for m in store.list_messages("HS"):
        assert "links" not in m
    for m in store.thread_messages(sent["id"])["messages"]:
        assert "links" not in m


def test_links_persisted_verbatim_in_letter_json(store: MailStore, tmp_path):
    f = _mkfile(tmp_path, "docs/a.txt")
    links = [{"title": "spec", "uri": f.as_uri(), "kind": "spec", "note": "n"}]
    sent = store.send("ZC", "HS", "with links", "b", links=links)[0]
    disk = _letter_on_disk(store, "HS", sent["id"])
    assert disk["links"] == links


def test_empty_links_list_writes_no_field(store: MailStore):
    sent = store.send("ZC", "HS", "empty links", "b", links=[])[0]
    assert "links" not in _letter_on_disk(store, "HS", sent["id"])


def test_states_computed_not_persisted(store: MailStore, tmp_path):
    _allow(store, str(tmp_path))
    f = _mkfile(tmp_path, "docs/a.txt")
    sent = store.send("ZC", "HS", "s", "b", links=[{"title": "a", "uri": f.as_uri()}])[0]
    got = store.check("HS", mark=False)
    assert got[0]["links"][0]["state"] == "ok"
    disk = _letter_on_disk(store, "HS", sent["id"])
    assert all("state" not in link and "reason" not in link for link in disk["links"])


# ------------------------------------------------- ②③ file:// check states


def test_file_link_ok_inside_allowed_roots(store: MailStore, tmp_path):
    docs = tmp_path / "docs"
    f = _mkfile(docs, "report.txt")
    _allow(store, str(docs))
    store.send(
        "ZC",
        "HS",
        "ok case",
        "b",
        links=[{"title": "r", "uri": f.as_uri(), "sha256": _sha(f.read_bytes())}],
    )
    (link,) = store.list_messages("HS")[0]["links"]
    assert link["state"] == "ok" and link["reason"] is None


def test_file_link_stale_when_content_changes(store: MailStore, tmp_path):
    docs = tmp_path / "docs"
    f = _mkfile(docs, "report.txt", b"v1")
    _allow(store, str(docs))
    store.send(
        "ZC",
        "HS",
        "stale case",
        "b",
        links=[{"title": "r", "uri": f.as_uri(), "sha256": _sha(b"v1")}],
    )
    f.write_bytes(b"v2 - edited after send")
    (link,) = store.list_messages("HS")[0]["links"]
    assert link["state"] == "stale" and link["reason"] == "sha256_mismatch"


def test_file_link_denied_when_file_missing(store: MailStore, tmp_path):
    docs = tmp_path / "docs"
    f = _mkfile(docs, "report.txt")
    _allow(store, str(docs))
    store.send("ZC", "HS", "missing case", "b", links=[{"title": "r", "uri": f.as_uri()}])
    f.unlink()
    (link,) = store.list_messages("HS")[0]["links"]
    assert link["state"] == "denied" and link["reason"] == "missing"


def test_sha256_absent_checks_existence_only(store: MailStore, tmp_path):
    docs = tmp_path / "docs"
    f = _mkfile(docs, "report.txt", b"content-that-will-change")
    _allow(store, str(docs))
    store.send("ZC", "HS", "no hash", "b", links=[{"title": "r", "uri": f.as_uri()}])
    (link,) = store.list_messages("HS")[0]["links"]
    assert link["state"] == "ok"  # content changed but no sha256 carried: no comparison
    f.write_bytes(b"totally different")
    (link,) = store.list_messages("HS")[0]["links"]
    assert link["state"] == "ok"
    f.unlink()
    (link,) = store.list_messages("HS")[0]["links"]
    assert link["state"] == "denied" and link["reason"] == "missing"


# ------------------------------------------- allowed_roots fail-closed model


def test_fail_closed_when_no_config(store: MailStore, tmp_path):
    f = _mkfile(tmp_path, "anywhere/a.txt")
    store.send("ZC", "HS", "no cfg", "b", links=[{"title": "a", "uri": f.as_uri()}])
    (link,) = store.list_messages("HS")[0]["links"]
    assert link["state"] == "denied" and link["reason"] == "no_allowed_roots"


def test_fail_closed_when_allowed_roots_empty(store: MailStore, tmp_path):
    _allow(store)  # explicit empty list
    assert load_allowed_roots(store.root) == []
    f = _mkfile(tmp_path, "anywhere/a.txt")
    store.send("ZC", "HS", "empty roots", "b", links=[{"title": "a", "uri": f.as_uri()}])
    (link,) = store.list_messages("HS")[0]["links"]
    assert link["state"] == "denied" and link["reason"] == "no_allowed_roots"


def test_home_not_allowed_by_default(store: MailStore, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # the file *is* under $HOME …
    f = _mkfile(tmp_path, "notes/secret.txt")
    store.send("ZC", "HS", "home default", "b", links=[{"title": "s", "uri": f.as_uri()}])
    (link,) = store.list_messages("HS")[0]["links"]
    assert link["state"] == "denied"  # … and is still denied: $HOME is never implicit


@pytest.mark.xfail(
    sys.platform == "win32",
    reason="windows file:// 根前缀匹配缺口（expanduser/盘符形态的交集，t-74 平台批真修）",
    strict=False,
)
def test_explicit_home_subtree_allowed(store: MailStore, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    f = _mkfile(tmp_path, "docs/ok.txt")
    _allow(store, "~/docs")  # operator opens exactly this door
    store.send("ZC", "HS", "home explicit", "b", links=[{"title": "o", "uri": f.as_uri()}])
    (link,) = store.list_messages("HS")[0]["links"]
    assert link["state"] == "ok"


def test_dotdot_traversal_denied(store: MailStore, tmp_path):
    docs = tmp_path / "docs"
    outside = _mkfile(tmp_path, "outside/real.txt")
    _allow(store, str(docs))
    store.send(
        "ZC",
        "HS",
        "traversal",
        "b",
        links=[{"title": "t", "uri": docs.as_uri() + "/../outside/real.txt"}],
    )
    (link,) = store.list_messages("HS")[0]["links"]
    assert link["state"] == "denied" and link["reason"] == "outside_allowed_roots"
    assert outside.exists()  # the probe never needed the file to exist to be denied


def test_symlink_escape_denied(store: MailStore, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir(parents=True)
    outside = _mkfile(tmp_path, "outside/real.txt")
    escape = docs / "escape.txt"
    escape.symlink_to(outside)
    _allow(store, str(docs))
    store.send("ZC", "HS", "symlink", "b", links=[{"title": "e", "uri": escape.as_uri()}])
    (link,) = store.list_messages("HS")[0]["links"]
    assert link["state"] == "denied" and link["reason"] == "outside_allowed_roots"


def test_wildcard_in_allowed_roots_rejected(store: MailStore, tmp_path):
    _allow(store, str(tmp_path / "docs") + "/*")
    with pytest.raises(MailboxError, match="wildcard"):
        load_allowed_roots(store.root)
    f = _mkfile(tmp_path, "docs/a.txt")
    store.send("ZC", "HS", "wc", "b", links=[{"title": "a", "uri": f.as_uri()}])
    with pytest.raises(MailboxError, match="wildcard"):
        store.list_messages("HS")  # a read path never runs on a half-open gate


def test_denied_reason_distinguishes_unconfigured_vs_outside(store: MailStore, tmp_path):
    f = _mkfile(tmp_path, "x/a.txt")
    store.send("ZC", "HS", "reasons", "b", links=[{"title": "a", "uri": f.as_uri()}])
    (link,) = store.list_messages("HS")[0]["links"]
    assert link["reason"] == "no_allowed_roots"
    _allow(store, str(tmp_path / "elsewhere"))
    (link,) = store.list_messages("HS")[0]["links"]
    assert link["reason"] == "outside_allowed_roots"


# ----------------------------------------------- ⑤ send-time structured reject


def test_unknown_scheme_rejected_at_send(store: MailStore):
    with pytest.raises(MailboxError) as ei:
        store.send("ZC", "HS", "bad scheme", "b", links=[{"title": "x", "uri": "ftp://h/f"}])
    assert ei.value.code == "invalid_field"
    assert "ftp" in str(ei.value)
    # zero trace: nothing landed, nothing was logged
    assert not list((store.root / "inbox" / "HS").glob("*.json"))
    assert not (store.root / "sent.log").exists()


@pytest.mark.parametrize(
    "links,fragment",
    [
        ("not-a-list", "must be a list"),
        ([{"title": "t"}], "uri"),  # missing uri → the uri check names its field
        ([{"uri": "file:///x"}], "title"),
        ([{"title": "", "uri": "file:///x"}], "title"),
        ([{"title": "t", "uri": ""}], "uri"),
        ([{"title": "t", "uri": "file:///x", "surprise": 1}], "unknown key"),
        ([{"title": "t", "uri": "file:///x", "sha256": "ABC123"}], "sha256"),
        ([{"title": "t", "uri": "ssh://h/r"}], "scheme"),
        (["a string entry"], "must be an object"),
    ],
)
def test_links_structural_rejects(store: MailStore, links, fragment):
    with pytest.raises(MailboxError) as ei:
        store.send("ZC", "HS", "structural", "b", links=links)
    assert ei.value.code == "invalid_field"
    assert fragment in str(ei.value)


def test_validate_links_normalizes_copies():
    src = [{"title": "t", "uri": "git://host/repo", "kind": "repo", "note": "n"}]
    out = validate_links(src)
    assert out == src and out[0] is not src[0]
    assert validate_links(None) == []


# ------------------------------------------------- v1 boundary: no network GET


def test_http_https_git_policy_only_never_hits_network(store: MailStore, monkeypatch):
    calls: list = []

    def _no_network(*a, **k):  # any call means the read path went online: fail
        calls.append(a)
        raise AssertionError("read path must never issue a network request")

    monkeypatch.setattr("urllib.request.urlopen", _no_network)
    store.send(
        "ZC",
        "HS",
        "web links",
        "b",
        links=[
            {"title": "h", "uri": "http://example.com/a"},
            {"title": "hs", "uri": "https://example.com/b"},
            {"title": "g", "uri": "git://example.com/repo.git"},
        ],
    )
    states = [(l["state"], l["reason"]) for l in store.list_messages("HS")[0]["links"]]
    assert states == [("ok", None), ("ok", None), ("ok", None)]
    assert calls == []


# ------------------------------------------------------- ④ sealed 同权剥 links


def test_sealed_strips_links_for_non_recipient(store: MailStore, tmp_path):
    f = _mkfile(tmp_path, "docs/s.txt")
    sent = store.send(
        "ZC",
        "HS",
        "sealed",
        "secret",
        sealed=True,
        links=[{"title": "s", "uri": f.as_uri()}],
    )[0]
    red = redact_sealed(_letter_on_disk(store, "HS", sent["id"]), reader="boss")
    assert red["redacted"] == "sealed"
    assert "body" not in red and "links" not in red


def test_sealed_recipient_keeps_links(store: MailStore, tmp_path):
    f = _mkfile(tmp_path, "docs/s.txt")
    sent = store.send(
        "ZC",
        "HS",
        "sealed self",
        "secret",
        sealed=True,
        links=[{"title": "s", "uri": f.as_uri()}],
    )[0]
    full = redact_sealed(_letter_on_disk(store, "HS", sent["id"]), reader="HS")
    assert "links" in full and "body" in full


def test_sealed_links_stripped_through_server_check(store: MailStore, tmp_path, monkeypatch):
    f = _mkfile(tmp_path, "docs/s.txt")
    store.send(
        "HS",
        "ZC",
        "sealed via srv",
        "secret",
        sealed=True,
        links=[{"title": "s", "uri": f.as_uri()}],
    )
    monkeypatch.setenv("AGENT_MAIL_ID", "boss")  # owner reads ZC's box: non-recipient
    out = mailbox_check(agent_id="ZC", mark=False)
    (msg,) = out["messages"]
    assert msg["redacted"] == "sealed"
    assert "body" not in msg and "links" not in msg


# ------------------------------------------------ ⑬ three read paths carry it


def test_check_list_thread_all_carry_states(store: MailStore, tmp_path):
    docs = tmp_path / "docs"
    f = _mkfile(docs, "r.txt")
    _allow(store, str(docs))
    sent = store.send(
        "ZC",
        "HS",
        "three paths",
        "b",
        links=[{"title": "r", "uri": f.as_uri(), "sha256": _sha(b"artifact-bytes")}],
    )[0]
    for msgs in (
        store.check("HS", mark=False),
        store.list_messages("HS"),
        store.thread_messages(sent["id"])["messages"],
    ):
        (link,) = msgs[0]["links"]
        assert link["state"] == "ok"


def test_list_archived_carries_states(store: MailStore, tmp_path):
    _allow(store, str(tmp_path))
    f = _mkfile(tmp_path, "docs/r.txt")
    sent = store.send("ZC", "HS", "archived", "b", links=[{"title": "r", "uri": f.as_uri()}])[0]
    store.check("HS")  # ack
    store.set_status("HS", sent["id"], "done")
    store.archive_done("HS")
    (link,) = store.list_archived("HS")[0]["links"]
    assert link["state"] == "ok"


def test_corrupt_links_on_disk_read_as_denied(store: MailStore):
    sent = store.send("ZC", "HS", "hostile disk", "b")[0]
    p = store.root / "inbox" / "HS" / f"{sent['id']}.json"
    hostile = json.loads(p.read_text(encoding="utf-8"))
    hostile["links"] = [{"title": "no uri"}, "a bare string"]
    p.write_text(json.dumps(hostile, ensure_ascii=False), encoding="utf-8")
    (msg,) = MailStore(root=store.root).list_messages("HS")
    assert [l["reason"] for l in msg["links"]] == ["malformed_link", "malformed_link"]


def test_annotate_link_states_legacy_passthrough():
    m = {"id": "x", "body": "b"}
    assert annotate_link_states(m, []) is m  # same object: zero-cost legacy path


# ---------------------------------------------------- ⑥ MCP surface (同源 store)


def test_server_mailbox_send_passes_links(store: MailStore, tmp_path, monkeypatch):
    f = _mkfile(tmp_path, "docs/via-server.txt")
    monkeypatch.setenv("AGENT_MAIL_ID", "ZC")
    out = mailbox_send(
        to="HS", subject="via server", body="b", links=[{"title": "v", "uri": f.as_uri()}]
    )
    assert out["count"] == 1
    msg_id = out["delivered"][0]["id"]
    disk = _letter_on_disk(store, "HS", msg_id)
    assert disk["links"] == [{"title": "v", "uri": f.as_uri()}]
    _allow(store, str(tmp_path / "docs"))
    got = store.list_messages("HS")
    assert got[0]["links"][0]["state"] == "ok"


def test_server_mailbox_send_rejects_unknown_scheme_structured(store: MailStore, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    monkeypatch.setenv("AGENT_MAIL_ID", "ZC")
    with pytest.raises(ToolError, match="MBE\\|invalid_field"):
        mailbox_send(to="HS", subject="bad", body="b", links=[{"title": "x", "uri": "gopher://h"}])
