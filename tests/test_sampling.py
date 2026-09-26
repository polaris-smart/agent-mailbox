"""v0.7 sampling 唤醒测试：能力协商矩阵 × 超时 × 去重 × policy 注入（改点5）。

两条测试通道：

- **内存对**（mcp.shared.memory）：真实 MCP 连接语义。ClientSession 带
  ``sampling_callback`` 即声明 ``capabilities.sampling``（_build_capabilities
  的行为），不带即未声明——宿主能力协商矩阵由此构造；回调里抓 createMessage
  请求体即为验收铁证。
- **真 stdio 子进程**：握手声明 sampling → 发信 → 从 stdout 流里抓
  ``sampling/createMessage`` 请求（任务书验收 a 的 wire 级铁证）。

自省注册表直接断言（srv._sampling_registry），配合 sampling.log 与
handled_log 双审计面。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time

import pytest
from mcp import ClientSession, types
from mcp.shared.memory import create_client_server_memory_streams

from agent_mailbox import server as srv
from agent_mailbox.sampling import ConnectionEntry, _AgentGate, _QueuedLetter
from agent_mailbox.store import MailStore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "bin", "python")
if not os.path.exists(PY):
    PY = sys.executable

DEFAULT_GIT_LINE = "git 写操作"  # 最小权限默认策略的禁区标记


# ----------------------------------------------------------------- fixtures


@pytest.fixture
def mailroot(tmp_path, monkeypatch):
    """隔离 mail root + 复位 server 进程级单例（store/binding/注册表/通知器）。"""
    monkeypatch.setenv("AGENT_MAIL_HOME", str(tmp_path))
    for var in (
        "AGENT_MAIL_ID",
        "AGENT_MAIL_SAMPLING_TIMEOUT",
        "AGENT_MAIL_TOKEN",
        "AGENT_MAIL_SESSION",
    ):
        monkeypatch.delenv(var, raising=False)
    srv._store = None
    srv._binding = None
    srv._sampling_registry.reset()
    srv._sampling_notifier.reset()
    yield tmp_path
    srv._store = None
    srv._binding = None


# ------------------------------------------------------------------ helpers


def read_sampling_log(root) -> list[dict]:
    """sampling.log 全量 JSONL（文件不存在 = 空）。"""
    path = root / "sampling.log"
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


async def wait_until(cond, timeout: float = 8.0, what: str = "condition"):
    """事件循环内轮询直到 cond() 为真，超时即 AssertionError。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def make_sampling_cb(state: dict, release: asyncio.Event | None = None, boom: bool = False):
    """测试宿主的 sampling 回调：抓请求体（铁证），可选挂起/报错。"""

    async def cb(context, params: types.CreateMessageRequestParams):
        state.setdefault("requests", []).append(params)
        state["count"] = len(state["requests"])
        if boom:
            raise RuntimeError("host llm exploded")
        if release is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(release.wait(), 20)
        return types.CreateMessageResult(
            role="assistant",
            content=types.TextContent(type="text", text="ok, will check mailbox"),
            model="test-host-model",
        )

    return cb


def make_seq_cb(state: dict, releases: list, boom_first: bool = False):
    """逐笔阻塞的宿主回调（执行锁 recording 铁证用）。

    第 i 个请求抓体并挂起直到 releases[i] 置位（缺位=None 不阻塞）；
    每个请求记 (start, end)——相邻请求时间窗不相交 ⇔ 任意时刻 in-flight
    sampling ≤ 1 ⇔ 全程无双分身。end 在 finally 记账：服务端超时可能把
    挂起的回调静默取消（SDK 行为），取消/异常收场同样算"已收场"。
    """

    async def cb(context, params: types.CreateMessageRequestParams):
        idx = len(state["requests"])
        state["requests"].append({"params": params, "start": time.monotonic(), "end": None})
        try:
            if idx == 0 and boom_first:
                raise RuntimeError("host llm exploded")
            ev = releases[idx] if idx < len(releases) else None
            if ev is not None:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(ev.wait(), 30)
            return types.CreateMessageResult(
                role="assistant",
                content=types.TextContent(type="text", text="ok, will check mailbox"),
                model="test-host-model",
            )
        finally:
            state["requests"][idx]["end"] = time.monotonic()

    return cb


def request_msg_ids(state: dict) -> list[str]:
    """recording 里逐笔抽出的 msg_id（请求正文注入的 msg_id 行）。"""
    out = []
    for r in state["requests"]:
        m = re.search(r"msg_id: (\S+)", r["params"].messages[0].content.text)
        out.append(m.group(1) if m else "")
    return out


async def run_pair(mailroot, body, sampling_cb=None):
    """起一对内存 MCP 连接跑场景：server 任务 + ClientSession。

    sampling_cb 非 None → 宿主声明 sampling capability（改点1 的协商输入）。
    """
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        c_read, c_write = client_streams
        s_read, s_write = server_streams
        init_opts = srv.server._lowlevel_server.create_initialization_options()
        server_task = asyncio.create_task(
            srv.server._lowlevel_server.run(s_read, s_write, init_opts)
        )
        try:
            async with ClientSession(c_read, c_write, sampling_callback=sampling_cb) as client:
                await client.initialize()
                await body(client)
        finally:
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server_task


async def call_tool(client, name, arguments) -> dict:
    """调工具，isError 即断言失败；返回 JSON 载荷。"""
    result = await client.call_tool(name, arguments)
    assert result.is_error is not True, f"{name} failed: {result.content}"
    assert result.content and isinstance(result.content[0], types.TextContent), f"{name}: no text"
    return json.loads(result.content[0].text)


# ================================================= 能力协商矩阵（改点1+2）


def test_declared_host_receives_policy_injected_sampling(mailroot):
    """验收a：声明 sampling 的宿主发信 → createMessage 收到且含 policy 注入。"""
    state: dict = {}
    cb = make_sampling_cb(state)
    registered: dict = {}

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB", "owner": "Workbuddy"})
        # 改点1 直证：per-connection 登记 + 身份绑定
        entry = srv._sampling_registry.entry_for("WB")
        assert entry is not None and entry.declared_sampling is True
        sent = await call_tool(
            client,
            "mailbox_send",
            {"from_id": "HS", "to": "WB", "subject": "fuel ready", "body": "1856"},
        )
        assert sent["count"] == 1
        registered["msg_id"] = sent["delivered"][0]["id"]
        await wait_until(lambda: state.get("count") == 1, what="sampling request at host")
        await wait_until(
            lambda: any(
                e.get("event") == "result" and e.get("outcome") == "ok"
                for e in read_sampling_log(mailroot)
            ),
            what="sampling result audit",
        )

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))
    params = state["requests"][0]
    text = params.messages[0].content.text
    assert registered["msg_id"] in text  # msg_id 注入
    assert "fuel ready" in text  # 信摘要注入
    assert DEFAULT_GIT_LINE in text  # 最小权限默认策略注入
    assert "留痕" in text
    assert params.system_prompt and "WB" in params.system_prompt  # 身份模板进 systemPrompt
    # 验收b 反面照应 + 验收f：注入请求体已落 sampling.log
    req_lines = [e for e in read_sampling_log(mailroot) if e.get("event") == "request"]
    assert len(req_lines) == 1
    assert req_lines[0]["msg_id"] == registered["msg_id"]
    assert req_lines[0]["policy_source"] == "default"
    # 信照常落箱
    letter = MailStore(mailroot).get_letter("WB", registered["msg_id"])
    assert letter["status"] == "pending"


def test_undeclared_host_zero_sampling_letter_lands(mailroot):
    """验收b：未声明 sampling 宿主 → 零 sampling 请求，信照常落箱。"""
    state: dict = {}

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        entry = srv._sampling_registry.entry_for("WB")
        assert entry is not None and entry.declared_sampling is False  # 登记为未声明
        sent = await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "quiet", "body": "shh"}
        )
        assert sent["count"] == 1
        registered = sent["delivered"][0]["id"]
        await asyncio.sleep(0.6)  # 给错误的 sampling 尝试留暴露窗口
        assert state.get("count", 0) == 0  # 宿主零 createMessage
        assert read_sampling_log(mailroot) == []  # 零请求/零错误落审计
        assert MailStore(mailroot).get_letter("WB", registered)["status"] == "pending"

    asyncio.run(run_pair(mailroot, body))  # 无 sampling_callback = 未声明宿主


# ================================================ sampling.enabled 开关（SEP-2577）


def test_sampling_enabled_defaults_true_and_garbage_fails():
    """SEP-2577 加固：enabled 未配置 = true（现行行为）；格式错 fail-loud。"""
    from agent_mailbox.sampling import sampling_enabled
    from agent_mailbox.store import MailboxError

    assert sampling_enabled({}) is True
    assert sampling_enabled({"sampling": {"enabled": True}}) is True
    assert sampling_enabled({"sampling": {"enabled": False}}) is False
    with pytest.raises(MailboxError):
        sampling_enabled({"sampling": "off"})  # 段非对象
    with pytest.raises(MailboxError):
        sampling_enabled({"sampling": {"enabled": 1}})  # enabled 非 bool


def test_sampling_disabled_by_wake_policy_letter_lands(mailroot):
    """SEP-2577 加固：宿主已声明 sampling 但 policy enabled=false → 零采样，信照常落箱。"""
    (mailroot / "wake.json").write_text(
        json.dumps({"agents": {"WB": {"sampling": {"enabled": False}}}}), encoding="utf-8"
    )
    state: dict = {}
    cb = make_sampling_cb(state)

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        entry = srv._sampling_registry.entry_for("WB")
        assert entry is not None and entry.declared_sampling is True  # 宿主已声明
        sent = await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "off", "body": "muted"}
        )
        registered = sent["delivered"][0]["id"]
        await asyncio.sleep(0.6)  # 给错误的采样尝试留暴露窗口
        assert state.get("count", 0) == 0  # 零 createMessage
        assert read_sampling_log(mailroot) == []  # 零请求/零错误落审计
        assert MailStore(mailroot).get_letter("WB", registered)["status"] == "pending"

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))


def test_sampling_enabled_garbage_fails_loud_letter_lands(mailroot, caplog):
    """SEP-2577 加固：enabled 格式错 → fail-loud（on_delivered 钩子兜住落日志），信照常落箱。"""
    (mailroot / "wake.json").write_text(
        json.dumps({"agents": {"WB": {"sampling": {"enabled": "yes"}}}}), encoding="utf-8"
    )
    state: dict = {}
    cb = make_sampling_cb(state)

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        sent = await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "bad", "body": "cfg"}
        )
        registered = sent["delivered"][0]["id"]
        await wait_until(
            lambda: any(e.get("event") == "error" for e in read_sampling_log(mailroot)),
            what="bad-enabled error audit",
        )
        assert state.get("count", 0) == 0  # 配置坏了不发 createMessage
        assert MailStore(mailroot).get_letter("WB", registered)["status"] == "pending"

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))


def test_unbound_recipient_no_sampling_letter_lands(mailroot):
    """能力协商矩阵·离线腿：收件人连接未登记（离线宿主）→ 不采样，信持久化。"""
    state: dict = {}
    cb = make_sampling_cb(state)

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "HS"})
        sent = await call_tool(
            client,
            "mailbox_send",
            {"from_id": "HS", "to": "WB", "subject": "offline", "body": "later"},
        )
        registered = sent["delivered"][0]["id"]
        await asyncio.sleep(0.6)
        assert srv._sampling_registry.entry_for("WB") is None
        assert state.get("count", 0) == 0
        assert read_sampling_log(mailroot) == []
        assert MailStore(mailroot).get_letter("WB", registered)["status"] == "pending"

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))


def test_env_identity_binding_wakes(mailroot, monkeypatch):
    """改点1 补充：工具未带身份参数时回退 AGENT_MAIL_ID env 绑定连接。"""
    monkeypatch.setenv("AGENT_MAIL_ID", "WB")
    state: dict = {}
    cb = make_sampling_cb(state)

    async def body(client):
        await call_tool(client, "mailbox_check", {})  # 无 agent_id → env 回退绑定 WB
        assert srv._sampling_registry.entry_for("WB") is not None
        await call_tool(
            client,
            "mailbox_send",
            {"from_id": "HS", "to": "WB", "subject": "env bind", "body": "x"},
        )
        await wait_until(lambda: state.get("count") == 1, what="env-bound wake")

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))


# ================================================= 超时 × 不阻塞（改点3）


def test_host_hang_times_out_letter_stays_pending(mailroot, monkeypatch):
    """验收d 前半：宿主模拟卡死 → server 在 timeout 内判超时、信留箱。"""
    monkeypatch.setenv("AGENT_MAIL_SAMPLING_TIMEOUT", "1.0")
    state: dict = {}
    release = asyncio.Event()
    cb = make_sampling_cb(state, release=release)

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        sent = await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "hang", "body": "z"}
        )
        msg_id = sent["delivered"][0]["id"]
        await wait_until(
            lambda: any(
                e.get("event") == "result" and e.get("outcome") == "timeout"
                for e in read_sampling_log(mailroot)
            ),
            what="timeout outcome audit",
        )
        letter = MailStore(mailroot).get_letter("WB", msg_id)
        assert letter["status"] == "pending"  # 超时信照常可被 mailbox_check
        assert any(
            e.get("action") == "sampling" and e.get("outcome") == "timeout"
            for e in letter.get("handled_log") or []
        )
        release.set()  # 放开卡死的宿主回调，让场景干净收尾

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))


def test_send_main_path_not_blocked(mailroot, monkeypatch):
    """验收d 后半：宿主不响应期间 send 主链路先返回（不受阻）。"""
    monkeypatch.setenv("AGENT_MAIL_SAMPLING_TIMEOUT", "30")
    state: dict = {}
    release = asyncio.Event()
    cb = make_sampling_cb(state, release=release)

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        t0 = time.monotonic()
        await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "fast", "body": "y"}
        )
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0, f"send blocked {elapsed:.1f}s behind a hung host"
        await wait_until(lambda: state.get("count") == 1, what="sampling request at host")
        release.set()
        await wait_until(
            lambda: any(
                e.get("event") == "result" and e.get("outcome") == "ok"
                for e in read_sampling_log(mailroot)
            ),
            what="result after release",
        )

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))


def test_host_error_degrades_silently(mailroot):
    """改点3 静默降级腿1：宿主回调抛错 → 不冒泡、落 error 审计、信留箱。"""
    state: dict = {}
    cb = make_sampling_cb(state, boom=True)

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        sent = await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "boom", "body": "b"}
        )
        msg_id = sent["delivered"][0]["id"]
        await wait_until(
            lambda: any(
                e.get("event") == "result" and e.get("outcome") == "error"
                for e in read_sampling_log(mailroot)
            ),
            what="error outcome audit",
        )
        assert MailStore(mailroot).get_letter("WB", msg_id)["status"] == "pending"

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))


def test_sampling_timeout_env_garbage_degrades(mailroot, monkeypatch):
    """改点3 静默降级腿2：超时配置垃圾值 fail-loud → 采样侧接住落 error。"""
    monkeypatch.setenv("AGENT_MAIL_SAMPLING_TIMEOUT", "not-a-number")
    state: dict = {}
    cb = make_sampling_cb(state)

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        sent = await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "bad cfg", "body": "c"}
        )
        msg_id = sent["delivered"][0]["id"]
        await wait_until(
            lambda: any(e.get("event") == "error" for e in read_sampling_log(mailroot)),
            what="config error audit",
        )
        assert state.get("count", 0) == 0
        assert MailStore(mailroot).get_letter("WB", msg_id)["status"] == "pending"

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))


# ================================================= 去重（改点3）


def test_same_msg_id_sampled_once(mailroot):
    """验收c：同一 msg_id 二次触发 → 仅一次 sampling（进程内去重）。"""
    state: dict = {}
    cb = make_sampling_cb(state)

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        sent = await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "once", "body": "1"}
        )
        msg_id = sent["delivered"][0]["id"]
        await wait_until(lambda: state.get("count") == 1, what="first sampling")
        # 直接对同一 msg_id 再触发一次（重试模拟）
        srv._sampling_notifier.notify(srv._store_instance(), "WB", msg_id)
        await asyncio.sleep(0.6)
        assert state["count"] == 1  # 仍是一次
        assert any(
            e.get("event") == "skip" and e.get("reason") == "duplicate-in-process"
            for e in read_sampling_log(mailroot)
        )

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))


def test_handled_log_preseed_skips_sampling(mailroot):
    """验收c 持久层：handled_log 已有 sampling 记录 → 跨进程也不重发。"""
    state: dict = {}
    cb = make_sampling_cb(state)

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        # 独立实例预置一封已采样过的信（该实例没挂钩子，不会自动触发）
        seed = MailStore(mailroot)
        mid = seed.send("HS", "WB", "preseeded", "already sampled", dedupe=False)[0]["id"]
        seed.record_handled("WB", mid, "sampling", outcome="ok")
        scheduled = srv._sampling_notifier.notify(srv._store_instance(), "WB", mid)
        assert scheduled is True  # 连接在、能力在——但去重拦下
        await asyncio.sleep(0.6)
        assert state.get("count", 0) == 0
        assert any(
            e.get("event") == "skip" and e.get("reason") == "duplicate-handled-log"
            for e in read_sampling_log(mailroot)
        )

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))


def test_deduped_resend_no_new_sampling(mailroot):
    """验收c 工具面：同信重发（dedupe=True）→ 去重信零副作用，sampling 不加次。"""
    state: dict = {}
    cb = make_sampling_cb(state)

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        args = {"from_id": "HS", "to": "WB", "subject": "same", "body": "identical"}
        first = await call_tool(client, "mailbox_send", dict(args))
        assert first["count"] == 1
        await wait_until(lambda: state.get("count") == 1, what="first sampling")
        second = await call_tool(client, "mailbox_send", dict(args))
        assert second["count"] == 0 and second["delivered"][0]["deduped"] is True
        await asyncio.sleep(0.6)
        assert state["count"] == 1  # 单信仅一次 sampling
        assert len([e for e in read_sampling_log(mailroot) if e.get("event") == "request"]) == 1

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))


def test_new_letter_after_dedupe_false_gets_own_sampling(mailroot):
    """验收c 边界：dedupe=False 重发是新信（新 msg_id）→ 各采一次、互不串扰。"""
    state: dict = {}
    cb = make_sampling_cb(state)
    ids: list = []

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        args = {"from_id": "HS", "to": "WB", "subject": "fresh", "body": "v", "dedupe": False}
        first = await call_tool(client, "mailbox_send", dict(args))
        ids.append(first["delivered"][0]["id"])
        await wait_until(lambda: state.get("count") == 1, what="first sampling")
        second = await call_tool(client, "mailbox_send", dict(args))
        ids.append(second["delivered"][0]["id"])
        await wait_until(lambda: state.get("count") == 2, what="second sampling")
        store = MailStore(mailroot)
        for mid in ids:
            log = store.get_letter("WB", mid).get("handled_log") or []
            assert sum(1 for e in log if e.get("action") == "sampling") == 1

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))


# ================================================= wake policy（改点4）


def test_default_minimal_policy_injected(mailroot):
    """改点4 默认面：无 wake.json → 最小权限默认策略随唤醒注入。"""
    state: dict = {}
    cb = make_sampling_cb(state)

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        await call_tool(
            client,
            "mailbox_send",
            {"from_id": "HS", "to": "WB", "subject": "policy d", "body": "d"},
        )
        await wait_until(lambda: state.get("count") == 1, what="sampling")

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))
    text = state["requests"][0].messages[0].content.text
    assert DEFAULT_GIT_LINE in text
    assert "生产环境写操作" in text
    assert "删除文件或数据" in text
    assert "留痕要求" in text
    assert "mailbox_check" in text and "mailbox_reply" in text  # 任务指令含留痕闭环


def test_custom_wake_json_override(mailroot):
    """改点4 定制面：wake.json per-agent 段整键覆盖 + 未列 agent 保持默认。"""
    (mailroot / "wake.json").write_text(
        json.dumps(
            {
                "agents": {
                    "WB": {
                        "identity": "你是WB的专属测试分身",
                        "task": "只做信里交代的一件事",
                        "forbidden": ["禁止碰核按钮"],
                        "require_receipt": True,
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    state: dict = {}
    cb = make_sampling_cb(state)

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        await call_tool(client, "mailbox_register", {"agent_id": "WC"})
        await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "for wb", "body": "1"}
        )
        await wait_until(lambda: state.get("count") == 1, what="WB sampling")
        await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WC", "subject": "for wc", "body": "2"}
        )
        await wait_until(lambda: state.get("count") == 2, what="WC sampling")

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))
    wb_text = state["requests"][0].messages[0].content.text
    wc_text = state["requests"][1].messages[0].content.text
    assert "专属测试分身" in wb_text and "禁止碰核按钮" in wb_text
    assert DEFAULT_GIT_LINE not in wb_text  # 整键覆盖：WB 的禁区以 wake.json 为准
    assert DEFAULT_GIT_LINE in wc_text  # 未列 agent 仍走最小权限默认
    req_lines = [e for e in read_sampling_log(mailroot) if e.get("event") == "request"]
    assert req_lines[0]["policy_source"] == "wake.json"
    assert req_lines[1]["policy_source"] == "default"


def test_corrupt_wake_json_silent_degrade_letter_persists(mailroot):
    """改点4 fail-loud 面：wake.json 损坏 → 不注入不采样、落 error、信不丢。"""
    (mailroot / "wake.json").write_text("{not json", encoding="utf-8")
    state: dict = {}
    cb = make_sampling_cb(state)

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        sent = await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "corrupt", "body": "x"}
        )
        msg_id = sent["delivered"][0]["id"]
        await wait_until(
            lambda: any(e.get("event") == "error" for e in read_sampling_log(mailroot)),
            what="corrupt-policy error audit",
        )
        assert state.get("count", 0) == 0  # 损坏策略绝不裸唤醒（无护栏不放行）
        letter = MailStore(mailroot).get_letter("WB", msg_id)
        assert letter["status"] == "pending"  # 信照常落箱等 fallback

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))


# ================================================= 注入请求体留证（验收f）


def test_sampling_log_request_payload_captures_full_body(mailroot):
    """验收f：sampling.log 的 request 行完整留证请求体（policy+摘要+msg_id）。"""
    state: dict = {}
    cb = make_sampling_cb(state)
    ids: dict = {}

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        sent = await call_tool(
            client,
            "mailbox_send",
            {"from_id": "HS", "to": "WB", "subject": "audit me", "body": "payload"},
        )
        ids["mid"] = sent["delivered"][0]["id"]
        await wait_until(lambda: state.get("count") == 1, what="sampling")

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))
    req = next(e for e in read_sampling_log(mailroot) if e.get("event") == "request")
    assert req["to"] == "WB" and req["msg_id"] == ids["mid"]
    assert req["timeout_s"] == 60.0  # 默认 60s 可配（改点3）
    body_text = req["messages"][0]["content"]["text"]
    assert ids["mid"] in body_text and "audit me" in body_text and DEFAULT_GIT_LINE in body_text
    assert "WB" in req["system_prompt"]
    assert any(
        e.get("event") == "result" and e.get("outcome") == "ok" for e in read_sampling_log(mailroot)
    )


# ================================================= 真 stdio wire 铁证


def test_sampling_stdio_e2e_wire(mailroot, tmp_path):
    """验收a wire 级铁证：真 stdio 子进程，握手声明 sampling → 发信 →
    从 stdout 抓 sampling/createMessage 请求 → 回应 → 全链路审计闭合。"""
    proc_mailroot = tmp_path / "stdio-root"
    env = {
        **os.environ,
        "AGENT_MAIL_HOME": str(proc_mailroot),
        "AGENT_MAIL_SAMPLING_TIMEOUT": "10",
        "AGENT_MAIL_ID": "",
        "AGENT_MAIL_SESSION": "",
        "AGENT_MAIL_TOKEN": "",
    }
    proc = subprocess.Popen(
        [PY, "-m", "agent_mailbox.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    q: queue.Queue = queue.Queue()
    threading.Thread(
        target=lambda: [q.put(proc.stdout.readline()) for _ in iter(int, 1)], daemon=True
    ).start()

    def send(m):
        proc.stdin.write((json.dumps(m) + "\n").encode())
        proc.stdin.flush()

    def read_resp(timeout=15):
        return json.loads(q.get(timeout=timeout))

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"sampling": {}},  # 宿主声明 sampling capability
                    "clientInfo": {"name": "sampling-e2e", "version": "0"},
                },
            }
        )
        assert read_resp()["result"]["serverInfo"]["name"] == "agent-mailbox"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "mailbox_register", "arguments": {"agent_id": "WB"}},
            }
        )
        read_resp()
        send(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "mailbox_send",
                    "arguments": {
                        "from_id": "HS",
                        "to": "WB",
                        "subject": "wire proof",
                        "body": "on the wire",
                    },
                },
            }
        )
        msg_id = None
        wake_req = None
        deadline = time.time() + 15
        while time.time() < deadline and (msg_id is None or wake_req is None):
            line = q.get(timeout=15)
            if not line:
                break
            m = json.loads(line)
            if m.get("id") == 4 and "result" in m:
                payload = json.loads(m["result"]["content"][0]["text"])
                assert payload["count"] == 1
                msg_id = payload["delivered"][0]["id"]
            elif m.get("method") == "sampling/createMessage":
                wake_req = m
        assert msg_id is not None, "send response missing"
        assert wake_req is not None, "no sampling/createMessage arrived on the wire"
        params = wake_req["params"]
        text = params["messages"][0]["content"]["text"]
        assert msg_id in text and "wire proof" in text
        assert DEFAULT_GIT_LINE in text  # policy 注入在 wire 上可见
        assert "WB" in (params.get("systemPrompt") or params.get("system_prompt") or "")
        # 回应 createMessage（sampling/createMessage 是 request：带 id 等 response）
        send(
            {
                "jsonrpc": "2.0",
                "id": wake_req["id"],
                "result": {
                    "role": "assistant",
                    "model": "test-host-model",
                    "content": {"type": "text", "text": "checking mailbox now"},
                },
            }
        )
        # 全链路审计闭合：request/result 落 sampling.log + handled_log 留痕
        store = MailStore(proc_mailroot)
        deadline = time.time() + 8
        done = False
        while time.time() < deadline and not done:
            log = read_sampling_log(proc_mailroot)
            done = any(e.get("event") == "result" and e.get("outcome") == "ok" for e in log)
            if not done:
                time.sleep(0.1)
        assert done, f"audit trail incomplete: {read_sampling_log(proc_mailroot)}"
        letter = store.get_letter("WB", msg_id)
        assert any(
            e.get("action") == "sampling" and e.get("outcome") == "ok"
            for e in letter.get("handled_log") or []
        )
        print("sampling wire e2e PASS")
    finally:
        proc.kill()
        proc.wait()


# ================================= per-agent 执行锁（HS 09-25 补钉①-⑥）


def test_concurrent_three_letters_single_flight_fifo_no_double_host(mailroot, monkeypatch):
    """负例铁证（收束目标②）：并发投 3 封同 agent 信 → 仅 1 个 sampling 在途
    → 剩余 2 封排队 → FIFO 依次发出；recording 逐笔验相邻请求时间窗不相交
    （任意时刻 in-flight ≤ 1，全程不出现双分身）。"""
    monkeypatch.setenv("AGENT_MAIL_SAMPLING_TIMEOUT", "30")
    state = {"requests": []}
    releases = [asyncio.Event(), asyncio.Event(), asyncio.Event()]
    cb = make_seq_cb(state, releases)
    landed: dict = {}

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        s1 = await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "one", "body": "1"}
        )
        landed["s1"] = s1["delivered"][0]["id"]
        await wait_until(lambda: len(state["requests"]) == 1, what="first letter in flight")
        s2 = await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "two", "body": "2"}
        )
        landed["s2"] = s2["delivered"][0]["id"]
        s3 = await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "three", "body": "3"}
        )
        landed["s3"] = s3["delivered"][0]["id"]
        await asyncio.sleep(0.8)
        # 锁在途：第二/三封只准排队，绝不并发出第二个 sampling
        assert len(state["requests"]) == 1, (
            f"双分身实锤：执行锁未拦住并发，host 已收到 {len(state['requests'])} 个在途请求"
        )
        releases[0].set()
        await wait_until(lambda: len(state["requests"]) == 2, what="second fires after first done")
        releases[1].set()
        await wait_until(lambda: len(state["requests"]) == 3, what="third fires after second done")
        releases[2].set()
        await wait_until(
            lambda: all(r["end"] is not None for r in state["requests"]), what="all settled"
        )

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))
    expect = [landed["s1"], landed["s2"], landed["s3"]]
    assert request_msg_ids(state) == expect, f"FIFO 违序: {request_msg_ids(state)} != {expect}"
    for prev, nxt in zip(state["requests"], state["requests"][1:]):
        assert prev["end"] is not None and prev["end"] <= nxt["start"], (
            "双分身实锤：相邻 sampling 请求时间窗相交（前一封未完成下一封已在途）"
        )
    store = MailStore(mailroot)
    for mid in expect:
        log = store.get_letter("WB", mid).get("handled_log") or []
        assert sum(1 for e in log if e.get("action") == "sampling") == 1  # 每信恰一次留痕


def test_host_hang_timeout_releases_lock_next_letter_fires(mailroot, monkeypatch):
    """补钉④ 超时路径：宿主卡死走 timeout 返回时锁即释放，后续排队信正常
    发出（§四负例：单卡死分身不得永久堵死该 agent 队列）。"""
    monkeypatch.setenv("AGENT_MAIL_SAMPLING_TIMEOUT", "1.0")
    state = {"requests": []}
    releases = [asyncio.Event(), asyncio.Event()]  # releases[0] 永不释放=模拟卡死
    cb = make_seq_cb(state, releases)
    landed: dict = {}

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        s1 = await call_tool(
            client,
            "mailbox_send",
            {"from_id": "HS", "to": "WB", "subject": "hang-forever", "body": "h"},
        )
        landed["s1"] = s1["delivered"][0]["id"]
        await wait_until(lambda: len(state["requests"]) == 1, what="hung letter in flight")
        s2 = await call_tool(
            client,
            "mailbox_send",
            {"from_id": "HS", "to": "WB", "subject": "queued-behind", "body": "q"},
        )
        landed["s2"] = s2["delivered"][0]["id"]
        await wait_until(
            lambda: len(state["requests"]) == 2,
            what="queued letter fired after timeout released the lock",
        )
        releases[0].set()  # 场景收尾：放开卡死回调
        releases[1].set()
        await wait_until(
            lambda: any(
                e.get("event") == "result" and e.get("outcome") == "timeout"
                for e in read_sampling_log(mailroot)
            ),
            what="timeout outcome audit",
        )
        await wait_until(
            lambda: any(
                e.get("event") == "result" and e.get("outcome") == "ok"
                for e in read_sampling_log(mailroot)
            ),
            what="queued letter ok audit",
        )

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))
    assert request_msg_ids(state) == [landed["s1"], landed["s2"]]


def test_host_error_releases_lock_next_letter_fires(mailroot):
    """补钉④ 失败路径：宿主回调抛错（error outcome）同样释放锁，下一封照常发出。"""
    state = {"requests": []}
    releases = [asyncio.Event(), asyncio.Event()]
    cb = make_seq_cb(state, releases, boom_first=True)
    landed: dict = {}

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        s1 = await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "boom", "body": "b"}
        )
        landed["s1"] = s1["delivered"][0]["id"]
        await wait_until(
            lambda: any(
                e.get("event") == "result" and e.get("outcome") == "error"
                for e in read_sampling_log(mailroot)
            ),
            what="error outcome audit",
        )
        s2 = await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "next", "body": "n"}
        )
        landed["s2"] = s2["delivered"][0]["id"]
        await wait_until(lambda: len(state["requests"]) == 2, what="next letter fires after error")
        releases[1].set()

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))
    assert request_msg_ids(state) == [landed["s1"], landed["s2"]]
    r0, r1 = state["requests"]
    assert r0["end"] <= r1["start"]  # 失败释放后才有第二路，无并发


def test_queue_order_follows_created_at_landing_time(mailroot):
    """补钉②：排队顺序=落箱时间序（created_at），非通知到达序——
    晚到但早落的信插队到先落者之后、先到晚落者之前。"""
    state = {"requests": []}
    releases = [asyncio.Event(), asyncio.Event(), asyncio.Event()]
    cb = make_seq_cb(state, releases)
    seed = MailStore(mailroot)  # 无钩子实例：手工种信 + 手工控制通知序
    m_c = seed.send("HS", "WB", "lands-last", "c", dedupe=False)[0]["id"]
    m_a = seed.send("HS", "WB", "lands-first", "a", dedupe=False)[0]["id"]
    m_b = seed.send("HS", "WB", "lands-second", "b", dedupe=False)[0]["id"]

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        notifier = srv._sampling_notifier
        store = srv._store_instance()
        assert notifier.notify(store, "WB", m_c, created_at="2026-09-25T12:00:03Z")
        await wait_until(lambda: len(state["requests"]) == 1, what="first letter in flight")
        # 到达序 b 先 a 后；落箱序 a（12:00:01）先 b（12:00:02）→ 队列须重排
        assert notifier.notify(store, "WB", m_b, created_at="2026-09-25T12:00:02Z")
        assert notifier.notify(store, "WB", m_a, created_at="2026-09-25T12:00:01Z")
        await asyncio.sleep(0.8)
        assert len(state["requests"]) == 1, "两封排队信都不得抢先在途"
        releases[0].set()
        await wait_until(lambda: len(state["requests"]) == 2, what="earlier-landing letter fires")
        releases[1].set()
        await wait_until(lambda: len(state["requests"]) == 3, what="later-landing letter fires")
        releases[2].set()
        await wait_until(lambda: all(r["end"] is not None for r in state["requests"]))

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))
    assert request_msg_ids(state) == [m_c, m_a, m_b], (
        "FIFO 须按 created_at 落箱序：早落的 a 先于晚落的 b（尽管 b 的通知先到）"
    )


def test_queue_same_created_at_keeps_arrival_order(mailroot):
    """补钉② tiebreak：同秒落箱（created_at 相同）的信按通知到达序排。"""
    state = {"requests": []}
    releases = [asyncio.Event(), asyncio.Event(), asyncio.Event()]
    cb = make_seq_cb(state, releases)
    seed = MailStore(mailroot)
    m1 = seed.send("HS", "WB", "tie-one", "1", dedupe=False)[0]["id"]
    m2 = seed.send("HS", "WB", "tie-two", "2", dedupe=False)[0]["id"]

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        notifier = srv._sampling_notifier
        store = srv._store_instance()
        assert notifier.notify(store, "WB", m1, created_at="2026-09-25T12:00:00Z")
        await wait_until(lambda: len(state["requests"]) == 1, what="first in flight")
        assert notifier.notify(store, "WB", m2, created_at="2026-09-25T12:00:00Z")
        await asyncio.sleep(0.5)
        assert len(state["requests"]) == 1
        releases[0].set()
        await wait_until(lambda: len(state["requests"]) == 2, what="tie letter fires")
        releases[1].set()
        await wait_until(lambda: all(r["end"] is not None for r in state["requests"]))

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))
    assert request_msg_ids(state) == [m1, m2]  # 同 created_at → 到达序保序


def test_max_concurrent_two_allows_parallel_wake(mailroot):
    """补钉③ 配置面：wake.json max_concurrent=2 → 同 agent 可 2 路在途，
    第 3 封仍排队且不超出配额。"""
    (mailroot / "wake.json").write_text(
        json.dumps({"agents": {"WB": {"max_concurrent": 2}}}), encoding="utf-8"
    )
    state = {"requests": []}
    releases = [asyncio.Event(), asyncio.Event(), asyncio.Event()]
    cb = make_seq_cb(state, releases)
    landed: dict = {}

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        s1 = await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "p1", "body": "1"}
        )
        landed["s1"] = s1["delivered"][0]["id"]
        await wait_until(lambda: len(state["requests"]) == 1, what="first in flight")
        s2 = await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "p2", "body": "2"}
        )
        landed["s2"] = s2["delivered"][0]["id"]
        await wait_until(lambda: len(state["requests"]) == 2, what="second route in parallel")
        s3 = await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "p3", "body": "3"}
        )
        landed["s3"] = s3["delivered"][0]["id"]
        await asyncio.sleep(0.8)
        assert len(state["requests"]) == 2, "第 3 封必须排队：不得超过 max_concurrent=2"
        releases[0].set()
        releases[1].set()
        await wait_until(lambda: len(state["requests"]) == 3, what="third fires within quota")
        releases[2].set()
        await wait_until(lambda: all(r["end"] is not None for r in state["requests"]))

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))
    assert request_msg_ids(state) == [landed["s1"], landed["s2"], landed["s3"]]
    r0, r1, r2 = state["requests"]
    assert r1["start"] < r0["end"], "并行实锤：第二路在第一路释放前已在途（配额 2 生效）"
    assert r2["start"] >= r0["end"] and r2["start"] >= r1["end"], "第 3 路不得超配额抢跑"


def test_max_concurrent_garbage_clamps_to_locked_default(mailroot, caplog):
    """补钉③ 防御面：max_concurrent 非法（0）→ 回落最保守的 1（锁语义），
    落 warning，队列仍按单飞执行不放大并发。"""
    (mailroot / "wake.json").write_text(
        json.dumps({"agents": {"WB": {"max_concurrent": 0}}}), encoding="utf-8"
    )
    state = {"requests": []}
    releases = [asyncio.Event(), asyncio.Event()]
    cb = make_seq_cb(state, releases)

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "g1", "body": "1"}
        )
        await wait_until(lambda: len(state["requests"]) == 1, what="first in flight")
        await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "g2", "body": "2"}
        )
        await asyncio.sleep(0.8)
        assert len(state["requests"]) == 1, "垃圾 max_concurrent 必须锁死在 1，不放并发"
        releases[0].set()
        await wait_until(lambda: len(state["requests"]) == 2, what="second fires after release")
        releases[1].set()
        await wait_until(lambda: all(r["end"] is not None for r in state["requests"]))

    with caplog.at_level(logging.WARNING, logger="agent_mailbox.sampling"):
        asyncio.run(run_pair(mailroot, body, sampling_cb=cb))
    assert any("locked to 1" in r.message for r in caplog.records), "配置坏了必须可见（warning）"


def test_degrade_pending_audits_and_clears_queue(mailroot, monkeypatch):
    """补钉⑤ 收场腿：内存排队未发出的 sampling 请求降级走 fallback——
    逐条 degrade 审计、清空内存队列（不驻留）、信仍 pending、不补发采样。"""
    monkeypatch.setenv("AGENT_MAIL_SAMPLING_TIMEOUT", "30")
    state = {"requests": []}
    releases = [asyncio.Event()]
    cb = make_seq_cb(state, releases)

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "d1", "body": "1"}
        )
        await wait_until(lambda: len(state["requests"]) == 1, what="first in flight")
        s2 = await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "d2", "body": "2"}
        )
        s2_id = s2["delivered"][0]["id"]
        await asyncio.sleep(0.5)  # 让 s2 的 enqueue 落进内存队列
        # 模拟 server 收场（main 的 finally 调同一路径）
        degraded = srv._sampling_notifier.degrade_pending("server-restart")
        assert [d["msg_id"] for d in degraded] == [s2_id]
        gate = srv._sampling_notifier._gates.get("WB")
        assert gate is not None and gate.queue == []  # 内存队列清空，不驻留
        assert any(
            e.get("event") == "degrade"
            and e.get("reason") == "server-restart"
            and e.get("msg_id") == s2_id
            for e in read_sampling_log(mailroot)
        )
        assert MailStore(mailroot).get_letter("WB", s2_id)["status"] == "pending"  # fallback 兜底
        releases[0].set()
        await wait_until(lambda: all(r["end"] is not None for r in state["requests"]))
        await asyncio.sleep(0.6)
        assert len(state["requests"]) == 1, "已降级的信不得再补发 sampling"
        log = MailStore(mailroot).get_letter("WB", s2_id).get("handled_log") or []
        assert not [e for e in log if e.get("action") == "sampling"]  # 零采样留痕=纯 fallback

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))


def test_reconnect_after_teardown_no_memory_queue_letters_reachable(mailroot):
    """补钉⑤ 连接腿（真 teardown 语义）：会话收场连接死亡 → 在途/排队请求
    经失败路径收场，内存队列不驻留；信留在盘上 pending，新连接首个 notify
    照常唤醒（mailbox_check fallback 永远可达）。"""
    state1 = {"requests": []}
    cb1 = make_seq_cb(state1, [asyncio.Event()])  # releases[0] 不置位=c1 卡死
    queued: dict = {}

    async def body1(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "c1", "body": "1"}
        )
        await wait_until(lambda: len(state1["requests"]) == 1, what="first in flight")
        s2 = await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "c2", "body": "2"}
        )
        queued["id"] = s2["delivered"][0]["id"]
        await asyncio.sleep(0.5)
        gate = srv._sampling_notifier._gates.get("WB")
        assert gate is not None and [i.msg_id for i in gate.queue] == [queued["id"]]
        # 会话在此收场：连接死亡，在途+排队的 sampling 请求随连接收场

    asyncio.run(run_pair(mailroot, body1, sampling_cb=cb1))

    gate = srv._sampling_notifier._gates.get("WB")
    assert gate is not None and gate.queue == [], "连接死亡后内存队列不得驻留排队信"
    assert MailStore(mailroot).get_letter("WB", queued["id"])["status"] == "pending"  # 不丢信

    state2 = {"requests": []}
    cb2 = make_seq_cb(state2, [])

    async def body2(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "c3", "body": "3"}
        )
        await wait_until(lambda: len(state2["requests"]) == 1, what="new connection wake fires")

    asyncio.run(run_pair(mailroot, body2, sampling_cb=cb2))
    assert MailStore(mailroot).get_letter("WB", queued["id"])["status"] == "pending"


def test_stale_gate_degrades_on_reconnect_notify(mailroot):
    """补钉⑤ 降级路径：旧闸门 loop 已死且队列有滞留信（进程硬杀的等价形态）
    → 新连接首个 notify 触发重建，滞留信逐条落 degrade 审计、不发出。"""
    seed = MailStore(mailroot)
    stale_mid = seed.send("HS", "WB", "hard-killed-queue", "s", dedupe=False)[0]["id"]
    dead_loop = asyncio.new_event_loop()
    dead_loop.close()
    stale_gate = _AgentGate("WB", dead_loop, owner=srv._sampling_notifier)
    stale_gate.queue.append(
        _QueuedLetter(
            msg_id=stale_mid,
            created_at="2026-09-25T12:00:00Z",
            seq=10**9,
            store=seed,
            entry=ConnectionEntry(connection=object(), declared_sampling=True, loop=dead_loop),
        )
    )
    srv._sampling_notifier._gates["WB"] = stale_gate

    state = {"requests": []}
    cb = make_seq_cb(state, [])

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        sent = await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "fresh", "body": "f"}
        )
        fresh_id = sent["delivered"][0]["id"]
        await wait_until(lambda: len(state["requests"]) == 1, what="fresh wake on new gate")
        assert request_msg_ids(state) == [fresh_id]  # 只有新信被唤醒
        assert any(
            e.get("event") == "degrade"
            and e.get("reason") == "connection-loop-closed"
            and e.get("msg_id") == stale_mid
            for e in read_sampling_log(mailroot)
        ), "滞留信必须在闸门重建时落降级审计"
        assert srv._sampling_notifier._gates["WB"].queue == []  # 降级后内存队列不驻留

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))
    assert MailStore(mailroot).get_letter("WB", stale_mid)["status"] == "pending"  # fallback 兜底


@pytest.mark.parametrize("plat", ["posix", "win32"], ids=["linux-ci", "windows-ci"])
def test_lock_timeout_release_platform_matrix(mailroot, monkeypatch, plat):
    """补钉⑥ Windows CI 参数化占位：锁超时释放路径按平台矩阵跑。
    posix 行 = 本机/Linux CI 照旧实跑；win32 行为 Windows CI 占位（skip）。"""
    current = "win32" if sys.platform == "win32" else "posix"
    if plat != current:
        pytest.skip(f"{plat} CI 占位：等待对应平台接入（当前 {sys.platform}）")
    monkeypatch.setenv("AGENT_MAIL_SAMPLING_TIMEOUT", "0.6")
    state = {"requests": []}
    releases = [asyncio.Event(), asyncio.Event()]
    cb = make_seq_cb(state, releases)
    landed: dict = {}

    async def body(client):
        await call_tool(client, "mailbox_register", {"agent_id": "WB"})
        s1 = await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "m1", "body": "1"}
        )
        landed["s1"] = s1["delivered"][0]["id"]
        await wait_until(lambda: len(state["requests"]) == 1, what="first in flight")
        s2 = await call_tool(
            client, "mailbox_send", {"from_id": "HS", "to": "WB", "subject": "m2", "body": "2"}
        )
        landed["s2"] = s2["delivered"][0]["id"]
        await wait_until(
            lambda: len(state["requests"]) == 2,
            what="lock released by timeout on this platform",
        )
        releases[0].set()
        releases[1].set()
        await wait_until(lambda: all(r["end"] is not None for r in state["requests"]))

    asyncio.run(run_pair(mailroot, body, sampling_cb=cb))
    assert request_msg_ids(state) == [landed["s1"], landed["s2"]]
