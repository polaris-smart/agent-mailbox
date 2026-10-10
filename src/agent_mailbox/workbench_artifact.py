"""Resolve artifact references by byte budget (roadmap T6).

T12 records *references* — ``art:<sha256[:16]>`` plus hash and size — precisely so
that delivery evidence never gets pasted into a message. That leaves the other
half: how does a consumer read the thing **without blowing up its context**? The
competitor read-through gave the rule we adopt here:

  * a token travels; the artifact is **never auto-expanded**;
  * a read returns a slice, chosen by the consumer's projection **and a byte budget**;
  * the reference must still resolve across sessions, so the mapping is *derived*
    from the delivery proofs already in the ledger (no new table, no new writer).

Safety rule of our own: if the bytes no longer match the recorded hash, we return
**no content at all**. Serving tampered evidence is worse than serving nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .workbench_events import event_order
from .workbench_proof import PROOF_EVENT

REF_PREFIX = "art:"

DEFAULT_BUDGET = 2000
MAX_BUDGET = 65536
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024  # 超过此大小拒绝读内容（避免整份进内存）
PROJECTIONS = ("text", "head", "json_keys")


def _payload(event: dict) -> dict:
    value = event.get("payload")
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


PUBLIC_FIELDS = ("ref", "task_id", "path", "sha256", "bytes", "recorded_at")


def index(store, *, task_id: str | None = None) -> dict[str, dict[str, Any]]:
    """ref → {task_id, path, sha256, bytes}: derived from recorded delivery proofs.

    对外**只给公开字段**：内部的候选列表与插入序不得污染 CLI/JSON 契约。
    """
    return {
        ref: {key: entry[key] for key in PUBLIC_FIELDS if key in entry}
        for ref, entry in _index_raw(store, task_id=task_id).items()
    }


def _index_raw(store, *, task_id: str | None = None) -> dict[str, dict[str, Any]]:
    """Internal rich index (keeps ``candidates``/``_order`` for read fallbacks)."""
    out: dict[str, dict[str, Any]] = {}
    for event in store.governance_events(limit=None):
        if event.get("type") != PROOF_EVENT:
            continue
        if task_id is not None and event.get("task_id") != task_id:
            continue
        payload = _payload(event)
        for artifact in payload.get("artifacts") or []:
            ref = artifact.get("ref")
            if not ref:
                continue
            entry = {
                "ref": ref,
                "task_id": event.get("task_id"),
                "path": artifact.get("resolved") or artifact.get("path"),
                "sha256": artifact.get("sha256"),
                "bytes": artifact.get("bytes"),
                "recorded_at": event.get("created_at"),
                "_order": event_order(event),
            }
            current = out.get(ref)
            if current is None:
                out[ref] = {**entry, "candidates": [entry]}
                continue
            # 同 ref 可能被多个任务记录（内容相同）。**新者优先**，但保留候选：
            # 最旧那份文件被删掉时，read 可以退到仍然存在的新副本（原先直接拒绝）。
            candidates = current["candidates"] + [entry]
            candidates.sort(key=lambda row: row["_order"], reverse=True)
            newest = candidates[0]
            out[ref] = {**newest, "candidates": candidates}
    return out


def resolve(store, ref: str) -> dict[str, Any]:
    """Look up one reference; raises with a usable message when unknown."""
    if not str(ref).startswith(REF_PREFIX):
        raise ValueError(f"引用格式应为 {REF_PREFIX}<16 位十六进制>：{ref}")
    found = index(store).get(ref)
    if not found:
        raise ValueError(f"引用不存在或未被记录：{ref}（可用 artifact list 查看已知引用）")
    return found


def _guard(path: str | None) -> str | None:
    """Pre-read guard: ``None`` = 可以读；否则给出拒绝原因。

    对**每个候选**都要过这道闸（原先只查首选，回退候选直接绕过 64MB 上限）。
    """
    import stat as _stat

    if not path:
        return "missing_path"
    try:
        info = Path(path).stat()
    except OSError as exc:
        # 不能硬编码 FileNotFoundError：链接环(E62)/EACCES/ENAMETOOLONG 会被谎报
        return f"unreadable:{type(exc).__name__}"
    if not _stat.S_ISREG(info.st_mode):
        return "not_a_regular_file"  # FIFO/目录/设备：读它会挂死或读不出
    if info.st_size > MAX_ARTIFACT_BYTES:
        return "too_large"
    return None


def _hash_file(path: str) -> tuple[int, str | None, str | None]:
    """Stream-hash a regular file: returns ``(size, sha256, error)`` — never holds it all."""
    import hashlib as _hashlib

    digest = _hashlib.sha256()
    size = 0
    try:
        with Path(path).open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        return 0, None, f"unreadable:{type(exc).__name__}"
    return size, digest.hexdigest(), None


def _verify(entry: dict[str, Any]) -> tuple[bool, bytes | None, str]:
    """Legacy whole-file verification (kept for callers/tests); hashes by chunks."""
    blocked = _guard(entry.get("path"))
    if blocked:
        return False, None, blocked
    _size, digest, error = _hash_file(entry["path"])
    if error:
        return False, None, error
    if entry.get("sha256") and digest != entry["sha256"]:
        return False, None, "hash_mismatch"
    data = Path(entry["path"]).read_bytes()
    return True, data, "ok"


def _open_regular(path: str) -> tuple[int, int] | str:
    """Open **once** and confirm it is a regular file. Returns ``(fd, size)`` or a reason.

    ``O_NONBLOCK`` 让"具名管道"立刻返回而不是永久阻塞；随后用 ``fstat`` 复核类型。
    校验/取窗口都复用这一个 fd ⇒ 路径被换掉也不会读到**未校验**的字节。
    """
    import os
    import stat as _stat

    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    except OSError as exc:
        return f"unreadable:{type(exc).__name__}"
    try:
        info = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        return f"unreadable:{type(exc).__name__}"
    if not _stat.S_ISREG(info.st_mode):
        os.close(fd)
        return "not_a_regular_file"
    size = int(info.st_size)
    if size > MAX_ARTIFACT_BYTES:
        os.close(fd)
        return "too_large"  # 大文件不整份读进内存（读内容有上限）
    return fd, size


def _hash_fd(fd: int) -> str:
    """Stream-hash an open fd (constant memory); rewinds, does not close."""
    import hashlib as _hashlib
    import os

    os.lseek(fd, 0, os.SEEK_SET)
    digest = _hashlib.sha256()
    while chunk := os.read(fd, 1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _read_window(fd: int, offset: int, length: int) -> bytes:
    import os

    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, length)


def _align_utf8(window: bytes) -> tuple[str, int, bool, int]:
    """Decode a window: ``(text, source_bytes_consumed, lossy, skipped_head_bytes)``.

    两端都对齐到 UTF-8 字符边界：
    * 右端切在半字符处 ⇒ 回退（``next_offset`` 随之回退），续读拼回的文本不缺字；
    * 左端（调用方给了非字符边界 offset）⇒ 跳过残字节并**如实计数**，
      保证 ``consumed >= 1`` —— 否则 ``next_offset`` 不前进会让调用方**死循环**；
    * 窗口内含非法字节 ⇒ 用 replace 解码并标记 ``lossy``。
    """
    import codecs

    skip = 0
    # 只跳过"续字节"（0b10xxxxxx）：这才是"窗口起点落在字符中间"的判据。
    # 若用 try-decode，尾部被截断（很常见）也会被判成头部问题，从而白跳掉真内容。
    while skip < min(4, len(window)) and (window[skip] & 0xC0) == 0x80:
        skip += 1
    body = window[skip:]
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        text = decoder.decode(body, final=False)
    except UnicodeDecodeError:
        # 窗口内有非法字节：整窗 replace 解码，按原始窗口计消耗（不虚增）
        return window.decode("utf-8", errors="replace"), len(window), True, 0
    pending = decoder.getstate()[0]
    consumed = skip + len(body) - len(pending)
    if consumed == 0 and window:
        consumed = 1  # 前进保证（宁可多消费 1 字节，不可原地打转）
    return text, consumed, False, skip


def _trim_to_bytes(text: str, budget: int) -> str:
    """Longest **character-aligned** prefix whose UTF-8 encoding fits ``budget`` bytes.

    非法字节经 ``errors="replace"`` 解码后可能把 1 字节膨胀成 3 字节；
    不裁的话"预算"只在回报字段上成立，调用方拿到的正文仍可 3× 预算。
    """
    if len(text.encode("utf-8")) <= budget:
        return text
    out: list[str] = []
    used = 0
    for char in text:
        size = len(char.encode("utf-8"))
        if used + size > budget:
            break
        out.append(char)
        used += size
    return "".join(out)


def read(
    store,
    ref: str,
    *,
    budget: int = DEFAULT_BUDGET,
    offset: int = 0,
    projection: str = "text",
) -> dict[str, Any]:
    """Read a **slice** of a referenced artifact, honouring the byte budget.

    安全不变式：**字节与记录的哈希不符就一个字节都不给**。为此：

    * 每个候选**只打开一次**（``O_NONBLOCK`` + ``fstat`` 复核普通文件），哈希与取窗口复用同一 fd
      —— 路径被中途换掉也读不到未校验的字节；具名管道不会把调用挂死；
    * 取窗口后**无条件再哈希一次**比对：同 inode 被就地改写时拒绝返回
      （``changed_while_reading``；内容变长则为 ``size_changed_while_reading``）；
    * 只消费完整 UTF-8 字符：右端切在半字符处会**多读至多 3 字节**补齐（``budget_extended_bytes``
      如实报出），这样按 ``next_offset`` 续读拼回的文本与原文一致（``ends_mid_character``
      说明本次是否回退到字符边界）；
    * ``returned_bytes`` 恒等于返回内容字节，**上界为 ``budget``（字符补齐至多 +3）**；
      非法字节走 ``lossy`` 路径：文本按 min(预算, 本窗源字节) 有损呈现，
      ``lossy_dropped_bytes`` 报出"未被文本呈现的源字节数"（绝不因替换符膨胀 ⇒ 预算形同虚设）。
    """
    if projection not in PROJECTIONS:
        raise ValueError(f"projection 只能是：{', '.join(PROJECTIONS)}")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        raise ValueError("budget 需要正整数（字节）")
    if budget > MAX_BUDGET:
        raise ValueError(f"budget 超过单次上限 {MAX_BUDGET} 字节（防一次性灌爆上下文）")
    rich = _index_raw(store).get(ref)
    if rich is None:
        raise ValueError(f"引用不存在或未被记录：{ref}（可用 artifact list 查看已知引用）")
    candidates = list(rich.get("candidates") or [rich])
    first = candidates[0]

    skipped: list[dict[str, Any]] = []
    reason: str | None = None
    hash_checked = False
    for index, candidate in enumerate(candidates):
        path = candidate.get("path")
        if not path:
            skipped.append({"path": None, "reason": "missing_path"})
            reason = reason or "missing_path"
            continue
        opened = _open_regular(path)
        if isinstance(opened, str):
            skipped.append({"path": path, "reason": opened})
            reason = reason or opened
            continue
        fd, size = opened
        try:
            digest = _hash_fd(fd)
            hash_checked = True
            if candidate.get("sha256") and digest != candidate["sha256"]:
                skipped.append({"path": path, "reason": "hash_mismatch"})
                reason = "hash_mismatch"
                continue
            if offset < 0 or offset > size:
                raise ValueError(f"offset 越界：{offset}（文件 {size} 字节）")
            base: dict[str, Any] = {
                "ref": ref,
                "path": path,
                "task_id": candidate.get("task_id"),
                "total_bytes": size,  # 0 字节也如实报 0
                "budget": budget,
                "offset": offset,
                "offset_ignored": bool(offset) and projection == "json_keys",
                "projection": projection,
                "hash_verified": True,
                "hash_checked": True,
                "is_newest": index == 0,
                "used_candidate_index": index,
                "candidate_count": len(candidates),
                "skipped": skipped,
            }
            if projection == "json_keys":
                raw = _read_window(fd, 0, size) if size else b""
                if _hash_fd(fd) != digest:
                    return {
                        **base,
                        "content": None,
                        "returned_bytes": 0,
                        "truncated": False,
                        "refused": "changed_while_reading",
                    }
                try:
                    parsed = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    return {
                        **base,
                        "content": None,
                        "returned_bytes": 0,
                        "truncated": False,
                        "refused": "not_json",
                    }
                except RecursionError:
                    return {
                        **base,
                        "content": None,
                        "returned_bytes": 0,
                        "truncated": False,
                        "refused": "too_deep",
                    }
                content, bounded = _keys_bounded(parsed, budget)
                refused = None if bounded else "budget_exceeded_structure_only"
                payload = json.dumps(content, ensure_ascii=False) if content is not None else ""
                returned = len(payload.encode("utf-8"))
                if returned > budget:
                    for fallback in (
                        {"_structure": "too_large_for_budget", "budget": budget},
                        {"_truncated": True},
                        {},
                    ):
                        text = json.dumps(fallback, ensure_ascii=False)
                        if len(text.encode("utf-8")) <= budget:
                            content, returned = fallback, len(text.encode("utf-8"))
                            refused = "budget_exceeded_structure_only"
                            break
                    else:
                        content, returned, refused = None, 0, "budget_too_small"
                return {
                    **base,
                    "content": content,
                    "returned_bytes": returned,
                    "truncated": False,
                    "refused": refused,
                }

            raw_window = _read_window(fd, offset, budget)
            # 先只读预算窗口；**仅当它确实切在字符中间（或解不出字符）**才补读至多 3 字节。
            # （原先无条件前瞻 ⇒ 纯 ASCII 也恒超发 3 字节，budget_extended_bytes 失去意义）
            window = raw_window
            extended = 0
            # 无条件复核：同 inode 是否在"取窗口"期间被改动。
            # （曾按"整份读完才哈希"的思路加门控，方向正好写反 ⇒ 大于 budget 的文件
            #   被就地改写时会返回**从未校验**的字节却自称 hash_verified=True。）
            if _hash_fd(fd) != digest:
                import os as _os

                return {
                    **base,
                    # 拒绝时不得自称已校验：本次没有任何字节通过校验（第九轮低-1）
                    "hash_verified": False,
                    "hash_checked": True,
                    "content": None,
                    "returned_bytes": 0,
                    "truncated": False,
                    "refused": (
                        "size_changed_while_reading"
                        if _os.fstat(fd).st_size != size
                        else "changed_while_reading"
                    ),
                }
            text, consumed, lossy, skipped_head = _align_utf8(window)
            if (lossy or consumed < len(raw_window) or not text) and raw_window:
                # 尾部切在半字符处（或预算不足以解出一个字符）⇒ 补读至多 3 字节再对齐
                window = raw_window + _read_window(fd, offset + len(raw_window), 3)
                text, consumed, lossy, skipped_head = _align_utf8(window)
            if lossy:
                # 真有非法字节：只消费**原始预算窗口**，不多吞前瞻。
                # 内容按 min(预算, 本窗源字节) 裁剪（替换符会膨胀 ⇒ 不裁就 3× 预算），
                # 并把"未被文本呈现的源字节数"如实报出：lossy 模式下文本是**有损呈现**，
                # 需要逐字还原的调用方应按字节分页，而不是按文本拼接。
                consumed = len(raw_window)
                cap = min(budget, len(raw_window))
                full_render = raw_window.decode("utf-8", errors="replace")
                text = _trim_to_bytes(full_render, cap)
                lossy_dropped_bytes = consumed - len(text.encode("utf-8"))
                lossy_truncated = lossy_dropped_bytes > 0
            else:
                extended = max(0, consumed - len(raw_window))
                lossy_truncated = False
                lossy_dropped_bytes = 0
            ends_mid = consumed < len(window)
            if projection == "head":
                text = text.splitlines()[0] if text else ""
            content_bytes = len(text.encode("utf-8"))
            return {
                **base,
                "content": text,
                # 内容字节不超过预算：非法字节导致的替换符膨胀不被计入
                "returned_bytes": content_bytes,  # 恒等于返回内容字节（与 docstring 一致）
                "content_bytes": content_bytes,
                "lossy": lossy,
                "lossy_truncated": lossy_truncated,
                "lossy_dropped_bytes": lossy_dropped_bytes,
                "ends_mid_character": ends_mid,
                "skipped_head_bytes": skipped_head,
                "budget_extended_bytes": max(0, extended),
                "truncated": offset + consumed < size,
                "next_offset": offset + consumed if offset + consumed < size else None,
                "refused": None,
            }
        finally:
            import os as _os

            _os.close(fd)

    current = {"path": first.get("path"), "task_id": first.get("task_id"), "total_bytes": None}
    return {
        "ref": ref,
        **current,
        "budget": budget,
        "offset": offset,
        "projection": projection,
        "hash_verified": False if reason == "hash_mismatch" else None,
        "hash_checked": hash_checked,
        "is_newest": False,
        "used_candidate_index": None,
        "candidate_count": len(candidates),
        "skipped": skipped,
        "content": None,
        "returned_bytes": 0,
        "truncated": False,
        "refused": reason or "missing_path",
    }


def _keys_bounded(node: Any, budget: int) -> tuple[Any, bool]:
    """Structure-only view, **bounded by the byte budget**.

    Returns ``(content, within_budget)``. Stops descending once the estimate is
    exhausted, so a 20k-key JSON cannot be expanded just because the caller asked
    for "keys only".
    """
    spent = 0

    def walk(value: Any, depth: int) -> Any:
        nonlocal spent
        if spent >= budget:
            return "…"
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for key, item in value.items():
                if spent >= budget:
                    out["…"] = f"{len(value) - len(out)} 个键省略"
                    break
                piece = walk(item, depth + 1)
                out[str(key)] = piece
                spent += len(str(key).encode("utf-8")) + len(str(piece).encode("utf-8")) + 4
            return out
        if isinstance(value, list):
            label = f"list[{len(value)}]"
            spent += len(label) + 4
            return label
        label = type(value).__name__
        spent += len(label) + 4
        return label

    content = walk(node, 0)
    return content, spent < budget


def _keys_of(node: Any, prefix: str = "", depth: int = 0) -> Any:
    """Structure only — keys and shapes, never values (for audit-style consumers)."""
    if depth >= 3:
        return "…"
    if isinstance(node, dict):
        return {key: _keys_of(value, f"{prefix}.{key}", depth + 1) for key, value in node.items()}
    if isinstance(node, list):
        return f"list[{len(node)}]"  # 只给规模标签，不下钻数组元素
    return type(node).__name__


def list_refs(store, *, task_id: str | None = None) -> list[dict[str, Any]]:
    """Known references, newest first (read-only)."""
    rows = list(index(store, task_id=task_id).values())
    rows.sort(key=lambda row: str(row.get("recorded_at") or ""), reverse=True)
    return rows
