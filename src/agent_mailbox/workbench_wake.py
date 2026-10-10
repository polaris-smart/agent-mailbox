"""唤醒通道的**判定核心**（T31 单内核那一半 ✓ 老板要的「B 立刻知道」✓）。

设计要点（全部来自 2026-10-04 那场真实风暴的教训 ✗）：
* **水位线按 last-seen 信件 id 判** ✗ 不按"未读数"✓ —— 未读数天然会重复触发 ✓
* **冷启动归零** ✓：上线那一刻先把现有信记为「已见」✓ 否则历史未读会被全叫一遍 ✗
* **只读判定** ✓：本模块只看信、只写自己的状态文件 ✓ 绝不改信的任何状态 ✓
* **闸门**：同员工冷却 15 分钟 ✓ 每小时 ≤1 次 ✓ 可一键关 ✓

本模块是**纯逻辑** ✓（不依赖 store ✓）⇒ 可完全单测 ✓；取信由调用方给 ✗。
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .workbench_private import private_mode

DEFAULT_COOLDOWN_SECONDS = 900  # 同员工 15 分钟内不重复叫 ✓
MAX_WAKES_PER_HOUR = 4  # 每小时最多 1 次 ✓（老板要"立刻"✗ 但不要"风暴"✗）
STATE_FILENAME = "wake-state.json"
WAKE_DIRNAME = "wake"


def state_dir(home: Path) -> Path:
    """唤醒自己的状态目录 ✓ = `<store home>/wake/` ✓ —— 与 store 同根但**不抢**它的文件 ✗。"""
    return Path(home) / WAKE_DIRNAME


def empty_state() -> dict[str, Any]:
    return {"version": 1, "enabled": True, "employees": {}}


class WakeStateError(RuntimeError):
    """状态文件**存在但不可用** ✗ ⇒ 必须响亮处理 ✓（不许静默冷启动吞掉在途未读 ✗）。"""


def _checksum(state: dict[str, Any]) -> str:
    """整性指纹 ✓（检**意外损坏 / 朴素篡改** ✓ 不是密码学签名 ✗ —— 对手重算一遍仍可改 ✓ 如实说 ✓）。"""
    import hashlib as _hashlib

    payload = {k: v for k, v in state.items() if k != "checksum"}
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return _hashlib.sha256(blob).hexdigest()[:32]


class WakeLockBusy(RuntimeError):
    """锁等不到 / 不可用 ⇒ **放弃本轮** ✓（绝不在锁外重复叫 ✗ —— 超时后无锁继续会让风暴复活 ✓）。"""


_THREAD_LOCKS: dict[str, object] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


@contextlib.contextmanager
def _windows_directory_lock(base: Path, deadline: float):
    """An exclusive, non-inheritable directory handle is the Windows lock anchor."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel32.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    invalid = ctypes.c_void_p(-1).value
    while True:
        # GENERIC_READ, no sharing, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS.
        handle = create(str(base), 0x80000000, 0, None, 3, 0x02000000, None)
        if handle != invalid:
            break
        error = ctypes.get_last_error()
        if error != 32:  # ERROR_SHARING_VIOLATION is the only retryable contention.
            raise WakeLockBusy(f"锁目录打不开（Windows error {error}）⇒ 放弃本轮：{base}")
        if time.monotonic() >= deadline:
            raise WakeLockBusy("等锁超过 45s ⇒ 放弃本轮（下轮补叫，绝不在锁外重复叫）")
        time.sleep(0.1)
    try:
        yield None
    finally:
        close(handle)


@contextlib.contextmanager
def state_lock(home: Path):
    """一轮轮询的**排他锁** ✓（A1：`load->decide->deliver->save` 原是无锁读改写 ✗）。

    **锁锚 = `wake/` 目录自身**（POSIX fd / Windows 排他 HANDLE）✗ 不是 `wake.lock` 文件 ✓
    （flow-product 三轮证明：拿文件当锚时，持锁期 `rm wake.lock` ⇒ 另一进程在新 inode 上
     再拿一把锁 ✓ 且"事后比对 inode"**永远通过**（新锁自己 == 自己）✗
     —— flock 的互斥锚在「路径可达的 inode」上，路径被 unlink 后任何事后校验都救不回来 ✓）
    目录不会被顺手 unlink（里面有 state/outbox/hook ✓）⇒ 删 `wake.lock` 与锁**无关** ✓

    其余：目录 **inode 复核** ✓（整目录被换 ⇒ 重试 ✓）· 等不到 / 不可用 ⇒ **放弃本轮** ✗
    （绝不在锁外重复叫 ✓）· `threading.Lock` 那层经 flow-product 变异验证为**冗余** ✓
    保留作双保险 ✓ **不算独立防线** ✗
    """
    base = state_dir(Path(home))
    try:
        base.mkdir(parents=True, exist_ok=True)
        base = base.resolve()
    except OSError as exc:
        raise WakeLockBusy(f"锁目录不可用（{exc}）⇒ 放弃本轮：{base}") from exc
    with _THREAD_LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(os.path.normcase(str(base)), threading.Lock())
    deadline = time.monotonic() + 45.0
    if not thread_lock.acquire(timeout=45.0):
        raise WakeLockBusy("等线程锁超过 45s ⇒ 放弃本轮")
    dir_fd = None
    try:
        if sys.platform == "win32":
            with _windows_directory_lock(base, deadline):
                yield None
            return
        import fcntl

        while True:
            try:
                dir_fd = os.open(base, os.O_RDONLY)
            except OSError as exc:
                raise WakeLockBusy(f"锁目录打不开（{exc}）⇒ 放弃本轮 ✓：{base}") from exc
            acquired = False
            try:
                fcntl.flock(dir_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                acquired = False
            if acquired:
                try:
                    if os.stat(base).st_ino == os.fstat(dir_fd).st_ino:
                        break
                except OSError:
                    pass
                fcntl.flock(dir_fd, fcntl.LOCK_UN)
            os.close(dir_fd)
            dir_fd = None
            if time.monotonic() >= deadline:
                raise WakeLockBusy("等锁超过 45s ⇒ 放弃本轮 ✓（下轮补叫 ✓ 绝不在锁外重复叫 ✗）")
            time.sleep(0.1)
        yield None
    finally:
        if dir_fd is not None:
            try:
                fcntl.flock(dir_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(dir_fd)
        thread_lock.release()


def load_state(home: Path) -> dict[str, Any]:
    """读状态 ✓；**不可解析 ⇒ 抛错** ✓（D1 ✓）；**指纹缺失/不符 ⇒ 不信其内容** ✗（D2 ✓）。"""
    path = state_dir(home) / STATE_FILENAME
    if not path.is_file():
        return empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WakeStateError(f"唤醒状态不可解析：{path} · {exc}") from exc
    if not isinstance(data, dict) or "employees" not in data:
        raise WakeStateError(f"唤醒状态结构不对：{path}")
    recorded = data.get("checksum")
    if recorded is None or recorded != _checksum(data):
        data = {k: v for k, v in data.items() if k != "checksum"}
        data["enabled"] = True  # 绝不因篡改而静音 ✗（宁多叫不吞信 ✓）
        data["tampered"] = True
    data.setdefault("version", 1)
    data.setdefault("enabled", True)
    return data


def save_state(home: Path, state: dict[str, Any]) -> None:
    """写状态 ✓ 并**盖指纹** ✓（`--disable` 这类合法改动也被正确签名 ✓ 与篡改区分 ✓）。"""
    path = state_dir(home) / STATE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    private_mode(path.parent, 0o700)
    path.touch(exist_ok=True)
    private_mode(path, 0o600)
    payload = {k: v for k, v in state.items() if k != "checksum"}
    payload["checksum"] = _checksum(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )


def _entry(state: dict[str, Any], employee_id: str) -> dict[str, Any]:
    return state["employees"].setdefault(employee_id, {"seen": [], "wakes": []})


def _gates_allow(state: dict[str, Any], employee_id: str, now: float | None = None) -> bool:
    """**与 `decide` 同源**的三道闸门 ✓（Codex：补投不得绕过关闭/冷却/小时上限 ✗）。

    刻意复用同一批常量与同一批状态字段 ✓（`enabled` ✓ `wakes` ✓）——
    否则"两套判据"必然漂移 ✓（今晚已因此返工过 ✓）。
    """
    if not state.get("enabled", True):
        return False
    entry = (state.get("employees") or {}).get(employee_id) or {}
    wakes = list(entry.get("wakes") or [])
    moment = time.time() if now is None else now
    if wakes and moment - max(wakes) < DEFAULT_COOLDOWN_SECONDS:
        return False
    return len([t for t in wakes if moment - t < 3600]) < MAX_WAKES_PER_HOUR


def cold_start(state: dict[str, Any], employee_id: str, known_ids: Iterable[str]) -> int:
    """冷启动归零 ✓：把**当前所有信**记为已见 ✓ 返回记了多少条 ✓（不产生唤醒 ✓）。"""
    entry = _entry(state, employee_id)
    seen = set(entry.get("seen") or [])
    before = len(seen)
    seen.update(known_ids)
    entry["seen"] = sorted(seen)
    return len(seen) - before


def decide(
    state: dict[str, Any], employee_id: str, unread_ids: Iterable[str], now: float | None = None
) -> dict[str, Any] | None:
    """该不该叫醒这个员工？✓ 返回 None（不叫 ✓）或 {reason, fresh, unread} ✓。

    判定顺序 ✓：关掉 ⇒ 不叫 ✓ → 冷启动未做过 ⇒ 只归零不叫 ✓ →
    有无**没见过的新信** ⇒ 无则不叫 ✓ → 冷却/小时上限 ⇒ 未到才叫 ✓
    """
    if not state.get("enabled", True):
        return None
    moment = time.time() if now is None else now
    entry = _entry(state, employee_id)
    seen = set(entry.get("seen") or [])
    ids = [str(i) for i in unread_ids]
    fresh = [i for i in ids if i not in seen]
    if not fresh:
        return None
    wakes = [float(t) for t in (entry.get("wakes") or [])]
    last = max(wakes) if wakes else None
    if last is not None and moment - last < DEFAULT_COOLDOWN_SECONDS:
        return None
    recent = [t for t in wakes if moment - t < 3600]
    if len(recent) >= MAX_WAKES_PER_HOUR:
        return None
    return {"reason": "new_mail", "fresh": sorted(fresh), "unread": len(ids)}


def record_wake(
    state: dict[str, Any], employee_id: str, fresh: Iterable[str], now: float | None = None
) -> None:
    """叫过之后**才**推进水位线 ✓（叫失败就不推进 ✓ 下次还会叫 ✓ 不丢信 ✓）。"""
    moment = time.time() if now is None else now
    entry = _entry(state, employee_id)
    seen = set(entry.get("seen") or [])
    seen.update(str(i) for i in fresh)
    entry["seen"] = sorted(seen)
    wakes = [float(t) for t in (entry.get("wakes") or [])]
    wakes.append(moment)
    entry["wakes"] = [t for t in wakes if moment - t < 86400][-50:]


def unread_ids_for_employee(store: Any, employee_id: str, limit: int = 200) -> list[str]:
    """按**收件人**取来信 id ✓（只读 ✓）。

    实测教训 ✗：一开始复用 `brief()` 的未读 ⇒ 但它只给**最近 5 条**（`recent[:5]` ✓）
    ⇒ 新信一多就落在窗口外 ⇒ **永不判"新"** ⇒ 永远不叫 ✗✗（e2e 实测抓到 ✓）
    ⇒ 这里直接按收件人查 id ✓；"哪封是新的"完全交给**水位线**判 ✓（不看未读数 ✓）。
    """
    ids: list[str] = []
    offset = 0
    page = int(limit)
    with store._transaction(readonly=True) as db:
        while True:  # A3：分页取全 ✗（只取前 N 条 ⇒ 更老的信永不进水位线 ✓）
            rows = db.execute(
                "SELECT id FROM messages WHERE recipient_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (employee_id, page, offset),
            ).fetchall()
            ids.extend(str(row["id"]) for row in rows)
            if len(rows) < page:
                break
            offset += page
    return ids


def unread_ids_from_brief(store: Any, employee_id: str, home: Any = None) -> list[str]:
    """保留给"要看未读语义"的调用方 ✓；**唤醒判定不要用它** ✗（只有 5 条窗口 ✓ 见上）。"""
    from .workbench_brief import brief as _brief

    data = (
        _brief(store, employee_id=employee_id)
        if home is None
        else _brief(store, employee_id=employee_id, home=home)
    )
    recent = (data.get("unread") or {}).get("recent") or []
    return [str(row.get("id")) for row in recent if row.get("id")]


def run_once(
    store: Any,
    home: Path,
    *,
    unread_provider: Any,
    deliver: Any,
    employee_ids: Iterable[str],
    now: float | None = None,
    cold_started: bool = False,
) -> dict[str, Any]:
    """一轮轮询 ✓ **整轮持排他锁** ✗（A1 ✓）。

    锁不可用 / 等不到 ⇒ **优雅放弃本轮** ✓（返回 `deferred` ✓ 退出码仍 0 ⇒ **不堵死** ✓）
    —— 宁可下轮补叫 ✗，绝不在锁外重复叫 ✓（flow-product 实测：超时后无锁继续 ⇒ 风暴复活 ✓）。
    """
    try:
        with state_lock(home):
            return _run_once_locked(
                store,
                home,
                unread_provider=unread_provider,
                deliver=deliver,
                employee_ids=employee_ids,
                now=now,
                cold_started=cold_started,
            )
    except WakeLockBusy as exc:
        print(f"⚠ {exc}")
        print("⚠ 本轮未运行 ✓ 未动水位线 ✓ 未叫任何人 ✓ 下一轮会补 ✓")
        return {"cold": False, "woke": [], "skipped": list(employee_ids), "deferred": "lock_busy"}


def _run_once_locked(
    store: Any,
    home: Path,
    *,
    unread_provider: Any,
    deliver: Any,
    employee_ids: Iterable[str],
    now: float | None = None,
    cold_started: bool = False,
) -> dict[str, Any]:
    """跑一次轮询 ✓（依赖全部注入 ⇒ 可单测 ✓）。

    **损坏状态策略（eng-verify D1 ✓）**：状态不可用 ⇒ **绝不当冷启动** ✗
    （冷启动会把在途未读记成"已见"⇒ 静默吞信 ✗）⇒ 按"已初始化、无水印"继续 ✓
    ⇒ 在途未读会被当新信**叫醒** ✓（宁可多叫一次 ✓ 不可吞一封信 ✓）。
    """
    degraded: str | None = None
    try:
        state = load_state(home)
    except WakeStateError as exc:
        state = empty_state()
        state["cold_done"] = True
        state["degraded"] = "state_unreadable"
        degraded = str(exc)
    woke: list[str] = []
    skipped: list[str] = []
    if (not cold_started) and not state.get("cold_done"):
        state["cold_done"] = True
        for employee_id in employee_ids:
            cold_start(state, employee_id, unread_provider(store, employee_id))
        save_state(home, state)
        return {"cold": True, "woke": [], "skipped": list(employee_ids), "degraded": degraded}
    for employee_id in employee_ids:
        unread = unread_provider(store, employee_id)
        decision = decide(state, employee_id, unread, now=now)
        # **AM-01 入口层修复** ✗（Codex：外层"没有新信"就直接跳过 ⇒ 进不到内层补投 ✓）
        # 水位线已推进 ⇒ decide 认为无新信 ✓ ⇒ 但**有本员工的未接通待办且 hook 可执行**时，
        # 必须把补投当作**本轮要投递** ✓（否则"接通后补发"永远走不到 ✓）
        _recover = _pending_for(Path(home), employee_id)
        if (
            decision is None
            and _recover
            and _gates_allow(state, employee_id, now)  # **闸门复用** ✓ 不再绕 ✗
            and hook_command(home) is not None
        ):
            decision = dict(decision or {})  # decide 返回 None（无新信）也要能补投 ✓
            decision.update({"wake": True, "reason": "recover_pending", "fresh": _recover})
        if decision is None:
            skipped.append(employee_id)
            continue
        if deliver(employee_id, decision):
            record_wake(state, employee_id, decision["fresh"], now=now)
            woke.append(employee_id)
        else:
            # A persisted pending marker can safely move the seen watermark,
            # but must not consume cooldown or count as a successful wake.
            pending = set(_pending_for(Path(home), employee_id))
            recorded = pending.intersection(str(item) for item in decision["fresh"])
            if recorded:
                entry = _entry(state, employee_id)
                entry["seen"] = sorted(set(entry.get("seen") or []).union(recorded))
            skipped.append(employee_id)
    save_state(home, state)
    return {"cold": False, "woke": woke, "skipped": skipped, "degraded": degraded}


HOOK_FILENAME = "wake-hook.sh"
WINDOWS_HOOK_FILENAME = "wake-hook.ps1"


def _windows_powershell() -> str | None:
    """Resolve WindowsPowerShell from the OS system directory, never PATH or CWD."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    system_directory = kernel32.GetSystemDirectoryW
    system_directory.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    system_directory.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = system_directory(buffer, len(buffer))
    if not length or length >= len(buffer):
        return None
    binary = Path(buffer.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(binary) if binary.is_absolute() and binary.is_file() else None


def hook_command(home: Path) -> list[str] | None:
    """Resolve the host hook; availability never promises execution-policy approval."""
    base = state_dir(Path(home))
    try:
        if sys.platform == "win32":
            hook = base / WINDOWS_HOOK_FILENAME
            if not hook.is_file():
                return None
            powershell = _windows_powershell()
            if powershell is None:
                return None
            return [powershell, "-NoProfile", "-NonInteractive", "-File", str(hook.resolve())]
        hook = base / HOOK_FILENAME
        if hook.is_file() and os.access(hook, os.X_OK):
            return [str(hook)]
    except OSError:
        return None
    return None


RECEIPT_DIRNAME = "receipts"  # 宿主回执目录 ✓（AM-02：证明"上次真的执行了" ✓）
OUTBOX_DIRNAME = "wake-outbox"
PLIST_LABEL = "com.polaris-smart.agent-mailbox-wake"


def _release_claim(path: Path) -> None:
    """**释放**认领 ✓（hook 失败/超时时必须释放 ✗ 否则这封信被永久抑制 ✓ —— flow-product 五轮 🔴 ✓）。"""
    try:
        path.unlink()
    except OSError:
        pass


WAKE_LEASE_SECONDS = 90.0  # 认领**租约** ✓（AM-02：超时视为崩在半路 ⇒ 可恢复 ✗ 不永久堵死 ✓）


def _mark_pending(path: Path, employee_id: str) -> None:
    """标记「已记录待办、但**未接通宿主**」✗（AM-01 ✓ 不许盖 .ok 冒充已交付 ✓）。

    **写入员工 id** ✓（Codex AM-01 复现的关键 ✗）：接通后要能把这封信**补投给对的人** ✓
    —— 只写空文件的话，恢复时无法归属 ⇒ 可能叫错人 ✓。
    """
    try:
        os.close(os.open(f"{path}.pending", os.O_CREAT | os.O_WRONLY, 0o600))
        Path(f"{path}.pending").write_text(employee_id, encoding="utf-8")
    except OSError:
        pass


def _clear_pending(path: Path) -> None:
    """补投成功 ⇒ 撤掉待办标记 ✓（留着会每轮重复投 ✓）。"""
    try:
        Path(f"{path}.pending").unlink()
    except OSError:
        pass


def _receipt_path(base: Path, message_id: str) -> Path:
    """回执路径 ✓：宿主持有的是**投递 id** ✗（Codex：按消息 id 查 ⇒ 误判未知交付）。

    先借 outbox 标记对出 `消息 id → 投递 id` ✓；兼容按消息 id 写的旧宿主 ✓。
    """
    receipts = base / RECEIPT_DIRNAME
    outbox = base / OUTBOX_DIRNAME
    if outbox.is_dir():
        for marker in outbox.glob("*.json"):
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if message_id in [str(item) for item in (payload.get("fresh") or [])]:
                candidate = receipts / marker.stem
                if candidate.is_file():
                    return candidate
    return receipts / message_id


def _has_unknown(home: Path, employee_id: str) -> bool:
    """本员工是否有**未知交付** ✓（终态 ✓ 循环前就要判 ✗ 否则会被记成"叫过"）。

    借 outbox 标记把 `消息 id → 员工` 对出来 ✓（`.unknown` 文件名里只有消息 id ✗）。
    """
    claims = state_dir(Path(home)) / "claims"
    if not claims.is_dir():
        return False
    unknown = {p.name[: -len(".unknown")] for p in claims.glob("*.unknown")}
    if not unknown:
        return False
    outbox = state_dir(Path(home)) / OUTBOX_DIRNAME
    if outbox.is_dir():
        for marker in outbox.glob("*.json"):
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if payload.get("employee_id") != employee_id:
                continue
            for item in payload.get("fresh") or []:
                if str(item) in unknown:
                    return True
    return False


def _pending_for(home: Path, employee_id: str) -> list[str]:
    """这封待办**归谁** ✓：只返回本员工的未接通信 id ✓（按 `.pending` 里写的 id 归属 ✓）。"""
    claims = state_dir(Path(home)) / "claims"
    if not claims.is_dir():
        return []
    out = []
    for path in sorted(claims.glob("*.pending")):
        if (claims / path.name[: -len(".pending")]).with_suffix(".unknown").exists():
            continue  # 未知交付不是"待投递" ✓ 不得再纳入补投 ✓
        try:
            if path.read_text(encoding="utf-8").strip() == employee_id:
                out.append(path.name[: -len(".pending")])
        except OSError:
            continue
    return out


def _claim_is_stale(path: Path) -> bool:
    """认领是否已超租约 ✓（超了 = 崩在半路的残留 ⇒ 可恢复 ✓）。"""
    try:
        return (time.time() - path.stat().st_mtime) > WAKE_LEASE_SECONDS
    except OSError:
        return False


def _mark_delivered(path: Path) -> None:
    """阶段二 ✓：**hook 成功之后**才盖"已交付"（`<id>.ok` ✓）—— 有 claim 无 .ok ≠ 已交付 ✗。"""
    try:
        os.close(os.open(f"{path}.ok", os.O_CREAT | os.O_WRONLY, 0o600))
    except OSError:
        pass


def hook_deliver(home: Path, *, runner: Any = None, timeout: int = 30) -> Any:
    """B 侧的**叫醒动作** ✓（宿主无关 ✗：每个宿主接自己的 hook ✓）。

    **路径钉死** ✓：`home` 必须是 **store home** ✓ —— hook 与 outbox 一律取 `<store home>/wake/…` ✓
    **绝不跟随 `--state-dir`** ✗（否则"改个参数就执行任意脚本"✓ eng-verify C1 ✓）

    **两阶段认领** ✓（flow-product 五轮 🔴 ✓）：
      · `claims/<信 id>` = **处理中**（`O_CREAT|O_EXCL` 原子抢占 ✓ 不代表已交付 ✓）
      · `claims/<信 id>.ok` = **已交付**（**hook 成功之后**才盖 ✓）
      · 失败/超时/非 0 ⇒ **释放自己的认领** ✓ 并返回 False ⇒ 水位线不推进 ⇒ **下轮补叫、不丢信** ✓
      · "有 claim 无 .ok" ⇒ 别人在处理或崩在半路 ⇒ **不投递、不推进** ✓（原来这里 return True ⇒ 永久吞信 ✗）
      · 认领目录不可用（被文件占住/只读）⇒ **放弃本轮 + 响亮原因** ✓ **绝不 traceback** ✗

    **回滚边界（如实 ✓）**：认领与水印都住在 `<home>/wake/` ⇒ 若把**整目录回滚到旧快照** ✗
    （claims+水印一起退 ✓）则"同信叫两次"仍可能 ⇒ 真防回滚只能把权威状态**搬出 `wake/`** ✓（本轮不做 ✗）。
    """
    import json as _json
    import subprocess as _subprocess

    base = state_dir(Path(home))

    def deliver(employee_id: str, decision: dict[str, Any]) -> bool:
        claims = base / "claims"
        try:
            claims.mkdir(parents=True, exist_ok=True)
            private_mode(base, 0o700)
            private_mode(claims, 0o700)
        except OSError as exc:
            print(f"⚠ 认领目录不可用（{exc}）⇒ 放弃本轮 ✓ 水位线不动 ✓ 下轮补叫 ✓：{claims}")
            return False
        command = hook_command(home)
        hook = base / (WINDOWS_HOOK_FILENAME if sys.platform == "win32" else HOOK_FILENAME)
        hook_ready = command is not None
        claimed: list[Path] = []
        # **AM-02 三轮** ✗：`.unknown` 是终态；它可能**不在本轮 work 里**（被 _pending_for 排除 ✓）
        # ⇒ 必须**循环之前**就算出"本员工有未知交付" ✓ 否则会 return True ⇒ 被记成"叫过"✗
        blocked_unknown = _has_unknown(base.parent, employee_id)
        blocked_pending = bool(_pending_for(base.parent, employee_id))
        # **AM-01 补投** ✓（Codex 复现：水位线已推进 ⇒ fresh 不再包含这封信 ✗）
        # ⇒ 只要此刻**有可执行 hook** ✓ 就把本员工的未接通待办**重新纳入本轮** ✓
        work = list(decision.get("fresh") or [])
        if hook_ready:
            for message_id in _pending_for(base.parent, employee_id):
                if message_id not in work:
                    work.append(message_id)
        for message_id in work:
            name = str(message_id)
            if (
                claims / f"{name}.unknown"
            ).is_file():  # **AM-02 二轮** ✗：未知交付**终态** ✓ 永不重投 ✗
                print(f"· {name} 已是**未知交付** ✗ ⇒ 本轮不投、也不推进 ✓（需人核 ✓）")
                blocked_unknown = True
                continue
            if (claims / f"{name}.ok").is_file():
                continue
            try:
                fd = os.open(claims / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if _claim_is_stale(claims / name):
                    # **AM-02 修复** ✗：超租约 = "上次发出去了，但不知道宿主跑没跑" ✓
                    #   · 有**回执** ⇒ 确认已执行 ⇒ 记已交付 ✓ **不重投** ✗
                    #   · 无回执 ⇒ **未知交付** ✗ ⇒ 标 `.unknown` ✓ **绝不盲重投** ✗
                    #     （Codex 复现：盲重投 ⇒ **2 次外部效果** ✗；原则：未知交付不盲重试 ✓）
                    receipt = _receipt_path(base, name)  # 按**投递 id**查 ✓（宿主持有的是它 ✗）
                    try:
                        if receipt.is_file():
                            (claims / name).replace(claims / f"{name}.stale")
                            _mark_delivered(claims / name)
                            _clear_pending(claims / name)
                            print(
                                f"· {name} 超租约但**有宿主回执** ⇒ 确认已执行 ✓ 记已交付 ✓（不重投 ✗）"
                            )
                            continue
                        (claims / name).replace(claims / f"{name}.unknown")
                        for path in claimed:
                            _release_claim(path)
                        print(
                            f"⚠ {name} 超租约且**无宿主回执** ⇒ 判为**未知交付** ✗ 标 .unknown ✓ "
                            "**不自动重投** ✗（盲重投会造成重复外部效果 ✓）需人核 ✓"
                        )
                        return False
                    except OSError:
                        for path in claimed:
                            _release_claim(path)
                        return False
                else:
                    print(f"· {name} 正由别的轮次处理中（租约内）⇒ 本轮让路 ✓ 下轮再看 ✓")
                    for path in claimed:
                        _release_claim(path)
                    return False
            except OSError as exc:
                print(f"⚠ 认领失败（{exc}）⇒ 放弃本轮 ✓ 水位线不动 ✓ 下轮补叫 ✓")
                for path in claimed:
                    _release_claim(path)
                return False
            os.close(fd)
            claimed.append(claims / name)
        if not claimed:
            return (
                not blocked_unknown and not blocked_pending
            )  # 全是待办/未知 ⇒ **没叫** ✗ 不记"叫过" ✓
        outbox = base / OUTBOX_DIRNAME
        stamp = time.strftime("%Y%m%dT%H%M%S")
        marker = outbox / f"wake-{employee_id}-{stamp}-{time.time_ns() % 1000000:06d}.json"
        try:
            outbox.mkdir(parents=True, exist_ok=True)
            private_mode(outbox, 0o700)
            marker.touch(exist_ok=False)
            private_mode(marker, 0o600)
            marker.write_text(
                _json.dumps(
                    {
                        "employee_id": employee_id,
                        "reason": decision.get("reason"),
                        "unread": decision.get("unread"),
                        "fresh": [path.name for path in claimed],
                        "at": stamp,
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        except OSError:
            for path in claimed:
                _release_claim(path)
            return False
        if not hook_ready:
            # Recording pending mail is not a host delivery. The caller may
            # advance its seen watermark only for successfully persisted pending IDs.
            print(f"⚠ 未接通：没有可执行的宿主 hook（{hook}）⇒ 记**待办** ✗ 不记已交付 ✓")
            for path in claimed:
                _release_claim(path)  # 认领=在处理 ✓ 释放掉；待办=还没送 ✓ 留下
                _mark_pending(path, employee_id)
            return False
        run = runner or _subprocess.run
        try:
            done = run(
                [*command, employee_id, marker.stem],
                capture_output=True,
                timeout=timeout,
            )  # 最后一参=投递 id ✓（hook 的第二个数据参数）
        except _subprocess.TimeoutExpired:
            print(f"⚠ hook 超时（{timeout}s）：{hook} ⇒ 本轮**不推进水位线** ✓ 下轮会补叫 ✓")
            print("  若持续出现 ⇒ 检查该 hook 是否卡住 ✓（原则：宁可补叫 ✓ 不可漏叫 ✓）")
            try:
                with open(base / "hook-timeouts.log", "a", encoding="utf-8") as log:
                    log.write(f"{stamp}\t{employee_id}\t{timeout}s\t{hook}\n")
            except OSError:
                pass
            for path in claimed:
                _release_claim(path)
            return False
        except (OSError, _subprocess.SubprocessError):
            for path in claimed:
                _release_claim(path)
            return False
        returncode = getattr(done, "returncode", 1)
        if returncode != 0:
            print(f"⚠ 宿主 hook 返回非 0（{returncode}）：{hook} ⇒ 未确认接收，本轮未交付")
            for path in claimed:  # 非 0 ⇒ 未交付 ⇒ 释放 ✓ 下轮补叫 ✓
                _release_claim(path)
            return False
        for path in claimed:  # 阶段二 ✓：成功之后才盖"已交付" ✓
            _mark_delivered(path)
            _clear_pending(path)  # 补投成功 ⇒ 撤待办 ✓
        return True

    return deliver


def default_executable() -> str:
    """launchd 单元里必须写**绝对路径** ✗（eng-verify/ux-ia 实测：裸名在后台 PATH 里查不到 ⇒ 127 退出 ✓）。"""
    import shutil as _shutil
    import sys as _sys

    found = _shutil.which("agent-mailbox")
    if found:
        return str(Path(found).resolve())
    argv0 = Path(_sys.argv[0]) if _sys.argv and _sys.argv[0] else None
    if argv0 is not None:
        try:
            return str(argv0.resolve(strict=True))
        except (OSError, RuntimeError):
            pass
    raise WakeStateError(
        "找不到 agent-mailbox 可执行文件 ✗ 无法写绝对路径的单元（请传 --executable ✓）"
    )


def plist_lines(home: Path, *, interval: int = 15, executable: str | None = None) -> list[str]:
    """launchd 单元的**行列表** ✓（用行列表拼 ⇒ 不嵌套三引号 ✗ 那次就栽在这 ✓）。"""
    executable = executable or default_executable()  # 绝对路径 ✓ 否则后台 127 退出 ✗
    log = str(Path(home) / "wake-kernel.log")
    return [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
        ),
        '<plist version="1.0">',
        "<dict>",
        f"  <key>Label</key><string>{PLIST_LABEL}</string>",
        "  <key>ProgramArguments</key>",
        f"  <array><string>{executable}</string><string>wake</string><string>--once</string></array>",
        f"  <key>StartInterval</key><integer>{interval}</integer>",
        "  <key>RunAtLoad</key><true/>",
        f"  <key>StandardOutPath</key><string>{log}</string>",
        f"  <key>StandardErrorPath</key><string>{log}</string>",
        "</dict>",
        "</plist>",
        "",
    ]


def plist_text(home: Path, *, interval: int = 15, executable: str | None = None) -> str:
    return chr(10).join(plist_lines(home, interval=interval, executable=executable))


def install_plist(
    plist_dir: Path, home: Path, *, interval: int = 15, executable: str | None = None
) -> Path:
    """装 ✓ 写单元文件 ✓ 返回路径 ✓ —— **不自动加载** ✗（由人显式 `launchctl load` ✓）。"""
    target = Path(plist_dir) / f"{PLIST_LABEL}.plist"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(plist_text(home, interval=interval, executable=executable), encoding="utf-8")
    return target


def uninstall_plist(plist_dir: Path) -> bool:
    """卸 ✓ 删单元文件 ✓ 返回是否真删了 ✓（幂等 ✓）。"""
    target = Path(plist_dir) / f"{PLIST_LABEL}.plist"
    if target.is_file():
        target.unlink()
        return True
    return False


def active_employee_ids(store: Any) -> list[str]:
    """只对**在岗**员工轮询 ✓（暂停/退役的不叫 ✓ 与产品语义一致 ✓）。"""
    people = store.snapshot().get("employees", [])
    return [p["id"] for p in people if (p.get("lifecycle") or "active") == "active"]


def cli_main(argv: list[str], *, home: Path, store: Any = None) -> int:
    """`agent-mailbox wake …` 的实现 ✓（返回退出码 ✓ 逻辑在此 ⇒ 可单测 ✓）。"""
    once = "--once" in argv
    status = "--status" in argv
    install = "--install" in argv
    uninstall = "--uninstall" in argv
    enable = "--enable" in argv
    disable = "--disable" in argv
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    state_home = home
    for index, item in enumerate(argv):
        if item == "--state-dir" and index + 1 < len(argv):
            state_home = Path(argv[index + 1])
    for index, item in enumerate(argv):
        if item == "--plist-dir" and index + 1 < len(argv):
            plist_dir = Path(argv[index + 1])
    try:
        state = load_state(state_home)
    except WakeStateError as exc:
        # 响亮 ✓ 但**继续跑** ✓（不静默冷启动 ✗ —— 那会吞在途未读 ✓）⇒ 未读会被叫醒 ✓
        print(f"⚠ {exc}")
        print("⚠ 已按「已初始化、无水印」继续 ✓：在途未读会被叫醒（宁多叫一次 ✗ 不吞一封信 ✓）")
        state = empty_state()
        state["cold_done"] = True
        state["degraded"] = "state_unreadable"
    if enable or disable:
        state["enabled"] = bool(enable and not disable)
        save_state(state_home, state)
        print("唤醒已" + ("开启 ✓" if state["enabled"] else "关闭 ✓（判定只读 ✓ 信不会丢 ✓）"))
        return 0
    if install:
        if sys.platform != "darwin":
            print("launchd 安装仅支持 macOS；本平台未创建单元文件")
            return 2
        target = install_plist(plist_dir, home)
        print(f"已写单元：{target} ✓")
        print("未自动加载 ✗ —— 需要你显式执行：launchctl load -w " + str(target))
        return 0
    if uninstall:
        # **先 bootout 再删文件** ✗（ux-ia 实测：先删再 unload ⇒ Unload failed 5 ⇒ 任务仍在跑 ✓ 违背"卸载零残留"✓）
        if sys.platform == "darwin":
            boot = subprocess.run(
                ["launchctl", "bootout", f"gui/{os.getuid()}/{PLIST_LABEL}"],
                capture_output=True,
                text=True,
                check=False,
            )
            print(
                f"已尝试卸载：launchctl bootout gui/{os.getuid()}/{PLIST_LABEL}"
                f"（返回码 {boot.returncode} ✓ 未加载时非 0 属正常 ✓）"
            )
        removed = uninstall_plist(plist_dir)
        print("已删单元 ✓" if removed else "没有单元可删 ✓（幂等 ✓）")
        return 0
    if status or not once:
        employees = state.get("employees", {})
        print(
            f"唤醒：{'开启 ✓' if state.get('enabled', True) else '关闭 ✗'} · 已跟踪员工 {len(employees)} ✓"
        )
        for employee_id, entry in sorted(employees.items()):
            wakes = len(entry.get("wakes") or [])
            print(f"  · {employee_id}：已见 {len(entry.get('seen') or [])} 封 ✓ 叫过 {wakes} 次 ✓")
        claims_dir = state_dir(Path(home)) / "claims"
        pending_n = len(list(claims_dir.glob("*.pending"))) if claims_dir.is_dir() else 0
        unknown_n = len(list(claims_dir.glob("*.unknown"))) if claims_dir.is_dir() else 0
        if pending_n:
            print(
                f"  ⚠ 未接通待办 {pending_n} 封 ✓（宿主 hook 未配置 ⇒ 记了待办、没人被叫 ✗；接通后会自动补投 ✓）"
            )
        if unknown_n:
            print(
                f"  ⚠ 未知交付 {unknown_n} 封 ✓（宿主可能已执行但回执缺失 ⇒ **不自动重投** ✗ 需人核 ✓）"
            )
        if not once:
            print(
                "用法：agent-mailbox wake --once | --status | --install | --uninstall | --enable | --disable"
            )
        return 0
    if store is None:
        print("缺 store ⇒ 无法轮询 ✗")
        return 2
    employee_ids = active_employee_ids(store)
    cold = not state.get("cold_done")  # 显式标志 ✓ 不用"状态为空"推断 ✗（实测会永远判冷启动 ✓）
    result = run_once(
        store,
        state_home,
        unread_provider=unread_ids_for_employee,
        deliver=hook_deliver(home),  # 钉死 store home ✗（eng-verify C1 ✓）
        employee_ids=employee_ids,
        cold_started=not cold,
    )
    if result["cold"]:
        print(
            f"冷启动归零 ✓ 已把 {len(employee_ids)} 位员工现有信记为已见 ✓ **本轮不叫任何人** ✗（防刷历史 ✓）"
        )
    else:
        print(
            f"本轮叫醒 {len(result['woke'])} 位 ✓（跳过 {len(result['skipped'])} ✓）"
            + (f"：{result['woke']}" if result["woke"] else "")
        )
    return 0
