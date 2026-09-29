"""t-56 claim-first 投递修复 — J1-J4 活体判据（HS 20260929001242 多窗重复回信）.

根因：store.claim() 原子且第二窗不可见，但三条投递路由（belt / daemon /
webhook 网关）都绕过 claim 直取信件内容 ⇒ 两窗各留一条 done、同 thread
连发多封回信。修复：三路由统一 reap→claim→只投认领信；认领不到的落
``claim_denied`` 审计（按信去重防刷屏）；认领了没投出去的释放回 pending
（信不丢）；handled_log ``by`` 带 ``路由:会话`` 标识（J3）。

判据映射：
- J1  同一封信完整 claim→处理→done 后 handled_log 恰好 1 条 done；
      第二路由抢跑（claim 得 0 封）不产生第二个 done。
- J2  同一触发信两窗先后到达 ⇒ 只有认领得手的窗回信（回信数 ≤1）。
- J3  claimed/wake/done 条目的 by 带路由:会话标识（格式断言）。
- J4  acked>600s 未 done 的孤儿：裸 claim 得 0 封、不投、留 claim_denied；
      reap→claim 正路营救并投递恰好一次。

webhook 路由（③）的消费端在仓外网关：仓内对齐点 = ``wake claim`` /
``wake release`` 子命令 + store 原语（见 webhook.py docstring），本文件对
认领/释放通道按 belt 同款标准实测。
"""

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from agent_mailbox.store import HANDLED_INTENT, HANDLED_OUTCOME, MailStore
from agent_mailbox.wake import claim_route_mail, run_once, wake_main


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "mail"
    monkeypatch.setenv("AGENT_MAIL_HOME", str(root))
    monkeypatch.delenv("AGENT_MAIL_SESSION", raising=False)  # by 字段判定要确定
    st = MailStore(root=root)
    st.register("HS")
    st.register("ZC")
    return root, st


def cfg(root, **kw):
    base = {
        "agent_id": "ZC",
        "adapter": "generic-webhook",
        "webhook": {"url": "http://127.0.0.1:9/hook", "secret": "s"},
        "retry_interval": 0,
    }
    base.update(kw)
    from agent_mailbox.wake import WakeConfig

    return WakeConfig(base, Path(root))


class _Recorder:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def deliver(self, msg):
        self.calls.append(msg["id"])
        return self.results.pop(0) if self.results else True


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + "Z"


def _backdate_acked(root: Path, msg_id: str, seconds_ago: float) -> None:
    path = Path(root) / "inbox" / "ZC" / f"{msg_id}.json"
    m = json.loads(path.read_text(encoding="utf-8"))
    m["acked_at"] = _iso(time.time() - seconds_ago)
    path.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------- J1


def test_j1_full_claim_cycle_yields_exactly_one_done(env):
    """J1: claim→处理→done 恰好 1 条 done；第二路由抢跑（claim 得 0 封）不产生第二个 done。"""
    _root, st = env
    mid = st.send("HS", "ZC", "hello", "wake me")[0]["id"]
    # 窗 A 认领得手并处理
    claimed, denied = claim_route_mail(st, "ZC", [mid], "wake:run-a")
    assert [m["id"] for m in claimed] == [mid] and denied == []
    st.record_handled("ZC", mid, HANDLED_INTENT, session_label="wake:run-a")
    st.record_handled("ZC", mid, HANDLED_OUTCOME, session_label="wake:run-a")
    st.set_status("ZC", mid, "done")
    # 第二路由抢跑：裸 claim 与路由认领都得 0 封 → 不可能再 done
    assert st.claim("ZC") == []
    claimed_b, denied_b = claim_route_mail(st, "ZC", [mid], "wake:run-b")
    assert claimed_b == [] and denied_b == [mid]
    log = st.get_letter("ZC", mid)["handled_log"]
    dones = [e for e in log if e.get("action") == "done"]
    assert len(dones) == 1, f"done must land exactly once, got {len(dones)}: {log}"


def test_j1_second_route_round_cannot_redeliver_done_letter(env):
    """J1 补充：done 后整轮路由重跑（daemon）零投递——done 语义不被第二路由重放。"""
    root, st = env
    mid = st.send("HS", "ZC", "hello", "wake me")[0]["id"]
    claim_route_mail(st, "ZC", [mid], "wake:run-a")
    st.set_status("ZC", mid, "done")
    rec = _Recorder([True])
    stats = run_once(root, cfg(root), adapter=rec)
    assert stats["woke"] == 0 and rec.calls == []
    assert len([e for e in st.get_letter("ZC", mid)["handled_log"] if e["action"] == "done"]) == 1


# --------------------------------------------------------------------- J2


def test_j2_two_windows_sequential_only_claimer_replies(env):
    """J2: 两窗先后到达——只有认领得手的窗回信（回信数 ≤1）。"""
    root, st = env
    st.send("HS", "ZC", "hello", "wake me")
    rec_a, rec_b = _Recorder([True]), _Recorder([True])
    s1 = run_once(root, cfg(root), adapter=rec_a)
    s2 = run_once(root, cfg(root), adapter=rec_b)
    assert s1["woke"] == 1 and s2["woke"] == 0
    assert rec_a.calls and rec_b.calls == []
    total_replies = len(rec_a.calls) + len(rec_b.calls)
    assert total_replies <= 1


def test_j2_two_windows_race_on_same_trigger_letter(env):
    """J2: 两窗都扫到同一封候选信（触发条件都成立），先认领者投、后到者审计后收手。"""
    _root, st = env
    mid = st.send("HS", "ZC", "hello", "wake me")[0]["id"]
    claimed_a, denied_a = claim_route_mail(st, "ZC", [mid], "wake:win-a")
    claimed_b, denied_b = claim_route_mail(st, "ZC", [mid], "wake:win-b")
    assert [m["id"] for m in claimed_a] == [mid] and denied_a == []
    assert claimed_b == [] and denied_b == [mid]
    # 窗 B claimed 为空 ⇒ 不投、不拉会话：回信总数 ≤ 1
    deliveries = [m["id"] for m in claimed_a] + [m["id"] for m in claimed_b]
    assert deliveries == [mid]
    log = st.get_letter("ZC", mid)["handled_log"]
    denied_entries = [e for e in log if e["action"] == "claim_denied"]
    assert len(denied_entries) == 1 and denied_entries[0]["by"] == "wake:win-b"


# --------------------------------------------------------------------- J3


def test_j3_by_labels_carry_route_session_identity(env, monkeypatch):
    """J3: claimed/wake 的 by 带 路由:会话 标识；done 的 by 带会话标识（可审计）。"""
    root, st = env
    st.send("HS", "ZC", "hello", "wake me")
    rec = _Recorder([True])
    run_once(root, cfg(root), adapter=rec, session_label="wake:run-777")
    mid = rec.calls[0]
    log = st.get_letter("ZC", mid)["handled_log"]
    claimed_by = [e["by"] for e in log if e["action"] == "claimed"]
    wake_by = [e["by"] for e in log if e["action"] == "wake"]
    assert claimed_by == ["wake:run-777"]  # 认领条目带 路由:会话
    assert wake_by == ["wake:run-777"]  # 投递留痕同标识
    assert re.fullmatch(r"wake:.+", claimed_by[0])  # 格式断言
    # done 条目带会话标识（AGENT_MAIL_SESSION 机制）
    monkeypatch.setenv("AGENT_MAIL_SESSION", "sess-hc-42")
    st.set_status("ZC", mid, "done")
    done_entry = [e for e in st.get_letter("ZC", mid)["handled_log"] if e["action"] == "done"][-1]
    assert done_entry["by"] == "sess-hc-42"


def test_j3_belt_claim_channel_stamps_belt_label(env, tmp_path):
    """J3: belt 认领通道（wake claim --label belt:<pid>）落 by=belt:<pid>。"""
    root, st = env
    st.send("HS", "ZC", "belt me", "b")
    claim_file = tmp_path / "claim.json"
    wake_main(
        [
            "claim",
            "--agent",
            "ZC",
            "--root",
            str(root),
            "--label",
            "belt:4242",
            "--out",
            str(claim_file),
        ]
    )
    log = st.list_messages("ZC", status="acked")[0]["handled_log"]
    assert [e["by"] for e in log if e["action"] == "claimed"] == ["belt:4242"]
    assert re.fullmatch(r"belt:.+", log[-1]["by"])


# --------------------------------------------------------------------- J4


def test_j4_stale_acked_orphan_bare_claim_denied_then_rescued_by_reap_claim(env):
    """J4 活体: acked>600s 未 done 的孤儿信——裸 claim 得 0 封、不投、留
    claim_denied；reap→claim 正路营救并投递恰好一次。"""
    root, st = env
    mid = st.send("HS", "ZC", "orphan", "rescue me")[0]["id"]
    st.check("ZC")  # pending -> acked（前任 handler 死在这）
    _backdate_acked(root, mid, seconds_ago=700)
    # 裸 claim（不带 reap）：0 封
    assert st.claim("ZC") == []
    # 路由认领阶段（不带 reap）：认领 0 封 → 不投、留 claim_denied
    claimed, denied = claim_route_mail(st, "ZC", [mid], "wake:bare")
    assert claimed == [] and denied == [mid]
    log = st.get_letter("ZC", mid)["handled_log"]
    bare_denials = [e for e in log if e["action"] == "claim_denied"]
    assert len(bare_denials) == 1 and bare_denials[0]["by"] == "wake:bare"
    assert not any(e["action"] == "wake" for e in log)  # 没投
    # 正路：reap→claim 的完整路由轮 → 营救并投递恰好一次
    rec = _Recorder([True])
    stats = run_once(root, cfg(root), adapter=rec, now=time.time())
    assert stats["woke"] == 1 and rec.calls == [mid]
    letter = st.get_letter("ZC", mid)
    assert letter["status"] == "acked"  # 认领态，等 handler 收尾
    actions = [e["action"] for e in letter["handled_log"]]
    assert "reclaimed" in actions  # reap 营救留痕
    assert "wake" in actions  # 投递留痕
    # claim_denied 仍只有 1 条——营救后不新增
    assert len([e for e in letter["handled_log"] if e["action"] == "claim_denied"]) == 1


def test_j4_reap_rescue_survives_claim_release_cycle(env):
    """J4 补充：营救投递失败 → 认领释放回 pending → 下一轮再营救（信不丢）。

    t-62 起 digest_fallback 默认开（失败降级 digest 消化信件）——本测试专测
    release/retry 路径本身，显式关掉降级。"""
    root, st = env
    mid = st.send("HS", "ZC", "orphan", "rescue me")[0]["id"]
    st.check("ZC")
    _backdate_acked(root, mid, seconds_ago=700)
    rec = _Recorder([False, False])  # 本轮投递全失败
    stats = run_once(
        root, cfg(root, retry_max=2, digest_fallback=False), adapter=rec, now=time.time()
    )
    assert stats["woke"] == 0 and stats["failed"] == 2
    letter = st.get_letter("ZC", mid)
    assert letter["status"] == "pending"  # 认领已释放
    assert any(e["action"] == "claim_released" for e in letter["handled_log"])
    rec2 = _Recorder([True])
    assert run_once(root, cfg(root), adapter=rec2, now=time.time())["woke"] == 1
    assert rec2.calls == [mid]


# --------------------------------------------------- claim_denied 防刷屏去重


def test_claim_denied_dedup_one_entry_per_letter(env):
    """防刷屏规则实测：belt 定时轮询反复撞同一封在途信，claim_denied 只落 1 条。"""
    _root, st = env
    mid = st.send("HS", "ZC", "in flight", "x")[0]["id"]
    st.claim("ZC", session_label="wake:owner")  # 别的窗认领在途
    for i in range(3):  # 三轮轮询都撞上
        claimed, denied = claim_route_mail(st, "ZC", [mid], f"wake:poll-{i}")
        assert claimed == [] and denied == [mid]
    log = st.get_letter("ZC", mid)["handled_log"]
    denials = [e for e in log if e["action"] == "claim_denied"]
    assert len(denials) == 1 and denials[0]["by"] == "wake:poll-0"  # 首条留痕，其后去重
    assert st.record_claim_denied("ZC", mid, session_label="wake:poll-x") is False  # store 直调同规


def test_claim_denied_by_carries_route_label(env):
    """claim_denied 的 by 必须带 路由:会话 标识（J3 审计面）。"""
    _root, st = env
    mid = st.send("HS", "ZC", "busy", "x")[0]["id"]
    st.claim("ZC", session_label="wake:owner")
    claim_route_mail(st, "ZC", [mid], "webhook:req-9f2")
    entry = next(
        e for e in st.get_letter("ZC", mid)["handled_log"] if e["action"] == "claim_denied"
    )
    assert entry["by"] == "webhook:req-9f2"
    assert re.fullmatch(r"webhook:.+", entry["by"])


# ------------------------------------------------- wake claim / release CLI


def test_wake_claim_cli_payload_is_the_delivery(env, tmp_path):
    """claim 通道输出即投递物：每封带 id/from/subject/body；认领落盘 acked+by。"""
    root, st = env
    mid = st.send("HS", "ZC", "claim me", "body here")[0]["id"]
    claim_file = tmp_path / "claim.json"
    wake_main(
        [
            "claim",
            "--agent",
            "ZC",
            "--root",
            str(root),
            "--label",
            "belt:4242",
            "--out",
            str(claim_file),
        ]
    )
    assert (claim_file.stat().st_mode & 0o077) == 0  # 信体落盘 0600
    data = json.loads(claim_file.read_text(encoding="utf-8"))
    assert data["agent"] == "ZC" and data["label"] == "belt:4242"
    assert [m["id"] for m in data["claimed"]] == [mid]
    letter0 = data["claimed"][0]
    assert letter0["from"] == "HS" and letter0["subject"] == "claim me"
    assert letter0["body"] == "body here"
    disk = st.get_letter("ZC", mid)
    assert disk["status"] == "acked" and disk["claimed_by"] == "ZC"
    assert disk["handled_log"][-1]["action"] == "claimed"
    assert disk["handled_log"][-1]["by"] == "belt:4242"


def test_wake_claim_cli_zero_claim_quiet_when_inflight(env, tmp_path):
    """认领 0 封 ⇒ claimed=[]（调用方不投、不拉会话）。别的窗已认领在途的信
    不是本路由候选（扫描只见 pending），不误记 claim_denied——denied 只在
    扫描→认领窗口的真竞争里出现（见 J2 race 用例）。"""
    root, st = env
    mid = st.send("HS", "ZC", "claim me", "b")[0]["id"]
    claim_route_mail(st, "ZC", [mid], "wake:other-window")  # 别的窗先认领在途
    claim2 = tmp_path / "claim2.json"
    wake_main(
        ["claim", "--agent", "ZC", "--root", str(root), "--label", "belt:51", "--out", str(claim2)]
    )
    data = json.loads(claim2.read_text(encoding="utf-8"))
    assert data["claimed"] == [] and data["denied"] == []
    disk = st.get_letter("ZC", mid)
    assert disk["status"] == "acked"  # 在途信原样，不被本路由惊动
    assert not [e for e in disk["handled_log"] if e["action"] == "claim_denied"]


def test_wake_claim_cli_denied_on_scan_claim_race(env, tmp_path, monkeypatch):
    """CLI 扫描到 pending 候选后、认领前被别的窗抢走 → denied=[id] 且去重。"""
    root, st = env
    mid = st.send("HS", "ZC", "claim me", "b")[0]["id"]
    real_claim = MailStore.claim

    def racing_claim(self, agent_id, **kw):
        # 模拟竞争：扫描已完成，认领执行前信被别的窗真抢走（翻 acked），
        # 本窗的 claim 空手而归
        only = kw.get("only")
        if only and mid in {str(i) for i in only}:
            real_claim(self, agent_id, session_label="wake:winner")  # 结果归别的窗
            return []
        return real_claim(self, agent_id, **kw)

    monkeypatch.setattr(MailStore, "claim", racing_claim)
    claim_file = tmp_path / "claim.json"
    wake_main(
        [
            "claim",
            "--agent",
            "ZC",
            "--root",
            str(root),
            "--label",
            "belt:51",
            "--out",
            str(claim_file),
        ]
    )
    data = json.loads(claim_file.read_text(encoding="utf-8"))
    assert data["claimed"] == [] and data["denied"] == [mid]
    disk = st.get_letter("ZC", mid)
    denials = [e for e in disk["handled_log"] if e["action"] == "claim_denied"]
    assert len(denials) == 1 and denials[0]["by"] == "belt:51"


def test_wake_release_cli_returns_undone_only_and_respects_label(env, tmp_path, capsys):
    """释放通道：done 的跳过、未完成的回 pending；认领权被别的窗接手的不放。"""
    root, st = env
    a = st.send("HS", "ZC", "will finish", "a")[0]["id"]
    b = st.send("HS", "ZC", "stays claimed", "b")[0]["id"]
    claim_file = tmp_path / "claim.json"
    wake_main(
        [
            "claim",
            "--agent",
            "ZC",
            "--root",
            str(root),
            "--label",
            "belt:51",
            "--out",
            str(claim_file),
        ]
    )
    st.set_status("ZC", a, "done")  # turn 只做完 A
    wake_main(
        [
            "release",
            "--agent",
            "ZC",
            "--root",
            str(root),
            "--label",
            "belt:51",
            "--claim-file",
            str(claim_file),
        ]
    )
    res = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert res["released"] == [b] and res["skipped"] == [a]
    lb = st.get_letter("ZC", b)
    assert lb["status"] == "pending" and "claimed_by" not in lb and "acked_at" not in lb
    released_entry = lb["handled_log"][-1]
    assert released_entry["action"] == "claim_released" and released_entry["by"] == "belt:51"
    assert st.get_letter("ZC", a)["status"] == "done"  # done 信不动
    # 认领权被别的窗接手后，旧标识的 release 放不动它
    st.claim("ZC", session_label="wake:other")
    wake_main(["release", "--agent", "ZC", "--root", str(root), "--label", "belt:51", "--ids", b])
    res2 = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert res2["skipped"] == [b] and res2["released"] == []
    assert st.get_letter("ZC", b)["status"] == "acked"


# ------------------------------------------------------- daemon 路由语义保持


def test_daemon_failure_releases_claim_letter_still_drainable(env):
    """投递全失败 → 认领释放回 pending（信不丢）→ 下次触发重投并留痕。

    t-62 起 digest_fallback 默认开——本测试专测 release/retry 路径本身，
    显式关掉降级。"""
    root, st = env
    mid = st.send("HS", "ZC", "hello", "wake me")[0]["id"]
    stats = run_once(
        root, cfg(root, retry_max=2, digest_fallback=False), adapter=_Recorder([False, False])
    )
    assert stats["failed"] == 2 and stats["woke"] == 0
    letter = st.get_letter("ZC", mid)
    assert letter["status"] == "pending"
    assert not any(e["action"] == "wake" for e in letter["handled_log"])
    assert any(e["action"] == "claim_released" for e in letter["handled_log"])
    rec2 = _Recorder([True])
    assert run_once(root, cfg(root), adapter=rec2)["woke"] == 1
    assert rec2.calls == [mid]


def test_skipped_woken_branch_after_reclaim_back_to_pending(env):
    """认领后信回 pending（reap）时，wake 留痕仍拦住二次投递（skipped_woken 支路）。"""
    root, st = env
    st.send("HS", "ZC", "hello", "wake me")
    assert run_once(root, cfg(root), adapter=_Recorder([True]))["woke"] == 1
    st.reap_stale_acked("ZC", ttl_seconds=0)  # handler 死在 check 前：认领回 pending
    assert st.list_messages("ZC", status="pending")
    rec = _Recorder([True])
    stats = run_once(root, cfg(root), adapter=rec)
    assert stats["skipped_woken"] == 1 and stats["woke"] == 0 and rec.calls == []


# --------------------------------------------------------------- belt 脚本


def test_belt_script_is_claim_first():
    """scripts/wake-zc.sh：bash 语法过 + claim-first 三件套齐备 + 旧数数模式移除。"""
    script = Path(__file__).resolve().parents[1] / "scripts" / "wake-zc.sh"
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available")
    proc = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    text = script.read_text(encoding="utf-8")
    assert "agent_mailbox.wake claim" in text  # 先认领：claim 结果即投递物
    assert '"status": *"pending"' not in text  # 旧的 grep 数 pending 直读模式已移除
    assert "AGENT_MAIL_CLAIM_FILE" in text  # drain turn 吃认领文件
    assert "agent_mailbox.wake release" in text  # 信不丢：释放未完成认领
    assert "belt:$$" in text  # by 带路由:会话标识（J3）
