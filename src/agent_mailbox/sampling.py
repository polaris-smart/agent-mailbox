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
          "max_tokens": 512,
          "max_concurrent": 1,
          "sampling": {"enabled": true}
        }
      }
    }

SEP-2577 弃用加固：MCP 官方已于 2026-07-28 弃用 sampling capability
（MCPDeprecationWarning 在即）。sampling 是**加速通道**而非送达保证——
信必达的保证自始至终来自 fallback（mailbox_check / wake-daemon /
webhook / local-command）。宿主侧弃用落地（不再支持 createMessage）时，
把 per-agent 段的 ``sampling.enabled`` 设 false 即彻底关停本路径；
``enabled`` 格式错 fail-loud（raise MailboxError，send 钩子兜住落日志、
信照常落箱）——「想关没关成」比「继续发」危害大，不许静默回落。

per-agent 段按**整键覆盖**合并到 DEFAULT_WAKE_POLICY 上：没写的键保持
最小权限默认（策略作者只显式放宽他写出来的键）。wake.json 损坏 =
fail-loud（安全边界不静默降级），由采样侧接住落 error 日志。

per-agent 执行锁（HS 09-25 补钉①②③④，老板收束目标②）：同一 agent
同时至多 ``max_concurrent``（未配置默认 1——schema 缺省即锁）个
createMessage 在途，后续信进入该 agent 的闸门队列排队，FIFO = 按
**落箱时间序**（created_at，同秒内按到达序 tiebreak）。锁释放覆盖
成功/失败/超时三路径（``_run_one`` finally 计数）——宿主卡死走
timeout 返回时锁即释放，单卡死分身不得永久堵死该 agent 队列。
重启/连接死亡降级（补钉⑤）：内存排队未发出的请求一律逐条落
sampling.log ``degrade`` 审计并清空内存队列（不驻留内存队列）——信在
通知前已落盘 pending，mailbox_check / wake-daemon fallback 天然接管；
真正的保底是盘上的信，degrade 审计只是诚实记账。
"""

from __future__ import annotations

import asyncio
import bisect
import itertools
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server.session import ServerSession

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
    # per-agent 执行锁并发上限（补钉③）：未配置默认 1 —— schema 缺省即锁，
    # 「默认策略=最小权限」同一口径（上一分身未 done，新 sampling 排队不发出）
    "max_concurrent": 1,
    # sampling 路径开关（SEP-2577 加固）：缺省 true 保持现行行为。MCP 官方
    # 已于 2026-07-28 弃用 sampling capability（SEP-2577），宿主侧移除落地时
    # 设 false 即关停该路径。sampling 只是加速通道，信必达的保证来自
    # fallback（mailbox_check / wake-daemon / webhook / local-command）。
    "sampling": {"enabled": True},
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


def wake_max_concurrent(policy: dict[str, Any]) -> int:
    """per-agent 执行锁并发上限（补钉③）：wake.json ``max_concurrent`` 可配，
    未配置默认 1（schema 缺省即锁）。

    值非法（非正整数）→ fail-loud 抛 MailboxError；闸门侧接住后回落到最
    保守的 1（锁语义）并落 warning——上限配置坏了宁可锁死也不放并发。
    """
    raw = policy.get("max_concurrent", 1)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise MailboxError(f"wake policy max_concurrent must be a positive integer, got {raw!r}")
    return raw


def sampling_enabled(policy: dict[str, Any]) -> bool:
    """sampling 路径开关（SEP-2577 加固）：wake policy ``sampling.enabled``，
    未配置默认 true（保持现行行为）。

    格式错（sampling 段非对象 / enabled 非 bool）→ fail-loud 抛
    MailboxError——用户意图是「关」，静默回落 true 等于关不掉，危害大于
    继续发；由 on_delivered 钩子兜住落日志，信照常落箱走 fallback。
    """
    section = policy.get("sampling", {"enabled": True})
    if not isinstance(section, dict) or not isinstance(section.get("enabled"), bool):
        raise MailboxError(
            f"wake policy sampling must be an object with a boolean 'enabled', got {section!r}"
        )
    return section["enabled"]


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


def stable_connection(session: Any) -> Any:
    """从 per-request 代理里取稳定的 per-connection 锚点。

    MCP SDK 的 ``ServerSession`` 是**每请求新建**的代理（ServerRunner.
    _make_context），真正跨请求存活的是 ``session._connection``
    （mcp.server.connection.Connection，独立出站通道 connection.outbound
    也挂在它身上）。SDK 没有公开 accessor，这里走私有属性并兜底回退
    （测试替身可直接传 Connection 或任意稳定对象）。
    """
    return getattr(session, "_connection", session)


@dataclass
class ConnectionEntry:
    """一条 MCP 连接在采样注册表里的登记（v0.7 改点1）。

    ``connection`` 是稳定锚点（ServerSession 每请求重建，Connection 不是）；
    ``loop`` 在 initialize 时捕获：同步工具跑在 anyio worker 线程，落箱钩子
    靠 ``run_coroutine_threadsafe(coro, loop)`` 把 createMessage 调度回连接的
    事件循环，send 主链路零阻塞。createMessage 经 ``ServerSession(None,
    connection)`` 代理、不带 related_request_id → 走 connection.outbound
    独立通道（SDK send_request 的既定路由）。
    """

    connection: Any  # mcp.server.connection.Connection（宽松引用，方便测试替身）
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

    def register_connection(self, connection: Any, declared: bool) -> ConnectionEntry:
        entry = ConnectionEntry(
            connection=connection,
            declared_sampling=declared,
            loop=asyncio.get_running_loop(),
            session_id=getattr(connection, "session_id", None),
        )
        if len(self._connections) >= self.MAX_CONNECTIONS:
            oldest = min(self._connections.values(), key=lambda e: e.connected_at)
            self._connections.pop(id(oldest.connection), None)
        self._connections[id(connection)] = entry
        logger.info(
            "sampling capability %s on connection %s",
            "declared" if declared else "not declared",
            entry.session_id or "stdio",
        )
        return entry

    def bind_agent(self, agent_id: str, connection: Any) -> None:
        if id(connection) in self._connections:
            self._agents[agent_id] = id(connection)

    def entry_for(self, agent_id: str) -> ConnectionEntry | None:
        key = self._agents.get(agent_id)
        return self._connections.get(key) if key is not None else None

    def reset(self) -> None:
        """测试钩子：清空全部登记。"""
        self._connections.clear()
        self._agents.clear()


# ------------------------------------------------------------- per-agent gate


@dataclass
class _QueuedLetter:
    """闸门队列里的一封待唤醒信。

    排序键 = ``(created_at, seq)``：created_at 是落箱时间序（FIFO 依据，
    ISO 字典序即时间序）；seq 是通知到达序，做同秒 created_at 的 tiebreak。
    created_at 缺失（直接 notify 的调用方）用 ``"\\uffff"`` 沉底，落箱序
    永远优先于无时间戳的请求。
    """

    msg_id: str
    created_at: str
    seq: int
    store: MailStore
    entry: ConnectionEntry

    @staticmethod
    def sort_key(item: _QueuedLetter) -> tuple[str, int]:
        return (item.created_at or "\uffff", item.seq)


class _AgentGate:
    """per-agent sampling 执行闸门（HS 补钉①②③④，收束目标②）。

    同一 agent 全程至多 ``max_concurrent``（默认 1）个 createMessage 在途；
    后续信落 ``queue`` 排队，FIFO = (created_at, 到达序)。全部状态只在
    gate 所属事件循环上读写（enqueue/drain 协程单线程串行），notify()
    的 worker 线程只做闸门创建与 ``run_coroutine_threadsafe`` 调度。

    锁释放三路径（成功/失败/超时）由 ``_run_one`` 的 finally 计数保证：
    ``_attempt`` 自身 fail-open 永不抛出，无论 outcome 如何 in_flight 都
    递减、drain 自动续跑下一封——单卡死分身走 timeout 返回后队列照常流动。
    """

    def __init__(
        self, agent_id: str, loop: asyncio.AbstractEventLoop, owner: SamplingNotifier
    ) -> None:
        self.agent_id = agent_id
        self.loop = loop
        self.owner = owner
        self.in_flight = 0
        self.queue: list[_QueuedLetter] = []
        self._drain_task: asyncio.Task | None = None
        self._refill = asyncio.Event()  # enqueue 唤醒信号（首个 wait 在 loop 上，绑定安全）

    async def enqueue(self, item: _QueuedLetter) -> None:
        """入队（落箱时间序插入，晚到早落的信插队到同批前头）并确保 drain 在跑。"""
        bisect.insort(self.queue, item, key=_QueuedLetter.sort_key)
        self._refill.set()
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        """补位式派发：有空槽就立刻从队首补信，slot 满则等任一在途完成或新信入队。

        与 enqueue 同循环串行：top-up 之间没有 await 间隙，晚到信要么被下一轮
        top-up 捡起（refill 唤醒），要么在 drain 已 done 后由 enqueue 重起新 drain。
        """
        pending: set[asyncio.Task] = set()
        refill: asyncio.Task | None = None
        try:
            while True:
                cap = self.owner._capacity(self)
                while self.queue and self.in_flight < cap:
                    item = self.queue.pop(0)
                    self.in_flight += 1
                    pending.add(asyncio.create_task(self._run_one(item)))
                if not pending:
                    return
                self._refill.clear()
                refill = asyncio.create_task(self._refill.wait())
                _, still = await asyncio.wait(
                    {*pending, refill}, return_when=asyncio.FIRST_COMPLETED
                )
                pending = {t for t in still if t is not refill}
                refill = None
        except asyncio.CancelledError:
            for t in pending:
                t.cancel()
            raise
        finally:
            if refill is not None:
                refill.cancel()

    async def _run_one(self, item: _QueuedLetter) -> None:
        try:
            await self.owner._attempt(item.entry, item.store, self.agent_id, item.msg_id)
        finally:
            # 锁释放三路径：成功/失败/超时（_attempt 吞掉一切异常）都走到这里
            self.in_flight -= 1


# ---------------------------------------------------------------- notifier


class SamplingNotifier:
    """send() 落箱后的 sampling 唤醒通知器（改点2/3 + HS 补钉①-⑤）。

    notify() 只做寻址 + 入闸门调度（worker 线程安全、微秒级返回）；排队、
    并发互斥、策略加载、去重、createMessage、超时、审计全部在连接事件
    循环侧 per-agent 闸门（_AgentGate）内跑。
    """

    def __init__(self, registry: SamplingRegistry) -> None:
        self._registry = registry
        self._fired: set[str] = set()  # 进程内 msg_id 去重（事件循环串行访问，无锁）
        # per-agent 执行闸门（补钉①）：notify 来自任意 anyio worker 线程，
        # 闸门表的创建/查找用 threading.Lock 护住；闸门内部状态只在 loop 上动。
        self._gates: dict[str, _AgentGate] = {}
        self._gates_lock = threading.Lock()
        self._seq = itertools.count()  # 通知到达序（同 created_at 的 FIFO tiebreak）

    def notify(
        self, store: MailStore, agent_id: str, msg_id: str, created_at: str | None = None
    ) -> bool:
        """对已声明 sampling 的收件人连接调度一次唤醒。返回是否已入队调度。

        未登记 / 未声明 / loop 已死 → False（静默跳过，信照常落箱）。
        收件人在途/排队时 → True（进入 per-agent 闸门排队，FIFO 等待，
        不并发出第二个 sampling——补钉①②）。
        """
        entry = self._registry.entry_for(agent_id)
        if entry is None or not entry.declared_sampling:
            logger.debug("sampling skip: %s has no sampling-declared connection", agent_id)
            return False
        # SEP-2577 加固：per-agent 显式关停 → 不入闸门不发 createMessage，
        # 信照常落箱走 fallback（mailbox_check / wake-daemon / webhook）。
        # policy 读取/开关格式错 → 落 sampling.log error 审计（fail-loud
        # 可见）+ 静默跳过——与 _attempt 的 corrupt-policy 语义同一口径。
        try:
            policy, _src = wake_policy_for(store.root, agent_id)
            enabled = sampling_enabled(policy)
        except MailboxError as exc:
            try:
                append_sampling_log(
                    store.root,
                    {"event": "error", "to": agent_id, "msg_id": msg_id, "detail": str(exc)},
                )
            except Exception:  # noqa: BLE001 — 审计都失败时只能放弃
                logger.debug("sampling audit write failed too: %s", exc)
            return False
        if not enabled:
            logger.debug("sampling skip: disabled by wake policy for %s", agent_id)
            return False
        with self._gates_lock:
            gate = self._gate_for(agent_id, entry)
            seq = next(self._seq)
        try:
            asyncio.run_coroutine_threadsafe(
                gate.enqueue(
                    _QueuedLetter(
                        msg_id=msg_id,
                        created_at=created_at or "",
                        seq=seq,
                        store=store,
                        entry=entry,
                    )
                ),
                gate.loop,
            )
        except RuntimeError:
            logger.debug("sampling skip: connection loop not running for %s", agent_id)
            return False
        return True

    def _gate_for(self, agent_id: str, entry: ConnectionEntry) -> _AgentGate:
        """取或建 per-agent 闸门（调用方须持 ``_gates_lock``）。

        旧闸门的事件循环已关（连接死亡/会话重建）→ 队列里未发出的信逐条
        落 degrade 审计（补钉⑤连接腿），闸门在当前连接的循环上重建。
        """
        gate = self._gates.get(agent_id)
        if gate is not None and gate.loop is not entry.loop and gate.loop.is_closed():
            self._degrade_gate(gate, "connection-loop-closed")
            gate = None
        if gate is None:
            gate = _AgentGate(agent_id, entry.loop, owner=self)
            self._gates[agent_id] = gate
        return gate

    def _degrade_gate(self, gate: _AgentGate, reason: str) -> list[dict[str, Any]]:
        """清空一个闸门的内存队列，逐条落 sampling.log degrade 审计。"""
        degraded: list[dict[str, Any]] = []
        while gate.queue:
            item = gate.queue.pop(0)
            degraded.append({"to": gate.agent_id, "msg_id": item.msg_id})
            try:
                append_sampling_log(
                    item.store.root,
                    {
                        "event": "degrade",
                        "reason": reason,
                        "to": gate.agent_id,
                        "msg_id": item.msg_id,
                    },
                )
            except Exception as exc:  # noqa: BLE001 — 审计失败不影响降级本身
                logger.debug(
                    "sampling degrade audit failed for %s/%s: %s",
                    gate.agent_id,
                    item.msg_id,
                    exc,
                )
        return degraded

    def degrade_pending(self, reason: str = "server-restart") -> list[dict[str, Any]]:
        """补钉⑤：内存排队未发出的 sampling 请求一律降级走 fallback 落箱。

        进程收场（server.main 的 finally）调用：逐条落 sampling.log
        ``degrade`` 审计并**清空内存队列**（不驻留内存队列，重启即失，
        fallback 天然接管——信在通知前已落盘 pending，mailbox_check 兜底）。
        返回降级清单。真正的保底是盘上的信，本审计只是诚实记账。
        """
        with self._gates_lock:
            degraded: list[dict[str, Any]] = []
            for gate in self._gates.values():
                degraded.extend(self._degrade_gate(gate, reason))
            return degraded

    def _capacity(self, gate: _AgentGate) -> int:
        """该 agent 当前批次的并发上限（补钉③）：wake.json max_concurrent，
        未配置默认 1；配置不可读（损坏/非法值）→ 回落最保守的 1 + warning。"""
        if not gate.queue:
            return 1  # 空队列上限无意义，不读配置不扰日志
        try:
            head = gate.queue[0]
            policy, _ = wake_policy_for(head.store.root, gate.agent_id)
            return wake_max_concurrent(policy)
        except Exception as exc:  # noqa: BLE001 — 上限坏了宁可锁死也不放并发
            logger.warning(
                "sampling max_concurrent unreadable for %s (%s); locked to 1", gate.agent_id, exc
            )
            return 1

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
                # ServerSession 是每请求代理：这里用轻量长命代理（只读
                # connection 的稳定状态），不带 related_request_id → 走
                # connection.outbound 独立通道（宿主已 initialized 才会有信）。
                proxy = ServerSession(None, entry.connection)
                result = await asyncio.wait_for(
                    proxy.create_message(
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
        """测试钩子：清空进程内去重集与全部执行闸门（不落降级审计）。"""
        self._fired.clear()
        with self._gates_lock:
            self._gates.clear()
