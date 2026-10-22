"""Enrolment: discover → plan → enrol → one paste (roadmap T2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_mailbox import workbench_enroll as we
from agent_mailbox import workbench_runtime
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore

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
    from agent_mailbox.workbench_private import private_access

    assert path.exists() and private_access(path, 0o600)
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


def test_plan_and_onboard_keep_discovered_connections_distinct(scene, monkeypatch):
    store, project = scene
    candidates = [
        {
            "kind": "codex",
            "name": "Codex CLI",
            "connection_type": "cli",
            "entrypoint": "/usr/local/bin/codex",
            "auth_status": "unknown",
        },
        {
            "kind": "codex",
            "name": "Codex Second CLI",
            "connection_type": "cli",
            "entrypoint": "/opt/codex/bin/codex",
            "auth_status": "unknown",
        },
        {
            "kind": "codex",
            "name": "Codex App",
            "connection_type": "app",
            "entrypoint": "/Applications/Codex.app",
            "auth_status": "unknown",
        },
    ]
    monkeypatch.setattr(workbench_runtime, "discover_employees", lambda: candidates)

    first = we.enroll(store, project["id"], "codex", name="Codex CLI", connection_type="cli")
    outline = we.plan(store, project["id"])
    by_name = {row["name"]: row for row in outline["suggestions"]}
    assert by_name["Codex CLI"]["already_enrolled"] is True
    assert by_name["Codex Second CLI"]["already_enrolled"] is False
    assert "Codex App" not in by_name  # App is mailbox-only, not managed auto-onboarding.
    assert outline["summary"]["already_enrolled"] == 1

    applied = we.onboard(store, project["id"], apply=True)
    assert len(applied["steps"]) == 1
    assert applied["steps"][0]["employee"]["name"] == "Codex Second CLI"
    assert applied["steps"][0]["employee"]["id"] != first["employee"]["id"]
    assert we.onboard(store, project["id"], apply=True)["steps"] == []

    app = we.enroll(store, project["id"], "codex", name="Codex App", connection_type="app")
    assert app["employee"]["id"] not in {
        first["employee"]["id"],
        applied["steps"][0]["employee"]["id"],
    }
    with store._transaction(readonly=True) as db:
        connections = {
            row["connection_type"]
            for row in db.execute(
                "SELECT e.connection_type FROM employees e "
                "JOIN memberships m ON m.employee_id=e.id WHERE m.project_id=?",
                (project["id"],),
            )
        }
    assert connections == {"app", "cli"}


def test_same_name_cli_profiles_cannot_share_one_mailbox(scene, monkeypatch):
    store, project = scene
    candidates = [
        {"kind": "codex", "name": "Codex", "connection_type": "cli", "entrypoint": path}
        for path in ("/opt/first/codex", "/opt/second/codex")
    ]
    monkeypatch.setattr(workbench_runtime, "discover_employees", lambda: candidates)

    with pytest.raises(WorkbenchError, match="多个连接入口"):
        we.enroll(store, project["id"], "codex", name="Codex")
    with pytest.raises(WorkbenchError, match="不同名称"):
        we.onboard(store, project["id"], apply=True)
    assert _snapshot(store)["employees"] == 0

    first = we.enroll(store, project["id"], "codex", name="Codex", entrypoint="/opt/first/codex")
    outline = we.plan(store, project["id"])
    assert [row["already_enrolled"] for row in outline["suggestions"]] == [True, False]
    assert all("--entrypoint " in row["command"] for row in outline["suggestions"])
    with pytest.raises(WorkbenchError, match="另一连接入口"):
        we.enroll(store, project["id"], "codex", name="Codex", entrypoint="/opt/second/codex")
    assert _snapshot(store)["mailbox_sessions"] == 1
    with store._transaction(readonly=True) as db:
        actual = db.execute(
            "SELECT entrypoint FROM employees WHERE id=?", (first["employee"]["id"],)
        ).fetchone()
    assert actual["entrypoint"] == "/opt/first/codex"


def test_legacy_blank_entrypoint_does_not_hide_same_name_profiles(scene, monkeypatch):
    store, project = scene
    store.create_employee("Codex", "codex", project["id"], connection_type="cli")
    monkeypatch.setattr(
        workbench_runtime,
        "discover_employees",
        lambda: [
            {"kind": "codex", "name": "Codex", "connection_type": "cli", "entrypoint": path}
            for path in ("/opt/first/codex", "/opt/second/codex")
        ],
    )
    outline = we.plan(store, project["id"])
    assert [row["already_enrolled"] for row in outline["suggestions"]] == [False, False]
    with pytest.raises(WorkbenchError, match="不同名称"):
        we.onboard(store, project["id"], apply=True)
    assert _snapshot(store)["mailbox_sessions"] == 0


def test_unique_discovered_entrypoint_fills_legacy_employee(scene):
    store, project = scene
    legacy = store.create_employee("Codex", "codex", project["id"], connection_type="cli")
    result = we.enroll(store, project["id"], "codex", name="Codex")
    assert result["employee"]["id"] == legacy["id"]
    with store._transaction(readonly=True) as db:
        actual = db.execute(
            "SELECT entrypoint FROM employees WHERE id=?", (legacy["id"],)
        ).fetchone()
    assert actual["entrypoint"] == "/usr/local/bin/codex"


def test_cli_enroll_passes_explicit_entrypoint(scene, capsys):
    from agent_mailbox.cli import main

    store, project = scene
    assert (
        main(
            [
                "enroll",
                "codex",
                "--home",
                str(store.root),
                "--project",
                project["id"],
                "--name",
                "Codex",
                "--entrypoint",
                "/opt/custom/codex",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    with store._transaction(readonly=True) as db:
        actual = db.execute(
            "SELECT entrypoint FROM employees WHERE id=?", (payload["employee"]["id"],)
        ).fetchone()
    assert actual["entrypoint"] == "/opt/custom/codex"


def test_enrol_same_identity_in_another_project_reuses_employee(scene, tmp_path):
    store, first_project = scene
    second_dir = tmp_path / "second-project"
    second_dir.mkdir()
    second_project = store.create_project("Second", second_dir)

    first = we.enroll(store, first_project["id"], "codex", name="Codex CLI")
    second = we.enroll(store, second_project["id"], "codex", name="Codex CLI")

    assert second["employee"]["id"] == first["employee"]["id"]
    assert second["created"] is False
    assert _snapshot(store)["employees"] == 1


def test_default_app_and_cli_enrolment_get_separate_mailbox_identities(scene):
    store, project = scene
    cli = we.enroll(store, project["id"], "codex", connection_type="cli")
    app = we.enroll(store, project["id"], "codex", connection_type="app")

    assert cli["employee"]["id"] != app["employee"]["id"]
    assert cli["employee"]["name"] == "Codex"
    assert app["employee"]["name"] == "Codex App"
    assert _snapshot(store)["employees"] == 2


def test_default_app_enrolment_reuses_legacy_app_name(scene):
    store, project = scene
    legacy = store.create_employee("Codex", "codex", project["id"], connection_type="app")

    enrolled = we.enroll(store, project["id"], "codex", connection_type="app")

    assert enrolled["employee"]["id"] == legacy["id"]
    assert enrolled["created"] is False
    assert _snapshot(store)["employees"] == 1


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


def test_re_enrol_preserves_other_conversation_for_same_employee(scene):
    """Re-enrolling one client must not disconnect another live conversation."""
    from agent_mailbox.workbench_mail_sessions import create_session, list_sessions

    store, project = scene
    first = we.enroll(store, project["id"], "claude", host="generic", binary="/x/agent-mailbox")
    other = create_session(store, first["employee"]["id"], project["id"], "Another conversation")
    second = we.enroll(store, project["id"], "claude", host="generic", binary="/x/agent-mailbox")

    sessions = {
        row["id"]: row for row in list_sessions(store, first["employee"]["id"], project["id"])
    }
    assert not sessions[first["session_id"]]["active"]
    assert sessions[second["session_id"]]["active"]
    assert sessions[other["id"]]["active"], "另一会话仍应可用"


def test_re_enrol_retires_legacy_duplicate_keys_for_only_its_connection(scene):
    """Historical duplicate keys for one connection must not survive rotation."""
    from agent_mailbox.workbench_mail_sessions import create_session, list_sessions

    store, project = scene
    first = we.enroll(store, project["id"], "claude", host="generic", binary="/x/agent-mailbox")
    legacy = create_session(store, first["employee"]["id"], project["id"], "Legacy duplicate")
    other = create_session(store, first["employee"]["id"], project["id"], "Another conversation")
    with store._transaction() as db:
        db.execute(
            "UPDATE mailbox_sessions SET label=? WHERE id=?",
            ("接入 · Claude Code", legacy["id"]),
        )

    replacement = we.enroll(
        store, project["id"], "claude", host="generic", binary="/x/agent-mailbox"
    )
    sessions = {
        row["id"]: row for row in list_sessions(store, first["employee"]["id"], project["id"])
    }
    assert not sessions[first["session_id"]]["active"]
    assert not sessions[legacy["id"]]["active"]
    assert sessions[replacement["session_id"]]["active"]
    assert sessions[other["id"]]["active"]


def test_re_enrol_never_leaves_two_live_keys_after_issuance_interruption(scene, monkeypatch):
    """A crash just after issuing a replacement must not leave two live keys."""
    from agent_mailbox import workbench_mail_sessions as sessions_module
    from agent_mailbox.workbench_mail_sessions import list_sessions

    store, project = scene
    first = we.enroll(store, project["id"], "claude", host="generic", binary="/x/agent-mailbox")
    original_create = sessions_module.create_session

    def interrupt_after_issue(*args, **kwargs):
        original_create(*args, **kwargs)
        raise RuntimeError("synthetic interruption after issuing replacement")

    monkeypatch.setattr(sessions_module, "create_session", interrupt_after_issue)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        we.enroll(store, project["id"], "claude", host="generic", binary="/x/agent-mailbox")

    active_same_connection = [
        session
        for session in list_sessions(store, first["employee"]["id"], project["id"])
        if session["active"] and session["label"] == "接入 · Claude Code"
    ]
    assert len(active_same_connection) == 1


def test_enrol_does_not_merge_gui_into_existing_cli_identity(scene):
    """A second connection shape needs its own named employee identity."""
    from agent_mailbox.workbench_mail_sessions import list_sessions

    store, project = scene
    cli = we.enroll(
        store,
        project["id"],
        "claude",
        connection_type="cli",
        host="generic",
        binary="/x/agent-mailbox",
    )

    with pytest.raises(WorkbenchError) as error:
        we.enroll(
            store,
            project["id"],
            "claude",
            name="Claude Code",
            connection_type="app",
            host="generic",
            binary="/x/agent-mailbox",
        )

    assert error.value.code == "IDENTITY_CONNECTION_CONFLICT"
    assert [
        session["id"]
        for session in list_sessions(store, cli["employee"]["id"], project["id"])
        if session["active"]
    ] == [cli["session_id"]]


def test_re_enrol_manifest_write_failure_keeps_original_connection(scene, monkeypatch):
    """A failed private file write must not revoke the only usable key."""
    from agent_mailbox.workbench_mail_sessions import list_sessions

    store, project = scene
    first = we.enroll(store, project["id"], "claude", host="generic", binary="/x/agent-mailbox")

    def fail_write(_path, *_args, **_kwargs):
        raise OSError("synthetic private manifest write failure")

    monkeypatch.setattr(Path, "write_text", fail_write)
    with pytest.raises(OSError, match="synthetic private manifest write failure"):
        we.enroll(store, project["id"], "claude", host="generic", binary="/x/agent-mailbox")

    active = [
        session["id"]
        for session in list_sessions(store, first["employee"]["id"], project["id"])
        if session["active"] and session["label"] == "接入 · Claude Code"
    ]
    assert active == [first["session_id"]]


def test_re_enrol_manifest_collision_does_not_delete_existing_file(scene, monkeypatch):
    """A reserved session filename must not be removed by failed cleanup."""
    from agent_mailbox import workbench_mail_sessions as sessions_module

    store, project = scene
    we.enroll(store, project["id"], "claude", host="generic", binary="/x/agent-mailbox")
    collision = store.root / we.SESSION_DIR / "mail_session_collision.json"
    collision.write_text("keep this pre-existing file", encoding="utf-8")
    monkeypatch.setattr(sessions_module, "_id", lambda _prefix: "mail_session_collision")

    with pytest.raises(FileExistsError):
        we.enroll(store, project["id"], "claude", host="generic", binary="/x/agent-mailbox")

    assert collision.read_text(encoding="utf-8") == "keep this pre-existing file"
