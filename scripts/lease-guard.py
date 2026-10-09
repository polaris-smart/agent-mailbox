#!/usr/bin/env python3
"""Pre-commit guard: refuse a commit that touches another agent's claimed paths (T4).

Install (explicitly, never silently):

    agent-mailbox lease --print-hook >> .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit

Environment (or flags):
    AGENT_MAIL_HOME      home of the store (default ~/.agent-mailbox)
    AGENT_MAIL_PROJECT   project id to check leases in
    AGENT_MAIL_EMPLOYEE  the employee making the commit (own claims never block)

Exit codes: 0 = clear, 1 = blocked by an active claim, 2 = usage/configuration error.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def staged_files() -> list[str]:
    # -z + core.quotePath=false：非 ASCII 路径不会被转义成 "src/\344\270\255…"（否则匹配不上）
    # --diff-filter 含 D：**删除**别人认领的文件同样要拦（最容易毁掉对方工作的动作）
    out = subprocess.run(
        [
            "git",
            "-c",
            "core.quotePath=false",
            "diff",
            "--cached",
            "--name-only",
            "-z",
            "--diff-filter=ACMRD",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return [item for item in (out.stdout or "").split("\0") if item.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Block commits that touch others' claims.")
    parser.add_argument(
        "--home",
        type=Path,
        default=Path(os.environ.get("AGENT_MAIL_HOME", ""))
        if os.environ.get("AGENT_MAIL_HOME")
        else Path.home() / ".agent-mailbox",
    )
    parser.add_argument("--project", default=os.environ.get("AGENT_MAIL_PROJECT", ""))
    parser.add_argument("--employee", default=os.environ.get("AGENT_MAIL_EMPLOYEE", ""))
    parser.add_argument(
        "--path", action="append", default=[], help="覆盖：直接检查这些路径（默认取暂存区）"
    )
    options = parser.parse_args(argv)

    paths = options.path or staged_files()
    if not paths:
        return 0
    if not options.project:
        print("lease-guard: 缺少 AGENT_MAIL_PROJECT（或 --project）", file=sys.stderr)
        print(
            "  · 用法：AGENT_MAIL_PROJECT=project_xxx AGENT_MAIL_EMPLOYEE=employee_yyy lease-guard.py",
            file=sys.stderr,
        )
        return 2

    from agent_mailbox import workbench_lease
    from agent_mailbox.workbench_store import WorkbenchStore

    store = WorkbenchStore(options.home.expanduser())
    conflicts = workbench_lease.check_conflicts(
        store, options.project, options.employee or None, paths
    )
    if not conflicts:
        return 0

    print("lease-guard: 提交被拦下 —— 以下路径正被别的员工认领：", file=sys.stderr)
    for item in conflicts:
        print(
            f"  · {item['path']}  ← {item['held_by']}（{item['mode']}，到期 {item['expires_at']}）"
            f" 原因：{item['reason'] or '—'}",
            file=sys.stderr,
        )
    print(
        "  怎么解：① 等它到期（TTL 自动释放）② 请对方释放：agent-mailbox lease release --holder <员工ID>",
        file=sys.stderr,
    )
    print(
        "          ③ 人工确认无冲突后：git commit --no-verify（**会跳过闸门且不自动留痕**，责任在人）",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
