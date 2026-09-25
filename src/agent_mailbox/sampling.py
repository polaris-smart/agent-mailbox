"""sampling 唤醒（v0.7）——信必达终态：协议内闭环，零进程外依赖。

send() 落箱后，对已声明 ``capabilities.sampling`` 的宿主连接异步发
``sampling/createMessage``（messages = 该 agent 的 wake policy 强制注入 +
信摘要 + msg_id）→ 宿主 agent 被调起 → 自动 mailbox_check → 处理 → 留痕。

铁律（任务书 2026-09-25 钉死，HS 复核通过）：

- ``createMessage`` 是 request（带 id 等 response）非 notification——
  **必须超时 + msg_id 级去重**，宿主卡死不得阻塞 server 主链路。
- 宿主未声明 ``capabilities.sampling`` → 静默跳过 sampling 路径
  （fail-open，信不丢，等 mailbox_check / wake-daemon fallback）。
- 失败静默降级落日志：sampling.log（审计面）+ 信上 handled_log
  （action="sampling"，持久去重的铁证），信必达靠 fallback 兜底。
- 分身唤醒必然是新会话 → 执行体/决策体分离：per-agent wake policy
  经 createMessage 的 messages 参数随唤醒强制注入，注入请求体落
  sampling.log 留证（"分身禁 git 写"铁律机制化）。

wake policy schema（wake.json，per-agent 段，改点4；缺省 = 最小权限::

    {
      "agents": {
        "<agent_id>": {
          "identity": "身份模板（可含 {agent_id} 占位）",
          "task": "唤醒后的任务指令",
          "forbidden": ["禁区列表，逐条注入为硬约束"],
          "require_receipt": true,
          "max_tokens": 512
        }
      }
    }

per-agent 段按**整键覆盖**合并到 DEFAULT_WAKE_POLICY 上：没写的键保持
最小权限默认（策略作者只显式放宽他写出来的键）。wake.json 损坏 =
fail-loud（安全边界不静默降级），由采样侧接住落 error 日志。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import types

from .store import MailboxError, MailStore

logger = logging.getLogger(__name__)

WAKE_POLICY_FILE = "wake.json"
SAMPLING_LOG_FILE = "sampling.log"
SAMPLING_LOG_MAX_BYTES = 10 * 1024 * 1024  # rotate one generation past this (sent.log 先例)
SAMPLING_ACTION = "sampling"  # handled_log action：sampling 唤醒的持久去重标记
TIMEOUT_ENV = "AGENT_MAIL_SAMPLING_TIMEOUT"
DEFAULT_TIMEOUT_S = 60.0
BODY_DIGEST_CHARS = 2000  # 信摘要正文截断，防长信灌爆 sampling 请求
DEFAULT_MAX_TOKENS = 512

HANDLED_LOG = "handled_log"


# --------------------------------------------------------------- wake policy


DEFAULT_WAKE_POLICY: dict[str, Any] = {
    "identity": (
        "你是 {agent_id} 的唤醒实例（agent-mailbox sampling 拉起的新会话，"
        "执行体/决策体分离）。你在宿主授权范围内行动，权限边界以本策略为准。"
    ),
    "task": (
        "收到本信后先 mailbox_check 拉取信件全文，再按信件内容处理；"
        "处理完用 mailbox_reply 回执（或 mailbox_done 关闭该信）。"
    ),
    # 默认最小权限：分身禁 git 写 / 禁生产写 / 禁删除（任务书二.2 铁律机制化）
    "forbidden": [
        "git 写操作（commit / push / branch / tag / rebase）",
        "生产环境写操作（部署、配置变更、数据变更）",
        "删除文件或数据",
    ],
    "require_receipt": True,
    "max_tokens": DEFAULT_MAX_TOKENS,
}

_POLICY_KEYS = set(DEFAULT_WAKE_POLICY)


def sampling_timeout() -> float:
    """单次 createMessage 超时（秒），``AGENT_MAIL_SAMPLING_TIMEOUT`` 可配。

    环境变量为垃圾值时 fail-loud（对齐身份绑定/窗口配置的口径）：
    抛出由采样侧接住落 error 日志——配置坏了必须可见，不许静默回落。
    """
    raw = os.environ.get(TIMEOUT_ENV, "")
    if not raw:
        return DEFAULT_TIMEOUT_S
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{TIMEOUT_ENV} must be a positive number, got {raw!r}")
    return value


def load_wake_policies(root: Path | str) -> dict[str, dict[str, Any]]:
    """读 wake.json 的 per-agent 段（文件缺失 = 空表，全部走默认最小权限）。

    文件存在但损坏（非法 JSON / 顶层不是 dict / agents 段不是 dict）→
    fail-loud：安全边界配置不许静默降级成"无策略注入"。
    """
    path = Path(root) / WAKE_POLICY_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MailboxError(f"corrupt {WAKE_POLICY_FILE}: {exc}") from exc
    if not isinstance(data, dict):
        raise MailboxError(f"corrupt {WAKE_POLICY_FILE}: top level must be an object")
    agents = data.get("agents", {})
    if not isinstance(agents, dict):
        raise MailboxError(f"corrupt {WAKE_POLICY_FILE}: 'agents' must be an object")
    return {str(k): v for k, v in agents.items() if isinstance(v, dict)}


def wake_policy_for(root: Path | str, agent_id: str) -> tuple[dict[str, Any], str]:
    """该 agent 的生效 wake policy + 来源标记（``default`` / ``wake.json``）。

    per-agent 段整键覆盖默认值；没写的键保持最小权限默认。
    """
    policy = dict(DEFAULT_WAKE_POLICY)
    source = "default"
    section = load_wake_policies(root).get(agent_id)
    if section:
        source = WAKE_POLICY_FILE
        for key, value in section.items():
            if key in _POLICY_KEYS:
                policy[key] = value
    return policy, source


def render_identity(policy: dict[str, Any], agent_id: str) -> str:
    """身份模板渲染（进 system_prompt；{agent_id} 占位替换）。"""
    return str(policy.get("identity", "")).replace("{agent_id}", agent_id)


def render_wake_prompt(policy: dict[str, Any], letter: dict[str, Any], unread: int = 1) -> str:
    """构造随 createMessage 强制注入的正文：policy 块 + 信摘要 + msg_id。

    msg_id 必须在正文里——宿主 agent 靠它 mailbox_reply / mailbox_done 留痕。
    """
    agent_id = str(letter.get("to", ""))
    forbidden = policy.get("forbidden") or []
    lines = [
        "[wake policy · 强制注入]",
        f"身份：{render_identity(policy, agent_id)}",
        f"任务：{policy.get('task', '')}",
    ]
    if forbidden:
        lines.append("禁区（违反即事故）：")
        lines += [f"  - {item}" for item in forbidden]
    if policy.get("require_receipt", True):
        lines.append("留痕要求：处理过程必须留痕，处理完 mailbox_reply 回执。")
    body = str(letter.get("body", ""))
    if len(body) > BODY_DIGEST_CHARS:
        body = body[:BODY_DIGEST_CHARS] + "…(truncated)"
    lines += [
        "",
        f"[信件摘要]（收件箱待处理 {unread} 封，本信 msg_id 如下）",
        f"msg_id: {letter.get('id', '')}",
        f"from: {letter.get('from', '')}",
        f"subject: {letter.get('subject', '')}",
        f"priority: {letter.get('priority', 'normal')}",
    ]
    if letter.get("thread_id"):
        lines.append(f"thread_id: {letter['thread_id']}")
    lines += ["", "[正文]", body]
    return "\n".join(lines)


# ---------------------------------------------------------------- audit log


def append_sampling_log(root: Path | str, entry: dict[str, Any]) -> None:
    """sampling.log 追加一行 JSONL（注入请求体留证，sent.log 旋转先例）。"""
    log_path = Path(root) / SAMPLING_LOG_FILE
    try:
        if log_path.stat().st_size > SAMPLING_LOG_MAX_BYTES:
            os.replace(log_path, Path(root) / (SAMPLING_LOG_FILE + ".1"))
    except FileNotFoundError:
        pass
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _serialize_messages(messages: list[types.SamplingMessage]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        out.append(m.model_dump(mode="json", exclude_none=True))
    return out


# ------------------------------------------------------- per-connection registry


@dataclass
class ConnectionEntry:
    """一条 MCP 连接在采样注册表里的登记（v0.7 改点1）。

    ``loop`` 在 initialize 时捕获：同步工具跑在 anyio worker 线程，
    落箱钩子靠 ``run_coroutine_threadsafe(coro, loop)`` 把 createMessage
    调度回连接的事件循环，send 主链路零阻塞。
    """

    session: Any  # mcp.server.session.ServerSession（宽松引用，方便测试替身）
    declared_sampling: bool
    loop: asyncio.AbstractEventLoop
    connected_at: float = field(default_factory=time.time)
    session_id: str | None = None


class SamplingRegistry:
    """per-connection sampling capability 登记 + 身份→连接绑定。

    - initialize（middleware 观察）→ register_connection：宿主声明
      ``capabilities.sampling`` 与否逐连接登记；
    - tools/call（middleware 观察）→ bind_agent：把 acting identity
      （agent_id / from_id 参数，缺省回退 AGENT_MAIL_ID env）绑到其连接，
      作为 send() 落箱后寻址收件人连接的依据。
    """

    MAX_CONNECTIONS = 256  # 防长命 HTTP 进程里死连接登记无限堆积

    def __init__(self) -> None:
        self._connections: dict[int, ConnectionEntry] = {}
        self._agents: dict[str, int] = {}

    def register_connection(self, session: Any, declared: bool) -> ConnectionEntry:
        entry = ConnectionEntry(
            session=session,
            declared_sampling=declared,
            loop=asyncio.get_running_loop(),
            session_id=getattr(session, "session_id", None),
        )
        if len(self._connections) >= self.MAX_CONNECTIONS:
            oldest = min(self._connections.values(), key=lambda e: e.connected_at)
            self._connections.pop(id(oldest.session), None)
        self._connections[id(session)] = entry
        logger.info(
            "sampling capability %s on connection %s",
            "declared" if declared else "not declared",
            entry.session_id or "stdio",
        )
        return entry

    def bind_agent(self, agent_id: str, session: Any) -> None:
        if id(session) in self._connections:
            self._agents[agent_id] = id(session)

    def entry_for(self, agent_id: str) -> ConnectionEntry | None:
        key = self._agents.get(agent_id)
        return self._connections.get(key) if key is not None else None

    def reset(self) -> None:
        """测试钩子：清空全部登记。"""
        self._connections.clear()
        self._agents.clear()


# ---------------------------------------------------------------- notifier


class SamplingNotifier:
    """send() 落箱后的 sampling 唤醒通知器（改点2/3）。

    notify() 只做寻址 + 调度（worker 线程安全、微秒级返回）；策略加载、
    去重、createMessage、超时、审计全部在连接事件循环侧 _attempt 内跑。
    """

    def __init__(self, registry: SamplingRegistry) -> None:
        self._registry = registry
        self._fired: set[str] = set()  # 进程内 msg_id 去重（事件循环串行访问，无锁）

    def notify(self, store: MailStore, agent_id: str, msg_id: str) -> bool:
        """对已声明 sampling 的收件人连接调度一次唤醒。返回是否已调度。

        未登记 / 未声明 / loop 已死 → False（静默跳过，信照常落箱）。
        """
        entry = self._registry.entry_for(agent_id)
        if entry is None or not entry.declared_sampling:
            logger.debug("sampling skip: %s has no sampling-declared connection", agent_id)
            return False
        try:
            asyncio.run_coroutine_threadsafe(
                self._attempt(entry, store, agent_id, msg_id), entry.loop
            )
        except RuntimeError:
            logger.debug("sampling skip: connection loop not running for %s", agent_id)
            return False
        return True

    async def _attempt(
        self, entry: ConnectionEntry, store: MailStore, agent_id: str, msg_id: str
    ) -> None:
        root = store.root
        try:
            # --- 去重（改点3）：进程内 set + handled_log 双层，同一 msg_id 仅一次
            if msg_id in self._fired:
                append_sampling_log(
                    root,
                    {
                        "event": "skip",
                        "to": agent_id,
                        "msg_id": msg_id,
                        "reason": "duplicate-in-process",
                    },
                )
                return
            try:
                letter = store.get_letter(agent_id, msg_id)
            except MailboxError:
                logger.debug("sampling skip: %s/%s no longer on disk", agent_id, msg_id)
                return
            if any(e.get("action") == SAMPLING_ACTION for e in letter.get(HANDLED_LOG) or []):
                self._fired.add(msg_id)
                append_sampling_log(
                    root,
                    {
                        "event": "skip",
                        "to": agent_id,
                        "msg_id": msg_id,
                        "reason": "duplicate-handled-log",
                    },
                )
                return
            self._fired.add(msg_id)  # 一次尝试即刻记账：后续任何失败也不重发

            # --- payload（改点4）：wake policy 强制注入 + 信摘要 + msg_id
            policy, policy_source = wake_policy_for(
                root, agent_id
            )  # corrupt → fail-loud → 下方 except
            system_prompt = render_identity(policy, agent_id)
            prompt = render_wake_prompt(policy, letter)
            messages = [
                types.SamplingMessage(
                    role="user", content=types.TextContent(type="text", text=prompt)
                )
            ]
            timeout = sampling_timeout()
            append_sampling_log(
                root,
                {
                    "event": "request",
                    "to": agent_id,
                    "msg_id": msg_id,
                    "timeout_s": timeout,
                    "policy_source": policy_source,
                    "system_prompt": system_prompt,
                    "messages": _serialize_messages(messages),
                },
            )
            try:
                result = await asyncio.wait_for(
                    entry.session.create_message(
                        messages=messages,
                        system_prompt=system_prompt,
                        max_tokens=int(policy.get("max_tokens") or DEFAULT_MAX_TOKENS),
                    ),
                    timeout,
                )
            except asyncio.TimeoutError:
                outcome, detail = "timeout", f"no response within {timeout}s"
            except Exception as exc:  # noqa: BLE001 — 宿主拒答/连接已死/协议错误，一律静默降级
                outcome, detail = "error", str(exc)
            else:
                outcome, detail = "ok", str(getattr(result, "model", "") or "")
            append_sampling_log(
                root,
                {
                    "event": "result",
                    "to": agent_id,
                    "msg_id": msg_id,
                    "outcome": outcome,
                    "detail": detail,
                },
            )
            try:
                store.record_handled(agent_id, msg_id, SAMPLING_ACTION, outcome=outcome)
            except Exception as exc:  # noqa: BLE001 — 留痕失败不影响唤醒结果，只告警
                logger.warning(
                    "sampling handled_log write failed for %s/%s: %s", agent_id, msg_id, exc
                )
        except Exception as exc:  # noqa: BLE001 — fail-open 总闸：唤醒永不向 send 链路抛错
            # 失败静默降级：落日志即止，信留在箱里等 mailbox_check / fallback（改点3）
            logger.warning("sampling wake degraded for %s/%s: %s", agent_id, msg_id, exc)
            try:
                append_sampling_log(
                    root, {"event": "error", "to": agent_id, "msg_id": msg_id, "detail": str(exc)}
                )
            except Exception as exc2:  # noqa: BLE001 — 审计都失败时只能放弃
                logger.debug("sampling audit write failed too: %s", exc2)

    def reset(self) -> None:
        """测试钩子：清空进程内去重集。"""
        self._fired.clear()
