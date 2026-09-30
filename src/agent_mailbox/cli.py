"""Unified CLI subcommands (v0.7.5 §3.1), mounted under the existing
``agent-mailbox`` console entry by :func:`agent_mailbox.server.main`.

    agent-mailbox setup [--yes]        # --yes: headless defaults, no browser
    agent-mailbox discover [--json] [--deep [DIR ...]] [--no-save]
    agent-mailbox status [--json]
    agent-mailbox test <member> [--timeout 60] [--json]
    agent-mailbox connect <name> [--yes]
    agent-mailbox uninstall [--letters keep|export|archive|delete]
    agent-mailbox upgrade [--check] [--yes]   # v0.7.6 E 单元（t-53）
    agent-mailbox doctor [--json]             # v0.7.6 A-2 单元（t-59 六检基座 + t-64 ⑦⑧诊断面）

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
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import connect as connect_mod
from . import installer
from . import version_check as version_mod
from . import wake as wake_mod
from .discover import (
    build_report,
    default_context,
    mask_url,
    now_local,
    test_member,
)

SUBCOMMANDS = (
    "setup",
    "discover",
    "status",
    "test",
    "connect",
    "uninstall",
    "upgrade",
    "doctor",
    "digest",
)

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


def _setup_hints(
    report: dict[str, Any],
    installed_names: tuple[str, ...] = (),
    skipped: list[tuple[str, str]] | None = None,
) -> None:
    print("下一步（按需执行）：")
    print("  · 起看板:      agent-mailbox --web 8642")
    skipped = skipped or []
    for m in report["members"]:
        if m["member"] in installed_names:
            print(
                f"  · {m['member']}: 唤醒已装好（WatchPaths 信到即醒）；"
                f"实测: agent-mailbox test {m['member']}"
            )
            continue
        reason = next((why for n, why in skipped if n == m["member"]), "")
        print(
            f"  · 唤醒 {m['member']}:  agent-mailbox setup --agent {m['member']} "
            f"--adapter <hermes|generic-webhook|local-command|claude-code>"
            + (f"  （跳过原因: {reason}）" if reason else "")
        )
        if m.get("connected") is False:
            print(f"  · 接入 {m['member']}:  agent-mailbox connect {m['member']} --yes（先备份）")
    print("  · 实测通道:    agent-mailbox test <member>")
    print("  · 体检:        agent-mailbox doctor")


def _print_install_result(out: dict[str, Any], *, belt_generated: bool = False) -> None:
    mark = "✅"
    print(f"{mark} {out.get('agent', '?')}: 注册 + agents 段（{out.get('adapter')}）+ OS 集成完成")
    for f in out.get("files", []):
        print(f"   · 生成 {f}")
    if out.get("wrapper"):
        print(f"   · 唤醒命令模板（provider env 注入内置）: {out['wrapper']}")
    if out.get("belt"):
        print(f"   · belt 脚本（部署副本=本产物）: {out['belt']}")
    cmd = out.get("wake_command") or []
    if cmd:
        print(f"   · 唤醒命令: {json.dumps(cmd, ensure_ascii=False)}")
    for n in out.get("notes", []):
        print(f"   · {n}")
    if not out.get("activated", False):
        print("   · 未 load（--no-activate）；手动: " + str(out.get("activate_cmd", "")))


# ----------------------------------------------------------- subcommands


def _setup_install_one(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    """--agent 显式安装一个身份（判据1/6 的 CLI 打通面）。

    --adapter 缺省时按 discovery 零分类题自动选通道：该成员有 CLI on PATH
    ⇒ local-command + 绝对路径；给了 --webhook-url ⇒ generic-webhook；都不
    行则给出明确修法退出（不猜）。"""
    adapter = str(getattr(args, "adapter", "") or "")
    command_raw = str(getattr(args, "command", "") or "")
    webhook_url = str(getattr(args, "webhook_url", "") or "")
    if not adapter:
        if webhook_url:
            adapter = "generic-webhook"
        else:
            ctx = default_context(root)
            report = build_report(ctx, save=False)
            member = next((m for m in report["members"] if m.get("member") == args.agent), None)
            cli_paths = [
                str(ev.get("detail", ""))
                for ev in (member or {}).get("evidence", [])
                if ev.get("layer") == "L1" and ev.get("type") == "cli" and ev.get("detail")
            ]
            if cli_paths:
                adapter = "local-command"
                command_raw = json.dumps([cli_paths[0]])
                print(f"· 未指定 --adapter：发现 {args.agent} CLI on PATH → local-command 自动接线")
            else:
                raise SystemExit(
                    f"setup: --agent {args.agent} 需要通道参数——"
                    "--adapter local-command --command '[\"<命令>\"]'，"
                    "或 --adapter generic-webhook --webhook-url <url>"
                )
    command = wake_mod._parse_command_arg(command_raw, "setup") if command_raw else None
    return installer.install_agent(
        root,
        args.agent,
        adapter=adapter,
        command=command,
        webhook_url=webhook_url,
        webhook_secret=str(getattr(args, "webhook_secret", "") or ""),
        belt=bool(getattr(args, "belt", False)),
        activate=not getattr(args, "no_activate", False),
        launch_agents_dir=getattr(args, "launch_agents_dir", None),
        systemd_dir=getattr(args, "systemd_dir", None),
        entry_id=str(getattr(args, "entry", "") or ""),
    )


def _cmd_setup(args: argparse.Namespace) -> int:
    if not args.yes:
        print("交互式向导（浏览器 UI）将在后续版本提供；本次按无头默认执行（不启浏览器）。\n")
    root = _root_from(args)
    installed_names: tuple[str, ...] = ()
    skipped: list[tuple[str, str]] = []
    if getattr(args, "agent", ""):
        # 显式单身份：一条命令装完（t-61 判据1/6）
        out = _setup_install_one(args, root)
        _print_install_result(out)
        installed_names = (str(out.get("agent", "")),)
        report = build_report(default_context(root), save=True)
    else:
        # 零输入：自动发现 + CLI 形态成员全自动装好（不让用户做分类题）
        report = build_report(default_context(root), save=True)
        installed, skipped = installer.auto_install(
            root,
            report,
            belt=bool(getattr(args, "belt", False)),
            activate=not getattr(args, "no_activate", False),
            launch_agents_dir=getattr(args, "launch_agents_dir", None),
            systemd_dir=getattr(args, "systemd_dir", None),
        )
        for out in installed:
            _print_install_result(out)
        for name, why in skipped:
            print(f"· {name}: 跳过自动安装（{why}）")
        installed_names = tuple(str(o.get("agent", "")) for o in installed)
    _print_report(report, header="agent-mailbox 初始化完成")
    _setup_hints(report, installed_names=installed_names, skipped=skipped)
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


def _cmd_upgrade(args: argparse.Namespace) -> int:
    """v0.7.6 E 单元（t-53）：查最新版 → 提示 → 确认后起外部命令升级。

    判据③ 禁 self-update：进程内不自替换代码，只 spawn uv tool / pipx /
    pip 的升级子命令；执行前必先显示完整命令；审计落 <root>/audit.log；
    判据⑤ fail-open：查不到最新版就到此为止，绝不影响信箱功能。
    """
    root = _root_from(args)
    info = version_mod.check_for_update(root, force=True)
    if info is None:
        print("⚠ 未能获取最新版（断网或 PyPI 不可达），本次不执行升级；信箱功能不受影响。")
        return 0
    if not info["update_available"]:
        print(f"已是最新版（当前 {info['current']}，PyPI 最新 {info['latest']}）。")
        return 0
    print(version_mod.format_update_notice(info))
    plan = version_mod.upgrade_plan()
    command_str = " ".join(plan["command"])
    if args.check:
        print("（--check 只查不升；执行升级请跑 agent-mailbox upgrade）")
        return 0
    print(f"将运行完整命令：{command_str}")  # 判据③ 执行前显示完整命令
    if not args.yes:
        if not sys.stdin.isatty():
            version_mod.run_upgrade(root, confirm=False, by="cli")  # dry-run 也留痕
            print("非交互终端：仅预览未执行；确认执行请加 --yes。")
            return 0
        try:
            confirmed = input("确认执行？[y/N]: ").strip().lower() in ("y", "yes", "是")
        except (EOFError, KeyboardInterrupt):
            confirmed = False
        if not confirmed:
            print("未执行（确认执行请重跑并回答 y，或加 --yes）。")
            return 0
    result = version_mod.run_upgrade(root, confirm=True, by="cli")
    if result["ok"]:
        print("✅ 升级完成（退出码 0）；重启 agent-mailbox 后新版本生效。")
        return 0
    code = result.get("exit_code")
    detail = code if code is not None else result.get("error", "?")
    print(f"❌ 升级失败（{detail}）；完整命令已留痕 <root>/audit.log。")
    return 1


# ------------------------------------------------------------------ doctor
#
# t-59（A-2）: `agent-mailbox doctor` 一条命令输出「哪段断了 + 怎么修」。
# 六检基座：① 信箱根可读 ② 唤醒器 ③ 最近一次唤醒是否真成功（区分「被
# 拉起」与「真消费」）④ 收件人路由指向谁 ⑤ 积压 ⑥ 宿主认证态。每项失败
# 必带可执行的下一步；全部正常输出健康摘要。日志存在才扫，不存在跳过
# 不报错。
# t-64（0.7.6 收口批第 1 批诊断面）：
#   G-1 ② 拆两行——「装载」= wake 单元文件在盘；「加载」= label 出现在
#       launchctl list / systemctl --user list-unit-files 实况（只读探测，
#       绝不 load/bootstrap）。装了没加载 = 链子是死的，加载缺失判 fail。
#   G-2 ⑦ breaker——<root>/wake-zc.breaker 存在且 age > 10 分钟判闩死
#       fail（输出 latched_at/rounds/pending_before→pending_after）；闩死
#       期间 ③ 不得报「唤醒正常」，顺带降级。
#   G-3 ⑧ 仓内/线上脚本一致——scripts/wake-zc.sh、scripts/resolve-
#       provider-config.sh 与 <root>/ 同名文件逐对 sha256 比对（只查不同
#       步，不同步——同步属合入批动作）。
# t-65（收口批第 2 批）：
#   ⑨ 告警投递可达性（G-5）——告警收件人（负责方 + 各箱发件人）逐人验证
#       ①已注册 ②inbox 目录存在 ③可写；只读探测，绝不真发信。
#   ④ 顺带孤儿进程只读提示（G-4 可选面）——该入口二进制有 PPID=1、存活
#       超阈值（>10 分钟）的残留 = 历史唤醒没被回收干净，只提示不判死。


DOCTOR_TITLES = (
    ("root", "① 信箱根可读"),
    ("wake_installed", "② 装载（wake 单元文件在盘）"),
    ("wake_loaded", "② 加载（launchctl/systemctl 实况）"),
    ("last_wake", "③ 最近一次唤醒"),
    ("routing", "④ 收件人路由"),
    ("backlog", "⑤ 积压"),
    ("host_auth", "⑥ 宿主认证态"),
    ("breaker", "⑦ 唤醒断路器（breaker）"),
    ("wake_scripts", "⑧ 仓内/线上脚本一致"),
    ("alert_reach", "⑨ 告警投递可达性"),
    ("entries", "⑩ 入口档体检"),
)

# ⑥ 宿主认证态的已知故障特征（对日志尾部逐行匹配；spawn_failed 用组提取
# 命令名，好给出指名道姓的修法）。
DOCTOR_LOG_SIGNATURES: tuple[tuple[str, re.Pattern[str], str, str | None], ...] = (
    (
        "auth_required",
        re.compile(r"authentication required|please use /login|未登录", re.IGNORECASE),
        "入口缺配置家/凭据（认证报错 ≠ 真需要 /login）",
        (
            "打开配置页面给该成员的入口补 config_env 与 model"
            "（如 codebuddy 的 CODEBUDDY_CONFIG_DIR=~/.workbuddy 与 "
            "--model custom-local:…），保存即生效，重跑 agent-mailbox doctor 复核。"
            "不要做任何 /login——本地自定义 provider 不需要账号登录（G-6 更正口径）。"
        ),
    ),
    (
        "spawn_failed",
        re.compile(r"spawn failed:.*?No such file or directory: '([^']+)'"),
        "唤醒命令不在 PATH/不存在",
        "",  # 运行时按捕获到的命令名拼装
    ),
    (
        "provider_missing",
        re.compile(r"无法定位 CLI"),
        "入口缺 provider 配置（非交互 shell 丢了 app 的 env）",
        (
            "该入口缺 provider 配置 → 配置页面补，或重跑 install 由产品探测注入；"
            "真源在 ~/.zcode/v2/provider_config.json 与 app 内 config/provider。"
        ),
    ),
)


def _doctor_read_tail(path: Path, max_bytes: int = 262144) -> list[str]:
    """读日志尾部（≤256KB），丢掉开头可能被截断的半行；文件不存在回 []。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = data.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # 丢弃被截断的半行
    return lines


def _doctor_wake_targets(cfg: wake_mod.WakeConfig | None) -> list[str]:
    """②③④ 的检查范围 = agents.<ID> 段 id ∪ wake.json agent_id。"""
    ids: dict[str, None] = {}
    if cfg:
        if cfg.agent_id:
            ids[cfg.agent_id] = None
        for k in cfg.agent_route_ids():
            ids[k] = None
    return list(ids)


def _doctor_route_problem(route: wake_mod.WakeRoute) -> tuple[str, str] | None:
    """④ 单身份路由体检：返回 (问题, 下一步) 或 None（健康）。"""
    if route.adapter == "local-command":
        if not route.command:
            return (
                "local-command 但没配 command",
                "wake install --agent <id> --command '[\"<绝对路径>\"]'（先 which <命令> 找绝对路径）",
            )
        head = route.command[0]
        if not os.path.isabs(head):
            return (
                f"command 首段 '{head}' 不是绝对路径（launchd 环境极简，PATH 不可信）",
                f"which {head} 找到实际位置，把绝对路径写进 agents.<id>.command",
            )
        # t-63（S3/S5）: 绝对路径但文件不在——「错路径命令」必须判出并给
        # 可直接粘贴执行的修法（非只报错误码）。
        if not Path(head).exists():
            name = Path(head).name
            fix = (
                f"which {name} 找到实际位置；或重装: "
                f"agent-mailbox setup --agent <id> --adapter local-command "
                f"--command '[\"<{name} 的正确绝对路径>\"]'"
            )
            return (f"command '{head}' 不存在（被移动/卸载或写错了路径）", fix)
        return None
    if route.adapter in ("hermes", "generic-webhook") and not route.webhook_url:
        return (
            "webhook 通道但没有 url（信到了也叫不醒人）",
            "wake install --agent <id> --webhook-url <url>，或检查 <root>/webhook.json",
        )
    return None


# G-4 孤儿探测阈值（秒）：存活超过 10 分钟的 PPID=1 残留才算样态（对齐
# 任务书验收「ps 里无超 10 分钟的残留」）。
DOCTOR_ORPHAN_MIN_ETIME_S = 600.0


def _parse_ps_etime(s: str) -> float:
    """ps 的 etime（``[[dd-]hh:]mm:ss``）→ 秒；解析不了回 0（宁可不报）。"""
    try:
        days = 0
        core = str(s)
        if "-" in core:
            days, core = core.split("-", 1)
            days = int(days)
        parts = [int(x) for x in core.split(":") if x != ""]
        while len(parts) < 3:
            parts.insert(0, 0)
        return days * 86400 + parts[-3] * 3600 + parts[-2] * 60 + parts[-1]
    except (ValueError, AttributeError):
        return 0.0


def _doctor_probe_key(command: list[str]) -> str:
    """④ 孤儿探测的匹配键：经唤醒包装脚本起的命令匹配**脚本路径**而非
    通用 shell（``/bin/bash /root/wake-cmd-X.sh`` 若匹配 bash 会满屏误报）。"""
    head = str(command[0]) if command else ""
    if Path(head).name in ("bash", "sh", "zsh", "dash") and len(command) > 1:
        return str(command[1])
    return head


def _doctor_orphan_rows() -> list[tuple[str, str, str]]:
    """只读 ps 探测（G-4）：返回 PPID=1 的 ``(pid, etime, command)`` 行。

    被收养（PPID=1）正是 G-4 病灶样态——旧式裸 kill PID 看门狗把孙进程
    留给了 launchd。绝不 kill、只读；探测失败（ps 缺席等）回空列表。"""
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,etime=,command="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except Exception:  # noqa: BLE001 — 探测失败 = 无样本，不挡其他判定
        return []
    rows: list[tuple[str, str, str]] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) == 4 and parts[1] == "1":
            rows.append((parts[0], parts[2], parts[3]))
    return rows


def _doctor_last_attempts(root: Path) -> dict[str, dict[str, Any]]:
    """③ 读 <root>/wake-attempts.jsonl 尾部，取每个 agent 的最后一行。"""
    rows: dict[str, dict[str, Any]] = {}
    for line in _doctor_read_tail(root / wake_mod.WAKE_ATTEMPTS_FILE):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("agent"):
            rows[str(row["agent"])] = row
    return rows


def _doctor_scan_log(path: Path) -> dict[str, Any] | None:
    """扫一个日志尾部，返回 {exists, hits:[{class,label,count,line,next_step}]}。"""
    if not path.is_file():
        return None
    lines = _doctor_read_tail(path)
    hits: dict[str, dict[str, Any]] = {}
    for line in lines:
        for sig_class, pattern, label, next_step in DOCTOR_LOG_SIGNATURES:
            m = pattern.search(line)
            if not m:
                continue
            if sig_class == "spawn_failed":
                cmd = m.group(1)
                step = (
                    f"'{cmd}' 不在 PATH/不存在 → 先 which {cmd} 找到实际位置，"
                    "然后把绝对路径写进 wake.json 的 agents.<id>.command"
                    "（wake install --agent <id> --command，JSON 数组、首段用绝对路径）"
                )
            else:
                step = next_step or "查看完整日志定位上下文。"
            hit = hits.setdefault(
                sig_class,
                {"class": sig_class, "label": label, "count": 0, "line": "", "next_step": step},
            )
            hit["count"] += 1
            hit["line"] = line.strip()[:200]
    return (
        {"path": str(path), "hits": list(hits.values())}
        if hits
        else {"path": str(path), "hits": []}
    )


# ---- t-64 诊断面（G-1 装/加载分行 · G-2 breaker · G-3 仓内/线上 sha）----

# G-2: breaker 闩死判据——文件存在且 age 超过该秒数（10 分钟）判 fail。
DOCTOR_BREAKER_MAX_AGE_S = 600.0
# G-3: 比对对（仓内 scripts/<name> vs 部署 <root>/<name>）。只查不同步。
DOCTOR_WAKE_SCRIPT_FILES = ("wake-zc.sh", "resolve-provider-config.sh")
# 仓内基准目录：干净工作树下 scripts/ 文件 == 当前分支 HEAD 版本（工作树
# 在施工单红线里收工必须干净）；不引 git 依赖——wheel 安装没有 .git，该
# 目录不存在时 ⑧ 降级为「跳过比对」而不是误报。CLI 测试 monkeypatch 此常量。
DOCTOR_REPO_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"


def _doctor_unit_path(
    agent_id: str,
    launch_agents_dir: Path | None,
    systemd_dir: Path | None,
) -> Path | None:
    """G-1「装」的判物：该身份的 wake 单元文件路径（None = 平台无法判装态）。"""
    if sys.platform == "darwin":
        return (
            Path(launch_agents_dir or (Path.home() / "Library" / "LaunchAgents"))
            / f"{wake_mod.WAKE_LABEL}-{agent_id}.plist"
        )
    if sys.platform == "linux":
        return (
            Path(systemd_dir or (Path.home() / ".config" / "systemd" / "user"))
            / f"{wake_mod.WAKE_LABEL}-{agent_id}.path"
        )
    return None  # 其他平台：run 手动模式，安装态无从判起


def _doctor_query_loaded_labels(platform: str = "") -> tuple[set[str], str]:
    """G-1「加载」实况：只读探测已加载的唤醒单元 label。

    返回 (label 集合, 探测状态)；状态 ``ok`` / ``unavailable``（命令失败/
    不存在）/ ``unsupported``（平台不认识）。macOS 走 ``/bin/launchctl
    list``（launchd 环境 PATH 不可信，写死绝对路径），Linux 走
    ``systemctl --user list-unit-files``。**只 list 绝不 load/bootstrap**
    ——装维写操作是老板权限面，诊断面只读。测试注入：
    monkeypatch 本函数或向 :func:`doctor_report` 传 ``loaded_probe``。
    """
    import subprocess

    platform = platform or sys.platform
    try:
        if platform == "darwin":
            proc = subprocess.run(
                ["/bin/launchctl", "list"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,  # 探测失败按 unavailable 降级，不抛
            )
        elif platform == "linux":
            proc = subprocess.run(
                ["systemctl", "--user", "list-unit-files", "--no-legend"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        else:
            return set(), "unsupported"
    except (OSError, subprocess.SubprocessError):
        return set(), "unavailable"
    if proc.returncode != 0:
        return set(), "unavailable"
    # launchctl list 行 = "PID 状态 Label"；systemd 行 = "单元名 状态"。
    # 统一用正则抠 WAKE_LABEL 开头的 token（systemd 单元带 .path/.service
    # 后缀也一并捕获，加载判定按前缀匹配兜住）。
    pat = re.compile(re.escape(wake_mod.WAKE_LABEL) + r"-\S*")
    return set(pat.findall(proc.stdout or "")), "ok"


def _doctor_loaded_probe(
    loaded_probe: Callable[[], tuple[set[str], str]] | None,
) -> tuple[set[str], str]:
    """加载探测统一入口：注入优先，缺省走真实只读探测。"""
    if loaded_probe is not None:
        return loaded_probe()
    return _doctor_query_loaded_labels()


def _doctor_breaker_fields(text: str) -> dict[str, str]:
    """G-2: 解析 breaker 文件的 key=value 键值。

    特例：``latched_at=2026-09-29 16:08:10`` 的值含空格——按 token 扫，
    带 ``=`` 的 token 开新键，不带 ``=`` 的 token 续填上一个键的值。
    """
    fields: dict[str, str] = {}
    last_key = ""
    for line in text.splitlines():
        for token in line.split():
            if "=" in token:
                key, _, value = token.partition("=")
                if key:
                    fields[key] = value
                    last_key = key
            elif last_key:
                fields[last_key] += " " + token
    return fields


def _doctor_breaker_state(root: Path) -> dict[str, Any] | None:
    """G-2: 读 <root>/wake-zc.breaker，返回状态面。

    文件不存在回 None（无闩死样本）；存在时回
    ``{"fields": {…}, "age_s": float, "status": "fresh"|"latched"}``。
    age 优先取 ``latched_at``（本地时间，与写入方 ``date '+%F %T'`` 同
    源），解析不了退回文件 mtime——两类「自造闩死样本」（改 latched_at /
    改 mtime）都能命中。
    """
    from datetime import datetime, timezone

    path = root / "wake-zc.breaker"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        mtime = path.stat().st_mtime
    except OSError:
        return None
    fields = _doctor_breaker_fields(text)
    age_s: float
    latched = fields.get("latched_at", "")
    try:
        # latched_at 是 wake-zc.sh 用 `date '+%F %T'` 写的本地钟面时刻；
        # naive 解析后 .astimezone() 挂本地时区，与 UTC now 相减得真间隔。
        latched_dt = datetime.strptime(latched, "%Y-%m-%d %H:%M:%S").astimezone()
        age_s = max(0.0, (datetime.now(timezone.utc) - latched_dt).total_seconds())
    except ValueError:
        from time import time

        age_s = max(0.0, time() - mtime)
    return {
        "path": str(path),
        "fields": fields,
        "age_s": age_s,
        "status": "latched" if age_s > DOCTOR_BREAKER_MAX_AGE_S else "fresh",
    }


def _doctor_file_digest(path: Path) -> tuple[str, int]:
    """G-3: 返回 (sha256 前 8 位短值, 行数)。读不了回 ("?", 0)。"""
    import hashlib

    try:
        data = path.read_bytes()
        return hashlib.sha256(data).hexdigest()[:8], len(data.decode("utf-8").splitlines())
    except OSError:
        return "?", 0


def _doctor_script_pairs(root: Path, repo_scripts_dir: Path) -> tuple[list[str], bool]:
    """G-3: 仓内/线上脚本逐对 sha256 比对。回 (摘要行, 是否有坏对)。

    只比对不同步（同步属合入批，doctor 不写部署面）；仓内基准缺失
    （wheel 安装无 scripts/）降级为跳过不误报。
    """
    rows: list[str] = []
    bad = False
    for name in DOCTOR_WAKE_SCRIPT_FILES:
        repo_f = repo_scripts_dir / name
        dep_f = root / name
        if not repo_f.is_file():
            rows.append(f"{name}: 仓内基准缺失（{repo_scripts_dir}），跳过比对")
            continue
        repo_sha, repo_lines = _doctor_file_digest(repo_f)
        if not dep_f.is_file():
            rows.append(f"{name}: 线上未部署（仓内 {repo_sha}({repo_lines}行) vs 线上 缺）")
            bad = True
            continue
        dep_sha, dep_lines = _doctor_file_digest(dep_f)
        if repo_sha == dep_sha and repo_sha != "?":
            rows.append(f"{name}: 一致（{repo_sha}…/{repo_lines}行）")
        else:
            rows.append(
                f"{name}: 不一致 仓内 {repo_sha}({repo_lines}行) vs 线上 {dep_sha}({dep_lines}行)"
            )
            bad = True
    return rows, bad


def doctor_report(
    root: Path,
    *,
    wb_wake_log: Path | None = None,
    launch_agents_dir: Path | None = None,
    systemd_dir: Path | None = None,
    loaded_probe: Callable[[], tuple[set[str], str]] | None = None,
    repo_scripts_dir: Path | None = None,
    orphan_probe: Callable[[], list[tuple[str, str, str]]] | None = None,
) -> dict[str, Any]:
    """doctor 诊断面数据（t-59 六检基座 + t-64 ⑦⑧ + t-65 ⑨）。``--json``
    原样输出；人读格式见 :func:`_print_doctor`。任何可执行环境差异（wb 日志
    路径 / plist 目录 / systemd 目录）都留了参数位，测试用 tmp 替身，不碰
    真机状态。t-64 追加两个注入点：``loaded_probe``（G-1 加载实况探测，测试
    monkeypatch 替身，不真跑 launchctl）与 ``repo_scripts_dir``（G-3 仓内
    基准目录，测试用 tmp 替身）。t-65 追加 ``orphan_probe``（G-4 孤儿进程
    只读探测替身，测试不真跑 ps）。"""
    root = Path(root)
    cfg = wake_mod.WakeConfig.load(root)
    targets = _doctor_wake_targets(cfg)
    checks: list[dict[str, Any]] = []
    boxes: list[str] = []  # ① 填充；② 的「身份不在唤醒名单」检查复用
    # G-2: breaker 状态先读出——⑦ 直接用；③ 的「唤醒正常」也要被它按住。
    breaker = _doctor_breaker_state(root)

    def _check(cid: str, ok: bool, detail: str, next_step: str = "") -> dict[str, Any]:
        c = {"id": cid, "ok": ok, "detail": detail}
        if next_step:
            c["next_step"] = next_step
        checks.append(c)
        return c

    # ① 信箱根可读
    if root.is_dir() and os.access(root, os.R_OK):
        inbox_root0 = root / "inbox"
        boxes = (
            sorted(p.name for p in inbox_root0.glob("*") if p.is_dir())
            if inbox_root0.is_dir()
            else []
        )
        _check(
            "root",
            True,
            f"可读；inbox 有 {len(boxes)} 个身份箱"
            + (f"：{' '.join(boxes[:12])}" if boxes else ""),
        )
    else:
        _check(
            "root",
            False,
            f"{root} 不存在或不可读",
            "确认 AGENT_MAIL_HOME/--home 指向对的根；首次使用先跑 agent-mailbox setup --yes",
        )

    # ② 装/加载分开报（G-1，两行两判）：「装」= wake 单元文件在盘；
    # 「加载」= label 出现在 launchctl/systemctl 实况。装了没加载 = 链子
    # 是死的（收口批实况：WB 在盘未加载），加载缺失判 fail 并点名 label。
    if not cfg:
        _check(
            "wake_installed",
            False,
            f"{root}/wake.json 不存在（唤醒器从未安装）",
            "python -m agent_mailbox.wake install --agent <id> 安装（macOS 写 launchd plist / Linux 写 systemd path unit）",
        )
        _check(
            "wake_loaded",
            False,
            "无 wake.json，加载态无从谈起（同 ② 装载）",
            "同 ② 装载：先 install 再复核加载",
        )
    elif not targets:
        _check(
            "wake_installed",
            False,
            "wake.json 存在但没有 agents.<ID> 身份段，也没有 agent_id",
            "wake install --agent <id> --adapter <通道> 逐身份安装",
        )
        _check(
            "wake_loaded",
            False,
            "没有可查加载态的唤醒身份（同 ② 装载）",
            "同 ② 装载",
        )
    else:
        # ② 装：单元文件在盘
        installed: list[str] = []
        not_installed: list[str] = []
        for tid in targets:
            unit = _doctor_unit_path(tid, launch_agents_dir, systemd_dir)
            (installed if (unit is None or unit.exists()) else not_installed).append(tid)
        if not_installed:
            _check(
                "wake_installed",
                False,
                f"已装 {len(installed)}/{len(targets)}（{', '.join(installed) or '无'}）；"
                f"未装: {' '.join(not_installed)}",
                "对缺失身份补跑 python -m agent_mailbox.wake install --agent <id>",
            )
        else:
            # t-63（S3/S5）「身份不在唤醒名单」: 收件箱里有**待处理信**的身份却
            # 不在 wake.json 唤醒名单里 ⇒ 信到了永远没人被叫醒。人类席（owner
            # kind）不在此列；agent/guest 席必须补注册+补装（给可直接粘贴的
            # setup 命令，setup 会自动接线 CLI 形态身份）。
            orphans: list[str] = []
            try:
                from .store import MailStore as _MS

                _st = _MS(root)
                for b in boxes:
                    if b in targets:
                        continue
                    try:
                        if _st.kind_of(b) == "owner":
                            continue
                        has_mail = any(
                            json.loads(p.read_text(encoding="utf-8")).get("status")
                            in ("pending", "acked")
                            for p in (root / "inbox" / b).glob("*.json")
                        )
                    except (OSError, json.JSONDecodeError):
                        has_mail = False
                    if has_mail:
                        orphans.append(b)
            except Exception:  # noqa: BLE001 — 孤儿检查 fail-open，不挡其他判定
                orphans = []
            if orphans:
                _check(
                    "wake_installed",
                    False,
                    f"身份 {', '.join(orphans)} 收件箱有信但不在唤醒名单（信到了永远没人被叫醒）",
                    "；".join(f"agent-mailbox setup --agent {o}" for o in orphans)
                    + "（setup 自动注册+接线 CLI 形态身份）",
                )
            else:
                kind = (
                    "launchd plist"
                    if sys.platform == "darwin"
                    else ("systemd unit" if sys.platform == "linux" else "单元文件")
                )
                _check(
                    "wake_installed",
                    True,
                    f"{kind} 已装 {len(targets)}/{len(targets)}：{' '.join(targets)}",
                )
        # ② 加载：launchctl/systemctl 实况（只读探测）
        labels, probe = _doctor_loaded_probe(loaded_probe)
        expected = [f"{wake_mod.WAKE_LABEL}-{tid}" for tid in targets]
        if probe != "ok":
            _check(
                "wake_loaded",
                True,
                f"加载态无法探测（{probe}）——不作判据，请人工 launchctl list / "
                "systemctl --user list-unit-files 复核",
            )
        else:
            not_loaded = [
                lbl
                for lbl in expected
                if lbl not in labels and not any(l.startswith(lbl + ".") for l in labels)
            ]
            if not_loaded:
                _check(
                    "wake_loaded",
                    False,
                    f"已加载 {len(expected) - len(not_loaded)}/{len(expected)}"
                    f"（{', '.join(l for l in expected if l not in not_loaded) or '无'}）；"
                    f"缺失: {' '.join(not_loaded)}——单元在盘没加载 = 链子死的",
                    "对缺失身份补跑 python -m agent_mailbox.wake install --agent <id>；"
                    "launchctl load/bootstrap 属装维写操作，由本人执行（诊断面只读探测）",
                )
            else:
                _check(
                    "wake_loaded",
                    True,
                    f"launchctl/systemctl 实况已加载 {len(expected)}/{len(expected)}：{' '.join(expected)}",
                )

    # ③ 最近一次唤醒是否真成功（被拉起 ≠ 真消费）
    attempts = _doctor_last_attempts(root)
    if not attempts:
        _check(
            "last_wake",
            True,
            "无唤醒记录（wake-attempts.jsonl 不存在或为空——从未触发过不算故障）",
            "",
        )
    else:
        bad: list[str] = []
        bad_ids: list[str] = []
        bad_errs: set[str] = set()
        good: list[str] = []
        for agent_id, row in sorted(attempts.items()):
            outcome = str(row.get("outcome", ""))
            err = str(row.get("error_class", ""))
            ts = str(row.get("ts", ""))
            if outcome == "ok":
                good.append(agent_id)
            else:
                why = (
                    "被拉起但没真消费（belt 判定无进展）＝假成功"
                    if err == "no_progress"
                    else f"投递失败（error_class={err or '?'})"
                )
                bad.append(f"{agent_id} @ {ts}: {why}")
                bad_ids.append(agent_id)
                bad_errs.add(err)
        if bad:
            # t-63（S3/S5）: 每个错误类给人话根因 + 可直接粘贴执行的验证命令
            # （带真实 agent id，非占位符）。
            fixes: list[str] = []
            if "auth_required" in bad_errs:
                fixes.append(
                    "入口缺配置家/凭据 → 配置页面补 config_env 与 model"
                    "（本地 provider 不需要 /login，G-6 口径）"
                )
            if "spawn_failed" in bad_errs:
                fixes.append(
                    "唤醒命令不在 PATH/不存在 → "
                    "agent-mailbox setup --agent <id> --adapter local-command --command '[\"<绝对路径>\"]'"
                )
            fixes.append("查日志: <root>/wake-daemon.log 与 ⑥ 的判定")
            fixes.append(f"修完手动验证一轮: agent-mailbox wake run --agent {bad_ids[0]} --once")
            _check(
                "last_wake",
                False,
                "；".join(bad) + (f"；正常: {' '.join(good)}" if good else ""),
                "；".join(fixes),
            )
        else:
            _check(
                "last_wake", True, f"最近一次均真成功：{' '.join(good)}（wake-attempts.jsonl 尾部）"
            )

    # ③ 顺带降级（G-2）：breaker 闩死期间「唤醒正常」不可信——即便
    # attempts 全 ok 也压成 fail 并说明，绝不让 ⑦ 闩死而 ③ 独绿。
    if breaker and breaker["status"] == "latched":
        lw = next((c for c in checks if c["id"] == "last_wake"), None)
        if lw is not None:
            bf = breaker["fields"]
            lw["ok"] = False
            lw["detail"] += (
                f"；⚠ breaker 已闩死（latched_at={bf.get('latched_at', '?')} "
                f"rounds={bf.get('rounds', '?')}）——「唤醒正常」不可信，见⑦"
            )
            lw["next_step"] = (
                "先按⑦处理 breaker（复位需老板授权且留痕：时刻+命令+"
                "pending_before→pending_after），再手动 wake run --agent <id> --once 复核"
            )

    # ④ 收件人路由（依赖 A-1 的 effective_route 解析）
    if not targets:
        _check(
            "routing",
            False,
            "没有可检查的唤醒身份（wake.json 无 agents 段/agent_id）",
            "wake install --agent <id> 逐身份配置；多身份各回各家靠 agents.<ID> 段",
        )
    else:
        route_lines: list[str] = []
        route_bad: list[tuple[str, str, str]] = []
        probe_keys: set[str] = set()  # G-4 孤儿探测的匹配键（存在的入口路径）
        for tid in targets:
            route = (cfg or wake_mod.WakeConfig({}, root)).effective_route(tid)
            desc = route.adapter
            if route.command:
                desc += f" cmd={route.command[0]}"
            if route.webhook_url:
                desc += f" url={mask_url(route.webhook_url)}"
            # 来源全量展示（t-58 的 sources）：adapter/webhook_url/command 各是
            # 谁配的——per-agent 段、全局默认还是 CLI，一眼分辨「信到了叫谁」。
            srcs = ",".join(
                f"{k}={route.sources[k]}"
                for k in ("adapter", "webhook_url", "command")
                if k in route.sources
            )
            route_lines.append(f"{tid}→{desc}（{srcs or '内置默认'}）")
            problem = _doctor_route_problem(route)
            if problem:
                route_bad.append((tid, problem[0], problem[1]))
            elif route.adapter == "local-command" and route.command:
                key = _doctor_probe_key([str(a) for a in route.command])
                if key and Path(key).exists():
                    probe_keys.add(key)
        # G-4（可选提示）: 孤儿进程只读探测——该入口有 PPID=1、存活超阈值的
        # 残留 = 历史唤醒的看门狗没回收干净（旧式裸 kill PID → 孙进程被
        # launchd 收养）。只提示不判死；真回收属本机属主动作，诊断面不动手。
        orphan_note = ""
        if probe_keys:
            try:
                rows = (orphan_probe or _doctor_orphan_rows)()
            except Exception:  # noqa: BLE001 — 探测失败不挡路由判定
                rows = []
            hits = [
                f"pid={pid} etime={et} cmd={cmd[:80]}"
                for pid, et, cmd in rows
                if _parse_ps_etime(et) >= DOCTOR_ORPHAN_MIN_ETIME_S
                and any(k in cmd for k in probe_keys)
            ]
            if hits:
                orphan_note = (
                    f"；⚠ 疑似孤儿残留（PPID=1，>{DOCTOR_ORPHAN_MIN_ETIME_S / 60:g}min 未回收，"
                    "看门狗进程组语义未覆盖历史唤醒）: " + "；".join(hits[:5])
                )
        if route_bad:
            _check(
                "routing",
                False,
                "；".join(f"{t}: {p}" for t, p, _ in route_bad)
                + "；"
                + "；".join(route_lines)
                + orphan_note,
                route_bad[0][2],
            )
        else:
            _check("routing", True, "；".join(route_lines) + orphan_note)

    # ⑤ 积压（各身份 pending 计数）
    backlog_lines: list[str] = []
    pending_total = 0
    inbox_root = root / "inbox"
    if inbox_root.is_dir():
        for box in sorted(p for p in inbox_root.glob("*") if p.is_dir()):
            pending = acked = other = 0
            try:
                for letter in box.glob("*.json"):
                    try:
                        m = json.loads(letter.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    status = m.get("status") if isinstance(m, dict) else None
                    if status == "pending":
                        pending += 1
                    elif status == "acked":
                        acked += 1
                    else:
                        other += 1
            except OSError as exc:
                _check(
                    "backlog", False, f"{box.name} 收件箱不可读: {exc}", "检查目录权限后重跑 doctor"
                )
                break
            if pending or acked:
                backlog_lines.append(f"{box.name}: pending={pending} acked={acked}")
                pending_total += pending
        else:
            _check(
                "backlog",
                True,
                (
                    f"待处理 {pending_total} 封（" + "; ".join(backlog_lines[:10]) + "）"
                    if backlog_lines
                    else "全部身份 0 待处理"
                ),
                "pending 长期不降 = 唤醒链路断了，看 ②③⑥ 的判定" if backlog_lines else "",
            )
    else:
        _check("backlog", True, "inbox 目录不存在（还没有信）")

    # ⑥ 宿主认证态（扫 wake 日志尾部；存在才扫，不存在跳过不报错）
    logs = [
        root / "wake-zc.log",
        root / "wake-daemon.log",
        wb_wake_log or (Path.home() / ".workbuddy" / "wb-wake" / "wake.log"),
    ]
    scanned: list[str] = []
    findings: list[dict[str, Any]] = []
    for log_path in logs:
        result = _doctor_scan_log(log_path)
        if result is None:
            continue  # 日志不存在：跳过，不报错
        scanned.append(str(log_path))
        findings.extend(result["hits"])
    if findings:
        parts = [
            f"{h['label']}×{h['count']}（{'…' + h['line'][-80:] if h['line'] else ''}）"
            for h in findings
        ]
        steps: list[str] = []
        for h in findings:
            if h["next_step"] and h["next_step"] not in steps:
                steps.append(h["next_step"])
        _check(
            "host_auth",
            False,
            f"扫过 {'; '.join(scanned)}；" + "；".join(parts),
            " → ".join(steps) if steps else "查看完整日志",
        )
    else:
        _check(
            "host_auth",
            True,
            f"扫过 {'; '.join(scanned)}，未发现未登录/命令缺失特征"
            if scanned
            else "无 wake 日志可扫（从未跑过唤醒）",
        )

    # ⑦ 唤醒断路器（G-2）：文件存在且 age > 10 分钟 = 闩死，判 fail 并
    # 原样吐出 latched_at / rounds / pending_before→pending_after；
    # 新鲜样本（≤10 分钟冷却窗）只提示不判死；无文件 = 无闩死样本。
    if breaker is None:
        _check("breaker", True, "无闩死样本（wake-zc.breaker 不存在）")
    elif breaker["status"] == "latched":
        bf = breaker["fields"]
        _check(
            "breaker",
            False,
            f"闩死 age≈{breaker['age_s']:.0f}s > {DOCTOR_BREAKER_MAX_AGE_S:.0f}s；"
            f"latched_at={bf.get('latched_at', '?')} rounds={bf.get('rounds', '?')} "
            f"pending={bf.get('pending_before', '?')}→{bf.get('pending_after', '?')}"
            f"（{breaker['path']}）",
            "复位 rm <root>/wake-zc.breaker 需老板授权且必须留痕（复位时刻+命令+"
            "pending_before→pending_after）；先看③⑥定位空转根因再复位",
        )
    else:
        bf = breaker["fields"]
        _check(
            "breaker",
            True,
            f"闩死文件存在但新鲜（age≈{breaker['age_s']:.0f}s ≤ {DOCTOR_BREAKER_MAX_AGE_S:.0f}s "
            f"冷却窗，暂不判闩死）：latched_at={bf.get('latched_at', '?')} "
            f"rounds={bf.get('rounds', '?')} "
            f"pending={bf.get('pending_before', '?')}→{bf.get('pending_after', '?')}；"
            "持续存在请复位或排查③",
        )

    # ⑧ 仓内/线上脚本一致（G-3）：scripts/<name>（干净工作树 = HEAD 版本）
    # vs <root>/<name> 逐对 sha256 + 行数。只查不同步——同步属合入批动作，
    # 诊断面绝不写部署面。
    repo_scripts = Path(repo_scripts_dir) if repo_scripts_dir else DOCTOR_REPO_SCRIPTS_DIR
    script_rows, script_bad = _doctor_script_pairs(root, repo_scripts)
    if script_bad:
        _check(
            "wake_scripts",
            False,
            "；".join(script_rows),
            "把仓内 scripts/<name> 同步到 <root>/<name>（tmp+rename 原子写，先备份线上副本）；"
            "同步动作归合入批，本项只诊断",
        )
    else:
        _check("wake_scripts", True, "；".join(script_rows))

    # ⑨ 告警投递可达性（G-5）: 「告警的告警断了」的防线——告警收件人 =
    # 负责方（WAKE_ALERT_RECIPIENT）+ 各箱信件里出现过的发件人（
    # send_wake_alert 的收件人集合）。逐人验证 ①已注册 ②inbox 目录存在
    # ③可写；只读探测，绝不真发信。无信件且无 registry = 无告警样本，
    # 不作判据（免得空机器被误判）。registry 直读（MailStore 构造器会
    # mkdir，诊断面零写盘）。
    try:
        _reg_raw = json.loads((root / "registry.json").read_text(encoding="utf-8"))
        _agents_raw = _reg_raw.get("agents", {}) if isinstance(_reg_raw, dict) else {}
        _reg_agents = {k for k, v in _agents_raw.items() if isinstance(v, dict)}
    except (OSError, json.JSONDecodeError, ValueError):
        _reg_agents = set()
    alert_senders: set[str] = set()
    _inbox_root = root / "inbox"
    if _inbox_root.is_dir():
        for _box in sorted(_inbox_root.glob("*")):
            if not _box.is_dir():
                continue
            for _n, _p in enumerate(sorted(_box.glob("*.json"))):
                if _n >= 200:  # 每箱采样上限：可达性判定不需要全量扫描
                    break
                try:
                    _f = str(json.loads(_p.read_text(encoding="utf-8")).get("from", "") or "")
                except (OSError, json.JSONDecodeError):
                    continue
                if _f:
                    alert_senders.add(_f)
    if not alert_senders and not _reg_agents:
        _check("alert_reach", True, "无告警收件人样本（无信件且无 registry）——无可达性判据")
    else:
        _alert_targets = sorted(alert_senders | {wake_mod.WAKE_ALERT_RECIPIENT})
        _alert_bad: list[str] = []
        _alert_ok: list[str] = []
        for _aid in _alert_targets:
            _dir = root / "inbox" / _aid
            if _aid not in _reg_agents:
                _alert_bad.append(f"{_aid}: 未注册（告警无落点）")
            elif not _dir.is_dir():
                _alert_bad.append(f"{_aid}: inbox 目录缺失")
            elif not (os.access(_dir, os.W_OK | os.X_OK) and os.access(_dir, os.R_OK)):
                _alert_bad.append(f"{_aid}: inbox 目录不可写")
            else:
                _alert_ok.append(_aid)
        if _alert_bad:
            _check(
                "alert_reach",
                False,
                "；".join(_alert_bad) + (f"；可达: {' '.join(_alert_ok)}" if _alert_ok else ""),
                "未注册 → agent-mailbox setup --agent <id> 补注册（setup 自动建箱）；"
                "目录缺失/不可写 → 检查 <root>/inbox/<id> 的存在与权限后重跑 doctor；"
                "可达性只读探测，投递确认见 alerts.deliver_confirmed（send 后回读）",
            )
        else:
            _check(
                "alert_reach",
                True,
                f"告警收件人均可达（已注册 + inbox 在且可写）: {' '.join(_alert_ok)}"
                "（只读探测, 未真发信）",
            )

        # ⑩ 入口档三级体检（G-6）: 每条入口的配置家/二进制按「在不在+通不通」
        # 逐条点名（人话 + 可操作指引），负例（配置家改错）由此现形。
        _entry_problems: list[str] = []
        _entry_ok: list[str] = []
        for _aid2 in targets:
            for _eid, _prof in sorted(cfg.agent_entries(_aid2).items()):
                _label2 = f"{_aid2}/{_eid}"
                _bad2 = ""
                _bin = str(_prof.get("binary") or "")
                if _bin and not _bin.startswith("/"):
                    _bad2 = f"二进制不是绝对路径（{_bin}）"
                elif _bin and not Path(_bin).exists():
                    _bad2 = f"二进制不存在（{_bin}）"
                for _k, _v in sorted((_prof.get("config_env") or {}).items()):
                    _vp = str(_v)
                    if _vp.startswith("/") and not _vp.endswith(".json") and not Path(_vp).is_dir():
                        _bad2 = f"配置家 {_k} 指向不存在的目录（{_vp}）"
                    elif _vp.startswith("/") and _vp.endswith(".json") and not Path(_vp).is_file():
                        _bad2 = f"配置家 {_k} 指向不存在的文件（{_vp}）"
                if _bad2:
                    _entry_problems.append(
                        f"{_label2}: {_bad2} → 打开配置页面给 {_label2} 补配置家/二进制后重跑 install"
                    )
                else:
                    _entry_ok.append(_label2)
        if _entry_problems:
            _check(
                "entries",
                False,
                "；".join(_entry_problems)
                + (f"；正常: {' '.join(_entry_ok)}" if _entry_ok else ""),
                "入口断开项在配置页面按成员逐条补（config_env/binary），"
                "补完重跑 agent-mailbox setup --agent <id> --entry <app|cli> 复绿。",
            )
        elif _entry_ok:
            _check(
                "entries",
                True,
                f"入口档全部健康（配置家/二进制在位）: {' '.join(_entry_ok)}",
            )

    unhealthy = [c for c in checks if not c["ok"]]
    return {
        "root": str(root),
        "checked_at_local": now_local(),
        "healthy": not unhealthy,
        "unhealthy_count": len(unhealthy),
        "checks": checks,
    }


def _print_doctor(report: dict[str, Any]) -> None:
    print(f"agent-mailbox doctor  （检查时间 {report['checked_at_local']}）")
    print(f"邮件根: {report['root']}\n")
    title_by_id = dict(DOCTOR_TITLES)
    for check in report["checks"]:
        mark = "✅" if check["ok"] else "❌"
        print(f"{title_by_id.get(check['id'], check['id'])}  {mark}  {check['detail']}")
        if check.get("next_step"):
            print(f"   → 下一步: {check['next_step']}")
    if report["healthy"]:
        print(f"\n结论: {len(report['checks'])} 检全过，唤醒链路健康。")
    else:
        print(
            f"\n结论: {report['unhealthy_count']} 项待修——逐条按上方「下一步」处理后重跑 "
            "agent-mailbox doctor 复核。"
        )


def _cmd_doctor(args: argparse.Namespace) -> int:
    root = _root_from(args)
    report = doctor_report(
        root,
        wb_wake_log=Path(args.wb_wake_log).expanduser()
        if getattr(args, "wb_wake_log", "")
        else None,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_doctor(report)
    return 0 if report["healthy"] else 1


# ------------------------------------------------------------------ digest
#
# t-62（判据7/S4）: `agent-mailbox digest` — LLM 可选的纯本地信件流转。
# 零网络、零 LLM、零 subprocess：读信 → 写摘要 → 标 done → 列「建议回复」。
# 引擎在 digest.py；本命令是手动触发面（自动降级在 wake.run_once）。


def _cmd_digest(args: argparse.Namespace) -> int:
    from .digest import run_digest  # local import: digest 是可选路径，保持 CLI 引入轻

    root = _root_from(args)
    if getattr(args, "agent", ""):
        agents = [str(args.agent)]
    else:
        inbox = root / "inbox"
        agents = sorted(p.name for p in inbox.glob("*") if p.is_dir()) if inbox.is_dir() else []
    results = [run_digest(root, a) for a in agents]
    total = sum(int(r.get("digested") or 0) for r in results)
    if getattr(args, "json", False):
        print(
            json.dumps(
                {"root": str(root), "total": total, "results": results},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(f"digest 完成（纯本地 · 零网络 · 零 LLM）：消化 {total} 封")
    for r in results:
        if r.get("digest_file"):
            print(f"  · {r['agent']}: {r['digested']} 封 → {r['digest_file']}")
    suggestions = [line for r in results for line in r.get("suggest_replies", [])]
    if suggestions:
        print("建议回复清单（占位 · 要不要回由人拍板，未调用 LLM）:")
        for line in suggestions:
            print(f"  {line}")
    elif total == 0:
        print("没有待处理的信（pending 为 0，无需摘要）。")
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

    p = sub.add_parser(
        "setup",
        parents=[common],
        help="初始化 + 一条命令装完（t-61 判据1/6：零输入自动发现接线；--agent X 显式装一个）",
    )
    p.add_argument("--yes", action="store_true", help="无头模式：全默认、纯命令行")
    p.add_argument(
        "--agent",
        default="",
        help="显式装这一个身份（配合 --adapter/--command/--webhook-url）；缺省=零输入自动发现接线",
    )
    p.add_argument(
        "--entry",
        default="",
        choices=("app", "cli"),
        help="入口类型（G-6 入口档：app|cli）——按名字+入口重装 = 配置页负例复绿的 CLI 面",
    )
    p.add_argument(
        "--adapter",
        default="",
        help="hermes | generic-webhook | local-command | claude-code（缺省时按 discovery 自动选）",
    )
    p.add_argument(
        "--command",
        default="",
        help='local-command argv JSON（如 \'["codex","--pull"]\'）——首段自动解析绝对路径，'
        "zcode 类命令自动生成带 provider env 注入的唤醒命令模板",
    )
    p.add_argument("--webhook-url", default="", help="webhook 通道 url（写 agents.<ID> 段）")
    p.add_argument("--webhook-secret", default="", help="webhook 签名密钥（写 agents.<ID> 段）")
    p.add_argument(
        "--belt",
        action="store_true",
        help="同时生成 per-身份 belt 脚本（包内模板渲染、绝对路径自动填；部署副本=生成产物）",
    )
    p.add_argument(
        "--no-activate",
        action="store_true",
        help="只生成文件，不 launchctl load / systemctl enable",
    )
    p.add_argument(
        "--launch-agents-dir", default=None, help="override ~/Library/LaunchAgents（测试/tmp 用）"
    )
    p.add_argument(
        "--systemd-dir", default=None, help="override ~/.config/systemd/user（测试/tmp 用）"
    )
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

    p = sub.add_parser(
        "upgrade",
        parents=[common],
        help="查 PyPI 最新版并升级（uv tool / pipx / pip 自动识别；执行前先显示完整命令）",
    )
    p.add_argument("--check", action="store_true", help="只查版本与提示，不执行升级")
    p.add_argument("--yes", action="store_true", help="跳过确认直接执行（仍会先打印完整命令）")
    p.set_defaults(func=_cmd_upgrade)

    p = sub.add_parser(
        "doctor",
        parents=[common],
        help="诊断一条命令：哪段断了 + 怎么修（信箱根/装/加载/最近唤醒/路由/积压/宿主认证/breaker/仓内线上脚本一致）",
    )
    p.add_argument("--json", action="store_true", help="输出结构化 JSON")
    p.add_argument(
        "--wb-wake-log",
        dest="wb_wake_log",
        default="",
        help="workbuddy 唤醒日志路径重定向（默认 ~/.workbuddy/wb-wake/wake.log；测试/沙箱用）",
    )
    p.set_defaults(func=_cmd_doctor)

    p = sub.add_parser(
        "digest",
        parents=[common],
        help="纯本地信件流转（t-62 判据7/S4：零网络零 LLM）——读信→写摘要→标 done→列建议回复",
    )
    p.add_argument("--agent", default="", help="只 digest 这个身份（缺省=inbox 里全部身份）")
    p.add_argument("--json", action="store_true", help="输出结构化 JSON")
    p.set_defaults(func=_cmd_digest)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(cli_main())
