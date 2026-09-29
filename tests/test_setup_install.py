"""t-61（判据1+6）: 一条命令装完 — ``agent-mailbox setup`` 全包揽。

判据1: setup 自动发现机器上的 agent（零输入、不让用户做分类题）——CLI 形态
成员全自动装好（注册 + agents.<ID> 段 + plist + 唤醒命令）。
判据6: launchd / PATH / 绝对路径 / provider 凭据归属全收进库——command 首段
自动解析绝对路径，zcode 类命令的唤醒命令模板内置 provider env 注入（t-60
同款契约），用户不必知道 provider 配置存在；--belt 从包内模板渲染 per-身份
belt 脚本（部署副本=生成产物）。
向后兼容 fail-open: 老三样（手写 wake.json / 手装 plist / 已部署 belt）不被
破坏；损坏 config 备份后重建不 crash。

全部走 tmp HOME / tmp root，绝不碰真机 launchd（激活面用 --no-activate 或
monkeypatch 的 _launchctl）。
"""

from __future__ import annotations

import json
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from agent_mailbox.cli import cli_main
from agent_mailbox.installer import (
    belt_path,
    is_zcode_command,
    resolve_command_head,
    wake_wrapper_body,
    wrapper_path,
)
from agent_mailbox.store import MailStore
from agent_mailbox.wake import WAKE_LABEL


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """Pin Path.home() + PATH to fixture dirs — no test touches the real HOME."""
    home = tmp_path / "home"
    home.mkdir()
    bindir = home / "bin"
    bindir.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("PATH", str(bindir))
    return home


def _setup(home: Path, root: Path, *extra: str) -> int:
    return cli_main(["setup", "--home", str(root), "--no-activate", *extra])


def _fake_cli(home: Path, name: str, body: str = "exit 0") -> Path:
    """A fake agent CLI on the fake PATH (absolute path after resolution)."""
    p = home / "bin" / name
    p.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    p.chmod(0o755)
    return p


# ------------------------------------------------------- 全流程（判据1/6 主链）


def test_setup_explicit_agent_full_flow(fake_home, capsys):
    """一条命令装完: 注册 + agents 段 + plist(WatchPaths 指该身份 inbox) +
    唤醒命令绝对路径——用户零手写 wake.json / 零手写 plist。"""
    root = fake_home / ".agent-mail"
    fake_cli = _fake_cli(fake_home, "fakeagent")
    rc = _setup(
        fake_home,
        root,
        "--agent",
        "worker",
        "--adapter",
        "local-command",
        "--command",
        '["fakeagent","--pull"]',
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "初始化完成" in out and "worker" in out
    # 注册进名册（WatchPaths 与告警的落点）
    assert "worker" in MailStore(root).registry()["agents"]
    # agents.<ID> 段已写，command 首段 = 绝对路径（判据6: PATH 细节收进库）
    saved = json.loads((root / "wake.json").read_text(encoding="utf-8"))
    section = saved["agents"]["worker"]
    assert section["adapter"] == "local-command"
    assert section["command"] == [str(fake_cli), "--pull"]
    # plist 生成且 WatchPaths 指该身份 inbox
    plist = fake_home / "Library" / "LaunchAgents" / f"{WAKE_LABEL}-worker.plist"
    assert plist.exists()
    data = plistlib.loads(plist.read_bytes())
    assert [str(x) for x in data["WatchPaths"]] == [str(root / "inbox" / "worker")]
    args = [str(x) for x in data["ProgramArguments"]]
    assert args[:4] == [sys.executable, "-m", "agent_mailbox.wake", "run"] and "worker" in args


def test_setup_zero_input_auto_installs_discovered_cli(fake_home, capsys):
    """判据1 零输入: setup 不带任何参数 → 自动发现 CLI 形态成员并全自动接线；
    app/未识别形态不猜通道、给指名道姓跳过原因。"""
    root = fake_home / ".agent-mail"
    fake_cli = _fake_cli(fake_home, "codex")
    (fake_home / ".codex").mkdir()  # L1 config-dir 信号（可选，CLI 已足够）
    rc = _setup(fake_home, root, "--yes")
    assert rc == 0
    out = capsys.readouterr().out
    saved = json.loads((root / "wake.json").read_text(encoding="utf-8"))
    assert saved["agents"]["codex"]["adapter"] == "local-command"
    assert saved["agents"]["codex"]["command"] == [str(fake_cli)]
    assert "codex" in MailStore(root).registry()["agents"]
    assert "自动接线" in out or "初始化完成" in out
    # 已有配置的身份不重复动（存量为空的根此处只验 codex 装上了）


def test_setup_zcode_command_wrapper_has_provider_env_injection(fake_home, capsys):
    """判据6: zcode 类命令 → 生成唤醒命令模板，provider env 注入内置
    （t-60 同款契约），用户不必知道 provider 配置存在；模板真实可跑且把
    ZCODE_PERSONAL_PROVIDER_CONFIG_FILE 注入子进程。"""
    root = fake_home / ".agent-mail"
    probe_out = root / "probe.out"
    fake_zcode = _fake_cli(fake_home, "zcode", f'env > "{probe_out}"\n')
    assert is_zcode_command(["zcode", "-p", "go"]) is True
    assert is_zcode_command(["codex"]) is False
    rc = _setup(
        fake_home,
        root,
        "--agent",
        "zc",
        "--adapter",
        "local-command",
        "--command",
        '["zcode","-p","信箱巡检"]',
    )
    assert rc == 0
    wrapper = wrapper_path(root, "zc")
    assert wrapper.exists()
    text = wrapper.read_text(encoding="utf-8")
    # provider env 注入三要素: resolver 契约 + 个人配置 + caller 值优先
    assert "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE" in text
    assert "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE" in text
    assert "sort -V" in text  # 确定性版本序（t-60 硬要求）
    assert f"set -- {_sh_quote_expect(str(fake_zcode))}" in text  # 绝对路径 exec 面在位
    # agents 段指向模板（argv-list，仍无 shell 拼接唤醒路径）
    saved = json.loads((root / "wake.json").read_text(encoding="utf-8"))
    assert saved["agents"]["zc"]["command"] == ["/bin/bash", str(wrapper)]
    # 模板真实可跑: 假 HOME 放个人 provider 配置 → 子进程读到注入的 env
    (fake_home / ".zcode" / "v2").mkdir(parents=True)
    (fake_home / ".zcode" / "v2" / "provider_config.json").write_text("{}", encoding="utf-8")
    run = subprocess.run(
        ["/bin/bash", str(wrapper)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(fake_home)},
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert (
        f"ZCODE_PERSONAL_PROVIDER_CONFIG_FILE={fake_home / '.zcode' / 'v2' / 'provider_config.json'}"
        in (probe_out.read_text(encoding="utf-8"))
    )


def _sh_quote_expect(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# ------------------------------------------------------- 存量不破坏（fail-open）


def test_setup_preserves_existing_config_and_foreign_plist(fake_home):
    """老三样不破坏: 存量 agents.HS 段 / sampling 未知子键 / 未知顶层键 /
    别人手装的 plist 全部原样；setup 只动本身份的段。"""
    root = fake_home / ".agent-mail"
    root.mkdir(parents=True)
    (root / "wake.json").write_text(
        json.dumps(
            {
                "agent_id": "HS",
                "adapter": "hermes",
                "webhook": {"url": "http://127.0.0.1:9/gw", "secret": "s"},
                "custom_future_key": {"keep": True},
                "agents": {
                    "HS": {
                        "adapter": "generic-webhook",
                        "webhook": {"url": "http://127.0.0.1:9/hs"},
                    },
                    "wb": {"sampling_policy": {"max_tokens": 42}},
                },
            }
        ),
        encoding="utf-8",
    )
    la = fake_home / "Library" / "LaunchAgents"
    la.mkdir(parents=True)
    foreign = la / f"{WAKE_LABEL}-HS.plist"
    foreign_bytes = plistlib.dumps({"Label": f"{WAKE_LABEL}-HS"})
    foreign.write_bytes(foreign_bytes)
    rc = _setup(
        fake_home,
        root,
        "--agent",
        "wb",
        "--adapter",
        "local-command",
        "--command",
        '["codex"]',  # 不在 PATH → 原样保留（doctor ④ 会判），不 crash
    )
    assert rc == 0
    saved = json.loads((root / "wake.json").read_text(encoding="utf-8"))
    # HS 段与全局键分毫未动
    assert saved["agents"]["HS"]["webhook"]["url"] == "http://127.0.0.1:9/hs"
    assert saved["adapter"] == "hermes" and saved["webhook"]["secret"] == "s"
    assert saved["custom_future_key"] == {"keep": True}
    # wb 段: sampling 未知子键保全 + 只补通道三键
    assert saved["agents"]["wb"]["sampling_policy"] == {"max_tokens": 42}
    assert saved["agents"]["wb"]["adapter"] == "local-command"
    # 别人手装的 plist 原样
    assert foreign.read_bytes() == foreign_bytes


def test_setup_corrupt_config_backed_up_then_rebuilt(fake_home, capsys):
    """损坏 wake.json 不炸: 备份后重建，setup 照常装完（fail-open）。"""
    root = fake_home / ".agent-mail"
    root.mkdir(parents=True)
    (root / "wake.json").write_text("{corrupt garbage!!", encoding="utf-8")
    rc = _setup(
        fake_home,
        root,
        "--agent",
        "worker",
        "--adapter",
        "local-command",
        "--command",
        '["fakeagent"]',
    )
    assert rc == 0
    out = capsys.readouterr().out
    backups = list(root.glob("wake.json.corrupt-*.bak"))
    assert len(backups) == 1 and "corrupt garbage" in backups[0].read_text(encoding="utf-8")
    saved = json.loads((root / "wake.json").read_text(encoding="utf-8"))
    assert saved["agents"]["worker"]["adapter"] == "local-command"
    assert "备份" in out


def test_setup_idempotent_rerun(fake_home):
    """重复 install 幂等: 两次 setup 后配置等价、plist 原地重写、无重复段。"""
    root = fake_home / ".agent-mail"
    args = (
        "--agent",
        "worker",
        "--adapter",
        "local-command",
        "--command",
        '["fakeagent","--pull"]',
        "--belt",
    )
    _setup(fake_home, root, *args)
    first = (root / "wake.json").read_text(encoding="utf-8")
    first_cfg = json.loads(first)
    first_plist = (fake_home / "Library" / "LaunchAgents" / f"{WAKE_LABEL}-worker.plist").read_text(
        encoding="utf-8"
    )
    first_belt = belt_path(root, "worker").read_text(encoding="utf-8")
    rc2 = _setup(fake_home, root, *args)
    assert rc2 == 0
    second_cfg = json.loads((root / "wake.json").read_text(encoding="utf-8"))
    assert second_cfg["agents"] == first_cfg["agents"]
    assert second_cfg["agents"]["worker"]["command"] == first_cfg["agents"]["worker"]["command"]
    assert (fake_home / "Library" / "LaunchAgents" / f"{WAKE_LABEL}-worker.plist").read_text(
        encoding="utf-8"
    ) == first_plist
    assert belt_path(root, "worker").read_text(encoding="utf-8") == first_belt
    assert len(json.loads((root / "wake.json").read_text(encoding="utf-8"))["agents"]) == 1


# ------------------------------------------------------- belt 入库生成


def test_setup_belt_script_generated_with_absolute_paths(fake_home):
    """--belt: per-身份 belt 脚本从包内模板渲染，绝对路径自动填，bash -n 通过；
    部署副本 = 生成产物（脚本自声明镜像）。"""
    root = fake_home / ".agent-mail"
    rc = _setup(
        fake_home,
        root,
        "--agent",
        "worker",
        "--adapter",
        "local-command",
        "--command",
        '["fakeagent","--pull"]',
        "--belt",
    )
    assert rc == 0
    belt = belt_path(root, "worker")
    assert belt.exists()
    assert belt.stat().st_mode & 0o111  # executable
    text = belt.read_text(encoding="utf-8")
    assert str(root) in text and "worker" in text and sys.executable in text
    assert "wake-attempts.jsonl" in text  # 锚A
    assert "release --agent" in text  # 信不丢
    assert "exit 1" in text  # 失败必响
    assert "__" not in text.replace("__file__", "")  # 无未渲染占位符
    assert "部署副本 = 本文件" in text
    bash_n = subprocess.run(
        ["/bin/bash", "-n", str(belt)], capture_output=True, text=True, check=False
    )
    assert bash_n.returncode == 0, bash_n.stderr


def test_setup_belt_drain_uses_wrapper_for_zcode(fake_home):
    """zcode 类命令的 belt 默认 drain 走唤醒命令模板（provider env 注入不丢）。"""
    root = fake_home / ".agent-mail"
    _fake_cli(fake_home, "zcode")
    _setup(
        fake_home,
        root,
        "--agent",
        "zc",
        "--adapter",
        "local-command",
        "--command",
        '["zcode"]',
        "--belt",
    )
    text = belt_path(root, "zc").read_text(encoding="utf-8")
    assert "DRAIN_DEFAULT" in text  # drain 走可覆盖的默认命令变量
    assert str(wrapper_path(root, "zc")) in text  # 默认 drain = 唤醒命令模板（注入不丢）


# ------------------------------------------------------- 其他小面


def test_resolve_command_head_absolute_and_missing(fake_home, monkeypatch):
    """绝对路径原样保留（存在与否都进配置，doctor ④ 负责）；裸名不在 PATH
    原样保留 + note。"""
    monkeypatch.setenv("PATH", str(fake_home / "bin"))
    fake = _fake_cli(fake_home, "ok")
    argv, note = resolve_command_head(["ok", "--x"])
    assert argv == [str(fake), "--x"] and "ok →" in note
    argv2, note2 = resolve_command_head(["/nonexistent/xyz/bin", "--y"])
    assert argv2 == ["/nonexistent/xyz/bin", "--y"] and note2 == ""
    argv3, note3 = resolve_command_head(["missing-cmd"])
    assert argv3 == ["missing-cmd"] and "不在 PATH" in note3


def test_wake_wrapper_body_escapes_quotes():
    """命令参数含单引号/中文时模板仍 eval 安全（set -- 单引号转义）。"""
    body = wake_wrapper_body("a", "/tmp/r", ["x", "it's", "信件"], "")
    assert "'it'\\''s'" in body and "'信件'" in body
    assert 'exec "$@"' in body


def test_setup_activation_wiring(monkeypatch, fake_home):
    """激活接线: 默认会调 launchctl load（测试里 monkeypatch，真机零触碰）。"""
    called: list[tuple] = []

    def fake_launchctl(*a, **k):
        called.append(a)
        return True

    monkeypatch.setattr("agent_mailbox.wake._launchctl", fake_launchctl)
    root = fake_home / ".agent-mail"
    rc = cli_main(
        [
            "setup",
            "--home",
            str(root),
            "--agent",
            "worker",
            "--adapter",
            "local-command",
            "--command",
            '["fakeagent"]',
        ]
    )
    assert rc == 0
    assert called and called[0][0] == "load"


def test_setup_explicit_agent_without_channel_fails_loud(fake_home):
    """显式 --agent 但机器上没有该成员 CLI、也没给通道 → 明确修法退出（不猜）。"""
    root = fake_home / ".agent-mail"
    with pytest.raises(SystemExit) as ei:
        _setup(fake_home, root, "--agent", "ghost-member")
    assert "local-command" in str(ei.value) or "webhook-url" in str(ei.value)


def test_setup_local_command_requires_command_flag(fake_home):
    """--adapter local-command 但没给 --command → 明确报错。"""
    root = fake_home / ".agent-mail"
    with pytest.raises(SystemExit) as ei:
        _setup(fake_home, root, "--agent", "worker", "--adapter", "local-command")
    assert "--command" in str(ei.value)


def test_setup_webhook_agent_writes_section(fake_home):
    """webhook 形态: url/secret 只进 agents.<ID> 段（t-58 硬约束不回退）。"""
    root = fake_home / ".agent-mail"
    rc = _setup(
        fake_home,
        root,
        "--agent",
        "hser",
        "--adapter",
        "generic-webhook",
        "--webhook-url",
        "http://127.0.0.1:9/hs-gw",
        "--webhook-secret",
        "sec",
    )
    assert rc == 0
    saved = json.loads((root / "wake.json").read_text(encoding="utf-8"))
    assert saved["agents"]["hser"]["adapter"] == "generic-webhook"
    assert saved["agents"]["hser"]["webhook"] == {
        "url": "http://127.0.0.1:9/hs-gw",
        "secret": "sec",
    }
    assert saved["adapter"] == "hermes"  # 全局分毫未动
