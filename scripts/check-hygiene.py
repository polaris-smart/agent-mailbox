#!/usr/bin/env python3
"""卫生门：仓库里**不得**出现凭据/会话/私有库/本机路径（发布前必过 ✓）。

用法：
    python scripts/check-hygiene.py            # 扫描 git 跟踪的文件（推荐 ✓）
    python scripts/check-hygiene.py --all      # 扫描工作区（排除 node_modules/.git/.venv）

退出码：0 = 通过 ✓；1 = 有 **FAIL** 项 ✗（WARN 不阻断 ✓ 但会被列出 ✓）。
分级依据（老板 2026-10-07「发布一定是很干净的系统」✓）：
  FAIL = 凭据/会话/私有库/私钥 —— 一旦进仓就是事故 ✗
  WARN = 本机绝对路径/内部资料 —— 发布前应清理 ✓ 不阻断日常开发 ✓
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

FAIL_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("会话文件", re.compile(r"mail_session_[0-9a-f]{8,}")),
    ("实例令牌文件", re.compile(r'"token"\s*:\s*"[A-Za-z0-9_\-]{16,}"')),
    ("飞书/模型凭据", re.compile(r"(FEISHU_APP_SECRET|LARK_APP_SECRET|ARK_[A-Z_]*KEY)\s*[:=]")),
    ("私钥", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("npm token", re.compile(r"_authToken\s*=")),
    # 只认"右值看起来就是密钥字面量"（评审：旧正则被 `token = await get_token()` 之类绕过/误伤 ✗）
    # 形态：KEY=值 或 KEY: 值，值必须带引号或为 ≥16 位 alnum/-_，且**到行尾**（排除表达式赋值 ✓）
    (
        "环境/YAML 赋值",
        re.compile(
            r"(?im)^\s*(export\s+)?[A-Za-z0-9_]*(SECRET|TOKEN|API_KEY|APIKEY|PASSWORD|PASSWD)[A-Za-z0-9_]*\s*[:=]\s*"
            r"(?:[\"']?[A-Za-z0-9_\-]{16,}[\"']?)[ \t]*(?:#.*)?$"
        ),
    ),
)
WARN_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("本机绝对路径", re.compile(r"/Users/[A-Za-z0-9._-]+/")),
    ("内部任务书/评审字样", re.compile(r"(任务书|评审报告|路线图)")),
)
SKIP_DIRS = {".git", "node_modules", ".venv", ".build-venv", "__pycache__", "dist", "build"}

# 显式白名单：**只放测试夹具**（伪造样本 ✓ 用于验证产品会拦私钥/凭据 ✓ 不是泄露 ✗）
# 加新条目必须写明理由 —— 不许为了"让扫描绿"而放宽规则 ✗
# 白名单**按规则粒度**（评审：整文件跳过 ⇒ 真凭据放进该文件也放行 ✗）
# 语义：{文件: {允许触发的规则名}} —— 该文件里**其它**规则照拦 ✓
FIXTURE_RULE_ALLOWLIST: dict[str, set[str]] = {
    # 归档的评审报告是**证据** ✓（不改写 ✗）：其中引用了会话 **id** 与规则名（非凭据值 ✗）
    # ⇒ 仅放行这两条规则 ✓ 该文件里其它规则照拦 ✓
    "docs/reviews/2026-10-07-round6-process.md": {"会话文件", "飞书/模型凭据"},
    "tests/test_workbench_resources.py": {"私钥"},  # 假 PEM 样本：断言产品拒绝私钥 ✓
    "tests/test_workbench_runtime.py": {
        "环境/YAML 赋值"
    },  # 运行时测试的假 key 样本 ✓（非真实凭据 ✗）
    "tests/test_hygiene_scan.py": {
        "会话文件",
        "实例令牌文件",
        "飞书/模型凭据",
        "环境/YAML 赋值",
        "私钥",
    },  # 扫描器自测样本 ✓
}
MAX_BYTES = 2_000_000


def _tracked(root: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files"], capture_output=True, text=True, check=False
    )
    return [root / line for line in out.stdout.splitlines() if line.strip()]


def _walk(root: Path) -> list[Path]:
    files = []
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file():
            files.append(path)
    return files


def scan(root: Path, files: list[Path]) -> tuple[list[str], list[str]]:
    fails: list[str] = []
    warns: list[str] = []
    for path in files:
        try:
            if path.stat().st_size > MAX_BYTES or path.suffix in {
                ".png",
                ".jpg",
                ".webp",
                ".icns",
                ".pdf",
            }:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(path.relative_to(root))
        allowed_rules = FIXTURE_RULE_ALLOWLIST.get(rel, set())
        for label, rule in FAIL_RULES:
            if label in allowed_rules:
                continue
            if rule.search(text):
                fails.append(f"[FAIL] {label}: {rel}")
        for label, rule in WARN_RULES:
            if label.startswith("内部") and not rel.startswith("docs/"):
                continue  # 测试文件名里的 review 不算内部资料 ✗
            if rule.search(text):
                warns.append(f"[WARN] {label}: {rel}")
    return fails, warns


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="仓库卫生门（凭据/会话/私有库/本机路径）")
    parser.add_argument("--all", action="store_true", help="扫描工作区而非仅 git 跟踪文件")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    root = args.root.resolve()
    files = _walk(root) if args.all else _tracked(root)
    fails, warns = scan(root, files)
    for line in sorted(set(fails)):
        print(line)
    for line in sorted(set(warns)):
        print(line)
    print(f"── 扫描 {len(files)} 个文件 · FAIL {len(set(fails))} · WARN {len(set(warns))}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
