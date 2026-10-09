"""B-side wake delivery: marker file + optional host hook, and the launchd unit."""

from __future__ import annotations

import stat
from types import SimpleNamespace

from agent_mailbox.workbench_wake import (
    PLIST_LABEL,
    hook_deliver,
    install_plist,
    plist_text,
    uninstall_plist,
)


def test_marker_is_written_even_without_a_hook(tmp_path):
    deliver = hook_deliver(tmp_path)
    assert deliver("e1", {"reason": "new_mail", "unread": 2, "fresh": ["m1"]}) is False
    markers = list((tmp_path / "wake" / "wake-outbox").glob("wake-e1-*.json"))
    assert len(markers) == 1, "a wake marker must be auditable"
    assert "m1" in markers[0].read_text(encoding="utf-8")


def test_host_hook_runs_and_its_failure_is_reported(tmp_path):
    calls = []

    def ok(args, **_kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0)

    hook = tmp_path / "wake" / "wake-hook.sh"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
    deliver = hook_deliver(tmp_path, runner=ok)
    assert deliver("e1", {"reason": "new_mail", "fresh": ["m1"], "unread": 1}) is True
    assert calls and calls[0][1] == "e1", "the hook must receive the employee id"

    def bad(args, **_kwargs):
        return SimpleNamespace(returncode=1)

    assert (
        hook_deliver(tmp_path, runner=bad)(
            "e1", {"reason": "new_mail", "fresh": ["m2"], "unread": 1}
        )
        is False
    ), "a failing hook must be reported so the watermark is NOT advanced"


def test_plist_install_and_uninstall(tmp_path):
    unit = install_plist(tmp_path, tmp_path, interval=30, executable="/usr/local/bin/agent-mailbox")
    assert unit.name == f"{PLIST_LABEL}.plist" and unit.is_file()
    text = unit.read_text(encoding="utf-8")
    assert "<string>wake</string>" in text and "<string>--once</string>" in text, (
        "unit must run wake --once"
    )
    assert "<integer>30</integer>" in text, "interval must be honoured"
    assert uninstall_plist(tmp_path) is True
    assert uninstall_plist(tmp_path) is False, "uninstall must be idempotent"
    assert PLIST_LABEL in plist_text(tmp_path)


def test_failed_hook_releases_the_claim_so_next_round_retries(tmp_path):
    """变异判据 ✓（flow-product 建议 ✓）：hook **失败** ⇒ 释放认领 ⇒ **下一轮必须再调** ✓。

    这正是它 slipped 的原因 ✗：全 `tests/` 当时没有任何用例覆盖"失败后下轮补叫" ✓
    （原来失败不释放 ⇒ 下一轮把"有 claim 无 .ok"当成"别人已叫过" ⇒ `return True`
     ⇒ **没投递却推进水位线** ⇒ 这封信**永久被吞** ✗ 还被记成"已叫" ✓）
    """
    import os

    hook = tmp_path / "wake" / "wake-hook.sh"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    os.chmod(hook, 0o755)
    calls: list[int] = []

    def runner(_cmd, **_kw):
        calls.append(1)
        return type("D", (), {"returncode": 1})()

    deliver = hook_deliver(tmp_path, runner=runner)
    decision = {"reason": "new_mail", "unread": 1, "fresh": ["m1"]}
    assert deliver("e1", decision) is False, "失败必须返回 False ⇒ 水位线不推进 ✓"
    assert deliver("e1", decision) is False, (
        "第二轮必须**真的再调** hook（下轮补叫 ✓），不是当场认输 ✗"
    )
    assert len(calls) == 2, f"hook 应被调 2 次，实测 {len(calls)}"
    assert not list((tmp_path / "wake" / "claims").glob("*.ok")), "失败不得盖已交付标记 ✓"
    assert not (tmp_path / "wake" / "claims" / "m1").exists(), (
        "失败必须**释放**认领 ✓（否则永久抑制这封信 ✗）"
    )


def test_claims_path_as_file_abandons_the_round_without_traceback(tmp_path):
    """变异判据 ✓：`claims` 被占成**普通文件** ⇒ 放弃本轮 ✓ · **不得 traceback** ✗。"""
    (tmp_path / "wake").mkdir(parents=True, exist_ok=True)
    (tmp_path / "wake" / "claims").write_text("x", encoding="utf-8")
    deliver = hook_deliver(tmp_path)
    assert deliver("e1", {"reason": "new_mail", "unread": 1, "fresh": ["m1"]}) is False


def test_same_fresh_twice_delivers_once(tmp_path):
    """同 `fresh` 连投两次 ⇒ **只留一个标记** ✓ 且第二次不调 hook ✓（幂等 ✓ flow-product 建议 ✓）。"""
    import os

    hook = tmp_path / "wake" / "wake-hook.sh"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(hook, 0o755)
    calls: list[int] = []

    def runner(_cmd, **_kw):
        calls.append(1)
        return type("D", (), {"returncode": 0})()

    deliver = hook_deliver(tmp_path, runner=runner)
    decision = {"reason": "new_mail", "unread": 1, "fresh": ["m5"]}
    assert deliver("e1", decision) is True
    assert deliver("e1", decision) is True, "同信再投 ⇒ 幂等返回 True ✓（水位线可推进 ✓）"
    markers = list((tmp_path / "wake" / "wake-outbox").glob("wake-e1-*.json"))
    assert len(markers) == 1, f"同 `fresh` 两次只该 1 个标记，实测 {len(markers)}"
    assert len(calls) == 1, "第二轮不得再调 hook ✓"


def test_no_hook_records_pending_and_never_claims_delivered(tmp_path):
    """AM-01 回归 ✓（Codex 实测：`hook_exists=false` 却 `returned_delivered=true` ✗ 假成功）。

    没有可执行 hook ⇒ 只能记「**待办、未接通**」✗ 绝不许盖 `.ok` 冒充已交付 ✓
    （水位线可以推进 ✓ —— 否则同一封信每轮都刷 ✓，但状态必须是 pending ✓）。
    """
    deliver = hook_deliver(tmp_path)
    assert deliver("e1", {"reason": "new_mail", "unread": 1, "fresh": ["m1"]}) is False
    claims = tmp_path / "wake" / "claims"
    assert not (claims / "m1.ok").exists(), "无 hook 不得记「已交付」✗"
    assert (claims / "m1.pending").exists(), "必须留下**待办**标记 ✓"


def test_stale_claim_without_receipt_is_unknown_not_retried(tmp_path):
    """AM-02 contract change: an expired lease with NO receipt must never be retried.

    This test used to assert re-delivery; Codex showed that retrying an unknown
    outcome produces duplicate external effects (2 measured). Now: mark .unknown,
    surface it, never auto-retry.
    """
    import os
    import time

    from agent_mailbox.workbench_wake import WAKE_LEASE_SECONDS

    claims = tmp_path / "wake" / "claims"
    claims.mkdir(parents=True)
    hook = tmp_path / "wake" / "wake-hook.sh"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(hook, 0o755)
    claim = claims / "m2"
    claim.write_text("", encoding="utf-8")
    os.utime(claim, (time.time() - WAKE_LEASE_SECONDS - 10,) * 2)
    calls: list[int] = []
    deliver = hook_deliver(
        tmp_path, runner=lambda _c, **_k: (calls.append(1), type("D", (), {"returncode": 0})())[1]
    )
    assert deliver("e1", {"reason": "new_mail", "unread": 1, "fresh": ["m2"]}) is False
    assert calls == [], "未知交付不得重投 ✗（AM-02）"
    assert (claims / "m2.unknown").is_file(), "应标 .unknown ✓"


def test_fresh_claim_yields_to_the_other_round(tmp_path):
    """租约**内**的认领 ⇒ 让路 ✓（不抢别人的活 ✓ 避免重复启动工作 ✗）。"""
    import os

    claims = tmp_path / "wake" / "claims"
    claims.mkdir(parents=True)
    (claims / "m3").write_text("", encoding="utf-8")
    hook = tmp_path / "wake" / "wake-hook.sh"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(hook, 0o755)
    calls: list[int] = []
    deliver = hook_deliver(
        tmp_path, runner=lambda _c, **_k: (calls.append(1), type("D", (), {"returncode": 0})())[1]
    )
    assert deliver("e1", {"reason": "new_mail", "unread": 1, "fresh": ["m3"]}) is False
    assert calls == [], "不得重复启动工作 ✗"


def test_pending_is_redelivered_once_a_hook_appears(tmp_path):
    """AM-01 recovery (Codex repro): no hook at first, then a hook appears.

    The watermark was already advanced for that letter, so `fresh` is empty on the
    second round -- recovery must NOT depend on `fresh`, or the notification is lost
    forever. Acceptance: host called exactly once, then never again.
    """
    import os

    from agent_mailbox.workbench_wake import hook_deliver

    deliver_without_hook = hook_deliver(tmp_path)
    assert (
        deliver_without_hook(
            "employee_demo", {"reason": "new_mail", "unread": 1, "fresh": ["m-alpha"]}
        )
        is False
    )
    claims = tmp_path / "wake" / "claims"
    pending = claims / "m-alpha.pending"
    assert pending.is_file(), "未接通必须留待办 ✓"
    assert pending.read_text(encoding="utf-8").strip() == "employee_demo", "待办必须记下归属员工 ✓"

    hook = tmp_path / "wake" / "wake-hook.sh"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(hook, 0o755)
    calls: list[int] = []
    deliver_with_hook = hook_deliver(
        tmp_path, runner=lambda _c, **_k: (calls.append(1), type("D", (), {"returncode": 0})())[1]
    )
    assert (
        deliver_with_hook("employee_demo", {"reason": "recover", "unread": 0, "fresh": []}) is True
    )
    assert len(calls) == 1, (
        f"接通后必须**补投 1 次** ✓（Codex 实测之前是 0 次 ✗，实测 {len(calls)}）"
    )
    assert (claims / "m-alpha.ok").is_file(), "补投成功必须记已交付 ✓"
    assert not pending.exists(), "补投成功必须撤待办 ✓（否则每轮重复投 ✗）"
    calls.clear()
    deliver_with_hook("employee_demo", {"reason": "again", "unread": 0, "fresh": []})
    assert calls == [], "同一封信不得重复补投 ✓"


def test_unknown_delivery_is_not_retried(tmp_path):
    """AM-02 regression (Codex repro): a crashed receipt must not cause a second effect.

    Codex measured TWO simulated external effects: the host had received the wake but
    the program crashed before writing the receipt, so the lease expired and it retried.
    A lease fixes "stuck forever" but never answers "did it already run?".
    Rule: unknown outcome => mark .unknown, surface it, NEVER auto-retry.
    """
    import os
    import time

    from agent_mailbox.workbench_wake import WAKE_LEASE_SECONDS

    claims = tmp_path / "wake" / "claims"
    claims.mkdir(parents=True)
    hook = tmp_path / "wake" / "wake-hook.sh"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(hook, 0o755)
    claim = claims / "m-delta"
    claim.write_text("", encoding="utf-8")
    (claims / "m-delta.pending").write_text("employee_demo", encoding="utf-8")
    os.utime(claim, (time.time() - WAKE_LEASE_SECONDS - 10,) * 2)

    calls: list[list[str]] = []
    deliver = hook_deliver(
        tmp_path, runner=lambda c, **_k: (calls.append(c), type("D", (), {"returncode": 0})())[1]
    )
    assert deliver("employee_demo", {"reason": "retry", "unread": 0, "fresh": []}) is False
    assert calls == [], f"未知交付**不得盲重投** ✗（Codex 实测 2 次外部效果，实测 {len(calls)}）"
    assert (claims / "m-delta.unknown").is_file(), "必须标 .unknown ✓ 交人核 ✓"
    assert not (claims / "m-delta.ok").exists(), "未知交付不得记已交付 ✗"


def test_hook_receives_a_delivery_id(tmp_path):
    """The host must receive a delivery id so it can write a receipt (AM-02 protocol)."""
    import os

    hook = tmp_path / "wake" / "wake-hook.sh"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(hook, 0o755)
    seen: list[list[str]] = []
    deliver = hook_deliver(
        tmp_path, runner=lambda c, **_k: (seen.append(c), type("D", (), {"returncode": 0})())[1]
    )
    assert (
        deliver("employee_demo", {"reason": "new_mail", "unread": 1, "fresh": ["m-zeta"]}) is True
    )
    assert seen and len(seen[0]) == 3, f"必须传 投递 id 作为第三个参数 ✓（实测 {seen}）"
    assert seen[0][2].startswith("wake-employee_demo-"), "投递 id 应能对上 outbox 标记 ✓"


def test_receipt_written_by_delivery_id_confirms_delivery(tmp_path):
    """Codex gap 1: the host writes the receipt under the id it RECEIVED (the delivery id).

    I looked it up by message id, so a real receipt was never found and the delivery was
    misjudged as unknown. Acceptance: recovery confirms delivery, calls the host 0 times
    and writes .ok.
    """
    import os
    import time

    from agent_mailbox.workbench_wake import WAKE_LEASE_SECONDS

    hook = tmp_path / "wake" / "wake-hook.sh"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(hook, 0o755)
    seen: list[str] = []
    hook_deliver(
        tmp_path, runner=lambda c, **_k: (seen.append(c[2]), type("D", (), {"returncode": 0})())[1]
    )("e1", {"reason": "new_mail", "unread": 1, "fresh": ["m-r"]})
    (tmp_path / "wake" / "receipts").mkdir()
    (tmp_path / "wake" / "receipts" / seen[0]).write_text("ok", encoding="utf-8")
    claims = tmp_path / "wake" / "claims"
    (claims / "m-r").write_text("", encoding="utf-8")
    (claims / "m-r.ok").unlink(missing_ok=True)
    os.utime(claims / "m-r", (time.time() - WAKE_LEASE_SECONDS - 10,) * 2)

    calls: list[int] = []
    result = hook_deliver(
        tmp_path, runner=lambda _c, **_k: (calls.append(1), type("D", (), {"returncode": 0})())[1]
    )("e1", {"reason": "recover", "unread": 1, "fresh": ["m-r"]})
    assert result is True, "有回执 ⇒ 确认已交付 ✓（应 True）"
    assert calls == [], "有回执 ⇒ 不得再调宿主 ✗"
    assert (claims / "m-r.ok").is_file(), "确认已交付 ⇒ 必须写 .ok ✓"


def test_unknown_is_terminal_even_when_letter_is_fresh(tmp_path):
    """Codex gap 2: the unknown letter may still be `fresh`; the round must not count as a wake.

    blocked_unknown is computed BEFORE the loop, otherwise `if not claimed: return True`
    reports a wake that never happened (status said "叫过 1 次" while nothing was called).
    """
    import os

    hook = tmp_path / "wake" / "wake-hook.sh"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(hook, 0o755)
    deliver = hook_deliver(tmp_path, runner=lambda _c, **_k: type("D", (), {"returncode": 0})())
    assert deliver("e1", {"reason": "new_mail", "unread": 1, "fresh": ["m-x"]}) is True
    (tmp_path / "wake" / "claims" / "m-x").replace(tmp_path / "wake" / "claims" / "m-x.unknown")
    assert deliver("e1", {"reason": "retry", "unread": 1, "fresh": ["m-x"]}) is False
    assert deliver("e1", {"reason": "retry", "unread": 1, "fresh": ["m-x"]}) is False
