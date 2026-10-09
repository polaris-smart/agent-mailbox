"""Independent product entry; no legacy mailbox startup or global installation."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def workbench_enroll_hosts() -> tuple[str, ...]:
    """可粘贴的宿主（从契约处取，避免 CLI 再写死一份）。"""
    from .workbench_enroll import HOSTS

    return HOSTS


def workbench_lease_modes() -> tuple[str, ...]:
    """认领模式（从契约处取，避免 CLI 里再写死一份）。"""
    from .workbench_lease import MODES

    return MODES


def main(argv: list[str] | None = None) -> None:
    """CLI 入口：**绝不让 WorkbenchError 变成裸 traceback**（铁律 U5 错误自解释）。"""
    from .workbench_store import WorkbenchError

    try:
        return _dispatch(argv)
    except WorkbenchError as exc:
        code = getattr(exc, "code", "error")
        message = getattr(exc, "message", None) or str(exc)
        command = argv[0] if argv else (sys.argv[1] if len(sys.argv) > 1 else "")
        where = f"（命令：{command}）" if command else ""
        print(f"agent-mailbox: [{code}] {message}{where}", file=sys.stderr)
        print(
            "  · 自查：agent-mailbox selfcheck · 状态：agent-mailbox wall / ledger", file=sys.stderr
        )
        raise SystemExit(2) from None


def _dispatch(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "mailbox-mcp":
        from .mailbox_mcp import main as mailbox_main

        return mailbox_main(args[1:])
        return
    if args and args[0] == "knowledge":
        # 几件套**写操作**（人侧）· HS 方案 §四/§七：写操作归仓主权方手工 ⇒ 必须 --yes 明确确认
        parser = argparse.ArgumentParser(
            description="Knowledge toolchain maintenance (human-only)."
        )
        sub = parser.add_subparsers(dest="action", required=True)
        index_parser = sub.add_parser("index", help="重建 codegraph 索引（写操作）")
        index_parser.add_argument(
            "--project", required=True, help="项目 id（路径由项目解析，不接受任意路径）"
        )
        index_parser.add_argument("--home", type=Path, default=Path.home() / ".agent-mailbox")
        index_parser.add_argument("--yes", action="store_true", help="明确确认执行写操作")
        options = parser.parse_args(args[1:])
        if options.action == "index":
            from . import workbench_codegraph
            from .workbench_store import WorkbenchStore

            store = WorkbenchStore(options.home.expanduser())
            result = workbench_codegraph.reindex(
                store.project_path_for(options.project), confirmed=bool(options.yes)
            )
            if result.get("ok"):
                print(result.get("text") or "索引已重建")
                return 0
            error = result.get("error") or {}
            print(
                f"agent-mailbox: [{error.get('code', 'error')}] {error.get('message', '未知错误')}",
                file=sys.stderr,
            )
            if error.get("code") == "CONFIRM_REQUIRED":
                print("  · 这是写操作：加 --yes 明确确认后再执行", file=sys.stderr)
            return 2

    if args and args[0] == "proof":
        # T12 交付证明：一屏可判（只读；不接聊天记录）
        parser = argparse.ArgumentParser(description="Render a task's delivery proof (read-only).")
        parser.add_argument("task_id")
        parser.add_argument("--home", type=Path, default=Path.home() / ".agent-mailbox")
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--verify", action="store_true", help="同时重算产出物哈希")
        options = parser.parse_args(args[1:])
        from . import workbench_proof
        from .workbench_store import WorkbenchStore

        store = WorkbenchStore(options.home.expanduser())
        proof = workbench_proof.latest_proof(store, options.task_id)
        if proof is None:
            print(f"agent-mailbox: 该任务还没有交付证明：{options.task_id}", file=sys.stderr)
            print(
                "  · 用 workbench_proof.build_proof(store, task_id, artifacts=[…]) 记录一份",
                file=sys.stderr,
            )
            return 2
        verification = (
            workbench_proof.verify_proof(store, options.task_id) if options.verify else None
        )
        if options.json:
            print(
                json.dumps(
                    {"proof": proof, "verification": verification}, ensure_ascii=False, indent=1
                )
            )
        else:
            print(workbench_proof.render_verdict(proof, verification))
        return 0
    if args and args[0] == "proof-verify":
        # 系统校验（可留痕）：重算哈希 + 记 governance 事件；**不推进终态**
        parser = argparse.ArgumentParser(
            description="Verify a delivery proof and record the verdict."
        )
        parser.add_argument("task_id")
        parser.add_argument("--home", type=Path, default=Path.home() / ".agent-mailbox")
        parser.add_argument("--record", action="store_true")
        parser.add_argument(
            "--verifier", default=None, help="校验者员工 ID（--record 时必填；必须换人）"
        )
        parser.add_argument("--json", action="store_true")
        options = parser.parse_args(args[1:])
        from . import workbench_proof
        from .workbench_store import WorkbenchStore

        if options.record and not options.verifier:
            print(
                "agent-mailbox: 留痕校验需要 --verifier <员工ID>（必须是被验者之外的本项目在职员工）",
                file=sys.stderr,
            )
            print(
                "  · 例：agent-mailbox proof-verify <task> --record --verifier employee_xxx",
                file=sys.stderr,
            )
            return 2
        result = workbench_proof.verify_proof(
            WorkbenchStore(options.home.expanduser()),
            options.task_id,
            record=options.record,
            verifier_id=options.verifier,
        )
        print(
            json.dumps(result, ensure_ascii=False, indent=1)
            if options.json
            else f"  系统校验：{result['verdict']}（检查 {result.get('checked', 0)}"
            f" · 不符 {result.get('mismatch', 0)} · 缺失 {result.get('missing', 0)}）"
        )
        return 0
    if args and args[0] in ("run", "schedule", "watch", "inspect"):
        # T32 四原语：run / schedule / watch（拉取式等待，无常驻）/ inspect
        action = args[0]
        parser = argparse.ArgumentParser(description=f"{action}: one of the four work primitives.")
        parser.add_argument("task_id", nargs="?", default=None)
        parser.add_argument("--home", type=Path, default=Path.home() / ".agent-mailbox")
        parser.add_argument("--project", default=None)
        parser.add_argument("--at", default=None, help="schedule：ISO 时间")
        parser.add_argument("--timeout", type=float, default=30.0, help="watch：最多等多少秒")
        parser.add_argument("--poll", type=float, default=1.0)
        parser.add_argument("--note", default="")
        parser.add_argument("--json", action="store_true")
        options = parser.parse_args(args[1:])
        from . import workbench_schedule
        from .workbench_store import WorkbenchStore

        store = WorkbenchStore(options.home.expanduser())
        try:
            if action == "run":
                if not options.task_id:
                    raise ValueError("run 需要 <task_id>")
                out = workbench_schedule.run_now(store, options.task_id)
            elif action == "schedule":
                if not options.task_id or not options.at:
                    raise ValueError("schedule 需要 <task_id> 与 --at <ISO时间>")
                out = workbench_schedule.schedule(
                    store, options.task_id, options.at, note=options.note
                )
            elif action == "watch":
                out = workbench_schedule.watch(
                    store,
                    options.project,
                    timeout_seconds=options.timeout,
                    poll_seconds=options.poll,
                )
                if not options.json:
                    state = (
                        "超时（没有到期的）" if out["timed_out"] else f"到期 {len(out['due'])} 件"
                    )
                    print(f"  watch：{state} · 等了 {out['waited_seconds']}s")
                    for row in out["due"]:
                        print(f"    · {row['title']}（到期 {row['due_at']}）")
                    return 0
            else:
                if not options.task_id:
                    raise ValueError("inspect 需要 <task_id>")
                out = workbench_schedule.inspect(store, options.task_id)
        except ValueError as exc:
            print(f"agent-mailbox: {exc}", file=sys.stderr)
            print(
                "  · 用法：run <task> | schedule <task> --at ISO | watch [--timeout S] | inspect <task>",
                file=sys.stderr,
            )
            return 2
        print(
            json.dumps(out, ensure_ascii=False, indent=1)
            if options.json
            else "\n".join(f"  {k} = {v}" for k, v in out.items() if not isinstance(v, list))
            or json.dumps(out, ensure_ascii=False)
        )
        return 0
    if args and args[0] == "selfcheck":
        # 一条命令验证我们自己的功能：临时 home 上走完整条产品链，绝不碰真实数据
        parser = argparse.ArgumentParser(
            description="Run the whole product path on a throwaway home."
        )
        parser.add_argument("--json", action="store_true")
        options = parser.parse_args(args[1:])
        from . import workbench_selfcheck

        report = workbench_selfcheck.run()
        print(
            json.dumps(report, ensure_ascii=False, indent=1)
            if options.json
            else workbench_selfcheck.render(report)
        )
        return 0 if report["ok"] else 1
    if args and args[0] == "observe":
        # 观察窗：把完成判据做成可执行的计数（事件判据，不是日历）
        parser = argparse.ArgumentParser(description="Observation window: criteria as counts.")
        parser.add_argument("--home", type=Path, default=Path.home() / ".agent-mailbox")
        parser.add_argument("--start", action="store_true", help="记录观察窗起点（幂等）")
        parser.add_argument("--sample", type=int, default=20)
        parser.add_argument("--json", action="store_true")
        options = parser.parse_args(args[1:])
        from . import workbench_observe
        from .workbench_store import WorkbenchStore

        store = WorkbenchStore(options.home.expanduser())
        if options.start:
            info = workbench_observe.start(store)
            print(f"  观察窗起点：{info['started_at']}（幂等；重复执行不覆盖）")
        result = workbench_observe.evaluate(store, sample=options.sample)
        if options.json:
            print(json.dumps(result, ensure_ascii=False, indent=1))
        else:
            c = result["criteria"]
            print(
                f"  观察窗：{result['window']['started_at'] or '（未开始）'}"
                + (
                    f" · 已观察 {result['window']['days']} 天"
                    if result["window"]["days"] is not None
                    else ""
                )
            )
            print(
                f"  风暴 {c['storms']}（要求 0）· 真验收 {c['acceptances']}（要求 ≥10）"
                f" · 审计抽样 {c['audit_reconstructable']}/{c['audit_sampled']}（要求全可还原）"
            )
            print(f"  判定：{result['verdict']}")
        return 0
    if args and args[0] == "wake":
        if "--help" in args or "-h" in args:
            print("""用法：agent-mailbox wake <动作> [--home <store home>] [--state-dir <目录>]
  --once        跑一轮：发现新信 ⇒ 叫醒（首轮=冷启动，不叫任何人 ✓）
  --status      看状态：开关 · 每位员工「已见 N 封 / 叫过 M 次」（M 是权威计数 ✓）
  --install     写 launchd 单元（**不自动加载** ✗ 需你自己 launchctl load -w ✓；写绝对路径 ✓）
  --uninstall   先 bootout 再删单元（零残留 ✓ 幂等 ✓）
  --enable/--disable  开/关唤醒判定（只写我们自己的状态 ✓ 不动信 ✓）
  --plist-dir   单元目录（默认 ~/Library/LaunchAgents ✓ 测试时可指临时目录 ✓）
  --state-dir   唤醒状态目录（默认 <store home>/wake ✓ hook 与 outbox **钉在 store home** ✗）
默认不装 ✓ opt-in ✓；四道闸（水位线/冷启动/冷却 15 分钟/≤4 次每小时）都在代码里 ✓""")
            return 0
        # T31 唤醒通道：默认**不装** ✗（人侧显式 --install ✓）—— 真闸在代码里，不在调度器上 ✓
        from .workbench_store import WorkbenchStore as _Store
        from .workbench_wake import cli_main as _wake_cli

        home = Path.home() / ".agent-mailbox-v08"
        for index, item in enumerate(args):
            if item == "--home" and index + 1 < len(args):
                home = Path(args[index + 1])
        store = (
            None
            if "--status" in args or "--install" in args or "--uninstall" in args
            else _Store(home)
        )
        return _wake_cli(
            [a for a in args[1:] if not (a == "--home" or a == home.name)], home=home, store=store
        )

    if args and args[0] == "brief":
        # T31 钩子轻通道：会话起始简报（零常驻、有预算、只读）
        parser = argparse.ArgumentParser(
            description="Session-start brief for a host hook (read-only)."
        )
        parser.add_argument("--home", type=Path, default=Path.home() / ".agent-mailbox")
        parser.add_argument("--employee", default=None)
        parser.add_argument("--project", default=None)
        parser.add_argument("--budget", type=int, default=1200)
        parser.add_argument("--json", action="store_true")
        options = parser.parse_args(args[1:])
        from . import workbench_brief
        from .workbench_store import WorkbenchStore

        try:
            data = workbench_brief.brief(
                WorkbenchStore(options.home.expanduser()),
                employee_id=options.employee,
                project_id=options.project,
                budget=options.budget,
            )
        except ValueError as exc:
            print(f"agent-mailbox: {exc}", file=sys.stderr)
            return 2
        print(
            json.dumps(data, ensure_ascii=False, indent=1)
            if options.json
            else workbench_brief.render(data, budget=options.budget)
        )
        return 0
    if args and args[0] == "contacts":
        # T33 通讯录：默认开放；收件人一旦有列表即白名单生效
        parser = argparse.ArgumentParser(
            description="Contacts whitelist (open unless a list exists)."
        )
        parser.add_argument("action", choices=["list", "add", "remove", "request", "check"])
        parser.add_argument("--home", type=Path, default=Path.home() / ".agent-mailbox")
        parser.add_argument("--project", required=True)
        parser.add_argument("--owner", default=None, help="通讯录归属（收件人）")
        parser.add_argument("--target", default=None, help="对方的员工 ID")
        parser.add_argument("--sender", default=None, help="check：发件人 ID")
        parser.add_argument("--note", default="")
        parser.add_argument("--json", action="store_true")
        options = parser.parse_args(args[1:])
        from . import workbench_contacts
        from .workbench_store import WorkbenchStore

        store = WorkbenchStore(options.home.expanduser())
        try:
            if options.action == "list":
                out = workbench_contacts.contacts(store, options.project, options.owner)
            elif options.action == "add":
                out = workbench_contacts.add_contact(
                    store,
                    options.project,
                    owner_id=options.owner,
                    target_id=options.target,
                    note=options.note,
                )
            elif options.action == "remove":
                out = workbench_contacts.remove_contact(
                    store, options.project, owner_id=options.owner, target_id=options.target
                )
            elif options.action == "request":
                out = workbench_contacts.request_contact(
                    store,
                    options.project,
                    owner_id=options.owner,
                    target_id=options.target,
                    note=options.note,
                )
            else:
                out = workbench_contacts.may_message(
                    store, options.project, options.sender, options.owner
                )
        except (ValueError, TypeError) as exc:
            print(f"agent-mailbox: {exc}", file=sys.stderr)
            print(
                "  · 用法：agent-mailbox contacts add --project P --owner <收件人> --target <发件人>",
                file=sys.stderr,
            )
            return 2
        print(
            json.dumps(out, ensure_ascii=False, indent=1)
            if options.json
            else "\n".join(f"  {k} = {v}" for k, v in out.items())
        )
        return 0
    if args and args[0] == "artifact":
        # T6 产出物引用：按字节预算读取（token 走、内容永不自动展开）
        parser = argparse.ArgumentParser(description="Resolve artifact refs by byte budget.")
        parser.add_argument("action", choices=["list", "read"])
        parser.add_argument("ref", nargs="?", default=None)
        parser.add_argument("--home", type=Path, default=Path.home() / ".agent-mailbox")
        parser.add_argument("--task", default=None)
        parser.add_argument("--budget", type=int, default=2000)
        parser.add_argument("--offset", type=int, default=0)
        parser.add_argument("--projection", default="text", choices=["text", "head", "json_keys"])
        parser.add_argument("--json", action="store_true")
        options = parser.parse_args(args[1:])
        from . import workbench_artifact
        from .workbench_store import WorkbenchStore

        store = WorkbenchStore(options.home.expanduser())
        try:
            if options.action == "list":
                rows = workbench_artifact.list_refs(store, task_id=options.task)
                if options.json:
                    print(json.dumps(rows, ensure_ascii=False, indent=1))
                else:
                    print(f"已知引用 {len(rows)} 个：")
                    for row in rows:
                        print(
                            f"  · {row['ref']}  {row['bytes']}B  {str(row['path'])[:56]}  （任务 {row['task_id']}）"
                        )
                return 0
            if not options.ref:
                print("agent-mailbox: artifact read 需要 <ref>", file=sys.stderr)
                return 2
            result = workbench_artifact.read(
                store,
                options.ref,
                budget=options.budget,
                offset=options.offset,
                projection=options.projection,
            )
        except ValueError as exc:
            print(f"agent-mailbox: {exc}", file=sys.stderr)
            print(
                "  · 用法：agent-mailbox artifact list | artifact read <ref> --budget 2000",
                file=sys.stderr,
            )
            return 2
        if options.json:
            print(json.dumps(result, ensure_ascii=False, indent=1))
        else:
            print(
                f"  {result['ref']} · {result['total_bytes']}B · 本次 {result['returned_bytes']}B"
                f" · 预算 {result['budget']} · 截断 {result['truncated']} · 哈希校验 {result['hash_verified']}"
            )
            if result.get("refused"):
                print(f"  ⛔ 拒绝返回内容（{result['refused']}）")
            elif result.get("content") is not None:
                print("  ── 内容：")
                for line in str(result["content"]).splitlines()[:20]:
                    print(f"     {line}")
            if result.get("next_offset"):
                print(f"  续读：--offset {result['next_offset']}")
        return 0
    if args and args[0] == "bridge":
        # T7 单向桥接：v0.7 信件 → 项目任务卡（默认演练；绝不写回旧信箱）
        parser = argparse.ArgumentParser(
            description="One-way bridge: legacy letters -> task cards."
        )
        parser.add_argument("action", choices=["scan", "project", "status"])
        parser.add_argument("--mail-root", type=Path, default=Path.home() / ".agent-mail")
        parser.add_argument("--home", type=Path, default=Path.home() / ".agent-mailbox")
        parser.add_argument("--project", default=None)
        parser.add_argument("--apply", action="store_true", help="真的建卡（默认只演练）")
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--json", action="store_true")
        options = parser.parse_args(args[1:])
        from . import workbench_bridge
        from .workbench_store import WorkbenchStore

        if options.action == "scan":
            out = workbench_bridge.scan(options.mail_root.expanduser())
            if options.json:
                print(json.dumps(out, ensure_ascii=False, indent=1))
            else:
                print(
                    f"扫描 {out['total']} 封：像任务 {len(out['candidates'])} · 跳过 {len(out['skipped'])}"
                )
                for row in out["skipped"][:10]:
                    print(
                        f"  · 跳过 {row['letter_id']}（{row['reason']}）{str(row['subject'])[:40]}"
                    )
            return 0
        store = WorkbenchStore(options.home.expanduser())
        if options.action == "status":
            print(json.dumps(workbench_bridge.status(store), ensure_ascii=False, indent=1))
            return 0
        if not options.project:
            print("agent-mailbox: bridge project 需要 --project", file=sys.stderr)
            return 2
        out = workbench_bridge.project(
            store,
            options.mail_root.expanduser(),
            options.project,
            apply=options.apply,
            limit=options.limit,
        )
        if options.json:
            print(json.dumps(out, ensure_ascii=False, indent=1))
        else:
            mode = "演练（未改动）" if out["dry_run"] else "已执行"
            print(
                f"桥接{mode}：共 {out['total_letters']} 封 · 候选 {out['candidates']} · 非任务 {out['skipped_not_task']}"
            )
            for step in out["steps"]:
                print(
                    f"  · {step['letter_id']} → {step['action']}"
                    + (f"（任务 {step['task_id']}）" if step.get("task_id") else "")
                    + (f" — {str(step['subject'])[:34]}" if step.get("subject") else "")
                )
            if out["dry_run"]:
                print("  加 --apply 才会真的建卡")
        return 0
    if args and args[0] == "discover":
        # T2 第一步：本机探测（只读）
        parser = argparse.ArgumentParser(
            description="Discover agent CLIs and apps on this machine (read-only)."
        )
        parser.add_argument("--installed-only", action="store_true")
        parser.add_argument("--json", action="store_true")
        options = parser.parse_args(args[1:])
        from . import workbench_enroll

        rows = workbench_enroll.discover(installed_only=options.installed_only)
        if options.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        else:
            print(f"发现 {len(rows)} 个（已安装优先）：")
            for row in rows:
                flag = "可执行" if row["execution_supported"] else "非受管"
                mark = "✓" if row["installed"] else "·"
                print(
                    f"  {mark} {flag}  {row['name']!s:<14} {row['connection_type']:<4}"
                    f" auth={row['auth_status']!s:<14} {str(row['entrypoint'] or '未安装')[:52]}"
                )
        return 0
    if args and args[0] == "plan":
        # T2 第二步：只读计划（不出手）
        parser = argparse.ArgumentParser(description="Plan enrolments for a project (read-only).")
        parser.add_argument("--home", type=Path, default=Path.home() / ".agent-mailbox")
        parser.add_argument("--project", required=True)
        parser.add_argument("--json", action="store_true")
        options = parser.parse_args(args[1:])
        from . import workbench_enroll
        from .workbench_store import WorkbenchStore

        outline = workbench_enroll.plan(WorkbenchStore(options.home.expanduser()), options.project)
        if options.json:
            print(json.dumps(outline, ensure_ascii=False, indent=1))
        else:
            s = outline["summary"]
            print(
                f"计划（只读）：已安装 {s['installed']} · 可执行 {s['executable']} · 已在项目 {s['already_enrolled']}"
            )
            for row in outline["suggestions"]:
                note = f"（已在：{', '.join(row['existing'])}）" if row["already_enrolled"] else ""
                print(f"  · {row['name']:<14} {note}")
                if not row["already_enrolled"]:
                    print(f"      {row['command']}")
        return 0
    if args and args[0] == "enroll":
        # T2 第三步：一键入伙 + 一个粘贴块
        parser = argparse.ArgumentParser(
            description="Enrol one host and print a paste-ready block."
        )
        parser.add_argument("kind")
        parser.add_argument("--home", type=Path, default=Path.home() / ".agent-mailbox")
        parser.add_argument("--project", required=True)
        parser.add_argument("--name", default=None)
        parser.add_argument("--host", default="generic", choices=list(workbench_enroll_hosts()))
        parser.add_argument("--binary", default=None)
        parser.add_argument("--json", action="store_true")
        options = parser.parse_args(args[1:])
        from . import workbench_enroll
        from .workbench_store import WorkbenchStore

        try:
            result = workbench_enroll.enroll(
                WorkbenchStore(options.home.expanduser()),
                options.project,
                options.kind,
                name=options.name,
                host=options.host,
                binary=options.binary,
            )
        except ValueError as exc:
            print(f"agent-mailbox: {exc}", file=sys.stderr)
            print("  · 可用类型见：agent-mailbox discover", file=sys.stderr)
            return 2
        if options.json:
            print(json.dumps(result, ensure_ascii=False, indent=1))
        else:
            state = "已新建" if result["created"] else "复用已有"
            print(f"  {state}员工：{result['employee']['name']}（{result['employee']['kind']}）")
            print(f"  会话：{result['session_id']}（到期 {result['expires_at']}）")
            print(f"  会话文件：{result['session_file']}")
            print(f"  ── 粘贴到 {result['host']} 的配置里：")
            for line in result["snippet"].splitlines():
                print(f"     {line}")
        return 0
    if args and args[0] == "onboard":
        # T2 一体化：探测 → （可选）入伙 → 粘贴；默认 dry-run，绝不擅自改机器
        parser = argparse.ArgumentParser(
            description="Three-step onboarding (dry run unless --apply)."
        )
        parser.add_argument("--home", type=Path, default=Path.home() / ".agent-mailbox")
        parser.add_argument("--project", required=True)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--kind", action="append", default=[])
        parser.add_argument("--host", default="generic", choices=list(workbench_enroll_hosts()))
        parser.add_argument("--json", action="store_true")
        options = parser.parse_args(args[1:])
        from . import workbench_enroll
        from .workbench_store import WorkbenchStore

        result = workbench_enroll.onboard(
            WorkbenchStore(options.home.expanduser()),
            options.project,
            apply=options.apply,
            kinds=options.kind or None,
            host=options.host,
        )
        if options.json:
            print(json.dumps(result, ensure_ascii=False, indent=1))
        else:
            mode = "演练（未改动任何东西）" if result["dry_run"] else "已执行"
            print(f"入伙{mode} · 项目 {result['project_id']}")
            for step in result["steps"]:
                print(f"  · {step['name']}（{step['kind']}）→ {step['action']}")
                if step.get("snippet"):
                    print(f"      会话文件：{step['session_file']}")
                    for line in step["snippet"].splitlines():
                        print(f"      {line}")
            if result["dry_run"]:
                print("  加 --apply 才会真的建员工与会话")
        return 0
    if args and args[0] == "lease":
        # T4 文件认领：声明/释放/查看/冲突检查 + pre-commit 钩子片段
        parser = argparse.ArgumentParser(description="Path leases (claim / release / check).")
        parser.add_argument("action", choices=["list", "claim", "release", "check"])
        parser.add_argument("--home", type=Path, default=Path.home() / ".agent-mailbox")
        parser.add_argument("--project", default=None)
        parser.add_argument("--employee", default=None)
        parser.add_argument("--pattern", action="append", default=[])
        parser.add_argument("--path", action="append", default=[])
        parser.add_argument("--mode", default="exclusive", choices=list(workbench_lease_modes()))
        parser.add_argument("--ttl", type=int, default=3600)
        parser.add_argument("--reason", default="")
        parser.add_argument("--claim-id", default=None)
        parser.add_argument("--print-hook", action="store_true")
        parser.add_argument("--json", action="store_true")
        options = parser.parse_args(args[1:])
        from . import workbench_lease
        from .workbench_store import WorkbenchStore

        if options.print_hook:
            print("#!/bin/sh")
            print('exec python3 "$(git rev-parse --show-toplevel)/scripts/lease-guard.py" "$@"')
            return 0
        store = WorkbenchStore(options.home.expanduser())
        try:
            if options.action == "list":
                out = {"claims": workbench_lease.active_claims(store, options.project)}
            elif options.action == "claim":
                out = workbench_lease.claim_paths(
                    store,
                    options.project,
                    options.employee,
                    options.pattern,
                    mode=options.mode,
                    ttl_seconds=options.ttl,
                    reason=options.reason,
                )
            elif options.action == "release":
                out = workbench_lease.release_paths(
                    store, options.project, options.employee, claim_id=options.claim_id
                )
            else:
                out = {
                    "conflicts": workbench_lease.check_conflicts(
                        store, options.project, options.employee, options.path
                    )
                }
        except (ValueError, TypeError) as exc:
            print(f"agent-mailbox: {exc}", file=sys.stderr)
            print(
                "  · 用法：agent-mailbox lease claim --project P --employee E --pattern 'src/**'",
                file=sys.stderr,
            )
            return 2
        if options.json:
            print(json.dumps(out, ensure_ascii=False, indent=1))
        else:
            for key, value in out.items():
                if isinstance(value, list):
                    print(f"  {key}：{len(value)} 条")
                    for item in value:
                        if isinstance(item, dict):
                            label = item.get("pattern") or item.get("claim_id") or item.get("path")
                            print(
                                f"    · {label}  ← {item.get('held_by') or item.get('holder') or '—'}"
                                f"  {item.get('mode') or ''} {item.get('expires_at') or ''}".rstrip()
                            )
                else:
                    print(f"  {key} = {value}")
        return 0
    if args and args[0] == "policy":
        # 审核机制：查看/修改项目策略（修改写 governance 事件，可审计）
        parser = argparse.ArgumentParser(
            description="Review policy: notify / escalation budget / peer review."
        )
        parser.add_argument("--home", type=Path, default=Path.home() / ".agent-mailbox")
        parser.add_argument("--project", default=None)
        parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
        parser.add_argument("--json", action="store_true")
        options = parser.parse_args(args[1:])
        from . import workbench_policy
        from .workbench_store import WorkbenchStore

        store = WorkbenchStore(options.home.expanduser())
        try:
            if options.set:
                updates = {}
                for item in options.set:
                    key, _, value = item.partition("=")
                    updates[key.strip()] = value.strip()
                policy = workbench_policy.set_policy(store, updates, project_id=options.project)
                print("  已更新（写入 governance 事件，可审计）")
            else:
                policy = workbench_policy.effective_policy(store, options.project)
        except ValueError as exc:
            print(f"agent-mailbox: {exc}", file=sys.stderr)
            print(
                "  · 用法：agent-mailbox policy [--project ID] [--set peer_review=required_for_write]",
                file=sys.stderr,
            )
            return 2
        print(
            json.dumps(policy, ensure_ascii=False, indent=1)
            if options.json
            else "\n".join(f"  {k} = {v}" for k, v in sorted(policy.items()))
        )
        return 0
    if args and args[0] == "ledger":
        # T11 记账（派生视图，零写）：按任务 + 按员工（上游健康）
        parser = argparse.ArgumentParser(description="Task ledger + upstream health (read-only).")
        parser.add_argument("--home", type=Path, default=Path.home() / ".agent-mailbox")
        parser.add_argument("--project", default=None)
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--all", action="store_true", help="连零活动的员工也列出")
        parser.add_argument("--json", action="store_true")
        options = parser.parse_args(args[1:])
        from . import workbench_ledger
        from .workbench_store import WorkbenchStore

        view = workbench_ledger.ledger(
            WorkbenchStore(options.home.expanduser()), options.project, options.limit, options.all
        )
        if options.json:
            print(json.dumps(view, ensure_ascii=False, indent=1))
        else:
            print("记账（派生视图 · 零写）")
            print("  按任务：")
            for row in view["tasks"]:  # ledger() 已按 limit 钳位，勿二次切片
                dur = row["duration_seconds"]
                proof = f"证明={row['proof_verdict'] or ('有' if row['has_proof'] else '无')}"
                # 系统校验 ≠ 同行校验：人读一屏必须看得出两人规则是否满足
                peer = " · 同行=" + ("是" if row.get("peer_verified") else "否")
                err = f" · 失败={row['error_code']}" if row["error_code"] else ""
                print(
                    f"    [{row['status']:<8}] {str(row['title'])[:34]:<36} {row['assignee'] or '—':<12}"
                    f" {dur if dur is not None else '?'}s  事件{row['events']}  {proof}{peer}{err}"
                )
            print("  按员工（上游健康，失败多的在前）：")
            for row in view["upstream"]:
                rate = "—" if row["success_rate"] is None else f"{row['success_rate']:.0%}"
                cap = "可执行" if row["execution_supported"] else "非受管"
                last = row["last_failure"]["code"] if row["last_failure"] else "—"
                print(
                    f"    {str(row['name'])[:14]:<16} {cap:<6} 单{row['tasks_total']:<3} 跑{row['running']:<2}"
                    f" 待人{row['waiting_review']:<2} 失败{row['failed']:<3} 成功率{rate:<5} 最近失败={last}"
                )
        return 0
    if args and args[0] == "wall":
        # U7 团队墙：只读聚合（不推送、不写入、不搬消息流水）
        parser = argparse.ArgumentParser(
            description="Team wall: running / waiting / stuck (read-only)."
        )
        parser.add_argument("--home", type=Path, default=Path.home() / ".agent-mailbox")
        parser.add_argument("--project", default=None)
        parser.add_argument("--json", action="store_true")
        options = parser.parse_args(args[1:])
        from . import workbench_wall
        from .workbench_store import WorkbenchStore

        view = workbench_wall.wall(WorkbenchStore(options.home.expanduser()), options.project)
        if options.json:
            print(json.dumps(view, ensure_ascii=False, indent=1))
        else:
            c = view["counts"]
            print("团队墙（只读）")
            print(
                f"  在跑 {len(view['running'])} · 等人 {len(view['waiting_on_human'])} · 卡住 {len(view['stuck'])}"
                f" · 冻结线程 {len(view['frozen_threads'])} · 折叠 {c['folded_messages']}"
            )
            for label, rows in (
                ("在跑", view["running"]),
                ("等谁", view["waiting_on_human"]),
                ("卡住", view["stuck"]),
            ):
                for row in rows[:6]:
                    title = str(row.get("title") or "")[:44]
                    who = row.get("assignee") or row.get("assignee_id") or "—"
                    # 三态：等交付人交证明 / 等同行复验 是两件不同的事，人读必须分得开
                    if row.get("has_proof"):
                        verdict = row.get("proof_verdict") or "有"
                        mark = "是" if row.get("peer_verified") else "否"
                        proof = f"  · 证明={verdict}·同行={mark}"
                    elif "has_proof" in row:
                        proof = "  · 证明=无"
                    else:
                        proof = ""
                    print(f"  [{label}] {title}  ← {who}  ({row.get('status')}){proof}")
            idx = view["health"]["search_index"]
            print(
                f"  健康：消息 {c['messages']} · 治理事件 {c['governance_events']} · "
                f"检索索引 {'同步' if idx['in_sync'] else '未建/不同步'}"
            )
        return 0
    if args and args[0] == "audit":
        # 第四成败手：任意一封信的全生命周期可查（只读）
        parser = argparse.ArgumentParser(
            description="Reconstruct one message's full life (read-only)."
        )
        parser.add_argument("message_id")
        parser.add_argument("--home", type=Path, default=Path.home() / ".agent-mailbox")
        parser.add_argument("--json", action="store_true")
        options = parser.parse_args(args[1:])
        from . import workbench_wall
        from .workbench_store import WorkbenchStore

        try:
            chain = workbench_wall.audit_chain(
                WorkbenchStore(options.home.expanduser()), options.message_id
            )
        except ValueError as exc:
            # 规则 U5：错误自解释
            print(f"agent-mailbox: {exc}", file=sys.stderr)
            print("  · 用法：agent-mailbox audit <消息ID>（ID 可用 search 查）", file=sys.stderr)
            return 2
        if options.json:
            print(json.dumps(chain, ensure_ascii=False, indent=1))
        else:
            print(f"审计链 · {options.message_id}")
            print(f"  终态：{chain['terminal_state']}")
            for step in chain["steps"]:
                print(f"  · {str(step['at'])[:19]}  {step['step']}  (by {step.get('by') or '—'})")
        return 0
    if args and args[0] == "search":
        # T5 检索：**查询只读**；建索引要显式 --index（一次查询不该顺手建 6 张表）
        parser = argparse.ArgumentParser(description="Search project messages (FTS5).")
        parser.add_argument("query", nargs="+", help="检索词（空格分词 = AND）")
        parser.add_argument("--home", type=Path, default=Path.home() / ".agent-mailbox")
        parser.add_argument("--project", default=None, help="限定项目 ID")
        parser.add_argument("--employee", default=None, help="限定员工 ID（配合 --folder）")
        parser.add_argument("--folder", default=None, choices=["inbox", "sent"])
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--bodies", action="store_true", help="连正文一起返回（更费 token）")
        parser.add_argument("--index", action="store_true", help="先建立/刷新检索索引（会写库）")
        parser.add_argument("--json", action="store_true")
        options = parser.parse_args(args[1:])
        from . import workbench_search
        from .workbench_store import WorkbenchStore

        store = WorkbenchStore(options.home.expanduser())
        from .workbench_store import WorkbenchError

        try:
            if options.index:
                status = workbench_search.build_index(store)
                print(f"  索引已建立：{status['indexed']}/{status['messages']}")
            result = workbench_search.search(
                store,
                " ".join(options.query),
                project_id=options.project,
                employee_id=options.employee,
                folder=options.folder,
                limit=options.limit,
                include_bodies=options.bodies,
            )
        except (ValueError, WorkbenchError) as exc:
            # 规则 U5：错误自解释（是什么 + 怎么修），不抛裸 traceback
            print(f"agent-mailbox: {exc}", file=sys.stderr)
            print(
                "  · 用法：agent-mailbox search <检索词> [--project <项目ID>] [--limit N]",
                file=sys.stderr,
            )
            print("  · 检索词不能为空；多个词用空格分隔（按 AND 组合）", file=sys.stderr)
            return 2
        if options.json:
            print(json.dumps(result, ensure_ascii=False, indent=1))
        else:
            print(f"  查「{result['query']}」（{result['mode']}）→ {result['count']} 条")
            for row in result["results"]:
                stamp = str(row.get("created_at") or "")[:19]
                title = str(row.get("title") or "")[:56]
                print(f"  · {stamp}  {title}")
            if result["mode"] == "like":
                print("  （2 字以内走 LIKE 扫描：trigram 分词需要 ≥3 字符）")
        return 0
    if args and args[0] == "prepare":
        parser = argparse.ArgumentParser(
            description="Prepare this home's locked execution runtime."
        )
        parser.add_argument("--home", type=Path, default=Path.home() / ".agent-mailbox")
        options = parser.parse_args(args[1:])
        from .runtime_bridge.install_runtime import install_runtime
        from .workbench_lock import WorkbenchLock
        from .workbench_runtime import runtime_status

        home = options.home.expanduser().resolve()
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        (home / "workbench").mkdir(exist_ok=True, mode=0o700)
        from .workbench_store import WorkbenchError

        try:
            with WorkbenchLock(home):
                install_runtime(home / "workbench/runtime/deps")
                print(json.dumps(runtime_status(home), ensure_ascii=False))
        except (
            WorkbenchError,
            OSError,
            ValueError,
            RuntimeError,
            subprocess.SubprocessError,
        ) as exc:
            code = exc.code if isinstance(exc, WorkbenchError) else "RUNTIME_INSTALL_FAILED"
            print(json.dumps({"error": {"code": code, "message": str(exc)}}), file=sys.stderr)
            raise SystemExit(1) from exc
        return
    if args and args[0] == "node":
        from .workbench_node import main as node_main

        raise SystemExit(node_main(args[1:]))
    if args and args[0] == "workbench":
        args.pop(0)
    if args == ["--version"]:
        from . import __version__

        print(__version__)
        return
    from .workbench import main as workbench_main

    workbench_main(args)


if __name__ == "__main__":
    main()
