"""t-65（0.7.6 收口批第 2 批）G-4: 纯 Python 进程组看门狗.

病灶（HS 实测）：WB 手写唤醒脚本的 ``( sleep 180; kill $CPID; pkill -P $CPID ) &``
先杀子壳 PID，孙进程被 launchd 收养（PPID=1），``pkill -P`` 找不到目标 ⇒
4 个 codebuddy 孤儿挂 7h+。本文件验证进程组语义的三条硬要求：

1. 超时回收**整个进程组**（孙进程一并回收，ps 无残留）；
2. SIGTERM → 宽限窗 → 仍存活补 **SIGKILL**（无视 SIGTERM 的子进程也逃不掉）；
3. 正常退出完全不受影响（rc 原样上抛、零杀戮）。

对照实验（真机 /tmp/g4-exp，两次实测 ps 原文进交付报告）：改前裸 kill PID
留 2 个 PPID=1 孤儿；改后同命令 ps 零残留。测试只用自己的 sleep 进程
（唯一数字标签），绝不触碰真机上的存量孤儿。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from agent_mailbox.installer import belt_script_body
from agent_mailbox.watchdog import WATCHDOG_TIMEOUT_RC, main, run_watchdog

# 唯一标签的假慢命令：sleep 秒数用罕用数字，ps grep 不撞真机进程
SLOW_SHELL = "sleep 2917 & sleep 2923 & wait"


def _residue_gone(tags: tuple[str, ...], deadline_s: float = 4.0) -> bool:
    """ps 里是否已无 tags 残留（轮询到 deadline；只读探测）。"""
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        out = subprocess.run(
            ["ps", "-axo", "command="], capture_output=True, text=True, timeout=10, check=False
        ).stdout
        if not any(t in out for t in tags):
            return True
        time.sleep(0.2)
    out = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,etime=,command="],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    ).stdout
    print(f"[test] residue still present:\n{out}", file=sys.stderr)
    return False


def test_watchdog_timeout_reclaims_process_group():
    """看门狗超时回收**进程组**：孙进程一并回收，ps 无残留（G-4 硬要求①）。"""
    res = run_watchdog(["bash", "-c", SLOW_SHELL], 1.5, grace=0.5)
    assert res.timed_out is True
    assert res.rc == WATCHDOG_TIMEOUT_RC
    assert res.kill_mode in ("terminated", "killed")
    assert _residue_gone(("sleep 2917", "sleep 2923"))


def test_watchdog_grace_escalates_to_sigkill():
    """宽限后 SIGKILL：子进程对 SIGTERM 装死（SIG_IGN 跨 exec 继承）也逃不掉。"""
    res = run_watchdog(
        [
            sys.executable,
            "-c",
            (
                "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('rdy', flush=True); time.sleep(60)"
            ),
        ],
        1.5,
        grace=1.0,
    )
    assert res.timed_out is True
    assert res.rc == WATCHDOG_TIMEOUT_RC
    assert res.kill_mode == "killed", f"expected SIGKILL escalation, got {res.kill_mode!r}"
    assert _residue_gone(("signal.SIG_IGN",))


def test_watchdog_normal_exit_unaffected():
    """正常退出不受影响：rc 原样上抛、未超时、零杀戮、stdout 原样带回。"""
    res = run_watchdog(["bash", "-c", "echo hi; exit 7"], 10)
    assert res.timed_out is False
    assert res.rc == 7
    assert res.kill_mode == ""
    assert res.stdout == "hi\n"


def test_watchdog_cli_shell_mode_rc124(tmp_path: Path):
    """belt 同款 --shell 形态：超时 rc=124（对齐 timeout(1) 惯例）+ 无残留。"""
    rc = main(["--timeout", "1.5", "--grace", "0.5", "--shell", SLOW_SHELL])
    assert rc == WATCHDOG_TIMEOUT_RC
    assert _residue_gone(("sleep 2917", "sleep 2923"))


def test_belt_template_uses_watchdog_no_bare_eval():
    """belt 模板：drain 经进程组看门狗 + 失败走 belt-fail 告警，裸 eval 退役。"""
    body = belt_script_body("zc", "/tmp/r", "/usr/bin/python3", "bash /tmp/w.sh")
    assert "agent_mailbox.watchdog" in body
    assert '--shell "$DRAIN_CMD"' in body  # 命令串当数据传入（不拼看门狗 shell）
    assert 'eval "$DRAIN_CMD"' not in body  # G-4 病灶形态（无看门狗裸跑）退役
    assert "agent_mailbox.alerts belt-fail" in body  # G-5: 失败必响走真投递
    assert "rc=124" in body and "timeout" in body  # 看门狗超时单独落锚
    assert "WAKE_WATCHDOG_SECS" in body  # 看门狗秒数可 env 覆盖


def test_belt_template_watchdog_secs_rendered(tmp_path: Path):
    """看门狗秒数按 watchdog_secs 参数渲染（WAKE_WATCHDOG_SECS env 可覆盖）。"""
    body = belt_script_body("a", "/tmp/r", "/usr/bin/python3", "true", watchdog_secs=123.0)
    assert "WAKE_WATCHDOG_SECS:-123" in body
    assert "--timeout" in body


def test_local_command_timeout_reclaims_group(monkeypatch):
    """LocalCommandAdapter 超时分支：判 timeout 失败（信不丢重试）且无孤儿。"""
    import agent_mailbox.wake as wake_mod
    from agent_mailbox.wake import LocalCommandAdapter

    monkeypatch.setattr(wake_mod, "DEFAULT_GRACE_SECS", 0.5)  # 测试时序旋钮（生产 5s）
    adapter = LocalCommandAdapter(["bash", "-c", SLOW_SHELL], timeout=1.5)
    delivered = adapter.deliver({"id": "m", "to": "ZC", "subject": "s", "body": "b"})
    assert delivered is False
    assert adapter.last_error_class == "timeout"
    assert _residue_gone(("sleep 2917", "sleep 2923"))
