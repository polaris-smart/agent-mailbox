"""Tool contract: the real servers must match the declaration (T30)."""

from __future__ import annotations

import asyncio
import json

import pytest

from agent_mailbox import mailbox_mcp, workspace_mcp
from agent_mailbox import workbench_tools as wt
from agent_mailbox.workbench_mail_sessions import create_session
from agent_mailbox.workbench_store import WorkbenchStore


@pytest.fixture
def scene(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    employee = store.create_employee("Alice", "codex", project["id"])
    session = create_session(store, employee["id"], project["id"], "contract-probe")
    session_file = tmp_path / "session.json"
    session_file.write_text(
        json.dumps(
            {
                "home": str(store.root),
                "session_id": session["id"],
                "employee_id": employee["id"],
                "project_id": project["id"],
                "token": session["token"],
            }
        ),
        encoding="utf-8",
    )
    from agent_mailbox.workbench_private import private_mode

    private_mode(session_file, 0o600)
    with store._transaction() as db:  # workspace 面用的是"员工×项目"的 membership token
        membership_token = db.execute(
            "SELECT secret_token FROM memberships WHERE employee_id=? AND project_id=?",
            (employee["id"], project["id"]),
        ).fetchone()["secret_token"]
    return store, project, employee, session_file, membership_token


def _tools(server):
    """MCPServer.list_tools() 是协程：在测试里同步取一次。"""
    return asyncio.run(server.list_tools())


def _signatures(server) -> dict[str, list[str]]:
    """name → ordered parameter names, straight from the live server."""
    out = {}
    for tool in _tools(server):
        schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
        properties = (schema or {}).get("properties") or {}
        out[tool.name] = sorted(properties)
    return out


def test_mailbox_server_matches_the_declaration(scene):
    _store, _project, _employee, session_file, _token = scene
    server = mailbox_mcp.build_server(session_file)
    names = [tool.name for tool in _tools(server)]
    assert wt.validate("mailbox", names) == {"missing": [], "unexpected": []}
    assert len(names) == 17


def test_workspace_server_matches_the_declaration(scene):
    store, project, employee, _session_file, membership_token = scene
    server = workspace_mcp.build_server(store, employee["id"], project["id"], membership_token)
    names = [tool.name for tool in _tools(server)]
    assert wt.validate("workspace", names) == {"missing": [], "unexpected": []}
    assert len(names) == 16  # 15 个 project_* + team_message（2026-10-07 双面加 5 个只读诊断）


def test_shared_tools_have_identical_signatures_across_servers(scene):
    """收敛的实质：共用工具在两套面上**同名同参**，不许各写一份。"""
    store, project, employee, session_file, membership_token = scene
    mailbox = _signatures(mailbox_mcp.build_server(session_file))
    workspace = _signatures(
        workspace_mcp.build_server(store, employee["id"], project["id"], membership_token)
    )
    for name in wt.SHARED_TOOLS:
        assert name in mailbox and name in workspace
        assert mailbox[name] == workspace[name], (
            f"{name} 参数不一致：{mailbox[name]} vs {workspace[name]}"
        )


def test_declaration_scopes_are_disjoint_in_the_right_way():
    assert not (set(wt.MAILBOX_ONLY) & set(wt.WORKSPACE_ONLY))
    # 能力不对称（刻意）：会建任务卡的同事派活只给工作台面，防 agent 间级联
    assert "team_message" in wt.tools_for("workspace")
    assert "team_message" not in wt.tools_for("mailbox")
    assert "project_code_search" in wt.tools_for("workspace")
    assert "project_code_search" not in wt.tools_for("mailbox")
    assert {"project_tasks", "project_task_accept", "project_task_submit"} <= wt.tools_for(
        "mailbox"
    )
    assert "project_tasks" not in wt.tools_for("workspace")


def test_validate_flags_drift_both_ways():
    planted_extra = sorted(wt.tools_for("mailbox")) + ["project_surprise"]
    assert wt.validate("mailbox", planted_extra)["unexpected"] == ["project_surprise"]
    assert wt.validate("mailbox", ["project_context"])["missing"] == sorted(
        set(wt.tools_for("mailbox")) - {"project_context"}
    )
    with pytest.raises(ValueError):
        wt.tools_for("nope")


def test_contract_is_documented_for_every_tool():
    declared = wt.contract()
    assert len(declared) == 19
    for name, meta in declared.items():
        assert meta["purpose"], f"{name} 没有用途说明"
        assert meta["scopes"], f"{name} 没有作用域"


def test_remote_scope_excludes_unimplemented_tools():
    """remote 声明必须与实现一致（评审实测：曾声明 16 / 实现 11 ✗）。"""
    from agent_mailbox import workbench_tools as wt

    assert wt.REMOTE_UNAVAILABLE == {
        "project_graft_ask",
        "project_graft_callers",
        "project_aoci_doctor",
        "project_aoci_status",
        "project_aoci_check",
    }
    remote = wt.tools_for("remote")
    assert not (remote & wt.REMOTE_UNAVAILABLE), "远端声明不得包含未实现工具 ✗"
    assert len(remote) == 11, f"远端面应为 11（实测 10 个 project_* + team_message）: {len(remote)}"
    assert len(wt.tools_for("workspace")) == 16 and len(wt.tools_for("mailbox")) == 17
    assert len(remote) < len(wt.tools_for("workspace"))
