"""Enrolment: discover → plan → enrol → one paste (roadmap T2)."""

from __future__ import annotations

import json

import pytest

from agent_mailbox import workbench_enroll as we
from agent_mailbox import workbench_runtime
from agent_mailbox.workbench_store import WorkbenchStore

FAKE = [
    {
        "kind": "claude",
        "name": "Claude Code",
        "connection_type": "cli",
        "entrypoint": "/usr/local/bin/claude",
        "auth_status": "authenticated",
        "detail": "",
    },
    {
        "kind": "hermes",
        "name": "Hermes",
        "connection_type": "cli",
        "entrypoint": None,
        "detail": "not installed",
    },
    {
        "kind": "codex",
        "name": "Codex",
        "connection_type": "cli",
        "entrypoint": "/usr/local/bin/codex",
        "auth_status": "unknown",
        "detail": "",
    },
]


@pytest.fixture
def scene(tmp_path, monkeypatch):
    monkeypatch.setattr(workbench_runtime, "discover_employees", lambda: [dict(r) for r in FAKE])
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    return store, project


def _snapshot(store):
    with store._transaction() as db:
        return {
            table: db.execute(f"SELECT count(*) AS c FROM {table}").fetchone()["c"]
            for table in ("employees", "memberships", "mailbox_sessions", "governance_events")
        }


def test_discover_normalises_and_sorts(scene):
    _store, _project = scene
    rows = we.discover()
    assert [row["kind"] for row in rows] == ["claude", "codex", "hermes"]  # 已安装+可执行优先
    assert rows[0]["execution_supported"] is True
    assert rows[-1]["installed"] is False
    assert len(we.discover(installed_only=True)) == 2


def test_plan_is_read_only_and_skips_missing_or_unsupported(scene):
    store, project = scene
    before = _snapshot(store)
    outline = we.plan(store, project["id"])
    assert before == _snapshot(store), "计划阶段不允许写入"
    assert [row["kind"] for row in outline["suggestions"]] == ["claude", "codex"]  # hermes 未安装
    assert outline["summary"]["installed"] == 2 and outline["summary"]["already_enrolled"] == 0


def test_onboard_dry_run_writes_nothing_then_enrols(scene):
    store, project = scene
    before = _snapshot(store)
    dry = we.onboard(store, project["id"])
    assert dry["dry_run"] is True
    assert [step["action"] for step in dry["steps"]] == ["would_enroll", "would_enroll"]
    assert before == _snapshot(store), "dry run 不允许写入"

    applied = we.onboard(
        store, project["id"], apply=True, host="generic", binary="/x/agent-mailbox"
    )
    assert [step["action"] for step in applied["steps"]] == ["enrolled", "enrolled"]
    after = _snapshot(store)
    assert after["employees"] == before["employees"] + 2
    assert after["memberships"] == before["memberships"] + 2
    assert after["mailbox_sessions"] >= 2


def test_enrol_is_idempotent_and_issues_a_private_session(scene, tmp_path):
    store, project = scene
    first = we.enroll(store, project["id"], "claude", host="generic", binary="/x/agent-mailbox")
    assert first["created"] is True
    from pathlib import Path

    path = Path(first["session_file"])
    assert path.exists() and (path.stat().st_mode & 0o777) == 0o600
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["employee_id"] == first["employee"]["id"]
    assert payload["project_id"] == project["id"] and payload["token"]
    assert "token" not in json.dumps(first["employee"])  # 令牌不进员工视图

    second = we.enroll(store, project["id"], "claude", host="generic", binary="/x/agent-mailbox")
    assert second["created"] is False
    assert second["employee"]["id"] == first["employee"]["id"]  # 复用，不重复建人
    assert second["session_id"] != first["session_id"]  # 但会签发新会话


def test_plan_marks_already_enrolled(scene):
    store, project = scene
    we.enroll(store, project["id"], "claude", host="generic")
    outline = we.plan(store, project["id"])
    claude = next(row for row in outline["suggestions"] if row["kind"] == "claude")
    assert claude["already_enrolled"] is True and claude["existing"] == ["Claude Code"]
    assert outline["summary"]["already_enrolled"] == 1


@pytest.mark.parametrize("host", list(we.HOSTS))
def test_snippets_are_ready_to_paste(host):
    snippet = we.mcp_snippet(host, binary="/x/agent-mailbox", session_file="/tmp/s.json")
    assert "mailbox-mcp" in snippet and "--session-file" in snippet and "/tmp/s.json" in snippet
    assert "/x/agent-mailbox" in snippet
    if host in ("generic", "workbuddy"):
        block = json.loads(snippet)["mcpServers"]["agent-mailbox"]
        assert block["command"] == "/x/agent-mailbox" and block["args"][0] == "mailbox-mcp"
    elif host == "hermes":
        assert snippet.startswith("mcp_servers:") and "agent-mailbox:" in snippet
    elif host == "codex":
        assert (
            snippet.startswith("[mcp_servers.agent-mailbox]")
            and 'command = "/x/agent-mailbox"' in snippet
        )
    else:
        assert snippet.startswith("claude mcp add agent-mailbox --")


def test_unknown_inputs_are_rejected(scene):
    store, project = scene
    with pytest.raises(ValueError):
        we.enroll(store, project["id"], "nope")
    with pytest.raises(ValueError):
        we.mcp_snippet("nope", binary="b", session_file="s")


def test_re_enrol_revokes_the_superseded_session(scene):
    """轮换即失效：每次入列签发新会话（既有契约），但旧会话必须被撤销。

    否则同一身份会攒下多条**仍然有效**的凭据（2026-10-06 实测 DSH 3 条 / ZCode 2 条）。
    """
    store, project = scene
    first = we.enroll(store, project["id"], "claude", host="generic", binary="/x/agent-mailbox")
    second = we.enroll(store, project["id"], "claude", host="generic", binary="/x/agent-mailbox")

    assert second["created"] is False  # 员工复用（既有契约不变）
    assert second["session_id"] != first["session_id"]  # 每次签发新会话（既有契约不变）
    with store._transaction(readonly=True) as db:
        rows = {
            row["id"]: row["revoked_at"]
            for row in db.execute(
                "SELECT id, revoked_at FROM mailbox_sessions WHERE employee_id=? AND project_id=?",
                (first["employee"]["id"], project["id"]),
            )
        }
    assert rows[first["session_id"]] is not None, "旧会话必须被撤销"
    assert rows[second["session_id"]] is None, "新会话必须有效"
