"""G-4（0.7.6 收口批第 2 批）: 纯 Python 进程组看门狗 — 治孤儿.

病灶（G-4，HS 实测坐实）: WB 侧手写唤醒脚本的 shell 看门狗::

    ( sleep 180; kill "$CPID"; pkill -P "$CPID" ) &

先 ``kill $CPID``（单 PID）⇒ CLI 的子进程被 launchd 收养（PPID=1），紧随
其后的 ``pkill -P "$CPID"`` 已找不到任何子进程 ⇒ 孤儿挂死（实锤 4 个
codebuddy 进程 PPID=1 挂 7h+）。

进程组语义治本：子进程以**新会话/新进程组**启动（
``subprocess.Popen(start_new_session=True)``，POSIX 等价 ``setsid``——
组组长 = 直启子进程，组内无论几层孙进程都在同一组），超时对**整个进程
组** ``os.killpg(os.getpgid(pid), SIGTERM)`` → 宽限窗（默认 5s）后仍存活
才 ``SIGKILL``——先礼后兵，且组内回收不依赖父子关系存活与否。

纯 Python 实现，不用 shell 拼接（``sleep``+``kill``+``pkill`` 后台子壳
那条路正是 G-4 病灶本身）。共用方：

- :func:`run_watchdog`：库内入口（Popen + 定时 killpg）。wake.py 的
  ``LocalCommandAdapter``（plist → wake run 的命令执行路径）超时分支用
  :func:`terminate_group`。
- ``python -m agent_mailbox.watchdog``：belt 模板（installer.py 生成）的
  drain 执行器。``--shell "cmd"`` 把既有 shell 命令串**当数据**传入（由
  本模块以 ``["/bin/bash","-c",cmd]`` 起组），belt 不再裸 ``eval`` 无看
  门狗跑长命令。

退出码约定（对齐 ``timeout(1)`` 惯例）：子进程正常退出 rc 原样上抛；
124 = 看门狗超时回收进程组；125 = 本模块自身错误（spawn 失败等）。
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

# belt 模板 drain 的默认硬超时（秒）；WAKE_WATCHDOG_SECS env 或 agents.<ID>
# 段 timeout 键可覆盖（installer 生成时把路由 timeout 渲染成默认值）。
DEFAULT_WATCHDOG_SECS = 900.0
# SIGTERM → SIGKILL 的宽限窗（秒）：给 CLI 存盘/收尾的机会，不响应才补刀。
DEFAULT_GRACE_SECS = 5.0
WATCHDOG_TIMEOUT_RC = 124
WATCHDOG_ERROR_RC = 125


def _alive(pid: int) -> bool:
    """进程是否仍在（不 kill、只探测；僵尸也算已死——wait 不到就不算活）。"""
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        # 不是我们的子进程（或已 reap）：用 kill 0 探测
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    return done == 0


def terminate_group(pid: int, grace: float = DEFAULT_GRACE_SECS) -> str:
    """对 ``pid`` 所在**进程组** SIGTERM → 宽限 → 仍存活补 SIGKILL。

    返回回收方式：``"terminated"``（SIGTERM 生效）/ ``"killed"``（宽限后
    补 SIGKILL）/ ``"gone"``（调用前已退出）。绝不向上抛——kill 与已退出
    进程赛跑是常态，不是故障。先取 PGID 再发信号：组长先死也不影响对整组
    补刀（组号在会话存续期内稳定）。"""
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        pgid = pid  # 收养态/已退出：退化按单 PID 处理
    has_killpg = hasattr(os, "killpg")
    try:
        if has_killpg:
            os.killpg(pgid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        return "gone"
    deadline = time.monotonic() + max(0.0, float(grace))
    while time.monotonic() < deadline:
        if not _alive(pid):
            return "terminated"
        time.sleep(0.05)
    if _alive(pid):
        try:
            if has_killpg:
                os.killpg(pgid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        return "killed"
    return "terminated"


@dataclass
class WatchdogResult:
    """:func:`run_watchdog` 的结果（rc/timed_out/回收方式/输出原样带回）。"""

    rc: int
    timed_out: bool
    kill_mode: str  # "" | terminated | killed | gone
    stdout: str
    stderr: str


def run_watchdog(
    argv: list[str],
    timeout: float,
    *,
    grace: float = DEFAULT_GRACE_SECS,
    env: dict[str, str] | None = None,
    shell_cmd: str | None = None,
) -> WatchdogResult:
    """跑一条命令，超时按**进程组**回收（G-4 语义）。

    POSIX 下子进程以新会话/新进程组启动（``start_new_session=True``），
    超时 :func:`terminate_group` 对整组 SIGTERM → 宽限 → SIGKILL；
    ``shell_cmd`` 非空时以 ``["/bin/bash","-c",cmd]`` 起组（belt 的 drain
    形态——命令串是**数据**，不拼任何看门狗 shell）。"""
    if shell_cmd is not None:
        argv = ["/bin/bash", "-c", str(shell_cmd)]
    if not argv:
        return WatchdogResult(WATCHDOG_ERROR_RC, False, "", "", "watchdog: empty argv")
    kwargs: dict[str, object] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if env is not None:
        kwargs["env"] = env
    if os.name == "posix":
        kwargs["start_new_session"] = True  # 新会话/新进程组：killpg 一锅端
    proc = subprocess.Popen([str(a) for a in argv], **kwargs)
    try:
        out, err = proc.communicate(timeout=max(1.0, float(timeout)))
        return WatchdogResult(int(proc.returncode or 0), False, "", out or "", err or "")
    except subprocess.TimeoutExpired:
        mode = terminate_group(proc.pid, grace)
        try:
            out, err = proc.communicate(timeout=10)  # reap——不留僵尸
        except (OSError, subprocess.SubprocessError, ValueError):
            out, err = "", ""
        return WatchdogResult(WATCHDOG_TIMEOUT_RC, True, mode, out or "", err or "")


def main(argv: list[str] | None = None) -> int:
    """CLI：``python -m agent_mailbox.watchdog [--timeout N] [--grace N]
    [--shell "cmd" | -- cmd ...]``（belt 模板的 drain 执行器）。"""
    ap = argparse.ArgumentParser(
        prog="agent_mailbox.watchdog",
        description="进程组看门狗执行器（G-4）：超时 SIGTERM 整组 → 宽限 → SIGKILL",
    )
    ap.add_argument("--timeout", type=float, default=DEFAULT_WATCHDOG_SECS)
    ap.add_argument("--grace", type=float, default=DEFAULT_GRACE_SECS)
    ap.add_argument(
        "--shell",
        default="",
        help="把整条 shell 命令串当数据传入（以 /bin/bash -c 起进程组）",
    )
    ap.add_argument("cmd", nargs="*", help="argv 形态命令（-- 之后原样传入）")
    ns = ap.parse_args(argv)
    try:
        if str(ns.shell):
            res = run_watchdog([], ns.timeout, grace=ns.grace, shell_cmd=str(ns.shell))
        elif ns.cmd:
            res = run_watchdog(list(ns.cmd), ns.timeout, grace=ns.grace)
        else:
            print("[agent-mailbox watchdog] no command given (--shell 或 -- cmd)", file=sys.stderr)
            return WATCHDOG_ERROR_RC
    except OSError as exc:
        print(f"[agent-mailbox watchdog] spawn failed: {exc}", file=sys.stderr)
        return WATCHDOG_ERROR_RC
    if res.stdout:
        sys.stdout.write(res.stdout)
        sys.stdout.flush()
    if res.stderr:
        sys.stderr.write(res.stderr)
        sys.stderr.flush()
    if res.timed_out:
        print(
            f"[agent-mailbox watchdog] timeout after {ns.timeout:g}s → 进程组已回收 "
            f"(SIGTERM→{ns.grace:g}s 宽限→{res.kill_mode})",
            file=sys.stderr,
        )
        return WATCHDOG_TIMEOUT_RC
    return res.rc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
