"""疑似 agent 提示（第 10c 条）：**只提示，绝不自动登记** ✗。"""

from __future__ import annotations

import stat

from agent_mailbox import workbench_runtime as rt


def test_suspects_finds_suffix_matches_in_path(tmp_path, monkeypatch):
    tool = tmp_path / "my-tool-agent"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    other = tmp_path / "unrelated"
    other.write_text("#!/bin/sh\n", encoding="utf-8")
    other.chmod(other.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(tmp_path))
    names = {item["name"] for item in rt.discover_suspects()}
    assert "my-tool-agent" in names, names
    assert "unrelated" not in names, "不匹配后缀的不得出现 ✗"


def test_suspects_never_include_known_agents(tmp_path, monkeypatch):
    known_name = min(rt.KNOWN_AGENTS)
    (tmp_path / known_name).write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / known_name).chmod((tmp_path / known_name).stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert all(item["name"] != known_name for item in rt.discover_suspects()), (
        "已知 agent 不该出现在疑似里 ✓"
    )


def test_suspects_are_only_hints_never_registrations(tmp_path, monkeypatch):
    """结构断言：疑似列表**不产生员工** ✗（第 10c 条：提示但不自作主张 ✓）。"""
    tool = tmp_path / "another-code"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(tmp_path))
    suspects = rt.discover_suspects()
    assert suspects and set(suspects[0]) == {"name", "path", "reason"}, (
        "只给提示三元组 ✓ 不含可写字段 ✗"
    )


def test_ui_shows_suspects_as_hints_only():
    """第 10c 的 UI 面：只显示提示 ✓ 且必须**由用户点击**才登记 ✓（绝不自动登记 ✗）。"""
    js = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "src"
        / "agent_mailbox"
        / "workbench_assets"
        / "workbench.js"
    ).read_text(encoding="utf-8")
    assert "疑似 agent" in js, "UI 必须显示疑似提示 ✗"
    assert "不会自动登记" in js, "必须写明不会自动登记 ✗"
    assert "result.suspects" in js, "必须读后端的 suspects 字段 ✓"
    assert 'data-action": "manual-employee"' in js, "提示必须指路手工登记（用户点了才登记 ✓）"
