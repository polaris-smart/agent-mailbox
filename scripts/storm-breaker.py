#!/usr/bin/env python3
"""Stop crash-looping launchd units (storm breaker · rule S1).

The workbench autostart once restarted 1086 times against a broken build while
nobody was notified. This checks every ``com.polaris-smart.*`` unit, records how
often its run counter climbs, and when a unit restarts too many times inside a
window it is **booted out** and the trip is recorded once.

Usage:
  scripts/storm-breaker.py                 # check + trip when needed
  scripts/storm-breaker.py --dry-run       # report only, never touch launchd
  scripts/storm-breaker.py --reset LABEL   # clear a recorded trip (after a fix)
  scripts/storm-breaker.py --labels A,B    # check specific units only

Exit code: 0 = nothing tripped, 3 = at least one unit tripped.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_mailbox.storm_breaker import (
    DEFAULT_HARD_CAP,
    DEFAULT_MAX_RESTARTS,
    DEFAULT_WINDOW_SECONDS,
    list_our_labels,
    load_state,
    looks_suspect,
    mark_tripped,
    parse_launchctl_print,
    print_unit,
    record_sample,
    reset_unit,
    save_state,
    should_trip,
)

DEFAULT_STATE = Path.home() / ".agent-mailbox-v08" / "storm-breaker.json"
DEFAULT_LOG = Path.home() / ".agent-mailbox-v08" / "storm-breaker.log"


def log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"[{stamp}] {message}\n")


def notify(message: str, enabled: bool) -> None:
    if not enabled or sys.platform != "darwin":
        return
    script = f'display notification {json.dumps(message)} with title "Storm Breaker"'
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stop crash-looping launchd units.")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--window", type=float, default=DEFAULT_WINDOW_SECONDS)
    ap.add_argument("--max-restarts", type=int, default=DEFAULT_MAX_RESTARTS)
    ap.add_argument(
        "--hard-cap",
        type=int,
        default=DEFAULT_HARD_CAP,
        help="已死且重试超过此数的单元 ⇒ 只告警（不杀）",
    )
    ap.add_argument("--labels", default="", help="逗号分隔；默认自动发现 com.polaris-smart.*")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset", default="", help="清除某单元的熔断记录")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    state = load_state(args.state)
    if args.reset:
        changed = reset_unit(state, args.reset)
        save_state(args.state, state)
        print(f"reset {args.reset}: {'ok' if changed else 'no trip recorded'}")
        return 0

    labels = [x.strip() for x in args.labels.split(",") if x.strip()] or list_our_labels()
    now = time.time()
    tripped: list[str] = []
    report: list[dict] = []

    for label in labels:
        sample = parse_launchctl_print(print_unit(label), label)
        if sample is None:
            report.append({"label": label, "loaded": False})
            continue
        record_sample(state, label, sample.runs, now, args.window)
        unit = state["units"][label]
        trip, delta = should_trip(unit["samples"], now, args.window, args.max_restarts)
        entry = {
            "label": label,
            "loaded": True,
            "runs": sample.runs,
            "delta": delta,
            "last_exit": sample.last_exit,
            "state": sample.state,
            "tripped_before": bool(unit.get("tripped_at")),
            "tripped_now": False,
        }
        if trip and not unit.get("tripped_at"):
            reason = f"{delta} 次重启 / {int(args.window)}s（上限 {args.max_restarts}）"
            if mark_tripped(state, label, reason, now):
                entry["tripped_now"] = True
                tripped.append(label)
                if not args.dry_run:
                    subprocess.run(
                        ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
                        capture_output=True,
                        timeout=20,
                        check=False,
                    )
                    log(
                        args.log,
                        f"熔断 {label}：{reason} ⇒ 已 bootout（修好后 --reset {label} 再拉起）",
                    )
                    notify(f"已熔断 {label}：{reason}", not args.no_notify)
        elif trip and unit.get("tripped_at"):
            entry["suppressed"] = True  # already tripped: never bounce twice (rule S2)
        elif looks_suspect(sample, args.hard_cap) and not unit.get("warned_at"):
            # 速率规则看不见"已经停下、但重试过千次"的单元 —— 只告警，绝不杀
            entry["suspect"] = True
            if not args.dry_run:
                unit["warned_at"] = now
                log(
                    args.log,
                    f"告警 {label}：已重试 {sample.runs} 次且未在运行"
                    f"（last exit {sample.last_exit}）—— 需人工确认，未停用",
                )
                notify(f"{label} 已重试 {sample.runs} 次且未运行，请人工检查", not args.no_notify)
        report.append(entry)

    if not args.dry_run:
        save_state(args.state, state)

    if args.json:
        print(json.dumps({"tripped": tripped, "units": report}, ensure_ascii=False, indent=1))
    else:
        for row in report:
            if not row.get("loaded"):
                continue
            flag = ""
            if row.get("suspect"):
                flag = "  ⚠️ 重试过多且未在运行（仅告警）"
            if row.get("tripped_now"):
                flag = "  ⛔ 已熔断并停用"
            elif row.get("suppressed"):
                flag = "  （已熔断，跳过）"
            elif row.get("delta"):
                flag = f"  Δ{row['delta']}"
            print(f"{row['label']:<52} runs={row['runs']:<6} exit={row['last_exit']}{flag}")
        if tripped:
            print(f"\n⛔ 本次熔断 {len(tripped)} 个：{', '.join(tripped)}")
            print(
                "   修好后：scripts/storm-breaker.py --reset <label>，再 launchctl bootstrap 拉起"
            )
    return 3 if tripped else 0


if __name__ == "__main__":
    raise SystemExit(main())
