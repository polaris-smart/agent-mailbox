"""门禁完整性：**空文件/零测试文件不得全绿** ✓（第 7 轮踩到的假绿 ✗）。

背景（真实事故 ✗）：一次提交里两个新文件是 **0 字节** ✓，而六门**全绿** ✓ ——
因为"空测试文件 = 没有测试" ⇒ pytest 依然通过 ✗。本文件把这条堵死：
① 仓库里**不得有 0 字节的 .py**（含测试 ✓）
② 每个 `tests/test_*.py` **必须至少能收集到一条测试** ✓（用 AST 判定 ✓ 不靠字符串匹配）
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _tracked_python() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "*.py"], capture_output=True, text=True, check=False
    )
    return [ROOT / line for line in out.stdout.splitlines() if line.strip()]


def test_no_empty_python_file_is_tracked():
    empty = [
        str(p.relative_to(ROOT)) for p in _tracked_python() if p.exists() and p.stat().st_size == 0
    ]
    assert empty == [], f"仓库里存在 0 字节的 .py（空文件照样能全绿 ✗）：{empty}"


def test_every_python_file_has_content():
    blank = [
        str(p.relative_to(ROOT))
        for p in _tracked_python()
        if p.exists()
        and p.stat().st_size
        and not p.read_text(encoding="utf-8", errors="replace").strip()
    ]
    assert blank == [], f"这些 .py 只有空白字符：{blank}"


def _test_functions(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            count += 1
    return count


def test_every_test_module_declares_at_least_one_test():
    offenders = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        try:
            if _test_functions(path) == 0:
                offenders.append(path.name)
        except SyntaxError as exc:
            offenders.append(f"{path.name}（语法错：{exc.msg}）")
    assert offenders == [], f"这些测试文件收集不到任何测试（全绿是假的 ✗）：{offenders}"


def test_dependency_trees_are_gitignored():
    """依赖树必须被 ignore ✓（2026-10-07 实测：未 ignore ✗ ⇒ 一次 add 出了 59 万行索引 ✗✗）。"""
    sample = "src/agent_mailbox/runtime_bridge/node_modules/acpx/package.json"
    done = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", sample], check=False)
    assert done.returncode == 0, f"{sample} 未被 .gitignore 覆盖 ✗"
