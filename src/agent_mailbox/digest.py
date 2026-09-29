"""t-62（判据7+S4）: digest — LLM 可选的纯本地信件流转.

机器上没有任何 CLI 登录态，信件仍能流转：读信 → 写摘要 → 标 done → 列
「建议回复」。LLM 是增强不是必需路径——本模块**零网络、零 LLM、零
subprocess**（纯 stdlib 文件操作，队列/锁语义全走 store 自己的 API），把
WB 那种「CLI 未登录即全断」从根上消除：

- :func:`run_digest`：认领（claim-first，t-56 纪律——两窗不重复消化同一封）
  → 逐封结构化摘要 → ``<root>/digest/<date>.md`` 落盘 → 标 done
  （handled_log 记 ``digest`` 动作带摘要锚）→ 产出「建议回复」占位清单
  （谁/何时/要不要回的判断留给人——不做任何 LLM 调用）。
- :func:`digest_claimed_letters`：wake drain 的降级接线——local-command
  投递失败（spawn_failed / auth_required / exit_nonzero / timeout）时，把
  **已认领**的信走 digest 路径：信不丢（内容进摘要）、标 done、告警照发
  （降级事实进 handled_log 与告警文本），见 wake.run_once。
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .store import MailStore

DIGEST_DIR = "digest"
DIGEST_ACTION = "digest"
DIGEST_BODY_CHARS = 500  # 摘要截断宽度（扁平化后的正文字符数）
DIGEST_HEADER = "# agent-mailbox digest · {date}（纯本地摘要 · 零网络 · 零 LLM）\n"


def _fmt_local(iso: str | None) -> str:
    """ISO-UTC → 本地时区人话时刻（解析失败原样返回；不引第三方）。"""
    if not iso:
        return "-"
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def summarize(msg: dict[str, Any]) -> dict[str, Any]:
    """一封信 → 结构化摘要（from/subject/body 截断/时间/优先级，纯本地）。"""
    body = str(msg.get("body", "") or "")
    flat = " ".join(body.split())
    excerpt = flat[:DIGEST_BODY_CHARS]
    if len(flat) > DIGEST_BODY_CHARS:
        excerpt += f"…（截断，全文 {len(flat)} 字符）"
    return {
        "id": str(msg.get("id", "")),
        "from": str(msg.get("from", "")),
        "to": str(msg.get("to", "")),
        "subject": str(msg.get("subject", "")),
        "body_excerpt": excerpt,
        "truncated": len(flat) > DIGEST_BODY_CHARS,
        "created_at": str(msg.get("created_at", "") or ""),
        "priority": str(msg.get("priority", "normal")),
        "attention": str(msg.get("attention", "")),
    }


def suggest_reply_line(s: dict[str, Any]) -> str:
    """「建议回复」占位行——谁/何时/要不要回的判断留给人，零 LLM。"""
    return (
        f"- {s['from'] or '?'} · {_fmt_local(s['created_at'])} · 「{s['subject']}」 "
        "— 建议回复：待人工判断（digest 占位清单，未调用 LLM）"
    )


def digest_date_name() -> str:
    return time.strftime("%Y-%m-%d")


def digest_path(root: Path | str) -> Path:
    return Path(root) / DIGEST_DIR / f"{digest_date_name()}.md"


def entry_block(s: dict[str, Any], index: int, reason: str, file_name: str) -> str:
    """单封信的 markdown 块（含建议回复占位行 + handled_log 摘要锚）。"""
    anchor = f"digest/{file_name}#{index}"
    handled = f"已标 done（handled_log {DIGEST_ACTION} 动作 · 摘要锚 {anchor}）"
    if reason:
        handled += f" · 降级原因: {reason}"
    return (
        f"## {index}. {s['id']} · {s['from'] or '?'} → {s['to'] or '?'} "
        f"· 优先级 {s['priority']} · {_fmt_local(s['created_at'])}\n"
        f"- 主题: {s['subject']}\n"
        f"- 摘要: {s['body_excerpt']}\n"
        f"- 处理: {handled}\n"
        f"- 建议回复: {suggest_reply_line(s).lstrip('- ')}\n"
    )


def digest_claimed_letters(
    root: Path | str,
    agent_id: str,
    msgs: list[dict[str, Any]],
    *,
    store: MailStore | None = None,
    reason: str = "",
    session_label: str | None = None,
) -> dict[str, Any]:
    """对**已认领**的信做纯本地 digest（wake 降级路径的接线点）。

    逐封：摘要 → ``<root>/digest/<date>.md`` 追加落盘 → handled_log 记
    ``digest`` 动作（note 带摘要锚 + 降级原因）→ 标 done。全程零网络、零
    LLM、零 subprocess。返回 ``{"digested", "digest_file", "suggest_replies"}``。"""
    root = Path(root)
    st = store or MailStore(root)
    if not msgs:
        return {"agent": agent_id, "digested": 0, "digest_file": "", "suggest_replies": []}
    path = digest_path(root)
    file_name = path.name
    index_offset = 0
    try:
        if path.exists():
            index_offset = sum(
                1
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.startswith("## ")
            )
    except OSError:
        index_offset = 0
    blocks: list[str] = []
    suggestions: list[str] = []
    digested = 0
    for m in msgs:
        s = summarize(m)
        idx = index_offset + digested + 1
        blocks.append(entry_block(s, idx, reason, file_name))
        suggestions.append(suggest_reply_line(s))
        mid = s["id"]
        try:
            st.record_handled(
                agent_id,
                mid,
                DIGEST_ACTION,
                session_label=session_label,
                note=f"digest:{file_name}#{idx}" + (f" reason={reason}" if reason else ""),
            )
        except Exception as exc:  # noqa: BLE001 — 摘要锚 fail-open，标 done 不受影响
            print(
                f"[agent-mailbox digest] handled_log anchor failed (fail-open): {exc}",
                file=sys.stderr,
            )
        try:
            st.set_status(agent_id, mid, "done")
            digested += 1
        except Exception as exc:  # noqa: BLE001 — 信可能已被并发窗处理（claim 纪律已防重）
            print(
                f"[agent-mailbox digest] set done failed for {mid} (fail-open): {exc}",
                file=sys.stderr,
            )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        header = "" if path.exists() else DIGEST_HEADER.format(date=digest_date_name())
        with open(path, "a", encoding="utf-8") as f:
            if header:
                f.write(header)
            f.write("\n".join(blocks))
            f.write("\n")
    except OSError as exc:
        print(
            f"[agent-mailbox digest] digest file write failed (fail-open): {exc}", file=sys.stderr
        )
    return {
        "agent": agent_id,
        "digested": digested,
        "digest_file": str(path) if digested else "",
        "suggest_replies": suggestions,
    }


def run_digest(
    root: Path | str,
    agent_id: str,
    *,
    store: MailStore | None = None,
    only: list[str] | tuple[str, ...] | set[str] | None = None,
    reason: str = "",
    session_label: str | None = None,
) -> dict[str, Any]:
    """一个身份的纯本地 digest 全链路（CLI ``agent-mailbox digest`` 的引擎）。

    claim-first（只领 pending；``only`` 限定范围）→
    :func:`digest_claimed_letters`。认领 0 封 ⇒ 不写摘要、不落盘。"""
    root = Path(root)
    st = store or MailStore(root)
    label = session_label or f"digest:{os.getpid()}"
    claimed = st.claim(agent_id, only=list(only) if only is not None else None, session_label=label)
    return digest_claimed_letters(
        root, agent_id, claimed, store=st, reason=reason, session_label=label
    )
