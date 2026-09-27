"""Unified CLI subcommands (v0.7.5 §3.1), mounted under the existing
``agent-mailbox`` console entry by :func:`agent_mailbox.server.main`.

    agent-mailbox setup [--yes]        # --yes: headless defaults, no browser
    agent-mailbox discover [--json] [--deep [DIR ...]] [--no-save]
    agent-mailbox status [--json]
    agent-mailbox test <member> [--timeout 60] [--json]
    agent-mailbox connect <name> [--yes]
    agent-mailbox uninstall [--letters keep|export|archive|delete]

Compatibility iron rule: the legacy invocations keep working untouched —
``agent-mailbox [--web PORT] [--http PORT]`` (stdio/HTTP/kanban server),
``agent-mailbox-watch``, ``agent_mailbox.wake`` and ``python -m
agent_mailbox`` (cleanup). Routing happens only when the first positional
token is one of the subcommand names above.

Human output is Chinese-first (brief §9), machine output is ``--json``.
Times are rendered in the machine's local timezone (brief §5⑪).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import connect as connect_mod
from . import wake as wake_mod
from .discover import (
    build_report,
    default_context,
    mask_url,
    now_local,
    test_member,
)

SUBCOMMANDS = ("setup", "discover", "status", "test", "connect", "uninstall")

STATUS_MARK = {"ok": "✅", "broken": "❌", "unknown": "· 未实测"}


def _root_from(args: argparse.Namespace) -> Path:
    return Path(
        getattr(args, "home", None)
        or os.environ.get("AGENT_MAIL_HOME", "")
        or Path.home() / ".agent-mail"
    ).expanduser()


# ------------------------------------------------------------- printing


def _print_channel(ch: dict[str, Any], indent: str = "   ") -> None:
    mark = STATUS_MARK.get(ch.get("status", "unknown"), "·")
    print(f"{indent}{mark} {ch.get('type', '?')}  {ch.get('value', '')}")
    print(f"{indent}   来源: {ch.get('source', '-')}")
    if ch.get("status") == "broken":
        print(f"{indent}   原因: {ch.get('reason', '')}")
        if ch.get("next_step"):
            print(f"{indent}   → 下一步: {ch['next_step']}")


def _print_member(m: dict[str, Any]) -> None:
    kind = m.get("kind_label") or {
        "cli": "CLI",
        "app": "App",
        "config": "仅配置痕迹",
        "unknown": "未识别",
    }.get(m.get("kind", ""), m.get("kind"))
    conn = "已接入" if m.get("connected") else "装了未接"
    print(f"● {m['member']}   形态: {kind}   接入: {conn}")
    for ev in m.get("evidence", []):
        print(f"   [{ev['layer']}] {ev['type']}  {ev['detail']}")
        print(f"      来源: {ev['source']}")
    chans = m.get("channels") or []
    if chans:
        print("   唤醒通道:")
        for ch in chans:
            _print_channel(ch)
    else:
        print("   唤醒通道: 未发现（信只能等人主动 mailbox_check）")
    if m.get("stale"):
        print(f"   ⚠ {m.get('stale_note', '发现指纹已变化')}")
    print()


def _print_report(report: dict[str, Any], *, header: str) -> None:
    print(f"{header}  （{report.get('generated_at_local') or now_local()}）")
    print(f"邮件根: {report['root']}")
    members = report["members"]
    if report.get("empty"):
        print()
        print("没有发现任何受支持的 agent。")
        print(f"支持清单: {' '.join(report['supported'])}")
        print(
            "装好其中任意一个后重跑 agent-mailbox discover；也可手动添加（向导将在后续版本提供）。"
        )
        return
    print(f"发现 {len(members)} 个成员：")
    print()
    for m in members:
        _print_member(m)
    others = report.get("other_listeners") or []
    if others:
        print("其他监听进程（不属于任何已知成员，仅列出、不猜身份）:")
        for o in others:
            print(f"   · {o['exe']}  (端口 {o['port']})")
        print()


def _print_status(report: dict[str, Any], wake_info: dict[str, Any]) -> None:
    print(f"agent-mailbox 状态  （检查时间 {now_local()}）")
    print(f"邮件根: {report['root']}")
    reg = report.get("members", [])
    registered = [m["member"] for m in reg if m.get("registered")]
    print(
        f"名册: {', '.join(registered) if registered else '（空）'}（{len(registered)} 个已注册）"
    )
    if wake_info.get("configured"):
        line = f"唤醒: wake.json → {wake_info.get('agent_id')} via {wake_info.get('adapter')}"
        if wake_info.get("webhook_url"):
            line += f"；webhook {wake_info['webhook_url']}"
        if wake_info.get("plist_installed"):
            line += "；launchd plist 已安装 ✅"
        print(line)
    else:
        print("唤醒: 未安装（python -m agent_mailbox.wake install --agent <id> 可装）")
    print()
    if report.get("empty"):
        print("没有发现任何成员。支持清单: " + " ".join(report["supported"]))
        return
    for m in reg:
        _print_member(m)
    broken = [
        (m["member"], ch)
        for m in reg
        for ch in m.get("channels", [])
        if ch.get("status") == "broken"
    ]
    if broken:
        print(f"待修 {len(broken)} 处：")
        for member, ch in broken:
            print(f"   ❌ {member}: {ch.get('reason', '')}")
            if ch.get("next_step"):
                print(f"      → {ch['next_step']}")
    else:
        print("通道体检：未发现断链。")


def _setup_hints(report: dict[str, Any]) -> None:
    print("下一步（按需执行）：")
    print("  · 起看板:      agent-mailbox --web 8642")
    for m in report["members"]:
        print(
            f"  · 唤醒 {m['member']}:  python -m agent_mailbox.wake install --agent {m['member']}"
        )
        if m.get("connected") is False:
            print(f"  · 接入 {m['member']}:  agent-mailbox connect {m['member']} --yes（先备份）")
    print("  · 实测通道:    agent-mailbox test <member>")


# ----------------------------------------------------------- subcommands


def _cmd_setup(args: argparse.Namespace) -> int:
    if not args.yes:
        print("交互式向导（浏览器 UI）将在后续版本提供；本次按无头默认执行（不启浏览器）。\n")
    root = _root_from(args)
    ctx = default_context(root)
    report = build_report(ctx, save=True)
    _print_report(report, header="agent-mailbox 初始化完成")
    _setup_hints(report)
    return 0


def _cmd_discover(args: argparse.Namespace) -> int:
    root = _root_from(args)
    ctx = default_context(
        root, deep_dirs=tuple(Path(d) for d in (args.deep or [])), probe_versions=bool(args.deep)
    )
    report = build_report(ctx, save=not args.no_save)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report, header="agent-mailbox 发现结果")
    return 0


def _wake_info(root: Path) -> dict[str, Any]:
    cfg = wake_mod.WakeConfig.load(root)
    info: dict[str, Any] = {"configured": cfg is not None}
    if cfg:
        info["agent_id"] = cfg.agent_id
        info["adapter"] = cfg.adapter
        if sys.platform == "darwin":
            plist = (
                Path.home()
                / "Library"
                / "LaunchAgents"
                / f"{wake_mod.WAKE_LABEL}-{cfg.agent_id}.plist"
            )
            info["plist_installed"] = plist.exists()
    return info


def _cmd_status(args: argparse.Namespace) -> int:
    root = _root_from(args)
    ctx = default_context(root)
    report = build_report(ctx, save=False)  # status never rewrites the fingerprint
    wake_info = _wake_info(root)
    if wake_info.get("agent_id"):
        try:
            data = json.loads((root / "wake.json").read_text(encoding="utf-8"))
            url = str((data.get("webhook") or {}).get("url", ""))
            if url:
                wake_info["webhook_url"] = mask_url(url)
        except (OSError, json.JSONDecodeError):
            pass
    if args.json:
        print(json.dumps({**report, "wake": wake_info}, ensure_ascii=False, indent=2))
    else:
        _print_status(report, wake_info)
    return 0


def _cmd_test(args: argparse.Namespace) -> int:
    root = _root_from(args)
    result = test_member(args.member, root, timeout=args.timeout, from_id=args.from_id)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    mark = "✅" if result["ok"] else "❌"
    print(
        f"{mark} test {result['member']}（耗时 {result['elapsed_s']}s，"
        f"{result['checked_at_local']}）"
    )
    print(f"   {result['reason']}")
    if result.get("next_step"):
        print(f"   → 下一步: {result['next_step']}")
    return 0 if result["ok"] else 1


def _cmd_connect(args: argparse.Namespace) -> int:
    root = _root_from(args)
    result = connect_mod.connect(args.name, root, yes=args.yes)
    if not result.get("known"):
        print(f"❌ {result['note']}")
        return 1
    if not args.yes:
        print(f"接入计划（预览，未写入任何文件）—— {result['member']}")
        for t in result["targets"]:
            note = f"  （{t['note']}）" if t.get("note") else ""
            print(f"   · {t['path']}  [{t['status']}]{note}")
        for s in result.get("skipped", []):
            if s.get("manual"):
                print(f"   手动添加提示: {s['manual']}")
        print("加 --yes 执行（会先备份再写入；JSON 配置支持自动还原）。")
        return 0
    for w in result.get("written", []):
        print(f"✅ 已写入 {w['path']}（备份: {w['backup'] or '新建文件，无备份'}）")
    for s in result.get("skipped", []):
        print(f"· 跳过 {s['path']}: {s['why']}")
        if s.get("manual"):
            print(f"  手动添加: {s['manual']}")
    for err in result.get("errors", []):
        print(f"❌ {err}")
    if result.get("points_file"):
        print(f"点位台账: {result['points_file']}（agent-mailbox uninstall 可逐项还原）")
    return 1 if result.get("errors") else 0


def _ask_letters(root: Path, choice: str, *, assume_tty: bool | None = None) -> str:
    is_tty = sys.stdin.isatty() if assume_tty is None else assume_tty
    if choice == "keep" and is_tty:
        # 询问挂点：四选一（保留/导出/归档/删除）；本版先实现「保留」，
        # 其余三项在向导（PR C）里落地——如实告知，不静默吞掉用户意图。
        try:
            answer = input(f"信件去留？[保留/导出/归档/删除]（直接回车=保留，信件在 {root}）: ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        answer = answer.strip().lower()
        mapped = {"保留": "keep", "导出": "export", "归档": "archive", "删除": "delete"}
        choice = mapped.get(answer, mapped.get(answer.lower(), "keep"))
    if choice == "keep":
        return f"信件保留在 {root}（未删除）。"
    return f"【挂点】「{choice}」将在向导中提供；本次按保留处理，信件仍在 {root}。"


def _stop_wake(launch_agents_dir: Path | None, activate: bool) -> list[str]:
    """Unload our own launchd/systemd wake integrations (nothing else's)."""
    stopped: list[str] = []
    la_dir = Path(launch_agents_dir or (Path.home() / "Library" / "LaunchAgents"))
    for plist in sorted(la_dir.glob(f"{wake_mod.WAKE_LABEL}-*.plist")):
        agent = plist.name[len(wake_mod.WAKE_LABEL) + 1 : -len(".plist")]
        out = wake_mod.uninstall(agent, launch_agents_dir=la_dir, activate=activate)
        stopped.extend(out.get("removed", []))
    return stopped


def _cmd_uninstall(args: argparse.Namespace) -> int:
    root = _root_from(args)
    print(
        "提示：若 agent-mailbox --web/--http 服务正在运行，请在其终端 Ctrl-C 停止"
        "（本命令不杀别人的进程）。"
    )
    stopped = _stop_wake(getattr(args, "launch_agents_dir", None), activate=not args.no_activate)
    for s in stopped:
        print(f"✅ 已移除唤醒集成 {s}")
    revert = connect_mod.revert_points(root)
    for r in revert["reverted"]:
        print(f"✅ 已还原 {r['file']}（{r['why']}）")
    for a in revert["already_clean"]:
        print(f"· {a['file']}: {a['why']}")
    residual = revert["residual"]
    print(_ask_letters(root, args.letters))
    if residual:
        print("❌ 以下点位未能自动还原（残留，请人工核对）：")
        for r in residual:
            print(f"   {r['file']}: {r['reason']}")
        return 1
    print("还原完成：所有记录过的写入点位均已核验（diff=0）。")
    return 0


# ------------------------------------------------------------------ main


def cli_main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--home",
        metavar="DIR",
        default=None,
        help="邮件根目录（默认 ~/.agent-mail，或 $AGENT_MAIL_HOME）",
    )
    parser = argparse.ArgumentParser(
        prog="agent-mailbox",
        description="agent-mailbox 统一子命令（setup/discover/status/test/connect/uninstall）",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("setup", parents=[common], help="初始化（--yes 无头默认，不启浏览器）")
    p.add_argument("--yes", action="store_true", help="无头模式：全默认、纯命令行")
    p.set_defaults(func=_cmd_setup)

    p = sub.add_parser("discover", parents=[common], help="四层自动发现（L1–L3 只读）")
    p.add_argument("--json", action="store_true", help="输出结构化 JSON")
    p.add_argument(
        "--deep",
        nargs="*",
        default=None,
        metavar="DIR",
        help="指定路径深扫（含 <cli> --version 指纹）",
    )
    p.add_argument("--no-save", action="store_true", help="不更新发现指纹文件")
    p.set_defaults(func=_cmd_discover)

    p = sub.add_parser("status", parents=[common], help="服务 + 成员通道状态（人话摘要）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_status)

    p = sub.add_parser("test", parents=[common], help="发测试信并等回执（L4）")
    p.add_argument("member", help="成员 id（见 discover 输出）")
    p.add_argument("--timeout", type=float, default=60.0, help="等待回执秒数（默认 60）")
    p.add_argument("--from-id", dest="from_id", default="", help="以谁的名义发（默认 tester）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_test)

    p = sub.add_parser("connect", parents=[common], help="接入一个成员（写配置前先备份+记点位）")
    p.add_argument("name", help="成员 id")
    p.add_argument("--yes", action="store_true", help="执行写入（默认仅预览）")
    p.set_defaults(func=_cmd_connect)

    p = sub.add_parser("uninstall", parents=[common], help="还原所有改过的他人配置 + 信件去留")
    p.add_argument(
        "--letters",
        choices=("keep", "export", "archive", "delete"),
        default="keep",
        help="信件去留（默认保留；其余为向导挂点）",
    )
    p.add_argument(
        "--launch-agents-dir", default=None, help="override ~/Library/LaunchAgents（测试用）"
    )
    p.add_argument("--no-activate", action="store_true", help="只动文件，不调用 launchctl")
    p.set_defaults(func=_cmd_uninstall)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(cli_main())
