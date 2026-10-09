"""手工登记表单：选项必须与后端白名单**同源** ✓（防漂移 ✗）。"""

from __future__ import annotations

from pathlib import Path

from agent_mailbox.workbench_store import CONNECTION_TYPES, EMPLOYEE_KINDS

JS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agent_mailbox"
    / "workbench_assets"
    / "workbench.js"
)


def test_form_options_come_from_the_backend_whitelist():
    source = JS.read_text(encoding="utf-8")
    for kind in EMPLOYEE_KINDS:
        assert f'value: "{kind}"' in source, (
            f"表单缺 kind 选项 {kind} ✗（要与 EMPLOYEE_KINDS 同步 ✓）"
        )
    for conn in ("cli", "app"):
        assert f'value: "{conn}"' in source, f"表单缺连接方式 {conn} ✗"
    assert CONNECTION_TYPES, "连接方式白名单不得为空 ✓"


def test_form_requires_a_real_executable_and_honest_copy():
    source = JS.read_text(encoding="utf-8")
    assert "openManualEmployeeForm" in source and 'data-action": "manual-employee"' in source, (
        "入口缺失 ✗"
    )
    assert "必须真实存在且可执行" in source, "前置条件必须写明（否则用户白填 ✗）"
    assert "执行未验证" in source, "成功后必须如实说未验证 ✗ 不假装已验证 ✓"


def test_manual_form_can_join_the_current_project():
    """缺口 1 回归 ✓：手工登记表单必须能**直接并入当前项目** ✗（原来登记完不进项目 ⇒ 用户以为加好了却没有 ✓）。"""
    source = JS.read_text(encoding="utf-8")
    assert "登记后加入当前项目" in source, "缺勾选框 ✓"
    assert "project_id: joinProject && joinProject.checked" in source, (
        "必须把 project_id 传给 addEmployee ✓"
    )
    assert "const hasProject = Boolean(project())" in source, "无项目时应默认不勾 ✓"
