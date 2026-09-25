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
import os
import queue
import subprocess
import sys
import threading
import time

import pytest
from mcp import ClientSession, types
from mcp.shared.memory import create_client_server_memory_streams

from agent_mailbox import server as srv
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
