"""Join a project => the mailbox key is issued automatically (boss decision 2026-10-08).

Notes for whoever edits this: docstrings stay ASCII-only, Chinese quoting uses 「」.
"""

from __future__ import annotations

import pathlib

from agent_mailbox.workbench_mail_sessions import ensure_member_session, list_sessions
from agent_mailbox.workbench_store import WorkbenchStore


def _scene(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    repo = tmp_path / "repo"
    repo.mkdir()
    project = store.create_project("Demo", repo)
    employee = store.create_employee("Alice", "codex", None)
    return store, project, employee


def test_join_issues_a_key_and_never_returns_the_token(tmp_path):
    store, project, employee = _scene(tmp_path)
    result = ensure_member_session(store, project["id"], employee["id"])
    assert result["issued"] is True, result
    assert "token" not in result, "凭据绝不回浏览器 ✗"
    live = [
        s for s in list_sessions(store, employee["id"], project["id"]) if not s.get("revoked_at")
    ]
    assert len(live) == 1, live
    session_file = pathlib.Path(live[0].get("session_file") or "")
    if session_file.name:
        assert (session_file.stat().st_mode & 0o777) == 0o600, "钥匙文件必须 0600 ✓"


def test_joining_twice_is_idempotent(tmp_path):
    store, project, employee = _scene(tmp_path)
    ensure_member_session(store, project["id"], employee["id"])
    again = ensure_member_session(store, project["id"], employee["id"])
    assert again["issued"] is False and again["reason"] == "already_has_key", again
    live = [
        s for s in list_sessions(store, employee["id"], project["id"]) if not s.get("revoked_at")
    ]
    assert len(live) == 1, "不许重复签 ✗"


def test_expired_key_is_reissued(tmp_path):
    """AM-04 回归 ✓：**过期**钥匙不算「已有钥匙」✗ ⇒ 必须重新签发 ✓。

    Codex 实测：复用过滤只看 `revoked_at` ⇒ 过期会话仍判 `already_has_key`
    ⇒ `issued=false` 而**有效数为 0** ✗ ⇒ 用户表面入组成功、实际访问不了 ✓。
    """
    store, project, employee = _scene(tmp_path)
    ensure_member_session(store, project["id"], employee["id"])
    with store._transaction() as db:
        db.execute(
            "UPDATE mailbox_sessions SET expires_at='2020-01-01T00:00:00+00:00' WHERE employee_id=?",
            (employee["id"],),
        )
    assert not [x for x in list_sessions(store, employee["id"], project["id"]) if x.get("active")]
    again = ensure_member_session(store, project["id"], employee["id"])
    assert again["issued"] is True, "过期必须重签 ✓（AM-04）"
    assert [x for x in list_sessions(store, employee["id"], project["id"]) if x.get("active")], (
        "重签后必须有有效钥匙 ✓"
    )
