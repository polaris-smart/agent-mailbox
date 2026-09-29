"""G-5（0.7.6 收口批第 2 批）: 告警真投递 — store.send + 回读确认.

病灶（G-5，HS 实测）：WB 脚本反复写 ``alert_to=HS``（wake.log fail=6/7/8/9），
但 HS 箱里告警信 **0 封**——告警只写了日志、没有落箱、更没人回读确认，
「告警的告警」整条断线。

统一投递语义（所有告警路径共用 :func:`deliver_confirmed`）：

1. 走 ``store.send``（信箱自身的落箱主链路：审计/去重/锁全都在）；
2. send 返回 per-收件人 message id ⇒ 用 ``MailStore.get_letter`` **回读**
   该信存在且 status 非终态（pending/acked）才算「投递成功」；
3. 回读失败（信不在/状态不对/收件人未注册）⇒ 记入 ``audit.log``（
   ``alert_delivery_unconfirmed``）+ 返回未确认 ⇒ 调用方下一轮重试
   （信不丢语义——source 信不标「已告警」，下轮 drain 再投）。

共用方：wake.py 的 ``send_wake_alert`` 家族（digest 降级告警同路）、
belt 模板（``python -m agent_mailbox.alerts belt-fail``）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .store import MailStore

# 回读确认的合法 status：告警信必须处于**待处理**态躺在收件人箱里才算到
# （done/archived = 被人消化过的旧信，不能拿来冒充本次投递成功）。
ALERT_CONFIRM_STATUSES = ("pending", "acked")
AUDIT_ACTION = "alert_delivery_unconfirmed"


def _audit(store: MailStore, action: str, **fields: Any) -> None:
    """审计留痕（fail-open：审计失败绝不挡告警主链路）。"""
    try:
        store.audit(action, **fields)
    except Exception as exc:  # noqa: BLE001 — 审计是留痕不是闸门
        print(f"[agent-mailbox alerts] audit failed (fail-open): {exc}", file=sys.stderr)


def deliver_confirmed(
    store: MailStore,
    from_id: str,
    recipients: list[str],
    subject: str,
    body: str,
    *,
    context: str = "",
) -> dict[str, Any]:
    """store.send → 逐收件人回读确认。返回 ``{"confirmed": bool, "rows": [...]}``。

    - 送达行（有 ``id``）与去重行（``deduped`` + ``existing_id``——同 hash
      的**未终态**告警已在箱里 = 收件人已持有该告警）都回读验证；
    - 任一行未注册/回读失败/状态非 pending|acked ⇒ 该行 ``confirmed=False``
      并落审计；``confirmed`` = 所有收件人全部回读确认。
    """
    rows: list[dict[str, Any]] = []
    try:
        out = store.send(from_id, [str(r) for r in recipients], subject, body)
    except Exception as exc:  # noqa: BLE001 — send 侧任何异常都算未投出
        _audit(store, AUDIT_ACTION, by=from_id, context=context, error=f"send raised: {exc}")
        print(f"[agent-mailbox alerts] send failed: {exc}", file=sys.stderr)
        return {"confirmed": False, "rows": [{"error": str(exc)}]}
    for r in out:
        if not isinstance(r, dict):
            rows.append({"row": str(r), "confirmed": False})
            continue
        rid = str(r.get("to", ""))
        mid = str(r.get("id") or r.get("existing_id") or "")
        row = dict(r)
        if not rid or not mid or r.get("warn") or r.get("error"):
            # 未注册（warn）/无 id（不可能回读）：投递失败态
            row["confirmed"] = False
            row["reason"] = str(r.get("warn") or r.get("error") or "no message id")
            _audit(store, AUDIT_ACTION, by=from_id, context=context, to=rid, reason=row["reason"])
            rows.append(row)
            continue
        try:
            letter = store.get_letter(rid, mid)
            ok = str(letter.get("status", "")) in ALERT_CONFIRM_STATUSES
            row["reason"] = "" if ok else f"readback status={letter.get('status')!r}"
        except Exception as exc:  # noqa: BLE001 — 回读不到 = 没投到（哪怕盘上刚写过）
            ok = False
            row["reason"] = f"readback failed: {exc}"
        row["confirmed"] = ok
        if not ok:
            _audit(
                store,
                AUDIT_ACTION,
                by=from_id,
                context=context,
                to=rid,
                msg_id=mid,
                reason=row["reason"],
            )
        rows.append(row)
    confirmed = bool(rows) and all(bool(r.get("confirmed")) for r in rows)
    return {"confirmed": confirmed, "rows": rows}


def send_belt_fail_alert(
    root: Path | str,
    agent_id: str,
    *,
    reason: str,
    claimed: int = 0,
    done: int = 0,
    store: MailStore | None = None,
) -> dict[str, Any]:
    """belt 一轮失败的告警（installer 生成单元的失败必响，G-5 统一投递）。

    内容**确定性**（不含时间戳）：同一身份同因反复失败在去重窗内收敛为
    同一封信（deduped 行回读 existing_id 仍算确认）——告警不丢也不刷屏。
    best-effort：任何异常兜住并返回 ``{"confirmed": False, ...}``，绝不
    改变 belt 自身的退出码语义。"""
    st = store or MailStore(Path(root))
    try:
        from .wake import WAKE_ALERT_RECIPIENT  # 函数级导入防循环

        recipients = [WAKE_ALERT_RECIPIENT]
    except Exception:  # noqa: BLE001 — 取不到常量时按 HS 兜底（产品口径）
        recipients = ["HS"]
    subject = f"[wake-belt-fail] {agent_id}"
    body = (
        f"[wake-belt-fail 自动告警] {agent_id} 的 belt 唤醒一轮失败：\n\n"
        f"  失败原因: {reason}\n"
        f"  本轮认领: {claimed} 封 / 完成: {done} 封\n"
        f"  信箱根: {Path(root)}\n\n"
        "belt 已按锚A 落 fail 行并 exit 1（失败必响）；未 done 的认领已 release 回 "
        "pending（信不丢）。请检查唤醒通道（wake.json / 唤醒命令 / "
        "wake-attempts.jsonl 的 error_class 行）。此信为系统自动告警，无需回执。"
    )
    try:
        return deliver_confirmed(
            st,
            agent_id,
            recipients,
            subject,
            body,
            context=f"belt_fail:{agent_id}:{reason}",
        )
    except Exception as exc:  # noqa: BLE001 — 告警绝不打断 belt
        print(f"[agent-mailbox alerts] belt alert failed (fail-open): {exc}", file=sys.stderr)
        return {"confirmed": False, "rows": [{"error": str(exc)}]}


def main(argv: list[str] | None = None) -> int:
    """CLI：``python -m agent_mailbox.alerts belt-fail --root R --agent A
    --reason timeout|no_progress [--claimed N] [--done N]``（belt 模板调用）。"""
    ap = argparse.ArgumentParser(
        prog="agent_mailbox.alerts",
        description="告警真投递（G-5）：store.send + 回读确认，失败进审计",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    bf = sub.add_parser("belt-fail", help="belt 一轮失败的告警")
    bf.add_argument("--root", required=True)
    bf.add_argument("--agent", required=True)
    bf.add_argument("--reason", required=True)
    bf.add_argument("--claimed", type=int, default=0)
    bf.add_argument("--done", type=int, default=0)
    ns = ap.parse_args(argv)
    result = send_belt_fail_alert(
        ns.root, ns.agent, reason=str(ns.reason), claimed=int(ns.claimed), done=int(ns.done)
    )
    confirmed = bool(result.get("confirmed"))
    print(
        json.dumps(
            {
                "alert": "belt-fail",
                "agent": ns.agent,
                "confirmed": confirmed,
                "rows": result.get("rows", []),
            },
            ensure_ascii=False,
        )
    )
    # best-effort：未确认也不改 belt 退出码（审计已留痕，belt 自身语义另行判定）
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
