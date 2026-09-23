"""Identity binding (v0.6.2): config parsing, token verification, and the
fail-open / fail-loud contract at the MCP tool layer.

Contract under test:
- binding disabled (default) or agent unbound → local trust, zero behavior
  change, even with a wrong ``AGENT_MAIL_TOKEN`` present;
- binding enabled + bound agent → the sha256 of ``AGENT_MAIL_TOKEN`` must
  match the table entry (constant-time compare); mismatch raises
  ``MailboxError("identity mismatch")``;
- a malformed ``identity_binding`` block fails loudly — at startup (the CLI
  subprocess test) and on first tool touch.
"""

import hashlib
import json
import subprocess
import sys

import pytest

from agent_mailbox import server
from agent_mailbox.store import MailboxError, load_identity_binding


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _write_config(root, payload: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    """Isolated mail root + reset server-side caches/env between tests."""
    root = tmp_path / "mail"
    monkeypatch.setenv("AGENT_MAIL_HOME", str(root))
    monkeypatch.delenv("AGENT_MAIL_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_MAIL_ID", raising=False)
    monkeypatch.setattr(server, "_store", None)
    monkeypatch.setattr(server, "_binding", None)
    yield root
    monkeypatch.setattr(server, "_store", None)
    monkeypatch.setattr(server, "_binding", None)


# ------------------------------------------------- load_identity_binding

def test_no_config_file_means_disabled(tmp_path):
    assert load_identity_binding(tmp_path) == {"enabled": False}


def test_block_absent_means_disabled(tmp_path):
    _write_config(tmp_path, {"dedup_ttl": 3600})
    assert load_identity_binding(tmp_path) == {"enabled": False}


def test_enabled_table_parsed(tmp_path):
    _write_config(tmp_path, {"identity_binding": {"enabled": True, "HS": _sha("tok")}})
    table = load_identity_binding(tmp_path)
    assert table["enabled"] is True
    assert table["HS"] == _sha("tok")


def test_disabled_flag_keeps_table_readable(tmp_path):
    _write_config(tmp_path, {"identity_binding": {"enabled": False, "HS": _sha("tok")}})
    table = load_identity_binding(tmp_path)
    assert table["enabled"] is False
    assert table["HS"] == _sha("tok")


@pytest.mark.parametrize(
    "block,frag",
    [
        (["not", "an", "object"], "must be an object"),
        ({"enabled": "yes"}, "must be a boolean"),
        ({"HS": "deadbeef"}, "sha256"),
        ({"HS": _sha("x").upper()}, "sha256"),  # hexdigest is lowercase
        ({"bad id!": _sha("x")}, "valid agent id"),
    ],
)
def test_malformed_block_fails_loudly(tmp_path, block, frag):
    _write_config(tmp_path, {"identity_binding": block})
    with pytest.raises(MailboxError, match=frag):
        load_identity_binding(tmp_path)


def test_corrupt_config_json_fails_loudly(tmp_path):
    (tmp_path / "config.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(MailboxError, match="corrupt config.json"):
        load_identity_binding(tmp_path)


# ------------------------------------------------------- tool-layer check

def test_enabled_missing_token_rejected(fresh):
    _write_config(fresh, {"identity_binding": {"enabled": True, "HS": _sha("s3cret")}})
    with pytest.raises(MailboxError, match="identity mismatch"):
        server.mailbox_register("HS")


def test_enabled_wrong_token_rejected(fresh, monkeypatch):
    _write_config(fresh, {"identity_binding": {"enabled": True, "HS": _sha("s3cret")}})
    monkeypatch.setenv("AGENT_MAIL_TOKEN", "wrong")
    with pytest.raises(MailboxError, match="identity mismatch"):
        server.mailbox_check("HS")


def test_enabled_right_token_passes_end_to_end(fresh, monkeypatch):
    _write_config(fresh, {"identity_binding": {"enabled": True, "HS": _sha("s3cret")}})
    monkeypatch.setenv("AGENT_MAIL_TOKEN", "s3cret")
    card = server.mailbox_register("HS")
    assert card["agent_id"] == "HS"
    server.mailbox_send(to="HS", subject="t", body="b", from_id="HS")
    out = server.mailbox_check("HS")
    assert out["unread"] == 1


def test_disabled_ignores_tokens(fresh, monkeypatch):
    _write_config(fresh, {"identity_binding": {"enabled": False, "HS": _sha("s3cret")}})
    monkeypatch.setenv("AGENT_MAIL_TOKEN", "wrong")
    card = server.mailbox_register("HS")
    assert card["new"] is True


def test_no_config_local_trust_unchanged(fresh, monkeypatch):
    monkeypatch.setenv("AGENT_MAIL_TOKEN", "whatever")
    card = server.mailbox_register("HS")
    assert card["new"] is True


def test_unbound_identity_stays_open(fresh, monkeypatch):
    _write_config(fresh, {"identity_binding": {"enabled": True, "HS": _sha("s3cret")}})
    monkeypatch.setenv("AGENT_MAIL_TOKEN", "s3cret")
    server.mailbox_register("HS")
    monkeypatch.delenv("AGENT_MAIL_TOKEN")  # WB presents no token at all
    card = server.mailbox_register("WB")
    assert card["new"] is True
    assert server.mailbox_check("WB")["unread"] == 0


def test_malformed_config_fails_on_first_tool_touch(fresh):
    _write_config(fresh, {"identity_binding": {"enabled": True, "HS": "zz"}})
    with pytest.raises(MailboxError, match="sha256"):
        server.mailbox_register("HS")


def test_malformed_config_kills_startup(tmp_path):
    """The CLI process refuses to start on a corrupt binding table."""
    root = tmp_path / "mail"
    root.mkdir()
    _write_config(root, {"identity_binding": {"enabled": True, "HS": "zz"}})
    result = subprocess.run(
        [sys.executable, "-m", "agent_mailbox.server", "--home", str(root)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode != 0
    assert "identity_binding" in result.stderr + result.stdout
