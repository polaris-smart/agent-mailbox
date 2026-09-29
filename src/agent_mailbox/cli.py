"""Unified CLI subcommands (v0.7.5 §3.1), mounted under the existing
``agent-mailbox`` console entry by :func:`agent_mailbox.server.main`.

    agent-mailbox setup [--yes]        # --yes: headless defaults, no browser
    agent-mailbox discover [--json] [--deep [DIR ...]] [--no-save]
    agent-mailbox status [--json]
    agent-mailbox test <member> [--timeout 60] [--json]
    agent-mailbox connect <name> [--yes]
    agent-mailbox uninstall [--letters keep|export|archive|delete]
    agent-mailbox upgrade [--check] [--yes]   # v0.7.6 E 单元（t-53）
    agent-mailbox doctor [--json]             # v0.7.6 A-2 单元（t-59）六检

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
import sys
from pathlib import Path
from typing import Any

from . import connect as connect_mod
from . import version_check as version_mod
from . import wake as wake_mod
from .discover import (
    build_report,
    default_context,
    mask_url,
    now_local,
    test_member,
)

SUBCOMMANDS = ("setup", "discover", "status", "test", "connect", "uninstall", "upgrade", "doctor")

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
# 六检：① 信箱根可读 ② 唤醒器已加载 ③ 最近一次唤醒是否真成功（区分「被
# 拉起」与「真消费」）④ 收件人路由指向谁 ⑤ 积压 ⑥ 宿主认证态。每项失败
# 必带可执行的下一步；全部正常输出健康摘要。日志存在才扫，不存在跳过
# 不报错。


DOCTOR_TITLES = (
    ("root", "① 信箱根可读"),
    ("wake_loaded", "② 唤醒器已加载"),
    ("last_wake", "③ 最近一次唤醒"),
    ("routing", "④ 收件人路由"),
    ("backlog", "⑤ 积压"),
    ("host_auth", "⑥ 宿主认证态"),
)

# ⑥ 宿主认证态的已知故障特征（对日志尾部逐行匹配；spawn_failed 用组提取
# 命令名，好给出指名道姓的修法）。
DOCTOR_LOG_SIGNATURES: tuple[tuple[str, re.Pattern[str], str, str | None], ...] = (
    (
        "auth_required",
        re.compile(r"authentication required|please use /login|未登录", re.IGNORECASE),
        "宿主 CLI 未登录",
        (
            "去宿主 CLI 完成登录（如 codex /login 或对应 CLI 的登录命令），"
            "登录后重跑 agent-mailbox doctor 确认。"
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
        "zcode provider 配置路径不存在",
        (
            "检查 zcode provider 配置：真源在 /Applications/ZCode.app/Contents/Resources/config/"
            "provider 与 ~/.zcode/v2/provider_config.json，唤醒命令里的路径要指向真源。"
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
        return None
    if route.adapter in ("hermes", "generic-webhook") and not route.webhook_url:
        return (
            "webhook 通道但没有 url（信到了也叫不醒人）",
            "wake install --agent <id> --webhook-url <url>，或检查 <root>/webhook.json",
        )
    return None


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


def doctor_report(
    root: Path,
    *,
    wb_wake_log: Path | None = None,
    launch_agents_dir: Path | None = None,
    systemd_dir: Path | None = None,
) -> dict[str, Any]:
    """doctor 六检的数据面（t-59）。``--json`` 原样输出；人读格式见
    :func:`_print_doctor`。任何可执行环境差异（wb 日志路径 / plist 目录 /
    systemd 目录）都留了参数位，测试用 tmp 替身，不碰真机状态。"""
    root = Path(root)
    cfg = wake_mod.WakeConfig.load(root)
    targets = _doctor_wake_targets(cfg)
    checks: list[dict[str, Any]] = []

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

    # ② 唤醒器已加载（launchd plist / systemd unit 存在性）
    if not cfg:
        _check(
            "wake_loaded",
            False,
            f"{root}/wake.json 不存在（唤醒器从未安装）",
            "python -m agent_mailbox.wake install --agent <id> 安装（macOS 写 launchd plist / Linux 写 systemd path unit）",
        )
    elif not targets:
        _check(
            "wake_loaded",
            False,
            "wake.json 存在但没有 agents.<ID> 身份段，也没有 agent_id",
            "wake install --agent <id> --adapter <通道> 逐身份安装",
        )
    else:
        loaded: list[str] = []
        missing: list[str] = []
        for tid in targets:
            if sys.platform == "darwin":
                unit = (
                    Path(launch_agents_dir or (Path.home() / "Library" / "LaunchAgents"))
                    / f"{wake_mod.WAKE_LABEL}-{tid}.plist"
                )
            elif sys.platform == "linux":
                unit = (
                    Path(systemd_dir or (Path.home() / ".config" / "systemd" / "user"))
                    / f"{wake_mod.WAKE_LABEL}-{tid}.path"
                )
            else:
                unit = None  # 其他平台：run 手动模式，安装态无从判起
            (loaded if (unit is None or unit.exists()) else missing).append(tid)
        if missing:
            _check(
                "wake_loaded",
                False,
                f"已装 {len(loaded)}/{len(targets)}（{', '.join(loaded) or '无'}）；"
                f"未装: {' '.join(missing)}",
                "对缺失身份补跑 python -m agent_mailbox.wake install --agent <id>",
            )
        else:
            kind = "launchd plist" if sys.platform == "darwin" else "systemd unit"
            _check(
                "wake_loaded",
                True,
                f"{kind} 已装 {len(targets)}/{len(targets)}：{' '.join(targets)}",
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
        if bad:
            _check(
                "last_wake",
                False,
                "；".join(bad) + (f"；正常: {' '.join(good)}" if good else ""),
                "按 agent 查对应日志（<root>/wake-zc.log、<root>/wake-daemon.log）与 ⑥ 的判定；"
                "修完可手动 wake run --agent <id> --once 验证",
            )
        else:
            _check(
                "last_wake", True, f"最近一次均真成功：{' '.join(good)}（wake-attempts.jsonl 尾部）"
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
        if route_bad:
            _check(
                "routing",
                False,
                "；".join(f"{t}: {p}" for t, p, _ in route_bad) + "；" + "；".join(route_lines),
                route_bad[0][2],
            )
        else:
            _check("routing", True, "；".join(route_lines))

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
        print("\n结论: 六检全过，唤醒链路健康。")
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
        help="六检一条命令：哪段断了 + 怎么修（信箱根/唤醒器/最近唤醒/路由/积压/宿主认证态）",
    )
    p.add_argument("--json", action="store_true", help="输出结构化 JSON")
    p.add_argument(
        "--wb-wake-log",
        dest="wb_wake_log",
        default="",
        help="workbuddy 唤醒日志路径重定向（默认 ~/.workbuddy/wb-wake/wake.log；测试/沙箱用）",
    )
    p.set_defaults(func=_cmd_doctor)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(cli_main())
