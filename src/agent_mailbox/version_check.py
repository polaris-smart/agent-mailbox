"""v0.7.6 E 单元（t-53）— 版本检查 + 提示 + 点击更新。

判据落点（HS 任务书 v2）：
① 查最新版：PyPI 版本级端点 ``https://pypi.org/pypi/agent-mailbox/json`` 取
  ``info.version``，只服务本包，零新依赖（urllib 标准库）；
② 提示：当前版 / 最新版 / 一行更新命令（README 口径
  ``uv tool upgrade agent-mailbox``）；
③ 点击更新：CLI ``agent-mailbox upgrade`` + web 看板按钮同走一条逻辑——
  只以「起外部命令」这一种形态升级（禁 self-update：进程内绝不自替换
  代码）；安装方式自动识别 uv tool / pipx / pip；owner/本机限定 +
  执行前显示完整命令（dry-run 回显）+ 审计留痕（``<root>/audit.log``）；
④ 不后台乱联网：无常驻轮询，全部由显式命令 / 带 token 的看板按钮触发；
  非强制查询走 ≥24h 本地缓存限流（``<root>/version_check.json``）；
⑤ fail-open：断网 / PyPI 不可达 / 坏 JSON 一律静默返回 ``None``，异常只
  留审计不抛出，绝不影响任何信件收发功能。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _metadata_version
from os import PathLike
from pathlib import Path
from typing import Any

from .store import MailStore

PACKAGE = "agent-mailbox"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
CHECK_INTERVAL_S = 24 * 3600  # 判据④：限流 ≥24h（本地缓存上次查询时间）
HTTP_TIMEOUT_S = 3.0
UPGRADE_TIMEOUT_S = 600.0
STATE_FILENAME = "version_check.json"

FetchFn = Callable[..., str]  # (url, timeout=…) -> body text
RunnerFn = Callable[[list[str]], Any]  # command -> CompletedProcess-like


# ------------------------------------------------------------------ version


def current_version() -> str:
    """本包当前版本：装好的 dist 元数据优先，退化到源码 ``__version__``。"""
    try:
        return _metadata_version(PACKAGE)
    except PackageNotFoundError:
        from . import __version__

        return __version__


def _http_get(url: str, timeout: float = HTTP_TIMEOUT_S) -> str:
    """Default PyPI fetch. Tests monkeypatch this module (or pass ``fetch=``);
    nothing else here ever opens a socket."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def fetch_latest_version(
    *, fetch: FetchFn | None = None, timeout: float = HTTP_TIMEOUT_S
) -> str | None:
    """判据①：PyPI 版本级端点取 ``info.version``。判据⑤ fail-open：断网 /
    非 JSON / 字段缺失一律返回 ``None``，绝不抛出。"""
    get = fetch or _http_get
    try:
        data = json.loads(get(PYPI_URL, timeout))
        version = data["info"]["version"]
        return str(version) if version else None
    except Exception:  # noqa: BLE001 — fail-open is the whole point (判据⑤)
        return None


def _version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", str(v))) or (0,)


def _is_newer(latest: str, current: str) -> bool:
    """纯数字段比较（0.7.7 > 0.7.6）；解析异常按「无新版」处理（fail-open）。"""
    try:
        a, b = _version_key(latest), _version_key(current)
        n = max(len(a), len(b))
        return (a + (0,) * n)[:n] > (b + (0,) * n)[:n]
    except Exception:  # noqa: BLE001
        return False


# -------------------------------------------------------- 24h-limited check


def _state_path(root: str | PathLike[str]) -> Path:
    return Path(root) / STATE_FILENAME


def _load_state(root: str | PathLike[str]) -> dict[str, Any]:
    try:
        data = json.loads(_state_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(root: str | PathLike[str], state: dict[str, Any]) -> None:
    try:
        Path(root).mkdir(parents=True, exist_ok=True)
        _state_path(root).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # 判据⑤：缓存写不进去也绝不连坐主功能


def check_for_update(
    root: str | PathLike[str],
    *,
    force: bool = False,
    fetch: FetchFn | None = None,
    now: float | None = None,
    which: Callable[[str], str | None] | None = None,
) -> dict[str, Any] | None:
    """查最新版 + 组装提示（判据①②）。非强制路径受 ≥24h 缓存限流（判据④）：
    ``force=False`` 且缓存未过期时直接走缓存、不联网；查不到（断网/坏
    JSON）返回 ``None`` 且不写缓存（判据⑤，下次调用仍可重试）。"""
    at = time.time() if now is None else now
    state = _load_state(root)
    latest: str | None = None
    cached = False
    within_ttl = (at - float(state.get("last_check_at", 0))) < CHECK_INTERVAL_S
    if not force and within_ttl and state.get("latest_version"):
        latest = str(state["latest_version"])
        cached = True
    else:
        latest = fetch_latest_version(fetch=fetch)
        if latest is None:
            return None  # 判据⑤ fail-open：查不到就静默跳过
        _save_state(root, {"last_check_at": at, "latest_version": latest})
    current = current_version()
    update_available = _is_newer(latest, current)
    return {
        "current": current,
        "latest": latest,
        "update_available": update_available,
        "command": detect_install_method(which=which)[1],
        "cached": cached,
    }


def format_update_notice(info: dict[str, Any]) -> str:
    """判据② 提示文案：当前版 / 最新版 / 一行更新命令。"""
    return "有新版本：当前 {} → 最新 {}；更新命令：{}".format(
        info["current"], info["latest"], " ".join(info["command"])
    )


# ----------------------------------------------------------- install detect


def detect_install_method(
    *, which: Callable[[str], str | None] | None = None
) -> tuple[str, list[str]]:
    """判据③ 三分支：uv tool → pipx → pip（``python -m pip``）。
    ``which`` 可注入（测试用假 PATH）；pip 分支钉住当前解释器，
    绝不碰进程自身代码（升级=起外部命令，禁 self-update）。"""
    lookup = which or shutil.which
    if lookup("uv"):
        return "uv", ["uv", "tool", "upgrade", PACKAGE]
    if lookup("pipx"):
        return "pipx", ["pipx", "upgrade", PACKAGE]
    return "pip", [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE]


def upgrade_plan(*, which: Callable[[str], str | None] | None = None) -> dict[str, Any]:
    method, command = detect_install_method(which=which)
    return {"method": method, "command": command}


# ------------------------------------------------------------------ upgrade


def _default_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=UPGRADE_TIMEOUT_S
    )


def run_upgrade(
    root: str | PathLike[str],
    *,
    confirm: bool = False,
    which: Callable[[str], str | None] | None = None,
    runner: RunnerFn | None = None,
    store: MailStore | None = None,
    by: str = "cli",
) -> dict[str, Any]:
    """判据③ 点击更新（唯一升级形态 = 起外部命令，禁 self-update）。

    ``confirm=False``（dry-run）只回显将运行的完整命令、绝不起进程；
    ``confirm=True`` 先写 ``upgrade_start`` 审计（执行前留痕）再 spawn，
    结束补 ``upgrade_result``（含退出码）。所有异常兜进审计，不外抛
    （判据⑤）。审计落 ``<root>/audit.log``（JSONL，append-only）。
    """
    st = store or MailStore(root=root)
    method, command = detect_install_method(which=which)
    if not confirm:
        entry = st.audit("upgrade_dry_run", by=by, method=method, command=command)
        return {
            "executed": False,
            "dry_run": True,
            "ok": None,
            "method": method,
            "command": command,
            "audit_at": entry["at"],
        }
    st.audit("upgrade_start", by=by, method=method, command=command)  # 执行前留痕
    run = runner or _default_runner
    started = time.monotonic()
    try:
        proc = run(command)
    except Exception as exc:  # noqa: BLE001 — 升级失败绝不连坐信箱（判据⑤）
        st.audit(
            "upgrade_result",
            by=by,
            method=method,
            command=command,
            ok=False,
            exit_code=None,
            error=str(exc)[:200],
        )
        return {
            "executed": True,
            "ok": False,
            "method": method,
            "command": command,
            "exit_code": None,
            "error": str(exc)[:200],
        }
    duration = round(time.monotonic() - started, 2)
    exit_code = getattr(proc, "returncode", None)
    ok = exit_code == 0
    st.audit(
        "upgrade_result",
        by=by,
        method=method,
        command=command,
        ok=ok,
        exit_code=exit_code,
        duration_s=duration,
    )
    return {
        "executed": True,
        "ok": ok,
        "method": method,
        "command": command,
        "exit_code": exit_code,
        "duration_s": duration,
    }
