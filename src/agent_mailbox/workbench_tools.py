"""Project MCP 工具名与作用域（**唯一声明源** ✓）。

计数（2026-10-07 实测）：受管面 16 · 邮箱面 17 · **远端面 11**（= 共享 14 + workspace-only 2 − 未实现 5 ✓ 与实现一致 ✓）。
共享 14 个。计数变更必须同步 tests/test_workbench_tools.py 的断言 ✓。

Before this module the product shipped **three** related but hand-maintained tool
surfaces, and nothing checked them against each other:

  * ``mailbox_mcp.build_server``        — 12 tools, bound project-mailbox session
  * ``workspace_mcp.build_server``      — 10 tools, workspace/manager scope
  * ``workspace_mcp.build_remote_server`` — 10 tools, same as workspace

Nine tools are shared, one is workspace-only (``project_code_search``) and three
are mailbox-only (``project_tasks`` / ``project_task_accept`` /
``project_task_submit`` — the employee's own act path). The scoping is deliberate;
what was missing was a single declaration plus a test that the servers actually
match it, so a rename or a stray tool cannot ship unnoticed.

Argument lists are **not** duplicated here: the test compares the real signatures
of the shared tools across servers, which is the property that matters.
"""

from __future__ import annotations

SHARED_TOOLS: dict[str, str] = {
    "project_graft_ask": "只读问答：仓结构（graft ask）· 不触发受管执行",
    "project_graft_callers": "只读：调用链（graft callers）· 不触发受管执行",
    "project_aoci_doctor": "只读：认知层健康（aoci doctor）· 不触发受管执行",
    "project_aoci_status": "只读：认知层状态（aoci status）· 不触发受管执行",
    "project_aoci_check": "只读：认知层 findings（aoci check）· 不触发受管执行",
    "project_context": "项目、员工、任务、资源与策略一览",
    "project_messages": "读本人收件箱/发件箱/项目公开信（读取不等于接单）",
    "project_message": "发一条项目消息（可请求工作）",
    "project_note": "写一条项目笔记（记忆）",
    "project_memory_search": "检索项目记忆",
    "project_resource_read": "读已批准资料（可钉版本）",
    "project_resource_versions": "查资料版本与审批状态",
    "project_resource_propose": "提交资料修订供人工批准",
    "project_delivery": "查某任务的交付记录",
}

MAILBOX_ONLY: dict[str, str] = {
    "project_tasks": "读本人名下的任务",
    "project_task_accept": "明确接受任务（读 ≠ 接单）",
    "project_task_submit": "提交结果，等待人工验收",
}

WORKSPACE_ONLY: dict[str, str] = {
    "project_code_search": "在工作区代码里检索（仅工作台/远端作用域）",
    # 显式请同事做"有界只读工作"——它会**建任务卡**，因此只在工作台/远端面提供；
    # 绑定邮箱会话的员工拿不到它：**防 agent 间派活级联**（今日风暴的同源风险）。
    # 这是刻意的能力不对称，不是漏配；若要放开须先过反风暴评审。
    "team_message": "显式请求同事做有界只读工作（会建任务卡）· 仅工作台/远端面",
}

# 远端面**尚未实现**的只读诊断（`build_remote_server` 只代理远端既有接口 ✗）：
# 声明必须排除它们，否则就是"声明 16 / 实现 11"的谎报 ✗（对抗评审实测 ✓）
# backlog：远端补上对应接口后删掉本集合即可 ✓
REMOTE_UNAVAILABLE: frozenset[str] = frozenset(
    {
        "project_graft_ask",
        "project_graft_callers",
        "project_aoci_doctor",
        "project_aoci_status",
        "project_aoci_check",
    }
)

SCOPES: dict[str, frozenset[str]] = {
    "mailbox": frozenset(SHARED_TOOLS) | frozenset(MAILBOX_ONLY),
    "workspace": frozenset(SHARED_TOOLS) | frozenset(WORKSPACE_ONLY),
    "remote": (frozenset(SHARED_TOOLS) | frozenset(WORKSPACE_ONLY)) - REMOTE_UNAVAILABLE,
}


def tools_for(scope: str) -> frozenset[str]:
    if scope not in SCOPES:
        raise ValueError(f"未知作用域：{scope}（可选：{', '.join(sorted(SCOPES))}）")
    return SCOPES[scope]


def contract() -> dict[str, dict[str, object]]:
    """Full declaration: name → {purpose, scopes} (used for docs and validation)."""
    out: dict[str, dict[str, object]] = {}
    for bucket, purpose_field in (
        (SHARED_TOOLS, "shared"),
        (MAILBOX_ONLY, "mailbox"),
        (WORKSPACE_ONLY, "workspace"),
    ):
        for name, purpose in bucket.items():
            out[name] = {
                "purpose": purpose,
                "scopes": sorted(scope for scope, names in SCOPES.items() if name in names),
                "bucket": purpose_field,
            }
    return out


def validate(scope: str, names: list[str] | set[str]) -> dict[str, list[str]]:
    """Compare an observed tool list with the declaration for one scope."""
    expected = set(tools_for(scope))
    observed = set(names)
    return {
        "missing": sorted(expected - observed),
        "unexpected": sorted(observed - expected),
    }
