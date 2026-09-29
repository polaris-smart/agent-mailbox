"""t-65（0.7.6 收口批第 2 批）G-5: 告警真投递 — store.send + 回读确认.

病灶（HS 实测）：WB 脚本反复写 ``alert_to=HS``，HS 箱 0 封告警信——
只写日志没投递、没人回读。本文件验证：

1. **回读成功才算投递**：send 后用 ``get_letter`` 回读信在且 status 非终态
   （pending/acked）才算 confirmed；
2. **回读失败走审计 + 重试标记**：回读失败 ⇒ ``audit.log`` 落
   ``alert_delivery_unconfirmed``，source 信**不标** ``wake_alert`` ⇒
   下一轮 drain 重投（信不丢语义）；
3. **doctor ⑨ 可达性三态**（注册+可写 / 未注册 / 目录缺失），只读探测；
4. belt-fail 告警确定性内容（同因重发走去重窗，防刷屏）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from agent_mailbox.alerts import AUDIT_ACTION, deliver_confirmed, send_belt_fail_alert
from agent_mailbox.cli import DOCTOR_TITLES, doctor_report
from agent_mailbox.store import MailStore
from agent_mailbox.wake import send_wake_alert


def _mkroot(tmp_path: Path) -> tuple[Path, MailStore]:
    root = tmp_path / "mail"
    st = MailStore(root)
    return root, st


def _handwrite_letter(root: Path, box: str, *, fid: str, lid: str = "x1") -> None:
    """手工落一封信（模拟历史来信/外部信，绕过 send 的注册校验）。"""
    inbox = root / "inbox" / box
    inbox.mkdir(parents=True, exist_ok=True)
    letter = {
        "id": lid,
        "from": fid,
        "to": box,
        "subject": "s",
        "body": "b",
        "status": "pending",
    }
    (inbox / f"{lid}.json").write_text(
        json.dumps(letter, ensure_ascii=False, indent=1), encoding="utf-8"
    )


# ------------------------------------------------------- ① 回读成功才算投递


def test_deliver_confirmed_success(tmp_path):
    """已注册收件人：send 落箱 + get_letter 回读 status=pending ⇒ confirmed。"""
    root, st = _mkroot(tmp_path)
    st.register("HS")
    st.register("ZC")
    result = deliver_confirmed(st, "ZC", ["HS"], "[wake-fail] s", "body", context="t")
    assert result["confirmed"] is True
    assert all(r["confirmed"] for r in result["rows"])
    letters = [
        json.loads(p.read_text(encoding="utf-8")) for p in (root / "inbox" / "HS").glob("*.json")
    ]
    assert len(letters) == 1 and letters[0]["status"] == "pending"


def test_deliver_confirmed_unregistered_is_unconfirmed(tmp_path):
    """未注册收件人：store.send 拒投（默认 reject）⇒ unconfirmed + 审计留痕。"""
    root, st = _mkroot(tmp_path)
    st.register("ZC")  # 只注册发件方；HS 不注册
    result = deliver_confirmed(st, "ZC", ["HS"], "subj", "body", context="t")
    assert result["confirmed"] is False
    assert result["rows"] and "error" in result["rows"][0]
    audit = (root / "audit.log").read_text(encoding="utf-8")
    assert AUDIT_ACTION in audit and "HS" in audit


def test_deliver_confirmed_deduped_still_confirmed(tmp_path):
    """去重行（existing_id）：同 hash 未终态信已在箱 = 收件人已持有 ⇒ 回读
    existing_id 确认（告警不丢也不刷屏）。"""
    root, st = _mkroot(tmp_path)
    st.register("HS")
    st.register("ZC")
    first = deliver_confirmed(st, "ZC", ["HS"], "subj", "body", context="t1")
    assert first["confirmed"] is True
    second = deliver_confirmed(st, "ZC", ["HS"], "subj", "body", context="t2")
    assert second["confirmed"] is True
    rows = [r for r in second["rows"] if r.get("deduped")]
    assert rows and rows[0]["existing_id"]
    # 箱里始终只有一封（去重窗收敛）
    assert len(list((root / "inbox" / "HS").glob("*.json"))) == 1


# --------------------------------------- ② 回读失败走审计 + 下一轮重试标记


def test_send_wake_alert_unconfirmed_not_marked_and_retried(tmp_path, monkeypatch):
    """回读失败：不标 wake_alert（下一轮重投）+ 审计留痕；恢复后重投成功。"""
    root, st = _mkroot(tmp_path)
    st.register("HS")
    st.register("ZC")
    st.send("HS", "ZC", "please handle", "body")
    letter_path = next((root / "inbox" / "ZC").glob("*.json"))
    msg = json.loads(letter_path.read_text(encoding="utf-8"))

    def _broken(agent_id, msg_id):
        raise RuntimeError("readback broken")

    monkeypatch.setattr(st, "get_letter", _broken)
    sent = send_wake_alert(st, root, "ZC", msg)
    assert sent is False  # 回读失败 ≠ 投递成功
    # source 信不标 wake_alert ⇒ 下一轮 drain 会重投（信不丢语义）
    msg_after = json.loads(letter_path.read_text(encoding="utf-8"))
    assert "wake_alert" not in [e.get("action") for e in (msg_after.get("handled_log") or [])]
    audit = (root / "audit.log").read_text(encoding="utf-8")
    assert AUDIT_ACTION in audit

    # 恢复回读 ⇒ 下一轮重投成功（去重行回读 existing_id 确认）且此时才标记
    monkeypatch.undo()
    sent2 = send_wake_alert(st, root, "ZC", json.loads(letter_path.read_text(encoding="utf-8")))
    assert sent2 is True
    msg_final = json.loads(letter_path.read_text(encoding="utf-8"))
    assert "wake_alert" in [e.get("action") for e in (msg_final.get("handled_log") or [])]
    # HS 箱里告警信始终只有一封（去重窗）
    assert len(list((root / "inbox" / "HS").glob("*.json"))) == 1


def test_send_wake_alert_confirmed_marks_handled(tmp_path):
    """回读确认成功：mark wake_alert（告警至多一次），boss 席零信（存量语义）。"""
    root, st = _mkroot(tmp_path)
    for aid in ("boss", "HS", "ZC"):
        st.register(aid)
    st.send("HS", "ZC", "please handle", "body")
    letter_path = next((root / "inbox" / "ZC").glob("*.json"))
    msg = json.loads(letter_path.read_text(encoding="utf-8"))
    assert send_wake_alert(st, root, "ZC", msg) is True
    assert not list((root / "inbox" / "boss").glob("*.json")), "boss 席不收告警"
    msg_final = json.loads(letter_path.read_text(encoding="utf-8"))
    assert "wake_alert" in [e.get("action") for e in (msg_final.get("handled_log") or [])]
    # 再次调用：已标记 ⇒ 不重发
    assert (
        send_wake_alert(st, root, "ZC", json.loads(letter_path.read_text(encoding="utf-8")))
        is False
    )


# --------------------------------------------------- ③ doctor ⑨ 可达性三态


def _check(report: dict, cid: str) -> dict:
    return next(c for c in report["checks"] if c["id"] == cid)


def test_doctor_titles_contain_alert_reach():
    assert any(cid == "alert_reach" for cid, _ in DOCTOR_TITLES)


def test_doctor_alert_reach_reachable(tmp_path):
    """可达态：收件人已注册 + inbox 存在可写 ⇒ ⑨ 绿。"""
    root, st = _mkroot(tmp_path)
    st.register("HS")
    st.register("ZC")
    st.send("HS", "ZC", "hi", "b")  # 发件人 HS 出现在信里
    report = doctor_report(root, wb_wake_log=tmp_path / "no.log")
    c = _check(report, "alert_reach")
    assert c["ok"] is True and "未真发信" in c["detail"]


def test_doctor_alert_reach_unregistered(tmp_path):
    """未注册态：信里出现未注册发件人 ⇒ ⑨ 红并点名。"""
    root, st = _mkroot(tmp_path)
    st.register("HS")
    st.register("ZC")
    _handwrite_letter(root, "ZC", fid="GHOST")
    report = doctor_report(root, wb_wake_log=tmp_path / "no.log")
    c = _check(report, "alert_reach")
    assert c["ok"] is False and "GHOST" in c["detail"] and "未注册" in c["detail"]
    assert "setup --agent" in c["next_step"]


def test_doctor_alert_reach_dir_missing(tmp_path):
    """目录缺失态：已注册但 inbox 被删 ⇒ ⑨ 红并点名。"""
    root, st = _mkroot(tmp_path)
    st.register("HS")
    st.register("MISS")
    _handwrite_letter(root, "ZC", fid="MISS")
    shutil.rmtree(root / "inbox" / "MISS")
    report = doctor_report(root, wb_wake_log=tmp_path / "no.log")
    c = _check(report, "alert_reach")
    assert c["ok"] is False and "MISS" in c["detail"] and "目录缺失" in c["detail"]


def test_doctor_alert_reach_no_sample(tmp_path):
    """无样本态（无信件且无 registry）：不作判据，绿。"""
    root, _st = _mkroot(tmp_path)
    (root / "inbox" / "ZC").mkdir(parents=True)  # 有箱无信、无 registry
    report = doctor_report(root, wb_wake_log=tmp_path / "no.log")
    c = _check(report, "alert_reach")
    assert c["ok"] is True and "无告警收件人样本" in c["detail"]


# ----------------------------------------------------- ④ belt-fail 告警


def test_belt_fail_alert_confirmed_and_deduped(tmp_path):
    """belt 失败告警：真落 HS 箱 + 回读确认；同因重发去重收敛（防刷屏）。"""
    root, _st = _mkroot(tmp_path)
    st = MailStore(root)
    st.register("HS")
    st.register("ZC")
    r1 = send_belt_fail_alert(root, "ZC", reason="timeout", claimed=2, done=0, store=st)
    assert r1["confirmed"] is True
    letters = list((root / "inbox" / "HS").glob("*.json"))
    assert len(letters) == 1
    r2 = send_belt_fail_alert(root, "ZC", reason="timeout", claimed=2, done=0, store=st)
    assert r2["confirmed"] is True  # 去重行回读 existing_id 仍算确认
    assert len(list((root / "inbox" / "HS").glob("*.json"))) == 1
    alert = json.loads(letters[0].read_text(encoding="utf-8"))
    assert "[wake-belt-fail]" in alert["subject"] and "timeout" in alert["body"]


def test_belt_fail_alert_cli(tmp_path, capsys):
    """CLI 形态（belt 模板同款调用）：exit 0 + confirmed 输出。"""
    from agent_mailbox.alerts import main as alerts_main

    root, st = _mkroot(tmp_path)
    st.register("HS")
    st.register("ZC")
    rc = alerts_main(["belt-fail", "--root", str(root), "--agent", "ZC", "--reason", "no_progress"])
    assert rc == 0
    out = capsys.readouterr().out
    assert json.loads(out)["confirmed"] is True
    assert list((root / "inbox" / "HS").glob("*.json"))
