"""t-58（A-1 per-agent 唤醒路由）: agents.<ID> 三键 + 按身份解析 + 旧配置兼容。

多身份同机是本产品的唯一价值场景（任务书 2026-09-29 §0.3），此前唤醒通道
全局一份——装第二个身份必然覆盖第一个，WB 的信把 HS 叫醒。这里钉死：

- install 的 --adapter/--command/--webhook-url 只写 ``agents.<ID>`` 身份段，
  绝不落全局；多身份互不覆盖。
- run 按身份解析生效通道，优先级 = CLI 覆盖 > agents.<ID> > 全局 > 默认。
- 旧配置（无 agents 段/缺键）fail-open 照旧可用，全局键保留为默认。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent_mailbox.store import MailStore
from agent_mailbox.wake import (
    GenericWebhookAdapter,
    LocalCommandAdapter,
    WakeConfig,
    make_adapter,
    run_once,
    wake_main,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="install 通道旗标断言钉 darwin launchd 形态；linux systemd / win32 unsupported 归平台批（0.7.6 发布 CI 实测定谳）",
)

SECRET = "s"


def _cfg(root: Path, **kw) -> WakeConfig:
    base = {
        "agent_id": "HS",
        "adapter": "generic-webhook",
        "webhook": {"url": "http://127.0.0.1:9/hermes-gw", "secret": SECRET},
        "retry_interval": 0,
    }
    base.update(kw)
    return WakeConfig(base, Path(root))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "mail"
    monkeypatch.setenv("AGENT_MAIL_HOME", str(root))
    st = MailStore(root=root)
    st.register("HS")
    st.register("WB")
    st.register("ZC")
    return root, st


# ------------------------------------------------------- 多身份互不覆盖


def test_install_channel_flags_write_agent_section_never_global(env, tmp_path, monkeypatch):
    """install --agent X --adapter/--command/--webhook-url 三键只进 agents.X 段；
    连装两个身份互不覆盖，全局键保持默认（各回各家的机器保证）。"""
    root, _ = env
    la = tmp_path / "launchagents"
    monkeypatch.setattr("agent_mailbox.wake._launchctl", lambda *a, **k: True)
    installs = [
        ("HS", ["--adapter", "generic-webhook", "--webhook-url", "http://127.0.0.1:9/hs-gw"]),
        ("ZC", ["--adapter", "local-command", "--command", '["/usr/local/bin/codex","--mail"]']),
    ]
    for agent, flags in installs:
        wake_main(
            [
                "install",
                "--agent",
                agent,
                *flags,
                "--root",
                str(root),
                "--launch-agents-dir",
                str(la),
                "--no-activate",
            ]
        )
    saved = json.loads((root / "wake.json").read_text(encoding="utf-8"))
    assert saved["agents"]["HS"]["adapter"] == "generic-webhook"
    assert saved["agents"]["HS"]["webhook"]["url"] == "http://127.0.0.1:9/hs-gw"
    assert saved["agents"]["ZC"]["adapter"] == "local-command"
    assert saved["agents"]["ZC"]["command"] == ["/usr/local/bin/codex", "--mail"]
    # 全局通道键分毫未动（ZC 的 install 没有覆盖 HS 的配置，也没有污染全局）
    assert saved["adapter"] == "hermes"  # 内置默认
    assert saved["webhook"]["url"] == ""
    # 两个身份的 plist 各自只带 --agent（与现役 plist 形态兼容），路由靠段解析
    hs_plist = (la / "com.polaris-smart.agent-mailbox-wake-HS.plist").read_text(encoding="utf-8")
    assert (
        "--agent" in hs_plist and "<string>HS</string>" in hs_plist and "--adapter" not in hs_plist
    )
    # sampling policy 类未知子键与路由键共居一段互不踩（写路由不抹 policy）
    cfg = _cfg(root, agents={"WB": {"forbidden": ["git 写操作"]}})
    cfg.set_agent_route("WB", adapter="local-command")
    assert cfg.agent_section("WB")["forbidden"] == ["git 写操作"]
    assert cfg.agent_section("WB")["adapter"] == "local-command"


class _Recorder:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def deliver(self, msg):
        self.calls.append(msg["id"])
        return self.results.pop(0) if self.results else True


# ------------------------------------------------------------- 旧配置 fail-open


def test_legacy_config_without_agents_section_still_drains(env):
    """旧 wake.json（无 agents 段）必须照旧可用：解析落回全局 adapter/webhook，
    drain 端到端不因缺键报错（fail-open 铁律）。"""
    root, st = env
    st.send("HS", "ZC", "legacy", "wake me")
    legacy = WakeConfig(
        {
            "agent_id": "ZC",
            "adapter": "generic-webhook",
            "webhook": {"url": "http://127.0.0.1:9/gw", "secret": SECRET},
            "retry_interval": 0,
        },
        Path(root),
    )
    route = legacy.effective_route("ZC")
    assert route.adapter == "generic-webhook"
    assert route.webhook_url == "http://127.0.0.1:9/gw"
    assert route.sources["adapter"] == "global"  # 全局兜底，来源可追溯
    stats = run_once(root, legacy, adapter=_Recorder([True]))
    assert stats["woke"] == 1
    # 缺单键同样 fail-open：段里只写 adapter，webhook 落回全局
    partial = WakeConfig(
        {
            "agent_id": "ZC",
            "adapter": "hermes",
            "webhook": {"url": "http://127.0.0.1:9/gw"},
            "agents": {"ZC": {"adapter": "generic-webhook"}},
        },
        Path(root),
    )
    assert partial.effective_route("ZC").webhook_url == "http://127.0.0.1:9/gw"
    assert partial.effective_route("ZC").sources["webhook_url"] == "global"
    # 段形状损坏（非 dict 值）也不炸——fail-open 回全局
    corrupt = WakeConfig(
        {"agent_id": "ZC", "adapter": "hermes", "agents": {"ZC": "garbage"}}, Path(root)
    )
    assert corrupt.agent_section("ZC") == {}
    assert corrupt.effective_route("ZC").adapter == "hermes"


# --------------------------------------------------- run 按身份解析到各自通道


def test_run_resolves_per_agent_channels_and_cli_priority(env):
    """三个身份同机：HS=webhook 网关、WB=自己的 webhook、ZC=local-command——
    effective_route 各回各家；CLI 覆盖最高、全局兜底次序钉死。"""
    root, _ = env
    cfg = _cfg(
        root,
        agents={
            "HS": {"adapter": "generic-webhook", "webhook": {"url": "http://127.0.0.1:9/hs"}},
            "WB": {"adapter": "hermes", "webhook": {"url": "http://127.0.0.1:9/wb"}},
            "ZC": {"adapter": "local-command", "command": ["/usr/local/bin/codex"]},
        },
    )
    hs = cfg.effective_route("HS")
    wb = cfg.effective_route("WB")
    zc = cfg.effective_route("ZC")
    assert hs.webhook_url == "http://127.0.0.1:9/hs" and hs.sources["webhook_url"] == "agents:HS"
    assert wb.webhook_url == "http://127.0.0.1:9/wb" and wb.sources["adapter"] == "agents:WB"
    assert zc.adapter == "local-command" and zc.command == ("/usr/local/bin/codex",)
    # make_adapter 按 agent_id 构建的适配器与通道一一对应
    cfg.agent_id = "HS"
    assert isinstance(make_adapter(cfg), GenericWebhookAdapter)
    cfg.agent_id = "ZC"
    assert isinstance(make_adapter(cfg), LocalCommandAdapter)
    # CLI 覆盖压过 agents 段
    cfg.set_cli_override(adapter="local-command", command=["/bin/echo"])
    assert cfg.effective_route("HS").adapter == "local-command"
    assert cfg.effective_route("HS").sources["adapter"] == "cli"
    # 全局兜底：无段身份（未知新成员）落回全局 webhook
    other = cfg.effective_route("NEWGUY")
    assert other.webhook_url == "http://127.0.0.1:9/hermes-gw"
    assert other.sources["webhook_url"] == "global"


def test_cmd_run_resolves_agent_section_end_to_end(env, tmp_path):
    """wake run --agent X 端到端：plist 不带 --adapter（现役 plist 形态），
    run 也能从 agents.X 段解析出 local-command 并真实投出。"""
    root, st = env
    st.register("codex")
    st.send("HS", "codex", "wake codex", "process me")
    marker = tmp_path / "marker.txt"
    # 共享 wake.json：全局 hermes（HS 的默认），codex 走自己的 local-command 段
    (root / "wake.json").write_text(
        json.dumps(
            {
                "agent_id": "HS",
                "adapter": "hermes",
                "webhook": {"url": "http://127.0.0.1:9/gw"},
                "agents": {
                    "codex": {
                        "adapter": "local-command",
                        "command": [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = WakeConfig.load(root)
    assert cfg is not None
    cfg.agent_id = "codex"
    stats = run_once(root, cfg)  # run_once 内部 make_adapter → effective_route("codex")
    assert stats["woke"] == 1 and marker.exists(), "run 必须按身份解析到 local-command 并执行"
