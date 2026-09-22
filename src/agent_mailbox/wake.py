"""wake-daemon: mail-arrived means agent-woken (v0.6 F1, 信必达).

One command, ``agent-mailbox wake install``, wires the OS file-watcher to a
tiny drain loop — the packaged, generalized form of the hand-rolled wake
scripts our four production lines were each maintaining separately:

- **macOS**: launchd ``WatchPaths`` on ``<root>/inbox/<agent>/`` — every new
  letter re-launches ``python -m agent_mailbox.wake run --once``.
- **Linux**: a systemd **user** path unit (``PathChanged=`` — inotify) plus a
  oneshot service running the same loop.

Reliability trio (straight from the 2026-09-22 incident review):

- **fail retry** — a failed wake POST retries ``retry_max`` (default 5) times
  at ``retry_interval`` (default 60s) inside one run; if all attempts fail the
  letter stays un-woken and the next WatchPaths trigger re-drains it. Mail is
  never dropped because the wake side failed.
- **idempotent dedup** — a letter is woken at most once: success appends a
  ``wake`` entry to the letter's ``handled_log`` (the v0.5 two-phase log, now
  carrying wake outcomes too), and any later drain round skips it.
- **count semantics v2** — a round wakes when the inbox holds ``pending``
  letters (all of them) or ``acked`` letters older than 600s (a handler
  died mid-turn); freshly-acked mail is being worked and stays quiet.

fail-open iron law: wake is a separate process that only ever reads letter
files and appends through the store's own locked APIs — if the daemon dies,
mail still lands and the next ``mailbox_check`` still delivers. Nothing in
the send path depends on wake existing.

Jev (F3) plugs in here as an optional, default-off router — see jev.py.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .store import MailStore, _now_iso
from .webhook import post_message

WAKE_LABEL = "com.polaris-smart.agent-mailbox-wake"  # + "-<agent>" per instance
DEFAULT_RETRY_INTERVAL = 60.0
DEFAULT_RETRY_MAX = 5
DEFAULT_STALE_ACKED = 600.0  # count semantics v2: acked older than this counts


# ------------------------------------------------------------------- config

class WakeConfig:
    """``<root>/wake.json`` — one wake installation per agent id."""

    def __init__(self, data: dict[str, Any], root: Path) -> None:
        self.root = root
        self.agent_id = str(data.get("agent_id", ""))
        self.adapter = str(data.get("adapter", "hermes"))
        webhook = data.get("webhook") or {}
        self.webhook_url = str(webhook.get("url", ""))
        self.webhook_secret = str(webhook.get("secret", ""))
        self.webhook_style = str(webhook.get("style", "")) or None
        jev = data.get("jev") or {}
        self.jev_enabled = bool(jev.get("enabled", False))
        self.jev_api_key = str(jev.get("api_key", ""))
        self.jev_endpoint = str(jev.get("endpoint", ""))
        self.jev_threshold = float(jev.get("threshold", 3.0))
        self.jev_timeout = float(jev.get("timeout", 3.0))
        self.retry_interval = float(data.get("retry_interval", DEFAULT_RETRY_INTERVAL))
        self.retry_max = int(data.get("retry_max", DEFAULT_RETRY_MAX))
        self.stale_acked = float(data.get("stale_acked", DEFAULT_STALE_ACKED))

    @classmethod
    def load(cls, root: Path) -> WakeConfig | None:
        try:
            data = json.loads((Path(root) / "wake.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return cls(data, Path(root))

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "adapter": self.adapter,
            "webhook": {
                "url": self.webhook_url,
                "secret": self.webhook_secret,
                "style": self.webhook_style or "github",
            },
            "jev": {
                "enabled": self.jev_enabled,
                "api_key": self.jev_api_key,
                "endpoint": self.jev_endpoint,
                "threshold": self.jev_threshold,
                "timeout": self.jev_timeout,
            },
            "retry_interval": self.retry_interval,
            "retry_max": self.retry_max,
            "stale_acked": self.stale_acked,
        }

    def save(self) -> Path:
        path = self.root / "wake.json"
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return path


def default_python() -> str:
    """Interpreter the installed units should run the drain loop with."""
    return sys.executable or "python3"


# ----------------------------------------------------------------- adapters

class WakeAdapter:
    """Delivery interface for one harness's wake mechanism."""

    name = "base"

    def deliver(self, msg: dict[str, Any]) -> bool:
        raise NotImplementedError


class HermesAdapter(WakeAdapter):
    """POST the gateway webhook. Signature format auto-adapts via
    AGENT_MAIL_SIGNATURE_STYLE (github / generic / slack) — the three
    validated receiver formats, one adapter."""

    name = "hermes"

    def deliver(self, msg: dict[str, Any]) -> bool:
        if not self.url:
            return False
        return post_message(self.url, self.secret, msg, timeout=5.0)

    def __init__(self, url: str, secret: str) -> None:
        self.url = url
        self.secret = secret


class GenericWebhookAdapter(HermesAdapter):
    """User-supplied URL + secret; same signed POST as hermes."""

    name = "generic-webhook"


class ClaudeCodeAdapter(WakeAdapter):
    """Claude Code has no gateway to POST — fall back to a terminal bell and
    a desktop notification (best-effort, never raises). The adapter slot is
    reserved for a richer integration later."""

    name = "claude-code"

    def deliver(self, msg: dict[str, Any]) -> bool:
        try:
            sys.stdout.write("\a")  # terminal bell
            sys.stdout.flush()
        except (OSError, ValueError):
            pass
        _desktop_notify(
            "agent-mailbox",
            f"{msg.get('from', '?')}: {msg.get('subject', '')}"[:120],
        )
        return True


def _desktop_notify(title: str, body: str) -> None:
    try:
        if sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{body}" with title "{title}"'],
                check=False, timeout=5,
            )
        elif sys.platform == "linux":
            subprocess.run(["notify-send", title, body], check=False, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass  # best-effort only


def make_adapter(cfg: WakeConfig) -> WakeAdapter:
    if cfg.adapter == "claude-code":
        return ClaudeCodeAdapter()
    if cfg.adapter == "generic-webhook":
        return GenericWebhookAdapter(cfg.webhook_url, cfg.webhook_secret)
    # hermes and unknown values share the signed-POST path (fail-open:
    # waking is better than silence).
    return HermesAdapter(cfg.webhook_url, cfg.webhook_secret)


# -------------------------------------------------------------------- jev

def jev_decide(cfg: WakeConfig, msg: dict[str, Any]) -> dict[str, Any] | None:
    """Ask the Jev scoring API whether this letter deserves a wake-up.

    Returns ``{"noul": bool, "score": float}`` or None on any failure.
    Pure stdlib (urllib) — zero hard dependency, the ``[jev]`` extra exists
    only as a forward-compat slot. api_key never appears in logs.
    """
    if not (cfg.jev_endpoint and cfg.jev_api_key):
        return None
    import urllib.error
    import urllib.request

    payload = json.dumps({
        "subject": msg.get("subject", ""),
        "body": (msg.get("body", "") or "")[:4000],
        "from": msg.get("from", ""),
        "priority": msg.get("priority", "normal"),
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        cfg.jev_endpoint,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.jev_api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.jev_timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {"noul": bool(data.get("noul", False)), "score": float(data.get("score", 0))}
    except (OSError, ValueError, KeyError):
        return None  # fail-open: caller wakes regardless


def jev_gate(
    cfg: WakeConfig, msg: dict[str, Any], log_path: Path
) -> tuple[bool, str]:
    """Jev routing with the fail-open iron law applied.

    Runs the scoring call on a worker thread bounded by ``jev_timeout`` — the
    main wake path never blocks longer than that, and any timeout/error falls
    back to "wake on any mail" immediately (宁多勿漏). Returns
    ``(should_wake, reason)``; every decision lands in the Jev log with its
    score so routing stays explainable.
    """
    start = time.monotonic()
    # daemon worker + bounded join: a hung Jev call can never hold the drain
    # hostage — past the deadline we fall open and the worker dies quietly.
    result_box: list[Any] = []

    def _work() -> None:
        try:
            result_box.append(jev_decide(cfg, msg))
        except Exception:  # noqa: BLE001 — fail-open on anything
            result_box.append(None)

    worker = threading.Thread(target=_work, daemon=True)
    worker.start()
    worker.join(timeout=cfg.jev_timeout + 1.0)
    result = result_box[0] if result_box else None
    latency_ms = int((time.monotonic() - start) * 1000)
    if result is None:
        decision, reason = True, "fail-open"
    elif result["noul"]:
        decision, reason = False, "noul"
    elif result["score"] < cfg.jev_threshold:
        decision, reason = False, "batch"
    else:
        decision, reason = True, "score>=threshold"
    entry = {
        "at": _now_iso(),
        "msg_id": msg.get("id", ""),
        "ok": result is not None,
        "score": result["score"] if result else None,
        "noul": result["noul"] if result else None,
        "decision": reason,
        "latency_ms": latency_ms,
    }
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return decision, reason


# ------------------------------------------------------------------- drain

def should_wake(msg: dict[str, Any], now: float, stale_acked: float) -> bool:
    """Count semantics v2: every pending letter counts; an acked letter only
    counts once it has sat acked longer than ``stale_acked`` (a handler died
    mid-turn); done/archived never count."""
    status = msg.get("status")
    if status == "pending":
        return True
    if status == "acked":
        stamp = msg.get("acked_at")
        if not stamp:
            return True  # malformed: err toward waking (宁多勿漏)
        try:
            from datetime import datetime

            acked = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return True
        return now - acked >= stale_acked
    return False


def _already_woken(m: dict[str, Any]) -> bool:
    return any(e.get("action") == "wake" for e in (m.get("handled_log") or []))


def run_once(
    root: Path,
    cfg: WakeConfig,
    *,
    adapter: WakeAdapter | None = None,
    store: MailStore | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """One drain round: scan, route (optional Jev), wake, dedup, retry.

    Never raises — the wake side failing must never take anything else down
    (fail-open iron law). Returns a stats dict for logging/tests.
    """
    stats: dict[str, Any] = {
        "scanned": 0, "due": 0, "woke": 0, "skipped_woken": 0,
        "failed": 0, "jev_skipped": 0,
    }
    try:
        store = store or MailStore(root)
        adapter = adapter or make_adapter(cfg)
        ref = time.time() if now is None else now
        jev_log = Path(root) / "wake-jev.log"
        inbox = Path(root) / "inbox" / cfg.agent_id
        if not inbox.is_dir():
            return stats
        for p in sorted(inbox.glob("*.json")):
            stats["scanned"] += 1
            try:
                m = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue  # partially-written letters wait for the next round
            stats["due"] += 1 if should_wake(m, ref, cfg.stale_acked) else 0
            if not should_wake(m, ref, cfg.stale_acked):
                continue
            if _already_woken(m):
                stats["skipped_woken"] += 1
                continue
            if cfg.jev_enabled:
                wake, reason = jev_gate(cfg, m, jev_log)
                if not wake:
                    stats["jev_skipped"] += 1
                    try:
                        store.record_handled(cfg.agent_id, m["id"], "jev_skip", note=reason)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[agent-mailbox wake] jev_skip mark failed (fail-open): {exc}",
                              file=sys.stderr, flush=True)
                    continue
            delivered = False
            for attempt in range(1, cfg.retry_max + 1):
                try:
                    delivered = adapter.deliver(m)
                except Exception:  # noqa: BLE001 — adapter bugs never kill the loop
                    delivered = False
                if delivered:
                    break
                stats["failed"] += 1
                if attempt < cfg.retry_max:
                    time.sleep(cfg.retry_interval)
            if delivered:
                # success marks the letter woken (idempotent dedup): later
                # rounds skip it even after a reclaim cycles acked->pending.
                store.record_handled(
                    cfg.agent_id, m["id"], "wake",
                    note=getattr(adapter, "name", "adapter"),
                )
                stats["woke"] += 1
            # else: every attempt failed — leave the letter un-marked so the
            # next WatchPaths trigger re-drains it. 信不丢。
    except Exception as exc:  # noqa: BLE001 — total fail-open: never raise out of drain
        print(f"[agent-mailbox wake] drain round failed (fail-open): {exc}",
              file=sys.stderr, flush=True)
    return stats


def run(root: Path, cfg: WakeConfig, poll_interval: float = 2.0, once: bool = False) -> None:
    """Drain loop. WatchPaths/path-unit mode passes ``once=True`` (the OS
    re-launches us on every inbox change); manual mode loops forever."""
    while True:
        run_once(root, cfg)
        if once:
            return
        time.sleep(poll_interval)


# ------------------------------------------------------- install / uninstall

def plist_body(cfg: WakeConfig, python_exe: str, root: Path) -> str:
    """launchd plist for one agent's wake (WatchPaths on its inbox).

    Pure string building — tests assert on the generated XML with tmp paths,
    nothing here touches the real LaunchAgents directory.
    """
    inbox = Path(root) / "inbox" / cfg.agent_id
    program = [
        python_exe, "-m", "agent_mailbox.wake", "run",
        "--root", str(Path(root)), "--agent", cfg.agent_id, "--once",
    ]
    prog_xml = "".join(f"        <string>{a}</string>\n" for a in program)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{WAKE_LABEL}-{cfg.agent_id}</string>
    <key>ProgramArguments</key>
    <array>
{prog_xml}    </array>
    <key>WatchPaths</key>
    <array>
        <string>{inbox}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{Path(root) / "wake-daemon.log"}</string>
    <key>StandardErrorPath</key>
    <string>{Path(root) / "wake-daemon.log"}</string>
</dict>
</plist>
"""


def systemd_unit_body(cfg: WakeConfig, python_exe: str, root: Path) -> dict[str, str]:
    """systemd user pair for one agent's wake: a path unit (inotify via
    PathChanged=) triggering a oneshot service. Returns {filename: body}."""
    inbox = Path(root) / "inbox" / cfg.agent_id
    exec_line = (
        f"{python_exe} -m agent_mailbox.wake run "
        f"--root {Path(root)} --agent {cfg.agent_id} --once"
    )
    return {
        f"{WAKE_LABEL}-{cfg.agent_id}.path": f"""[Unit]
Description=agent-mailbox wake trigger for {cfg.agent_id}

[Path]
PathChanged={inbox}
Unit={WAKE_LABEL}-{cfg.agent_id}.service

[Install]
WantedBy=default.target
""",
        f"{WAKE_LABEL}-{cfg.agent_id}.service": f"""[Unit]
Description=agent-mailbox wake drain for {cfg.agent_id}

[Service]
Type=oneshot
ExecStart={exec_line}
""",
    }


def _launchctl(*args: str, home: Path | None = None) -> bool:
    """Best-effort launchctl; ``home`` exists so tests can intercept."""
    try:
        subprocess.run(["launchctl", *args], check=False, timeout=15)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def install(
    cfg: WakeConfig,
    *,
    python_exe: str | None = None,
    launch_agents_dir: Path | None = None,
    systemd_dir: Path | None = None,
    activate: bool = True,
) -> dict[str, Any]:
    """Write the OS integration for this agent and (best-effort) activate it.

    macOS: plist into ``launch_agents_dir`` (default ~/Library/LaunchAgents)
    then ``launchctl load``. Linux: user units into ``systemd_dir`` (default
    ~/.config/systemd/user) then ``systemctl --user enable --now``. Pass
    ``activate=False`` (or tmp dirs) to only generate files — that is what
    the tests do; nothing here is ever run automatically at import.
    """
    py = python_exe or default_python()
    out: dict[str, Any] = {"files": [], "activated": False}
    if sys.platform == "darwin":
        target = Path(launch_agents_dir or (Path.home() / "Library" / "LaunchAgents"))
        target.mkdir(parents=True, exist_ok=True)
        plist = target / f"{WAKE_LABEL}-{cfg.agent_id}.plist"
        plist.write_text(plist_body(cfg, py, cfg.root), encoding="utf-8")
        out["files"].append(str(plist))
        out["activate_cmd"] = f"launchctl load {plist}"
        if activate:
            out["activated"] = _launchctl("load", str(plist))
    elif sys.platform == "linux":
        target = Path(systemd_dir or (Path.home() / ".config" / "systemd" / "user"))
        target.mkdir(parents=True, exist_ok=True)
        for name, body in systemd_unit_body(cfg, py, cfg.root).items():
            (target / name).write_text(body, encoding="utf-8")
            out["files"].append(str(target / name))
        out["activate_cmd"] = (
            f"systemctl --user enable --now {WAKE_LABEL}-{cfg.agent_id}.path"
        )
        if activate:
            out["activated"] = _launchctl(
                "--user", "enable", "--now", f"{WAKE_LABEL}-{cfg.agent_id}.path"
            )
    else:
        raise SystemExit(f"wake install: unsupported platform {sys.platform!r} "
                         "(run `agent-mailbox wake run` manually instead)")
    cfg.save()
    out["config"] = str(cfg.save())
    return out


def uninstall(
    agent_id: str,
    *,
    launch_agents_dir: Path | None = None,
    systemd_dir: Path | None = None,
    activate: bool = True,
) -> dict[str, Any]:
    """Remove the OS integration for one agent (config file stays — other
    agents may share the root). Missing files are fine (idempotent)."""
    out: dict[str, Any] = {"removed": [], "deactivated": False}
    if sys.platform == "darwin":
        target = Path(launch_agents_dir or (Path.home() / "Library" / "LaunchAgents"))
        plist = target / f"{WAKE_LABEL}-{agent_id}.plist"
        if activate and plist.exists():
            out["deactivated"] = _launchctl("unload", str(plist))
        if plist.exists():
            plist.unlink()
            out["removed"].append(str(plist))
    elif sys.platform == "linux":
        target = Path(systemd_dir or (Path.home() / ".config" / "systemd" / "user"))
        if activate:
            _launchctl("--user", "disable", "--now", f"{WAKE_LABEL}-{agent_id}.path")
        for suffix in (".path", ".service"):
            unit = target / f"{WAKE_LABEL}-{agent_id}{suffix}"
            if unit.exists():
                unit.unlink()
                out["removed"].append(str(unit))
    return out


# ----------------------------------------------------------------------- CLI

def _cmd_install(args: argparse.Namespace) -> None:
    root = Path(args.root or os.environ.get("AGENT_MAIL_HOME", Path.home() / ".agent-mail"))
    cfg = WakeConfig.load(root) or WakeConfig({}, root)
    if args.agent:
        cfg.agent_id = args.agent
    if not cfg.agent_id:
        raise SystemExit("wake install: --agent required (no wake.json and no --agent)")
    if args.adapter:
        cfg.adapter = args.adapter
    if args.webhook_url:
        cfg.webhook_url = args.webhook_url
    if args.webhook_secret:
        cfg.webhook_secret = args.webhook_secret
    if args.jev:
        cfg.jev_enabled = True
        cfg.jev_api_key = args.jev_api_key
        cfg.jev_endpoint = args.jev_endpoint or cfg.jev_endpoint
    if not cfg.webhook_url and cfg.adapter in ("hermes", "generic-webhook"):
        # fall back to the store-level webhook.json when wake.json carries none
        try:
            wh = json.loads((root / "webhook.json").read_text(encoding="utf-8"))
            cfg.webhook_url = str(wh.get("url", ""))
            cfg.webhook_secret = str(wh.get("secret", ""))
        except (OSError, json.JSONDecodeError):
            pass
    out = install(
        cfg,
        launch_agents_dir=args.launch_agents_dir,
        systemd_dir=args.systemd_dir,
        activate=not args.no_activate,
    )
    print(json.dumps(out, ensure_ascii=False))


def _cmd_uninstall(args: argparse.Namespace) -> None:
    out = uninstall(
        args.agent,
        launch_agents_dir=args.launch_agents_dir,
        systemd_dir=args.systemd_dir,
        activate=not args.no_activate,
    )
    print(json.dumps(out, ensure_ascii=False))


def _cmd_status(args: argparse.Namespace) -> None:
    root = Path(args.root or os.environ.get("AGENT_MAIL_HOME", Path.home() / ".agent-mail"))
    cfg = WakeConfig.load(root)
    info: dict[str, Any] = {"root": str(root), "configured": cfg is not None}
    if cfg:
        info["agent_id"] = cfg.agent_id
        info["adapter"] = cfg.adapter
        info["jev_enabled"] = cfg.jev_enabled
        if sys.platform == "darwin":
            plist = (Path.home() / "Library" / "LaunchAgents"
                     / f"{WAKE_LABEL}-{cfg.agent_id}.plist")
            info["plist_installed"] = plist.exists()
    print(json.dumps(info, ensure_ascii=False))


def _cmd_run(args: argparse.Namespace) -> None:
    root = Path(args.root or os.environ.get("AGENT_MAIL_HOME", Path.home() / ".agent-mail"))
    cfg = WakeConfig.load(root)
    if cfg is None:
        raise SystemExit(f"wake run: no wake.json in {root} — run `agent-mailbox wake install` first")
    if args.agent:
        cfg.agent_id = args.agent
    if not cfg.agent_id:
        raise SystemExit("wake run: no agent_id in wake.json — pass --agent")
    run(root, cfg, once=args.once)


def wake_main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="agent-mailbox wake", description="mail-arrived means agent-woken"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_inst = sub.add_parser("install", help="install the OS file-watcher integration")
    p_inst.add_argument("--agent", default="", help="agent inbox to watch")
    p_inst.add_argument("--adapter", default="", help="hermes | claude-code | generic-webhook")
    p_inst.add_argument("--webhook-url", default="")
    p_inst.add_argument("--webhook-secret", default="")
    p_inst.add_argument("--jev", action="store_true", help="enable the Jev router (default off)")
    p_inst.add_argument("--jev-api-key", default="")
    p_inst.add_argument("--jev-endpoint", default="")
    p_inst.add_argument("--root", default="", help="mail root (default ~/.agent-mail)")
    p_inst.add_argument("--launch-agents-dir", default=None,
                        help="override ~/Library/LaunchAgents (tests/tmp)")
    p_inst.add_argument("--systemd-dir", default=None,
                        help="override ~/.config/systemd/user (tests/tmp)")
    p_inst.add_argument("--no-activate", action="store_true",
                        help="write files only, do not load into launchd/systemd")
    p_inst.set_defaults(func=_cmd_install)

    p_un = sub.add_parser("uninstall", help="remove the OS integration for one agent")
    p_un.add_argument("agent")
    p_un.add_argument("--launch-agents-dir", default=None)
    p_un.add_argument("--systemd-dir", default=None)
    p_un.add_argument("--no-activate", action="store_true")
    p_un.set_defaults(func=_cmd_uninstall)

    p_run = sub.add_parser("run", help="run one drain round (--once) or loop")
    p_run.add_argument("--agent", default="")
    p_run.add_argument("--root", default="")
    p_run.add_argument("--once", action="store_true", help="one round then exit (WatchPaths mode)")
    p_run.set_defaults(func=_cmd_run)

    p_st = sub.add_parser("status", help="show wake configuration and install state")
    p_st.add_argument("--root", default="")
    p_st.set_defaults(func=_cmd_status)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    wake_main()
