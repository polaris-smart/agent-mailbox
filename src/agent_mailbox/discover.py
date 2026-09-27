"""Four-layer auto-discovery (v0.7.5 §3.2): who is installed, who is
connected, how to wake them — and (L4) a live test letter.

Layer contract (task brief §3.2):

- **L1 装了没** — known CLIs on PATH, ``/Applications/*.app`` bundles, user
  config dirs → candidate members + kind.
- **L2 接了没** — known MCP client configs scanned for a mounted mailbox +
  the local registry (真名册).
- **L3 怎么叫醒** — ① app ``Info.plist`` → ``CFBundleURLTypes`` URL schemes
  ② listening ports reverse-looked-up **by executable path** (never by
  process name — WorkBuddy's process is just ``Electron``) ③ usable
  commands (absolute paths) ④ our own wake integration (wake.json/plist).
- **L4 实测** — :func:`test_member` sends a real test letter and waits for
  the receipt (status moves off ``pending`` / handled_log grows).

Iron rules from the brief:

- L1–L3 are strictly read-only. The only file this module writes is the
  discovery fingerprint inside **our own** mail root
  (``discover-fingerprint.json``).
- kind is never guessed: signals that do not resolve to a catalog member are
  reported as 未识别 (unknown), never promoted.
- every channel carries its **source** (which config file / which
  Info.plist / which port reverse-lookup) so each finding is auditable.
- credentials never surface: webhook URLs are masked, secrets never read.

All system probes go through an injectable runner and injectable paths, so
tests run against fixture homes / fake plists / canned ``lsof`` output and
never depend on the real machine.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

try:  # stdlib TOML parser, Python 3.11+; text-scan fallback on 3.10
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py3.10 only
    tomllib = None  # type: ignore[assignment]

from .wake import WAKE_LABEL

# --------------------------------------------------------------- catalog
# The supported roster. Every field is evidence used to *confirm* identity —
# a member is only reported when at least one signal actually matches.
CATALOG: dict[str, dict[str, tuple[str, ...]]] = {
    "claude": {
        "clis": ("claude",),
        "config_dirs": (".claude",),
        "apps": ("Claude", "Claude Code"),
        "mcp_configs": (
            ".claude.json",
            "Library/Application Support/Claude/claude_desktop_config.json",
            ".config/Claude/claude_desktop_config.json",
        ),
        "schemes": ("claude",),
    },
    "codex": {
        "clis": ("codex",),
        "config_dirs": (".codex",),
        "apps": ("Codex",),
        "mcp_configs": (".codex/config.toml",),
        "schemes": ("codex",),
    },
    "workbuddy": {
        "clis": ("workbuddy",),
        "config_dirs": (".workbuddy",),
        "apps": ("WorkBuddy",),
        "mcp_configs": (".workbuddy/mcp.json",),
        "schemes": ("workbuddy",),
    },
    "zcode": {
        "clis": ("zcode",),
        "config_dirs": (".zcode",),
        "apps": ("ZCode",),
        "mcp_configs": (),
        "schemes": ("zcode",),
    },
    "hermes": {
        "clis": ("hermes",),
        "config_dirs": (".hermes",),
        "apps": ("Hermes",),
        "mcp_configs": (".hermes/config.yaml",),
        "schemes": ("hermes",),
    },
    "dsh": {
        "clis": ("dsh",),
        "config_dirs": (),
        "apps": (),
        "mcp_configs": (),
        "schemes": (),
    },
    "gemini": {
        "clis": ("gemini",),
        "config_dirs": (".gemini",),
        "apps": (),
        "mcp_configs": (),
        "schemes": (),
    },
    "opencode": {
        "clis": ("opencode",),
        "config_dirs": (".config/opencode",),
        "apps": (),
        "mcp_configs": (".config/opencode/opencode.json",),
        "schemes": (),
    },
    "codebuddy": {
        "clis": ("codebuddy",),
        "config_dirs": (".codebuddy",),
        "apps": ("CodeBuddy",),
        "mcp_configs": (".codebuddy/mcp.json",),
        "schemes": (),
    },
    "cursor": {
        "clis": ("cursor", "cursor-agent"),
        "config_dirs": (".cursor",),
        "apps": ("Cursor",),
        "mcp_configs": (".cursor/mcp.json",),
        "schemes": (),
    },
}

SUPPORTED: tuple[str, ...] = tuple(CATALOG)
MAILBOX_TOKENS = ("agent-mailbox", "agent_mailbox")
FINGERPRINT_FILE = "discover-fingerprint.json"

Runner = Callable[[list[str]], tuple[int, str]]


def _default_runner(cmd: list[str], timeout: float = 10.0) -> tuple[int, str]:
    """Real system probe: run a command, return (rc, stdout). Never raises."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return proc.returncode, proc.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return 1, ""


@dataclass
class DiscoverContext:
    """Everything the engine touches, injectable for tests."""

    root: Path
    home: Path = field(default_factory=Path.home)
    path_env: str = field(default_factory=lambda: os.environ.get("PATH", ""))
    apps_dirs: tuple[Path, ...] = ()
    deep_dirs: tuple[Path, ...] = ()
    launch_agents_dir: Path | None = None
    runner: Runner | None = None
    probe_versions: bool = False  # deep mode only: run `<cli> --version`
    probe_urls: bool = True  # health-check webhook URLs (connection probe only)

    def run(self, cmd: list[str]) -> tuple[int, str]:
        return (self.runner or _default_runner)(cmd)


def default_context(
    root: Path | None = None,
    *,
    deep_dirs: tuple[Path, ...] = (),
    probe_versions: bool = False,
) -> DiscoverContext:
    """Build the real-machine context (injectable knobs left at defaults)."""
    home = Path.home()
    apps = (Path("/Applications"), home / "Applications")
    return DiscoverContext(
        root=Path(root or os.environ.get("AGENT_MAIL_HOME", home / ".agent-mail")).expanduser(),
        home=home,
        apps_dirs=apps,
        deep_dirs=deep_dirs,
        launch_agents_dir=home / "Library" / "LaunchAgents",
        probe_versions=probe_versions,
    )


# ------------------------------------------------------------ time (§5⑪)


def now_local() -> str:
    """Human timestamp in the machine's local timezone."""
    dt = datetime.now().astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S %z")


def fmt_local(iso_utc: str | None) -> str:
    """Render an ISO-UTC stamp in the local timezone; unparseable → as-is."""
    if not iso_utc:
        return "-"
    try:
        dt = datetime.fromisoformat(str(iso_utc).replace("Z", "+00:00"))
    except ValueError:
        return str(iso_utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


# ------------------------------------------------------------------ L1


def _which_all(ctx: DiscoverContext, names: tuple[str, ...]) -> dict[str, str]:
    found: dict[str, str] = {}
    for name in names:
        p = shutil.which(name, path=ctx.path_env)
        if p:
            found[name] = p
    return found


def _apps_in(dirs: tuple[Path, ...]) -> dict[str, Path]:
    """Bundle name (without .app) -> bundle path, across the given dirs."""
    out: dict[str, Path] = {}
    for d in dirs:
        try:
            if not d.is_dir():
                continue
            for child in d.iterdir():
                if child.name.endswith(".app") and child.is_dir():
                    out.setdefault(child.name[:-4], child)
        except OSError:
            continue
    return out


def scan_l1(ctx: DiscoverContext) -> dict[str, dict[str, Any]]:
    """Per catalog member: CLI paths, app bundles, config dirs (read-only)."""
    apps = _apps_in(ctx.apps_dirs + ctx.deep_dirs)
    out: dict[str, dict[str, Any]] = {}
    for name, spec in CATALOG.items():
        clis = _which_all(ctx, spec["clis"])
        found_apps = {
            alias: apps[alias]
            for alias in spec["apps"]
            if alias in apps  # exact bundle-name match only — no fuzzy guessing
        }
        config_dirs: dict[str, Path] = {}
        for rel in spec["config_dirs"]:
            for base in (ctx.home, *ctx.deep_dirs):
                p = base / rel
                if p.exists():
                    config_dirs[str(p)] = p
                    break
        out[name] = {
            "clis": clis,
            "apps": found_apps,
            "config_dirs": config_dirs,
            "signals": bool(clis or found_apps or config_dirs),
        }
    return out


# ------------------------------------------------------------------ L2


def _tokens_in(obj: Any) -> bool:
    if isinstance(obj, str):
        return any(tok in obj for tok in MAILBOX_TOKENS)
    if isinstance(obj, dict):
        return any(_tokens_in(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_tokens_in(v) for v in obj)
    return False


def _find_mcp_entry(obj: Any, depth: int = 0) -> str | None:
    """Locate the MCP server entry name that mounts the mailbox (JSON)."""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        for key, val in obj.items():
            if isinstance(val, dict) and key.lower().replace("_", "").replace("-", "") in (
                "mcpservers",
                "servers",
                "mcp",
            ):
                for entry, cfg in val.items():
                    if _tokens_in(cfg):
                        return str(entry)
        for val in obj.values():
            hit = _find_mcp_entry(val, depth + 1)
            if hit:
                return hit
    elif isinstance(obj, list):
        for val in obj:
            hit = _find_mcp_entry(val, depth + 1)
            if hit:
                return hit
    return None


def _scan_config_file(path: Path) -> dict[str, Any] | None:
    """One MCP client config → detection verdict (never echoes contents)."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    parsed, entry = False, None
    if path.suffix.lower() == ".json" and path.stat().st_size < 8 * 1024 * 1024:
        try:
            data = json.loads(raw)
            parsed, entry = True, _find_mcp_entry(data)
        except (json.JSONDecodeError, ValueError):
            pass
    elif path.suffix.lower() == ".toml" and tomllib is not None:
        try:
            data = tomllib.loads(raw)
            parsed, entry = True, _find_mcp_entry(data)
        except ValueError:  # TOMLDecodeError ⊂ ValueError
            pass
    detected = any(tok in raw for tok in MAILBOX_TOKENS)
    if not detected:
        return {"path": str(path), "exists": True, "detected": False, "parsed": parsed}
    return {
        "path": str(path),
        "exists": True,
        "detected": True,
        "parsed": parsed,
        "entry": entry,  # None → detected by text scan, entry not located
    }


def _registry_names(root: Path) -> list[str]:
    """The local 真名册: registered agent ids (read-only, no mkdir)."""
    try:
        reg = json.loads((root / "registry.json").read_text(encoding="utf-8"))
        return sorted(str(k) for k in reg.get("agents", {}))
    except (OSError, json.JSONDecodeError, AttributeError):
        return []


def scan_l2(ctx: DiscoverContext) -> dict[str, dict[str, Any]]:
    """Who already has the mailbox mounted (MCP configs + registry)."""
    out: dict[str, dict[str, Any]] = {}
    for name, spec in CATALOG.items():
        findings = []
        for rel in spec["mcp_configs"]:
            p = (ctx.home / rel).expanduser()
            if p.exists():
                res = _scan_config_file(p)
                if res:
                    findings.append(res)
        out[name] = {"mcp_configs": findings}
    out["_registry"] = {"names": _registry_names(ctx.root)}
    return out


# ------------------------------------------------------------------ L3


def mask_url(url: str) -> str:
    """Strip query + userinfo from a URL — tokens must never be echoed."""
    try:
        sp = urlsplit(url)
    except ValueError:
        return "<url>"
    netloc = sp.hostname or ""
    if sp.port:
        netloc = f"{netloc}:{sp.port}"
    return urlunsplit((sp.scheme, netloc, sp.path, "", ""))


def _bundle_schemes(app_path: Path) -> tuple[list[str], str | None]:
    """URL schemes declared in a bundle's Info.plist (macOS; None on miss)."""
    plist = app_path / "Contents" / "Info.plist"
    try:
        data = plistlib.loads(plist.read_bytes())
        schemes: list[str] = []
        for item in data.get("CFBundleURLTypes", []) or []:
            schemes.extend(str(s) for s in item.get("CFBundleURLSchemes", []) or [])
        return schemes, str(plist)
    except (OSError, ValueError, plistlib.InvalidFileException, AttributeError):
        return [], None


def _listening_ports(ctx: DiscoverContext) -> list[tuple[int, str]]:
    """(port, pid) for TCP listeners; [] when lsof is unavailable."""
    rc, out = ctx.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "+c0"])
    if rc != 0 or not out:
        return []
    rows: list[tuple[int, str]] = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue
        name = parts[-1]
        if name.endswith(")"):  # state glued to NAME on some platforms
            if len(parts) < 10:
                continue
            name = parts[-2]
        port_str = name.rpartition(":")[2]
        if not port_str.isdigit():
            continue
        rows.append((int(port_str), parts[1]))
    return rows


def _pid_exe(ctx: DiscoverContext, pid: str) -> str:
    """Executable path for a PID — ps argv[0] first, lsof txt-map fallback.

    This is the path-based reverse lookup the brief mandates: WorkBuddy's
    process *name* is just ``Electron``, but its executable *path* still
    lives inside ``…/WorkBuddy.app/Contents/MacOS/``.
    """
    _rc, out = ctx.run(["ps", "-p", pid, "-o", "command="])
    argv0 = out.strip().split(" ", 1)[0] if out.strip() else ""
    if argv0.startswith("/"):
        return argv0
    _rc2, out2 = ctx.run(["lsof", "-a", "-p", pid, "-d", "txt", "-Fn"])
    for line in out2.splitlines():
        if line.startswith("n/"):
            return line[1:].strip()
    return argv0


def _member_for_exe(exe: str) -> str | None:
    """Resolve an executable path to a catalog member — bundle path match
    first (…/WorkBuddy.app/…), then bare binary-name match. No match → None
    (unidentified; reported, never guessed)."""
    for member, spec in CATALOG.items():
        for alias in spec["apps"]:
            if f"/{alias}.app/" in exe:
                return member
    base = Path(exe).name
    for member, spec in CATALOG.items():
        if base in spec["clis"]:
            return member
    return None


def _cli_version(ctx: DiscoverContext, cli_path: str) -> str | None:
    rc, out = ctx.run([cli_path, "--version"])
    if rc != 0 or not out.strip():
        return None
    return " ".join(out.strip().splitlines()[0].split())[:80] or None


def _probe_url(url: str, timeout: float = 1.5) -> tuple[bool, str]:
    """Connection probe only: any HTTP answer counts as reachable."""
    req = urllib.request.Request(url, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open(req, timeout=timeout)
        return True, ""
    except urllib.error.HTTPError:
        return True, ""  # an HTTP server answered — someone is listening
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, str(getattr(exc, "reason", exc))


def _wake_channels(ctx: DiscoverContext, member: str) -> list[dict[str, Any]]:
    """Channels coming from *our own* integration (wake.json / launchd)."""
    channels: list[dict[str, Any]] = []
    wake_json = ctx.root / "wake.json"
    try:
        data = json.loads(wake_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if str(data.get("agent_id", "")) == member:
        webhook = data.get("webhook") or {}
        adapter = str(data.get("adapter", ""))
        url = str(webhook.get("url", ""))
        if adapter in ("hermes", "generic-webhook") and url:
            channels.append(
                {
                    "type": "webhook",
                    "value": mask_url(url),
                    "probe_url": url,  # engine-internal; stripped before output
                    "source": str(wake_json),
                }
            )
        elif adapter == "claude-code":
            channels.append(
                {
                    "type": "claude-code",
                    "value": "终端响铃 + 系统通知",
                    "source": str(wake_json),
                }
            )
    la_dir = ctx.launch_agents_dir
    if la_dir is not None:
        plist = Path(la_dir) / f"{WAKE_LABEL}-{member}.plist"
        if plist.exists():
            channels.append(
                {
                    "type": "mailbox_wake",
                    "value": "launchd WatchPaths（邮件到达即唤醒）",
                    "source": str(plist),
                }
            )
    return channels


def _channel_health(ctx: DiscoverContext, ch: dict[str, Any]) -> dict[str, Any]:
    """Fill status/reason/next_step for one channel. ✅/❌/未实测 only —
    never a silent pass."""
    out = dict(ch)
    out.pop("probe_url", None)
    ctype = ch.get("type")
    if ctype == "webhook":
        if not ctx.probe_urls:
            out.update(status="unknown", reason="未探测（--no-probe）", next_step="")
            return out
        ok, err = _probe_url(ch["probe_url"])
        if ok:
            out.update(status="ok", reason="唤醒地址有响应", next_step="")
        else:
            out.update(
                status="broken",
                reason=f"唤醒地址连不上（{err or '无响应'}）——服务可能没起或地址写错",
                next_step="启动对应接收服务，或检查 wake.json 里的 webhook 配置；"
                "改完后可运行 agent-mailbox wake install 重建",
            )
    elif ctype == "command":
        p = Path(ch["value"])
        if p.exists():
            out.update(status="ok", reason="命令存在", next_step="")
        else:
            out.update(
                status="broken",
                reason=f"命令 {ch['value']} 已不存在（可能被移动/卸载）",
                next_step="重跑 agent-mailbox discover 刷新发现结果",
            )
    elif ctype == "port":
        out.update(status="ok", reason="发现时正在监听", next_step="")
    elif ctype == "url_scheme":
        out.update(
            status="unknown",
            reason="本机无法静默验证 scheme 注册（不猜）",
            next_step="如需用该 scheme 唤醒，请在宿主应用中实测一次",
        )
    else:  # mailbox_wake / claude-code: our own files — existence was the check
        out.update(status="ok", reason="集成在位", next_step="")
    return out


def scan_l3(ctx: DiscoverContext, l1: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Wake channels per member: scheme / port / command / our integration."""
    ports = _listening_ports(ctx)
    by_member: dict[str, list[dict[str, Any]]] = {m: [] for m in CATALOG}
    other: list[dict[str, str]] = []
    seen_exe_port: set[tuple[str, int]] = set()
    for port, pid in ports:
        exe = _pid_exe(ctx, pid)
        if not exe or (exe, port) in seen_exe_port:
            continue
        seen_exe_port.add((exe, port))
        member = _member_for_exe(exe)
        if member is None:
            if len(other) < 10:
                other.append({"exe": exe, "port": str(port)})
            continue
        by_member[member].append(
            {
                "type": "port",
                "value": str(port),
                "source": f"lsof 端口 {port} → 可执行路径 {exe}",
            }
        )
    for member in CATALOG:
        # ① URL scheme straight from the bundle's Info.plist
        for app_path in l1[member]["apps"].values():
            schemes, plist_src = _bundle_schemes(app_path)
            for scheme in schemes:
                by_member[member].append(
                    {
                        "type": "url_scheme",
                        "value": f"{scheme}://",
                        "source": plist_src or str(app_path),
                    }
                )
        # ③ usable commands (absolute paths from L1)
        for cli_path in l1[member]["clis"].values():
            by_member[member].append({"type": "command", "value": cli_path, "source": "PATH"})
        # ④ our own wake integration
        by_member[member].extend(_wake_channels(ctx, member))
    out = {m: [_channel_health(ctx, ch) for ch in chans] for m, chans in by_member.items()}
    out["_other_listeners"] = other  # type: ignore[assignment]
    return out


# ------------------------------------------------------------ fingerprint (§5⑨)


def load_fingerprint(root: Path) -> dict[str, Any]:
    try:
        return json.loads((Path(root) / FINGERPRINT_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_fingerprint(root: Path, fp: dict[str, Any]) -> Path:
    path = Path(root) / FINGERPRINT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fp, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _identity_fingerprint(
    member: str, l1: dict[str, dict[str, Any]], versions: dict[str, str | None]
) -> dict[str, Any]:
    """Stable identity only: paths/scheme/version — never ports or URLs
    (those change run-to-run and would flap the staleness flag)."""
    entry = l1[member]
    schemes: list[str] = []
    for app_path in entry["apps"].values():
        found, _ = _bundle_schemes(app_path)
        schemes.extend(found)
    fp: dict[str, Any] = {
        "cli": sorted(entry["clis"].values()),
        "app": sorted(str(p) for p in entry["apps"].values()),
        "scheme": sorted(set(schemes)),
    }
    if versions.get(member):
        fp["version"] = versions[member]
    return fp


def fingerprint_changes(old: Any, new: Any) -> list[str]:
    if not isinstance(old, dict):
        return ["首次记录"]
    changed = [key for key in set(old) | set(new) if old.get(key) != new.get(key)]
    return sorted(changed)


# ------------------------------------------------------------------ report


def build_report(ctx: DiscoverContext, *, save: bool = True) -> dict[str, Any]:
    """Full four-layer report. Writes only the fingerprint in our own root."""
    l1 = scan_l1(ctx)
    l2 = scan_l2(ctx)
    l3 = scan_l3(ctx, l1)
    registry = l2["_registry"]["names"]

    versions: dict[str, str | None] = {}
    if ctx.probe_versions:
        for member in CATALOG:
            for cli_path in l1[member]["clis"].values():
                versions[member] = _cli_version(ctx, cli_path)
                break

    members: list[dict[str, Any]] = []
    for name in CATALOG:
        sig = l1[name]
        if not sig["signals"]:
            continue
        mcp_hits = [f for f in l2[name]["mcp_configs"] if f["detected"]]
        registered = name in registry
        kind = "cli" if sig["clis"] else ("app" if sig["apps"] else "config")
        evidence: list[dict[str, str]] = []
        for cli_path in sig["clis"].values():
            evidence.append({"layer": "L1", "type": "cli", "source": "PATH", "detail": cli_path})
        for app_path in sig["apps"].values():
            evidence.append(
                {"layer": "L1", "type": "app", "source": str(app_path), "detail": app_path.name}
            )
        for cfg_path in sig["config_dirs"]:
            evidence.append(
                {"layer": "L1", "type": "config_dir", "source": str(cfg_path), "detail": "存在"}
            )
        for hit in l2[name]["mcp_configs"]:
            if hit["detected"]:
                where = f"条目 {hit['entry']}" if hit.get("entry") else "文本命中（条目未定位）"
                evidence.append(
                    {
                        "layer": "L2",
                        "type": "mcp_config",
                        "source": hit["path"],
                        "detail": f"已挂 agent-mailbox（{where}）",
                    }
                )
            else:
                evidence.append(
                    {
                        "layer": "L2",
                        "type": "mcp_config",
                        "source": hit["path"],
                        "detail": "配置存在，未见 mailbox",
                    }
                )
        if registered:
            evidence.append(
                {
                    "layer": "L2",
                    "type": "registry",
                    "source": str(ctx.root / "registry.json"),
                    "detail": "已在本地名册注册",
                }
            )
        fp = _identity_fingerprint(name, l1, versions)
        members.append(
            {
                "member": name,
                "kind": kind,
                "registered": registered,
                "connected": bool(mcp_hits) or registered,
                "evidence": evidence,
                "channels": l3[name],
                "fingerprint": fp,
            }
        )
    # Registry-only members (真名册里有、目录里认不出的): report honestly as
    # 未识别 — the brief forbids guessing a kind.
    for agent in registry:
        if agent in CATALOG:
            continue
        members.append(
            {
                "member": agent,
                "kind": "unknown",
                "kind_label": "未识别（已注册）",
                "registered": True,
                "connected": True,
                "evidence": [
                    {
                        "layer": "L2",
                        "type": "registry",
                        "source": str(ctx.root / "registry.json"),
                        "detail": "已在本地名册注册；目录/配置里认不出形态，不猜",
                    }
                ],
                "channels": [],
                "fingerprint": {},
            }
        )

    old_fp = load_fingerprint(ctx.root)
    for m in members:
        if not m["fingerprint"]:
            m["fingerprint_changed"] = []
            m["stale"] = False
            continue
        changed = fingerprint_changes(old_fp.get(m["member"]), m["fingerprint"])
        m["fingerprint_changed"] = changed
        m["stale"] = bool(old_fp) and changed != ["首次记录"] and bool(changed)
        if m["stale"]:
            m["stale_note"] = (
                "发现指纹已变化（{}）——环境可能升级/移动，建议重跑 "
                "agent-mailbox test {} 复测".format("、".join(changed), m["member"])
            )

    report: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        "generated_at_local": now_local(),
        "root": str(ctx.root),
        "members": members,
        "other_listeners": l3["_other_listeners"],
        "supported": list(SUPPORTED),
        "empty": not members,
    }
    if save:
        save_fingerprint(
            ctx.root, {m["member"]: m["fingerprint"] for m in members if m["fingerprint"]}
        )
    return report


# ------------------------------------------------------- L4: test letter


def _member_exists(member: str, report: dict[str, Any]) -> bool:
    return any(m["member"] == member for m in report["members"])


def _diagnose_failure(member: str, report: dict[str, Any]) -> tuple[str, str]:
    """Human failure reason + next step for a member that never picked up."""
    entry = next((m for m in report["members"] if m["member"] == member), None)
    if entry is None:
        known = ", ".join(m["member"] for m in report["members"]) or "（无）"
        return (
            f"不认识成员 {member!r}——本机没有发现它，名册里也没有",
            f"先运行 agent-mailbox discover 查看本机有哪些成员（当前：{known}）",
        )
    chans = entry.get("channels") or []
    broken = [c for c in chans if c.get("status") == "broken"]
    if broken:
        c = broken[0]
        return f"唤醒通道不通：{c.get('reason', '')}", c.get("next_step", "") or "修复该通道后重试"
    if not chans:
        return (
            "信已放进收件箱，但该成员没有任何已知唤醒通道——没人会被叫醒",
            (
                "把 agent-mailbox 挂进它的 MCP 配置（agent-mailbox connect "
                f"{member} --yes），或让它主动运行 mailbox_check"
            ),
        )
    if not entry.get("connected"):
        return (
            "有候选通道，但 agent-mailbox 还没接入该成员（装了未接）",
            f"运行 agent-mailbox connect {member} --yes 接入，或让它运行 mailbox_check",
        )
    return (
        "通道看着都在，但成员没有来取信（会话可能没在跑、或没到取信时机）",
        (
            f"确认 {member} 的会话在运行并会调用 mailbox_check；或检查它的唤醒集成"
            "（python -m agent_mailbox.wake status）"
        ),
    )


def test_member(
    member: str,
    root: Path | None = None,
    *,
    timeout: float = 60.0,
    from_id: str = "",
    poll: float = 0.25,
) -> dict[str, Any]:
    """L4: send a real test letter and wait for the receipt.

    Receipt = the letter's status leaves ``pending`` (mailbox_check / claim /
    done) or its handled_log grows. Never raises; returns a human-readable
    result dict with ``ok`` plus ``reason``/``next_step`` either way.
    """
    root = Path(root or os.environ.get("AGENT_MAIL_HOME", Path.home() / ".agent-mail"))
    report = build_report(default_context(root), save=False)
    started = time.monotonic()

    from .store import MailStore  # local import: engine stays import-light

    st = MailStore(root)
    frm = from_id or os.environ.get("AGENT_MAIL_ID", "") or "tester"
    subject = "[agent-mailbox] 测试信（可安全忽略）"
    body = (
        f"这是一封投递测试信，发给 {member}，用于验证通道。\n"
        "回执方式：运行 mailbox_check 即视为已收；mailbox_done 即已处理。\n"
        f"发送时间：{now_local()}"
    )
    try:
        sent = st.send(frm, member, subject, body, dedupe=False)
    except Exception as exc:  # noqa: BLE001 — human error out, no traceback
        ok, elapsed = False, time.monotonic() - started
        reason, nxt = _diagnose_failure(member, report)
        if "invalid agent id" in str(exc):
            reason = f"成员名 {member!r} 不合法（只允许字母数字_-，且不以符号开头）"
            nxt = "换一个名字，或运行 agent-mailbox discover 查看已发现的成员"
        return {
            "member": member,
            "ok": ok,
            "stage": "send_failed",
            "elapsed_s": round(elapsed, 1),
            "letter_id": None,
            "reason": reason,
            "next_step": nxt,
            "checked_at_local": now_local(),
        }
    letter_id = sent[0].get("id", "")
    letter_path = root / "inbox" / member / f"{letter_id}.json"

    stage, snapshot = "pending", None
    deadline = started + max(0.5, timeout)
    while time.monotonic() < deadline:
        try:
            m = json.loads(letter_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            m = {}
        status = m.get("status", "pending")
        log_len = len(m.get("handled_log") or [])
        if status != "pending" or (snapshot is not None and log_len > snapshot):
            stage = "done" if status == "done" else "acked"
            break
        snapshot = log_len
        time.sleep(poll)
    elapsed = time.monotonic() - started
    result: dict[str, Any] = {
        "member": member,
        "elapsed_s": round(elapsed, 1),
        "letter_id": letter_id,
        "checked_at_local": now_local(),
    }
    if stage != "pending":
        verdict = "已取走并处理（done）" if stage == "done" else "已取走（回执 acked）"
        result.update(
            ok=True,
            stage=stage,
            reason=f"测试信{verdict}",
            next_step="通道正常，可以正常收发。",
        )
    else:
        reason, nxt = _diagnose_failure(member, report)
        result.update(
            ok=False,
            stage="timeout",
            reason=f"等待 {timeout:g}s 没等到回执——{reason}",
            next_step=nxt,
        )
    return result
