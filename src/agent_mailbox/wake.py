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

t-56 claim-first 投递修复（HS 20260929001242 多窗重复回信）: every delivery
route claims BEFORE it delivers — ``reap_stale_acked`` (orphan rescue) then
``claim`` (store.py, atomic), and only the claimed letters go out. A letter
another window already claimed is never delivered twice: the loser's round
audits ``claim_denied`` (deduped per letter) and pulls no session. The
shared primitive is :func:`claim_route_mail`; the ``wake claim`` /
``wake release`` subcommands expose the same discipline to the belt script
and to out-of-repo consumers (the webhook gateway alignment point, see
webhook.py). Each round stamps handled_log ``by`` with ``wake:<runid>``
(J3 auditability).

Jev (F3) plugs in here as an optional, default-off router — see jev.py.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .store import MailStore, _now_iso, load_visibility
from .webhook import post_message

WAKE_LABEL = "com.polaris-smart.agent-mailbox-wake"  # + "-<agent>" per instance
DEFAULT_RETRY_INTERVAL = 60.0
DEFAULT_RETRY_MAX = 5
DEFAULT_STALE_ACKED = 600.0  # count semantics v2: acked older than this counts
DEFAULT_COMMAND_TIMEOUT = 300.0  # local-command adapter: hard-kill deadline

# 锚A (t-50②, HS 0.7.5 收口施工单): every wake attempt — success or failure —
# appends one JSON line to ``<root>/wake-attempts.jsonl`` (0600, append-only).
# Eight fixed fields so the file stays greppable/jq-able:
#   ts / agent / route(belt|daemon|sampling) / attempt / outcome(ok|fail)
#   / error_class / executor / latency_ms
# Belt (wake-zc.sh) writes the same shape natively in bash; the G1 主判据
# counts attempts from this file, not from process logs.
WAKE_ATTEMPTS_FILE = "wake-attempts.jsonl"
# U2 可见通道: when every delivery attempt for a letter has failed, the drain
# drops an alert letter so a broken wake is readable from the mailbox face
# alone (no shell needed). Marked on the letter via ``handled_log`` so one
# letter alerts at most once — retries on later WatchPaths triggers stay
# silent (anti-flood), wake itself retries.
#
# t-59（A-2）告警收件人收敛: 只发负责方 HS。boss 席仅存档语义——老板只看
# 飞书、不看信箱，自动告警发 boss 席等于没送达还制造噪音（09-29 任务书
# §0.2：``wake-fail alert sent to ['HS','boss']`` 越界）。急事走飞书，不在
# 本代码里恢复 boss 收件人。
WAKE_ALERT_RECIPIENT = "HS"
WAKE_ALERT_ACTION = "wake_alert"


def record_wake_attempt(
    root: Path | str,
    agent: str,
    route: str,
    attempt: int,
    outcome: str,
    error_class: str = "",
    executor: str = "",
    latency_ms: int | None = None,
) -> bool:
    """Append one attempt row to ``<root>/wake-attempts.jsonl`` (锚A).

    Append-only, created 0600, and fail-open by iron law: an anchor write
    problem must never disturb the wake itself — any error is swallowed
    after a stderr note. Returns True when a row landed.
    """
    row = {
        "ts": _now_iso(),
        "agent": str(agent),
        "route": str(route),
        "attempt": int(attempt),
        "outcome": "ok" if outcome == "ok" else "fail",
        "error_class": str(error_class or ""),
        "executor": str(executor or ""),
        "latency_ms": int(latency_ms) if latency_ms is not None else None,
    }
    try:
        path = Path(root) / WAKE_ATTEMPTS_FILE
        line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
        return True
    except OSError as exc:
        print(f"[agent-mailbox wake] anchor write failed (fail-open): {exc}", file=sys.stderr)
        return False


# ------------------------------------------------------------------- config

# WakeConfig 认识的顶层键；其余（如 "custom_future_key"）在 load→save 往返
# 中原样保留，绝不静默丢弃（t-37）。t-58（A-1）：顶层 "agents" 段收编为显式
# 已知键——per-agent 唤醒路由（adapter/command/webhook）与 sampling 的
# per-agent policy 共居同一段，未知子键照旧原样保留。
_KNOWN_KEYS = frozenset(
    {
        "agent_id",
        "adapter",
        "webhook",
        "command",
        "timeout",
        "jev",
        "retry_interval",
        "retry_max",
        "stale_acked",
        "agents",
        "digest_fallback",
    }
)


@dataclasses.dataclass(frozen=True)
class WakeRoute:
    """t-58（A-1）: 一个身份解析后的生效唤醒通道。

    解析优先级（逐键独立）：``--adapter/--command/--webhook-url`` CLI 覆盖 >
    ``agents.<ID>`` per-agent 段 > 全局 adapter/webhook > 内置默认（hermes）。
    ``sources`` 记录每个键的来源（``cli`` / ``agents:<ID>`` / ``global`` /
    ``default``），doctor ④ 收件人路由检查直接可读——「这条通道是谁配的」
    不再靠猜。
    """

    adapter: str = "hermes"
    command: tuple[str, ...] = ()
    webhook_url: str = ""
    webhook_secret: str = ""
    webhook_style: str | None = None
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT
    sources: dict[str, str] = dataclasses.field(default_factory=dict)

    def to_display(self) -> dict[str, Any]:
        """doctor/status 用的一行式摘要（secret 不落输出）。"""
        return {
            "adapter": self.adapter,
            "command": list(self.command),
            "webhook_url": self.webhook_url,
            "sources": dict(self.sources),
        }


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
        # t-37 同根因·嵌套层：webhook/jev 段的未知子键也原样保留。
        self._webhook_extra = (
            {k: v for k, v in webhook.items() if k not in ("url", "secret", "style")}
            if isinstance(webhook, dict)
            else {}
        )
        # local-command adapter: argv list only — a bare string is rejected
        # (there is no shell-concatenation wake path, ever).
        raw_command = data.get("command")
        self.command = (
            [str(a) for a in raw_command] if isinstance(raw_command, (list, tuple)) else []
        )
        self.command_timeout = float(data.get("timeout", DEFAULT_COMMAND_TIMEOUT))
        jev = data.get("jev") or {}
        self.jev_enabled = bool(jev.get("enabled", False))
        self.jev_api_key = str(jev.get("api_key", ""))
        self.jev_endpoint = str(jev.get("endpoint", ""))
        self.jev_threshold = float(jev.get("threshold", 3.0))
        self.jev_timeout = float(jev.get("timeout", 3.0))
        self._jev_extra = (
            {
                k: v
                for k, v in jev.items()
                if k not in ("enabled", "api_key", "endpoint", "threshold", "timeout")
            }
            if isinstance(jev, dict)
            else {}
        )
        self.retry_interval = float(data.get("retry_interval", DEFAULT_RETRY_INTERVAL))
        self.retry_max = int(data.get("retry_max", DEFAULT_RETRY_MAX))
        self.stale_acked = float(data.get("stale_acked", DEFAULT_STALE_ACKED))
        # t-62（判据7/S4）LLM 可选: 投递失败时自动降级 digest 纯本地路径
        # （摘要落盘 + 标 done + 告警照发），默认开——「CLI 未登录即全断」
        # 从根上消除；显式置 false 恢复 0.7.5 语义（信留在收件箱重投）。
        self.digest_fallback = bool(data.get("digest_fallback", True))
        # t-58（A-1）: 顶层 "agents" 段——per-agent 唤醒路由（adapter/command/
        # webhook）与 sampling per-agent policy 共居。整段原样持有（未知子键
        # 一起保留），写路由只动自己的三键，sampling 键绝不被抹。
        agents_raw = data.get("agents")
        self._agents: dict[str, Any] = (
            {str(k): v for k, v in agents_raw.items()} if isinstance(agents_raw, dict) else {}
        )
        # CLI 覆盖（仅内存态，永不落盘）：wake run --adapter/--command/
        # --webhook-url 的最高优先级覆写，见 effective_route。
        self._cli_override: dict[str, Any] = {}
        # 未知顶层键原样保留（t-37）：本类不认识它——不保留的话任何 load→save
        # 往返（wake install 等）都会把它静默抹掉。
        self._extra = {k: v for k, v in data.items() if k not in _KNOWN_KEYS}

    # -------------------------------------------------- per-agent routing (t-58)

    def agent_route_ids(self) -> list[str]:
        """配置了身份段（``agents.<ID>``）的 id 清单——doctor ②④ 的检查范围。"""
        return [k for k, v in self._agents.items() if isinstance(v, dict)]

    def agent_section(self, agent_id: str) -> dict[str, Any]:
        """``agents.<ID>`` 段（dict 值才认）；缺失/形状不对都回 {}（fail-open：
        旧配置没有这段必须照旧可用，绝不因缺键报错）。大小写不同键作兜底匹配
        （HS/hs 同一身份）。"""
        if not agent_id:
            return {}
        raw = self._agents.get(agent_id)
        if isinstance(raw, dict):
            return raw
        lowered = str(agent_id).lower()
        for k, v in self._agents.items():
            if str(k).lower() == lowered and isinstance(v, dict):
                return v
        return {}

    def set_agent_route(
        self,
        agent_id: str,
        *,
        adapter: str | None = None,
        command: list[str] | tuple[str, ...] | None = None,
        webhook_url: str | None = None,
        webhook_secret: str | None = None,
        webhook_style: str | None = None,
    ) -> dict[str, Any]:
        """只写 ``agents.<ID>`` 段的通道键（t-58 硬约束：install 绝不落全局）。

        传 None 的键一律不动；段内既有键（sampling policy 等）原样保留。
        返回更新后的段。"""
        section = dict(self.agent_section(agent_id))
        if adapter is not None:
            section["adapter"] = str(adapter)
        if command is not None:
            section["command"] = [str(a) for a in command]
        if webhook_url is not None or webhook_secret is not None or webhook_style is not None:
            webhook = section.get("webhook")
            webhook = dict(webhook) if isinstance(webhook, dict) else {}
            if webhook_url is not None:
                webhook["url"] = str(webhook_url)
            if webhook_secret is not None:
                webhook["secret"] = str(webhook_secret)
            if webhook_style is not None:
                webhook["style"] = str(webhook_style)
            section["webhook"] = webhook
        self._agents[str(agent_id)] = section
        return section

    def set_cli_override(
        self,
        *,
        adapter: str | None = None,
        command: list[str] | tuple[str, ...] | None = None,
        webhook_url: str | None = None,
    ) -> None:
        """wake run 的 CLI 覆盖（最高优先级，仅本进程内存态，永不写盘）。"""
        if adapter:
            self._cli_override["adapter"] = str(adapter)
        if command:
            self._cli_override["command"] = [str(a) for a in command]
        if webhook_url:
            self._cli_override["webhook_url"] = str(webhook_url)

    def effective_route(self, agent_id: str) -> WakeRoute:
        """解析一个身份的生效唤醒通道（t-58 优先级链）。

        逐键独立解析：CLI 覆盖 > ``agents.<ID>`` > 全局 > 默认。旧配置（无
        agents 段/缺键）每个键都自然落回全局/默认——fail-open 向后兼容，
        多身份同机各回各家则靠段内键生效。"""
        section = self.agent_section(agent_id)
        webhook = section.get("webhook")
        webhook = webhook if isinstance(webhook, dict) else {}
        src_agent = f"agents:{agent_id}" if section else ""
        sources: dict[str, str] = {}

        adapter = str(self._cli_override.get("adapter") or "") or str(section.get("adapter") or "")
        if adapter:
            sources["adapter"] = "cli" if self._cli_override.get("adapter") else src_agent
        else:
            adapter = self.adapter
            sources["adapter"] = "global" if self.adapter != "hermes" else "default"

        command: list[str] = []
        raw_command = self._cli_override.get("command") or section.get("command")
        if isinstance(raw_command, (list, tuple)) and raw_command:
            command = [str(a) for a in raw_command]
            sources["command"] = "cli" if self._cli_override.get("command") else src_agent
        elif self.command:
            command = list(self.command)
            sources["command"] = "global"

        webhook_url = str(
            self._cli_override.get("webhook_url") or webhook.get("url") or self.webhook_url or ""
        )
        if webhook_url:
            if self._cli_override.get("webhook_url"):
                sources["webhook_url"] = "cli"
            elif webhook.get("url"):
                sources["webhook_url"] = src_agent
            else:
                sources["webhook_url"] = "global"
        webhook_secret = str(webhook.get("secret") or self.webhook_secret or "")
        if webhook.get("secret"):
            sources["webhook_secret"] = src_agent
        elif self.webhook_secret:
            sources["webhook_secret"] = "global"
        webhook_style = str(webhook.get("style") or "") or self.webhook_style
        if webhook.get("style"):
            sources["webhook_style"] = src_agent

        command_timeout = self.command_timeout
        try:
            if section.get("timeout") is not None:
                command_timeout = float(section["timeout"])
        except (TypeError, ValueError):
            pass

        return WakeRoute(
            adapter=adapter,
            command=tuple(command),
            webhook_url=webhook_url,
            webhook_secret=webhook_secret,
            webhook_style=webhook_style,
            command_timeout=command_timeout,
            sources=sources,
        )

    @classmethod
    def load(cls, root: Path) -> WakeConfig | None:
        try:
            data = json.loads((Path(root) / "wake.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return cls(data, Path(root))

    def to_dict(self) -> dict[str, Any]:
        # 已知键重建、未知键（_extra）原样带回——已知键优先，extra 不覆盖。
        # t-58: agents 段（含 sampling policy 未知子键）整段原样带回。
        out: dict[str, Any] = {**self._extra}
        if self._agents:
            out["agents"] = self._agents
        out.update(
            {
                "agent_id": self.agent_id,
                "adapter": self.adapter,
                "webhook": {
                    **self._webhook_extra,
                    "url": self.webhook_url,
                    "secret": self.webhook_secret,
                    "style": self.webhook_style or "github",
                },
                "command": list(self.command),
                "timeout": self.command_timeout,
                "jev": {
                    **self._jev_extra,
                    "enabled": self.jev_enabled,
                    "api_key": self.jev_api_key,
                    "endpoint": self.jev_endpoint,
                    "threshold": self.jev_threshold,
                    "timeout": self.jev_timeout,
                },
                "retry_interval": self.retry_interval,
                "retry_max": self.retry_max,
                "stale_acked": self.stale_acked,
                "digest_fallback": self.digest_fallback,
            }
        )
        return out

    def save(self) -> Path:
        path = self.root / "wake.json"
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        return path


def default_python() -> str:
    """Interpreter the installed units should run the drain loop with."""
    return sys.executable or "python3"


# ----------------------------------------------------------------- adapters


class WakeAdapter:
    """Delivery interface for one harness's wake mechanism."""

    name = "base"
    # Class of the most recent delivery failure (锚A error_class): short
    # stable labels — "no_url" / "post_failed" / "no_command" /
    # "spawn_failed" / "timeout" / "exit_nonzero". "" after success.
    last_error_class = ""

    def deliver(self, msg: dict[str, Any]) -> bool:
        raise NotImplementedError


class HermesAdapter(WakeAdapter):
    """POST the gateway webhook. Signature format auto-adapts via
    AGENT_MAIL_SIGNATURE_STYLE (github / generic / slack) — the three
    validated receiver formats, one adapter."""

    name = "hermes"

    def deliver(self, msg: dict[str, Any]) -> bool:
        self.last_error_class = ""
        if not self.url:
            self.last_error_class = "no_url"
            return False
        if post_message(self.url, self.secret, msg, timeout=5.0):
            return True
        self.last_error_class = "post_failed"
        return False

    def __init__(self, url: str, secret: str) -> None:
        self.url = url
        self.secret = secret


class GenericWebhookAdapter(HermesAdapter):
    """User-supplied URL + secret; same signed POST as hermes."""

    name = "generic-webhook"


def _kill_group(proc: subprocess.Popen) -> None:
    """Timeout = hard kill. On POSIX the child was started with
    ``start_new_session`` so killing its process group also takes any
    grandchildren it spawned; elsewhere kill the direct child. Never raises —
    a kill racing an already-exited process is normal life."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    finally:
        try:
            proc.communicate(timeout=5)  # reap — no zombies left behind
        except (OSError, subprocess.SubprocessError, ValueError):
            pass


class LocalCommandAdapter(WakeAdapter):
    """On-demand CLI agents (``codex`` etc.) have no resident process to POST
    — waking them means running a configured local command that pulls them
    onto the mailbox (老板 09-25 令: all-agent mail auto-trigger). Exit 0 =
    delivered; non-zero exit, timeout kill, or spawn failure all return False
    so the drain's retry path keeps the letter (信不丢).

    t-59（A-2）失败必响·未登录态: exit 0 不再天然等于成功——命令输出命中
    认证失败特征（``Authentication required`` / ``Please use /login`` /
    未登录）时按 ``auth_required`` 判失败（WB 现场真故障：CLI 未登录但
    exit 0，``[degraded] rc=0`` 伪装成功）。任一失败路径都把命令自己的输出
    尾部落 stderr，真因可从日志直读。

    Injection posture (硬约束): the command is an argv *list* executed
    without a shell — never a concatenated string — and letter content rides
    only ``AGENT_MAIL_*`` environment variables, never command-line arguments.
    """

    name = "local-command"

    # 宿主 CLI 未登录的已知输出特征（小写比较）；命中即判失败——「假装
    # 成功的登录墙」比明摆着的失败危害大。
    AUTH_SIGNATURES = (
        "authentication required",
        "please use /login",
        "please run /login",
        "not logged in",
        "未登录",
        "请先登录",
    )

    def __init__(self, command: list[str], timeout: float = DEFAULT_COMMAND_TIMEOUT) -> None:
        self.command = [str(a) for a in (command or [])]
        self.timeout = max(1.0, float(timeout or DEFAULT_COMMAND_TIMEOUT))

    @classmethod
    def _auth_hit(cls, output: str) -> str:
        """返回命中的认证特征行（无命中回 ``""``）。"""
        for line in output.splitlines():
            low = line.lower()
            if any(sig in low for sig in cls.AUTH_SIGNATURES):
                return line.strip()[:300]
        return ""

    def deliver(self, msg: dict[str, Any]) -> bool:
        self.last_error_class = ""
        if not self.command or not all(isinstance(a, str) and a for a in self.command):
            # Misconfigured (empty or string-form command): nothing was run —
            # return False so the retry path keeps the letter visible.
            self.last_error_class = "no_command"
            print(
                "[agent-mailbox wake] local-command: no valid argv list configured",
                file=sys.stderr,
                flush=True,
            )
            return False
        env = dict(os.environ)
        env.update(
            {
                # Letter content travels via env only — execve passes these
                # verbatim, no shell ever parses them.
                "AGENT_MAIL_MSG_ID": str(msg.get("id", "")),
                "AGENT_MAIL_AGENT_ID": str(msg.get("to", "")),
                "AGENT_MAIL_SUBJECT": str(msg.get("subject", "")),
                "AGENT_MAIL_MSG_BODY": str(msg.get("body", "") or ""),
            }
        )
        kwargs: dict[str, Any] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "env": env,
        }
        if os.name == "posix":
            kwargs["start_new_session"] = True  # own group: killpg below takes grandchildren
        try:
            proc = subprocess.Popen(self.command, **kwargs)
        except OSError as exc:  # binary missing etc. — retry path, 信不丢
            self.last_error_class = "spawn_failed"
            print(
                f"[agent-mailbox wake] local-command spawn failed: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return False
        try:
            out, err = proc.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            self.last_error_class = "timeout"
            print(
                f"[agent-mailbox wake] local-command timed out after {self.timeout:g}s (killed)",
                file=sys.stderr,
                flush=True,
            )
            return False
        combined = (out or "") + "\n" + (err or "")
        auth_line = self._auth_hit(combined)
        if auth_line:
            # t-59: 宿主 CLI 未登录——哪怕 exit 0 也不是真投递，必须非零语义
            self.last_error_class = "auth_required"
            print(
                f"[agent-mailbox wake] local-command host not logged in "
                f"(exit {proc.returncode}): {auth_line}",
                file=sys.stderr,
                flush=True,
            )
            return False
        if proc.returncode != 0:
            self.last_error_class = "exit_nonzero"
            tail = combined.strip()[-300:]
            print(
                f"[agent-mailbox wake] local-command exit {proc.returncode}"
                + (f"; output tail: {tail}" if tail else ""),
                file=sys.stderr,
                flush=True,
            )
            return False
        return True


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
                ["osascript", "-e", f'display notification "{body}" with title "{title}"'],
                check=False,
                timeout=5,
            )
        elif sys.platform == "linux":
            subprocess.run(["notify-send", title, body], check=False, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass  # best-effort only


def make_adapter(cfg_or_route: WakeConfig | WakeRoute) -> WakeAdapter:
    """从 WakeConfig（内部按 agent_id 解析生效路由）或现成 WakeRoute 构建适配器。

    t-58: 传 WakeConfig 时先走 :meth:`WakeConfig.effective_route`——per-agent
    段优先，旧配置（无段）自然落回全局/默认，dispatch 行为与 0.7.5 逐字节
    兼容。"""
    route = (
        cfg_or_route
        if isinstance(cfg_or_route, WakeRoute)
        else cfg_or_route.effective_route(cfg_or_route.agent_id)
    )
    if route.adapter == "local-command":
        return LocalCommandAdapter(list(route.command), route.command_timeout)
    if route.adapter == "claude-code":
        return ClaudeCodeAdapter()
    if route.adapter == "generic-webhook":
        return GenericWebhookAdapter(route.webhook_url, route.webhook_secret)
    # hermes and unknown values share the signed-POST path (fail-open:
    # waking is better than silence).
    return HermesAdapter(route.webhook_url, route.webhook_secret)


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

    payload = json.dumps(
        {
            "subject": msg.get("subject", ""),
            "body": (msg.get("body", "") or "")[:4000],
            "from": msg.get("from", ""),
            "priority": msg.get("priority", "normal"),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        cfg.jev_endpoint,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.jev_api_key}",
        },
    )
    # ProxyHandler({}) pins a direct connection — urllib otherwise inherits
    # the system HTTP proxy (macOS CI runners set one) and a wake-path probe
    # must never detour through it (same lesson as webhook.py).
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=cfg.jev_timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {"noul": bool(data.get("noul", False)), "score": float(data.get("score", 0))}
    except (OSError, ValueError, KeyError):
        return None  # fail-open: caller wakes regardless


def jev_gate(cfg: WakeConfig, msg: dict[str, Any], log_path: Path) -> tuple[bool, str]:
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


def claim_route_mail(
    store: MailStore,
    agent_id: str,
    candidate_ids: list[str] | tuple[str, ...] | set[str],
    session_label: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """t-56 claim-first 路由原语：认领候选信，只把认领到的交给投递。

    belt / daemon / 仓外网关（webhook 对齐点）共用这一条纪律：投递前先
    ``claim``（原子、locked），没认领到的信＝别的窗已认领在途，逐封落
    ``claim_denied`` 审计（store 侧按信去重，防定时轮询刷屏）。返回
    ``(claimed_letters, denied_ids)``——调用方只投 ``claimed``；denied 的
    信本次不投、不拉会话。认领 0 封 ⇒ 投递列表为空，天然不拉会话。
    """
    only = list(dict.fromkeys(str(i) for i in candidate_ids))
    if not only:
        return [], []
    claimed = store.claim(agent_id, only=only, session_label=session_label)
    claimed_ids = {str(m.get("id", "")) for m in claimed}
    denied = [mid for mid in only if mid not in claimed_ids]
    for mid in denied:
        try:
            store.record_claim_denied(agent_id, mid, session_label=session_label)
        except Exception as exc:  # noqa: BLE001 — 审计 fail-open，绝不影响投递主链路
            print(
                f"[agent-mailbox wake] claim_denied audit failed for {mid} (fail-open): {exc}",
                file=sys.stderr,
                flush=True,
            )
    return claimed, denied


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


def _external_auto_execute(root: Path) -> bool:
    """§4.3 可见性开关 ``external_auto_execute``（默认关＝执行门开着）.

    Fail-closed on any problem: a corrupt config must widen no gate, so any
    error reading the switch keeps external mail non-executing (the default).
    """
    try:
        return bool(load_visibility(Path(root)).get("external_auto_execute"))
    except Exception:  # noqa: BLE001 — fail-closed: the gate stays shut
        return False


def _registered_members(root: Path) -> set[str]:
    """Member ids from ``<root>/registry.json`` — U2 alerts only address
    registered boxes, so a failed wake never mints a dead inbox directory
    (死箱 hygiene). Any read problem yields an empty set (alert still goes
    to the responsible party, see WAKE_ALERT_RECIPIENT)."""
    try:
        reg = json.loads((Path(root) / "registry.json").read_text(encoding="utf-8"))
        agents = reg.get("agents", {}) if isinstance(reg, dict) else {}
        if not isinstance(agents, dict):
            return set()
        return {k for k, v in agents.items() if isinstance(v, dict)}
    except (OSError, json.JSONDecodeError, ValueError):
        return set()


def send_wake_alert(
    store: MailStore, root: Path, agent_id: str, msg: dict[str, Any], *, degraded: str = ""
) -> bool:
    """U2 可见通道: drop an alert letter when a letter's wake has failed.

    t-59（A-2）收件人 = 负责方 HS（WAKE_ALERT_RECIPIENT）+ 原发件人（已注册、
    非被唤醒者本人时给一份，让发件方知道信卡住了）。boss 席一律不发——
    boss 仅存档语义，老板只看飞书；自动告警发 boss 席等于没送达还制造噪音。
    The alert is marked on the letter (``handled_log`` action ``wake_alert``)
    so one letter alerts at most once no matter how many later drain rounds
    retry it. Fail-open end to end: an alert problem is logged and forgotten —
    the wake's own retry path is unaffected.

    t-62（判据7）: ``degraded`` 非空 = 该信已降级 digest 纯本地路径消化，
    降级事实必须进告警文本（负责人看到的不是「信卡住了」而是「已摘要落盘、
    请人工跟进」）。"""
    already = any(e.get("action") == WAKE_ALERT_ACTION for e in (msg.get("handled_log") or []))
    if already:
        return False
    sender = str(msg.get("from", ""))
    recipients: list[str] = [WAKE_ALERT_RECIPIENT]
    if (
        sender
        and sender != agent_id
        and sender != WAKE_ALERT_RECIPIENT
        and sender != "boss"  # boss 席仅存档，永不作为告警通道
        and sender in _registered_members(root)
    ):
        recipients.append(sender)
    subject = str(msg.get("subject", ""))[:80]
    degrade_note = (
        f"\n[降级·t-62] 本信已由 digest 纯本地路径消化（零网络零 LLM）：\n"
        f"  摘要落盘: {degraded}\n"
        "  信已标 done——内容不会丢，但收件 agent 没有真正读到它；请按摘要人工跟进。\n"
        if degraded
        else ""
    )
    body = (
        f"[wake-fail 自动告警] {agent_id} 的唤醒通道连续失败，这封信可能没人处理：\n\n"
        f"  信件 id: {msg.get('id', '')}\n"
        f"  发件人: {sender or '?'} → 收件人: {msg.get('to', agent_id)}\n"
        f"  主题: {subject}\n"
        f"  信箱根: {Path(root)}\n"
        f"{degrade_note}\n"
        "唤醒侧已按 retry 策略重试仍未投出（信不丢）。\n"
        "请检查 wake 通道（wake.json / webhook / wake-attempts.jsonl 的 "
        "error_class 行）后重投。此信为系统自动告警，无需回执。"
    )
    sent = False
    try:
        out = store.send(agent_id, recipients, f"[wake-fail] {subject}", body)
        sent = bool(out)
    except Exception as exc:  # noqa: BLE001 — alert must never break the drain
        print(f"[agent-mailbox wake] alert send failed (fail-open): {exc}", file=sys.stderr)
    try:
        store.record_handled(
            agent_id, str(msg.get("id", "")), WAKE_ALERT_ACTION, note="alert letter dropped"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[agent-mailbox wake] alert mark failed (fail-open): {exc}", file=sys.stderr)
    return sent


def run_once(
    root: Path,
    cfg: WakeConfig,
    *,
    adapter: WakeAdapter | None = None,
    store: MailStore | None = None,
    now: float | None = None,
    session_label: str | None = None,
) -> dict[str, Any]:
    """One drain round: reap orphans, scan, claim-first, route (optional
    Jev), wake, dedup, retry.

    t-56 claim-first: the round first reaps stale-acked orphans (with the
    same TTL as count semantics v2, so ``reap(stale_acked) → claim`` is
    exactly equivalent to "pending 全数 + acked>stale_acked 兑底"), then
    claims every candidate letter and delivers ONLY the claimed ones. A
    letter another window already claimed is never delivered twice — the
    loser's round records ``claim_denied`` (deduped per letter) instead, and
    zero claimed letters means nothing is delivered and no session is pulled.
    A letter whose delivery ultimately failed gets its claim released back to
    pending so the next trigger re-drains it (信不丢, unchanged semantics).

    Never raises — the wake side failing must never take anything else down
    (fail-open iron law). Returns a stats dict for logging/tests.
    """
    stats: dict[str, Any] = {
        "scanned": 0,
        "due": 0,
        "woke": 0,
        "skipped_woken": 0,
        "failed": 0,
        "jev_skipped": 0,
        "skipped_external": 0,
        "claim_denied": 0,
        # t-62（判据7）: 投递失败但已降级 digest 纯本地流转的信数——降级不是
        # 成功（rc 仍非零），但信有了下文（摘要落盘 + 标 done + 告警）。
        "digested": 0,
        # t-59（A-2）失败必响: 整轮异常（fail-open 吞掉的那条）也要在统计里
        # 留痕——run --once 据此非零退出，rc=0 伪装成功从此不可能。
        "round_error": "",
    }
    try:
        store = store or MailStore(root)
        adapter = adapter or make_adapter(cfg)
        ref = time.time() if now is None else now
        # t-56: per-round session identity for handled_log ``by`` (J3) —
        # "wake:<runid>" makes the claiming window auditable across windows.
        label = session_label or f"wake:{ref:.0f}-{os.urandom(3).hex()}"
        jev_log = Path(root) / "wake-jev.log"
        inbox = Path(root) / "inbox" / cfg.agent_id
        if not inbox.is_dir():
            return stats
        # claim-first 前置（孤儿营救组合）: stale-acked orphans go back to
        # pending so the claim below can take them. TTL = stale_acked keeps
        # this exactly equivalent to count semantics v2. Fail-open, same as
        # the belt's own reap step.
        try:
            store.reap_stale_acked(cfg.agent_id, ttl_seconds=cfg.stale_acked, now=ref)
        except Exception as exc:  # noqa: BLE001 — reap 失败不挡 wake（fail-open）
            print(
                f"[agent-mailbox wake] reap failed (fail-open, continuing): {exc}",
                file=sys.stderr,
                flush=True,
            )
        candidates: list[dict[str, Any]] = []
        for p in sorted(inbox.glob("*.json")):
            stats["scanned"] += 1
            try:
                m = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue  # partially-written letters wait for the next round
            if isinstance(m, dict) and not m.get("id"):
                # Legacy letter (pre-id format): the filename stem *is* the id
                # in the canonical scheme. Without the back-fill the bare
                # m["id"] further down poisoned the whole drain round
                # (KeyError → fail-open skipped every letter after it),
                # 09-25: one stale-format letter starved the entire inbox.
                m["id"] = p.stem
            if m.get("origin") == "external" and not _external_auto_execute(root):
                # v0.7.5 外部来源执行门: external mail lands but never wakes
                # anyone — an owner must confirm it (confirm_external) first,
                # which flips origin back to local and lets later rounds act.
                # The §4.3 visibility switch can open the gate, but any read
                # failure keeps it shut (fail-closed).
                stats["skipped_external"] += 1
                continue
            due = should_wake(m, ref, cfg.stale_acked)
            stats["due"] += 1 if due else 0
            if not due:
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
                        print(
                            f"[agent-mailbox wake] jev_skip mark failed (fail-open): {exc}",
                            file=sys.stderr,
                            flush=True,
                        )
                    continue
            candidates.append(m)
        # t-56 claim-first: claim the candidates, deliver ONLY the claimed
        # ones. Zero claimed ⇒ nothing delivered, no session pulled.
        claimed, denied = claim_route_mail(
            store, cfg.agent_id, [str(m.get("id", "")) for m in candidates], label
        )
        stats["claim_denied"] += len(denied)
        claimed_by_id = {str(m.get("id", "")): m for m in claimed}
        for m in candidates:
            mid = str(m.get("id", ""))
            claimed_msg = claimed_by_id.get(mid)
            if claimed_msg is None:
                continue  # claim_denied 已审计 — this window does not own it
            delivered = False
            for attempt in range(1, cfg.retry_max + 1):
                t0 = time.monotonic()
                try:
                    delivered = adapter.deliver(claimed_msg)
                except Exception:  # noqa: BLE001 — adapter bugs never kill the loop
                    delivered = False
                # 锚A: every attempt lands a row — success and failure alike
                record_wake_attempt(
                    root,
                    cfg.agent_id,
                    "daemon",
                    attempt,
                    "ok" if delivered else "fail",
                    error_class=""
                    if delivered
                    else str(getattr(adapter, "last_error_class", "") or "unknown"),
                    executor=getattr(adapter, "name", "adapter"),
                    latency_ms=int((time.monotonic() - t0) * 1000),
                )
                if delivered:
                    break
                stats["failed"] += 1
                if attempt < cfg.retry_max:
                    time.sleep(cfg.retry_interval)
            if delivered:
                # success marks the letter woken (idempotent dedup): later
                # rounds skip it even after a reclaim cycles acked->pending.
                store.record_handled(
                    cfg.agent_id,
                    mid,
                    "wake",
                    note=getattr(adapter, "name", "adapter"),
                    session_label=label,
                )
                stats["woke"] += 1
            else:
                # t-62（判据7/S4）LLM 可选降级: 全部重试失败时，若 digest_fallback
                # 开着（默认），把已认领的信走 digest 纯本地路径——摘要落盘、
                # 标 done（handled_log digest 动作带降级原因锚）、告警照发。
                # 「CLI 未登录即全断」从根上消除；降级不是成功（rc 仍非零，
                # 失败必响不回退）。关掉（digest_fallback=false）则回到原样：
                # release claim 回 pending，下一个触发重投（信不丢）。
                err_class = str(getattr(adapter, "last_error_class", "") or "unknown")
                degraded = ""
                if getattr(cfg, "digest_fallback", True):
                    try:
                        from .digest import digest_claimed_letters

                        d = digest_claimed_letters(
                            root,
                            cfg.agent_id,
                            [claimed_msg],
                            store=store,
                            reason=err_class,
                            session_label=label,
                        )
                        if int(d.get("digested") or 0) > 0:
                            degraded = str(d.get("digest_file") or "digest")
                            stats["digested"] += 1
                            print(
                                f"[agent-mailbox wake] 投递失败({err_class}) 已降级 digest "
                                f"纯本地流转（零网络零 LLM）: {degraded}",
                                file=sys.stderr,
                                flush=True,
                            )
                    except Exception as exc:  # noqa: BLE001 — 降级失败回退 release 路径
                        print(
                            f"[agent-mailbox wake] digest degrade failed (fail-open): {exc}",
                            file=sys.stderr,
                            flush=True,
                        )
                if not degraded:
                    # every attempt failed — release OUR claim so the letter is
                    # pending again and the next WatchPaths trigger re-drains it
                    # (信不丢, same recovery as the old un-marked-pending path;
                    # the U2 alert below still fires once per letter).
                    try:
                        store.release_claim(cfg.agent_id, mid, session_label=label)
                    except Exception as exc:  # noqa: BLE001 — 释放失败信仍在（reap 兜底）
                        print(
                            f"[agent-mailbox wake] claim release failed (fail-open): {exc}",
                            file=sys.stderr,
                            flush=True,
                        )
                send_wake_alert(store, root, cfg.agent_id, claimed_msg, degraded=degraded)
    except Exception as exc:  # noqa: BLE001 — total fail-open: never raise out of drain
        stats["round_error"] = str(exc)
        print(
            f"[agent-mailbox wake] drain round failed (fail-open): {exc}",
            file=sys.stderr,
            flush=True,
        )
    return stats


def run(
    root: Path, cfg: WakeConfig, poll_interval: float = 2.0, once: bool = False
) -> dict[str, Any]:
    """Drain loop. WatchPaths/path-unit mode passes ``once=True`` (the OS
    re-launches us on every inbox change); manual mode loops forever.

    t-59（A-2）失败必响: 返回最后一轮 stats——``_cmd_run`` 在 once 模式据
    ``failed``/``round_error`` 非零退出；loop 模式永不返回（每轮失败已有
    U2 告警 + 锚A fail 行留痕）。"""
    while True:
        stats = run_once(root, cfg)
        if once:
            return stats
        time.sleep(poll_interval)


# ------------------------------------------------------- install / uninstall


def plist_body(cfg: WakeConfig, python_exe: str, root: Path) -> str:
    """launchd plist for one agent's wake (WatchPaths on its inbox).

    Pure string building — tests assert on the generated XML with tmp paths,
    nothing here touches the real LaunchAgents directory.
    """
    inbox = Path(root) / "inbox" / cfg.agent_id
    program = [
        python_exe,
        "-m",
        "agent_mailbox.wake",
        "run",
        "--root",
        str(Path(root)),
        "--agent",
        cfg.agent_id,
        "--once",
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
        f"{python_exe} -m agent_mailbox.wake run --root {Path(root)} --agent {cfg.agent_id} --once"
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
        out["activate_cmd"] = f"systemctl --user enable --now {WAKE_LABEL}-{cfg.agent_id}.path"
        if activate:
            out["activated"] = _launchctl(
                "--user", "enable", "--now", f"{WAKE_LABEL}-{cfg.agent_id}.path"
            )
    else:
        raise SystemExit(
            f"wake install: unsupported platform {sys.platform!r} "
            "(run `agent-mailbox wake run` manually instead)"
        )
    out["config"] = str(cfg.save())  # 单次写入（HS 派单项①：双调清理）
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


def _parse_command_arg(raw: str, usage: str) -> list[str]:
    """--command 只收 JSON argv 列表（与 LocalCommandAdapter 的 argv-list-only
    注入姿态一致：没有 shell 拼接的唤醒路径，过去没有，以后也不该有）。"""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"{usage}: --command must be a JSON argv list, "
            f'e.g. ["{Path(sys.executable).name}", "--flag"]: {exc}'
        ) from exc
    if (
        not isinstance(parsed, (list, tuple))
        or not parsed
        or not all(isinstance(a, str) and a for a in parsed)
    ):
        raise SystemExit(f"{usage}: --command must be a non-empty JSON array of strings")
    return [str(a) for a in parsed]


def _cmd_install(args: argparse.Namespace) -> None:
    root = Path(args.root or os.environ.get("AGENT_MAIL_HOME", Path.home() / ".agent-mail"))
    cfg = WakeConfig.load(root) or WakeConfig({}, root)
    if args.agent:
        cfg.agent_id = args.agent
    if not cfg.agent_id:
        raise SystemExit("wake install: --agent required (no wake.json and no --agent)")
    # t-58（A-1）: install 的通道三键只写 agents.<ID> 身份段，绝不落全局——
    # 一台机装第二个身份不再覆盖第一个的唤醒配置（各回各家）。全局键保留为
    # 旧配置的默认值（向后兼容，本函数不碰它们）。
    if args.adapter:
        cfg.set_agent_route(cfg.agent_id, adapter=args.adapter)
    if args.command:
        cfg.set_agent_route(cfg.agent_id, command=_parse_command_arg(args.command, "wake install"))
    if args.webhook_url:
        cfg.set_agent_route(cfg.agent_id, webhook_url=args.webhook_url)
    if args.webhook_secret:
        cfg.set_agent_route(cfg.agent_id, webhook_secret=args.webhook_secret)
    if args.jev:
        cfg.jev_enabled = True
        cfg.jev_api_key = args.jev_api_key
        cfg.jev_endpoint = args.jev_endpoint or cfg.jev_endpoint
    route = cfg.effective_route(cfg.agent_id)
    if not route.webhook_url and route.adapter in ("hermes", "generic-webhook"):
        # fall back to the store-level webhook.json when the resolved route
        # carries none (fills the GLOBAL default — per-agent url above wins).
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
    out["agent_route"] = cfg.effective_route(cfg.agent_id).to_display()
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
        # t-58: 一并给出该身份解析后的生效通道（agents.<ID> 段优先）。
        if cfg.agent_id:
            info["route"] = cfg.effective_route(cfg.agent_id).to_display()
        if sys.platform == "darwin":
            plist = Path.home() / "Library" / "LaunchAgents" / f"{WAKE_LABEL}-{cfg.agent_id}.plist"
            info["plist_installed"] = plist.exists()
    print(json.dumps(info, ensure_ascii=False))


def _cmd_claim(args: argparse.Namespace) -> None:
    """t-56 claim-first 认领通道（belt 脚本与仓外网关共用的对齐点）。

    扫描收件箱 pending 候选 → ``claim``（只认领扫到的）→ 没认领到的逐封落
    ``claim_denied`` 审计（按信去重）→ 输出 JSON：``claimed`` 每封带
    id/from/subject/body（即投递物），``denied`` 是被别的窗认领在途的 id。
    ``claimed`` 为空 ⇒ 调用方本轮不投、不拉会话。认领结果文件 0600（信体
    落盘，权限对齐 letter 文件本身）。
    """
    root = Path(args.root or os.environ.get("AGENT_MAIL_HOME", Path.home() / ".agent-mail"))
    agent = str(args.agent or "").strip()
    if not agent:
        raise SystemExit("wake claim: --agent required")
    store = MailStore(root)
    label = str(args.label or "").strip() or f"claim:{os.getpid()}"
    inbox = root / "inbox" / agent
    candidate_ids: list[str] = []
    if inbox.is_dir():
        for p in sorted(inbox.glob("*.json")):
            try:
                m = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue  # partially-written letters wait for the next round
            if isinstance(m, dict) and m.get("status") == "pending":
                candidate_ids.append(str(m.get("id") or p.stem))
    claimed, denied = claim_route_mail(store, agent, candidate_ids, label)
    payload: dict[str, Any] = {
        "agent": agent,
        "label": label,
        "root": str(root),
        "claimed": [
            {
                "id": str(m.get("id", "")),
                "from": str(m.get("from", "")),
                "to": str(m.get("to", agent)),
                "subject": str(m.get("subject", "")),
                "body": str(m.get("body", "") or ""),
                "priority": str(m.get("priority", "normal")),
                **({"thread_id": m["thread_id"]} if m.get("thread_id") else {}),
            }
            for m in claimed
        ],
        "denied": denied,
        "denied_note": (
            "claimed in-flight by another window; audited as claim_denied (deduped per letter)"
        ),
    }
    out = json.dumps(payload, ensure_ascii=False)
    if args.out:
        target = Path(args.out)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(out + "\n")
        except OSError as exc:
            raise SystemExit(f"wake claim: cannot write {target}: {exc}") from exc
    else:
        print(out)


def _cmd_release(args: argparse.Namespace) -> None:
    """t-56: 释放本路由没投出去/没做完的认领信（翻回 pending，信不丢）。

    id 来源二选一：``--claim-file``（``wake claim --out`` 的 JSON，取其中
    claimed 的 id）或 ``--ids``（逗号分隔）。已 done/archived 的信自动跳过；
    带 ``--label`` 时只释放仍是本标识认领的信（别的窗接手的不动）。
    """
    root = Path(args.root or os.environ.get("AGENT_MAIL_HOME", Path.home() / ".agent-mail"))
    agent = str(args.agent or "").strip()
    if not agent:
        raise SystemExit("wake release: --agent required")
    store = MailStore(root)
    label = str(args.label or "").strip() or f"claim:{os.getpid()}"
    ids: list[str] = []
    if args.claim_file:
        try:
            data = json.loads(Path(args.claim_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"wake release: cannot read claim file: {exc}") from exc
        ids = [str(m.get("id", "")) for m in (data.get("claimed") or []) if isinstance(m, dict)]
    if args.ids:
        ids += [i.strip() for i in str(args.ids).split(",") if i.strip()]
    released: list[str] = []
    skipped: list[str] = []
    for mid in dict.fromkeys(ids):
        ok = False
        try:
            ok = store.release_claim(agent, mid, session_label=label)
        except Exception as exc:  # noqa: BLE001 — fail-open：释放失败信仍在（reap 兜底）
            print(
                f"[agent-mailbox wake] release {mid} failed (fail-open): {exc}",
                file=sys.stderr,
                flush=True,
            )
        (released if ok else skipped).append(mid)
    print(
        json.dumps(
            {"agent": agent, "label": label, "released": released, "skipped": skipped},
            ensure_ascii=False,
        )
    )


def _cmd_run(args: argparse.Namespace) -> None:
    root = Path(args.root or os.environ.get("AGENT_MAIL_HOME", Path.home() / ".agent-mail"))
    cfg = WakeConfig.load(root)
    if cfg is None:
        raise SystemExit(
            f"wake run: no wake.json in {root} — run `agent-mailbox wake install` first"
        )
    if args.agent:
        cfg.agent_id = args.agent
    cli_adapter = getattr(args, "adapter", "") or ""
    if cli_adapter:
        # per-agent plist override: multi-agent installs share one wake.json,
        # each plist passes --adapter to pick its own wake path (HS=hermes
        # stays untouched, codex=local-command). In-memory only.
        cfg.adapter = cli_adapter
    cli_command = getattr(args, "command", "") or ""
    if cli_command:
        cfg.command = _parse_command_arg(cli_command, "wake run")
    cli_webhook = getattr(args, "webhook_url", "") or ""
    if cli_webhook:
        cfg.webhook_url = cli_webhook
    # t-58（A-1）: run 按身份解析生效通道（agents.<ID> 段优先于全局，缺段
    # fail-open 落回全局/默认）；CLI 覆盖是最高优先级（仅本进程内存态）。
    cfg.set_cli_override(
        adapter=cli_adapter,
        command=_parse_command_arg(cli_command, "wake run") if cli_command else None,
        webhook_url=cli_webhook,
    )
    if not cfg.agent_id:
        raise SystemExit("wake run: no agent_id in wake.json — pass --agent")
    stats = run(root, cfg, once=args.once) or {}
    # t-59（A-2）失败必响: once 模式（WatchPaths/手跑）投递失败必须非零退出，
    # 禁 rc=0 伪装成功；信不丢，下一个触发会重投。
    failed = int(stats.get("failed") or 0)
    round_error = str(stats.get("round_error") or "")
    if args.once and (failed or round_error):
        detail = f"{failed} delivery attempt(s) failed"
        if round_error:
            detail += f"; round_error={round_error}"
        print(
            f"[agent-mailbox wake] 失败必响: {detail} — exit 1 (信不丢, 下个触发重投)",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1)


def wake_main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="agent-mailbox wake", description="mail-arrived means agent-woken"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_inst = sub.add_parser("install", help="install the OS file-watcher integration")
    p_inst.add_argument("--agent", default="", help="agent inbox to watch")
    p_inst.add_argument(
        "--adapter", default="", help="hermes | claude-code | generic-webhook | local-command"
    )
    p_inst.add_argument(
        "--command",
        default="",
        help='local-command argv list as JSON (e.g. \'["/usr/local/bin/codex","--mail"]\') — '
        "t-58: written to agents.<ID> only, never the global config",
    )
    p_inst.add_argument(
        "--webhook-url", default="", help="t-58: written to agents.<ID> only, never global"
    )
    p_inst.add_argument(
        "--webhook-secret", default="", help="t-58: written to agents.<ID> only, never global"
    )
    p_inst.add_argument("--jev", action="store_true", help="enable the Jev router (default off)")
    p_inst.add_argument("--jev-api-key", default="")
    p_inst.add_argument("--jev-endpoint", default="")
    p_inst.add_argument("--root", default="", help="mail root (default ~/.agent-mail)")
    p_inst.add_argument(
        "--launch-agents-dir", default=None, help="override ~/Library/LaunchAgents (tests/tmp)"
    )
    p_inst.add_argument(
        "--systemd-dir", default=None, help="override ~/.config/systemd/user (tests/tmp)"
    )
    p_inst.add_argument(
        "--no-activate",
        action="store_true",
        help="write files only, do not load into launchd/systemd",
    )
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
    p_run.add_argument(
        "--adapter",
        default="",
        help="override the resolved wake route (e.g. local-command for per-agent wake)",
    )
    p_run.add_argument(
        "--command",
        default="",
        help="local-command argv list as JSON (e.g. '[\"/usr/local/bin/codex\"]') — "
        "highest-priority in-memory override (t-58)",
    )
    p_run.add_argument(
        "--webhook-url", default="", help="highest-priority in-memory webhook override (t-58)"
    )
    p_run.add_argument("--once", action="store_true", help="one round then exit (WatchPaths mode)")
    p_run.set_defaults(func=_cmd_run)

    p_st = sub.add_parser("status", help="show wake configuration and install state")
    p_st.add_argument("--root", default="")
    p_st.set_defaults(func=_cmd_status)

    # t-56 claim-first 认领/释放通道（belt 脚本与仓外网关的对齐点）
    p_claim = sub.add_parser(
        "claim", help="claim pending mail for one delivery round (JSON; claim-first t-56)"
    )
    p_claim.add_argument("--agent", default="", help="agent inbox to claim from (required)")
    p_claim.add_argument("--root", default="", help="mail root (default ~/.agent-mail)")
    p_claim.add_argument(
        "--label", default="", help="路由:会话 标识落 handled_log by（如 belt:<pid>）"
    )
    p_claim.add_argument("--out", default="", help="write the claim JSON here (default stdout)")
    p_claim.set_defaults(func=_cmd_claim)

    p_release = sub.add_parser(
        "release",
        help="release this route's un-delivered claimed letters back to pending (t-56)",
    )
    p_release.add_argument("--agent", default="", help="agent inbox (required)")
    p_release.add_argument("--root", default="")
    p_release.add_argument("--label", default="", help="must match the claim label")
    p_release.add_argument(
        "--claim-file", default="", help="wake claim --out JSON; releases its claimed ids"
    )
    p_release.add_argument("--ids", default="", help="comma-separated msg ids (alternative)")
    p_release.set_defaults(func=_cmd_release)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    wake_main()
