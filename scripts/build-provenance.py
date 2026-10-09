#!/usr/bin/env python3
"""构建溯源自检（可用 `AGENT_MAILBOX_APP=<path>` 指向任意 bundle ✓ 例如新构建 ✓）（Codex AM-06 ✓）：把"**你打开的到底是哪份代码**"钉死 ✓。

Codex 实测：`/Applications/Agent Mailbox.app` 里的 `workbench.js`/`index.html`
与**当前源码** SHA256 **不同** ✗ ⇒ 修复到不了用户手里 ✓（我也实测过同类：App 曾跑 uv 缓存旧副本 ✗）。
本脚本只读 ✓ 不改任何东西 ✓ —— 输出每一行的 `来源 / 期望 / 实际 / 判定` ✓
并绑定：**revision · 构建号 · 包哈希 · home · schema** ✓，供"关于页/证据"引用 ✓。
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
APP = pathlib.Path(os.environ.get("AGENT_MAILBOX_APP", "/Applications/Agent Mailbox.app"))
ASSETS = ("workbench.js", "index.html")


def sha256(path: pathlib.Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "(读不到)"


def revision() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        return out.stdout.strip() or "(未知)"
    except (OSError, subprocess.SubprocessError):
        return "(未知)"


def main() -> int:
    print(f"仓库: {REPO}")
    print(f"源码 revision: {revision()}")
    print(f"安装包: {APP}{'' if APP.exists() else '（不存在 ⇒ 走 CLI/pip 渠道 ✓）'}")
    rows: list[tuple[str, str, str, bool]] = []
    bindings = {
        "revision": revision(),
        "app_version": "(无原生包)",
        "app_build": "(无原生包)",
        "home": str(pathlib.Path.home() / ".agent-mailbox-v08"),
        "schema": "(见 state.db)",
    }
    if APP.exists():
        plist = APP / "Contents" / "Info.plist"
        try:
            for key, name in (
                ("CFBundleShortVersionString", "app_version"),
                ("CFBundleVersion", "app_build"),
            ):
                out = subprocess.run(
                    ["defaults", "read", str(plist.with_suffix("")), key],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                bindings[name] = out.stdout.strip() or "(未知)"
        except (OSError, subprocess.SubprocessError):
            pass
        for name in ASSETS:
            source = REPO / "src" / "agent_mailbox" / "workbench_assets" / name
            # 资源在 bundle 里的位置**不固定** ✗ ⇒ 直接搜索 ✓（不同打包方式路径不同 ✓ 别猜路径 ✗）
            found = sorted(APP.rglob(name))
            if not found:
                rows.append((f"资源 {name}", sha256(source), "(bundle 内未找到)", False))
                continue
            for installed in found:
                a, b = sha256(installed), sha256(source)
                rows.append(
                    (f"资源 {name} @ {installed.relative_to(APP).as_posix()}", b, a, a == b)
                )
    print("\n绑定（关于页/证据应当引用的字段 ✓）:")
    for key, value in bindings.items():
        print(f"  {key:<12} = {value}")
    print("\n资源一致性 ✓（安装包 vs 当前源码 ✗）:")
    for label, expect, actual, same in rows:
        print(f"  {label:<18} 源码={expect} 安装={actual} {'一致 ✓' if same else '**不同 ✗**'}")
    if not rows:
        print("  （无原生包 ⇒ 本项不适用 ✓ 由 CLI/pip 的 wheel 血统决定 ✓）")
    elif any(not same for *_rest, same in rows):
        print("\n⚠️ 安装包与源码**不同** ⇒ 修复**到不了**用户 ✓ 必须重打包并重验（AM-06 ✓）")
        return 1
    print("\n✓ 安装包与源码一致 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
