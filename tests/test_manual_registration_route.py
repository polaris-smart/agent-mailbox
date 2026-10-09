"""手工登记路由规则：入口**真实可执行**才允许手工登记 ✓；垃圾/未发现 ⇒ 仍拒 ✗。"""

from __future__ import annotations

from pathlib import Path

from agent_mailbox import workbench


def test_route_allows_manual_registration_but_keeps_the_hard_refusal():
    """四者必须同时存在 ✓（结构性护栏 ✓ 防后人把口子开成无条件注册 ✗）。"""
    source = Path(workbench.__file__).read_text(encoding="utf-8")
    assert "os.path.isfile(str(entrypoint))" in source, "入口必须真实存在 ✓"
    assert "os.access(str(entrypoint), os.X_OK)" in source, "入口必须可执行 ✓"
    assert "if discovered is None and not manual:" in source, "旧硬拒必须保留 ✗"
    assert "AGENT_NOT_DISCOVERED" in source, "原错误码必须仍在 ✗"
    assert "t_manual_detail(entrypoint)" in source, "必须用如实文案 ✓"
