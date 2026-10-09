"""run_once: cold start does not wake, then exactly-once wakes per new letter."""

from __future__ import annotations

from agent_mailbox.workbench_wake import run_once

STEP = 901


def test_pending_mail_is_seen_without_counting_a_wake_then_recovers_once(tmp_path):
    from types import SimpleNamespace

    from agent_mailbox.workbench_wake import hook_deliver, load_state

    calls = []
    deliver = hook_deliver(
        tmp_path, runner=lambda *_a, **_k: calls.append(1) or SimpleNamespace(returncode=0)
    )

    def poll(now):
        return run_once(
            None,
            tmp_path,
            unread_provider=lambda *_: ["new-mail"],
            deliver=deliver,
            employee_ids=["e1"],
            cold_started=True,
            now=now,
        )

    assert poll(1000)["woke"] == []
    entry = load_state(tmp_path)["employees"]["e1"]
    assert entry == {"seen": ["new-mail"], "wakes": []}
    assert (tmp_path / "wake/claims/new-mail.pending").read_text() == "e1"
    assert not (tmp_path / "wake/claims/new-mail.ok").exists()
    assert poll(1001)["woke"] == []
    assert len(list((tmp_path / "wake/wake-outbox").glob("*.json"))) == 1
    hook = tmp_path / "wake/wake-hook.sh"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o700)
    assert poll(1002)["woke"] == ["e1"]
    assert poll(1003 + STEP)["woke"] == []
    assert calls == [1]
    assert load_state(tmp_path)["employees"]["e1"]["wakes"] == [1002]
    assert not (tmp_path / "wake/claims/new-mail.pending").exists()


def test_failed_host_does_not_mark_new_mail_seen_or_count_a_wake(tmp_path):
    from types import SimpleNamespace

    from agent_mailbox.workbench_wake import hook_deliver, load_state

    hook = tmp_path / "wake/wake-hook.sh"
    hook.parent.mkdir()
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o700)
    calls = []
    deliver = hook_deliver(
        tmp_path, runner=lambda *_a, **_k: calls.append(1) or SimpleNamespace(returncode=1)
    )
    for now in [1000, 1001]:
        result = run_once(
            None,
            tmp_path,
            unread_provider=lambda *_: ["new-mail"],
            deliver=deliver,
            employee_ids=["e1"],
            cold_started=True,
            now=now,
        )
        assert result["woke"] == []
        assert load_state(tmp_path)["employees"]["e1"] == {"seen": [], "wakes": []}
    assert calls == [1, 1]


def test_recovery_keeps_new_mail_and_receipt_names_all_delivered_ids(tmp_path):
    import json
    from types import SimpleNamespace

    from agent_mailbox.workbench_wake import hook_deliver, load_state

    inbox = ["pending-mail"]
    payloads = []

    def host(command, **_):
        marker = tmp_path / "wake/wake-outbox" / (command[2] + ".json")
        payloads.append(json.loads(marker.read_text()))
        return SimpleNamespace(returncode=0)

    def poll(now):
        return run_once(
            None,
            tmp_path,
            unread_provider=lambda *_: inbox,
            deliver=hook_deliver(tmp_path, runner=host),
            employee_ids=["e1"],
            cold_started=True,
            now=now,
        )

    assert poll(1000)["woke"] == []
    hook = tmp_path / "wake/wake-hook.sh"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o700)
    inbox.append("new-mail")
    assert poll(1001)["woke"] == ["e1"]
    assert set(payloads[0]["fresh"]) == set(inbox)
    assert set(load_state(tmp_path)["employees"]["e1"]["seen"]) == set(inbox)
    assert poll(1002 + STEP)["woke"] == []
    assert len(payloads) == 1


class Fake:
    def __init__(self, inboxes):
        self.inboxes = inboxes
        self.delivered = []

    def provider(self, _store, employee_id):
        return list(self.inboxes.get(employee_id, []))

    def deliver(self, employee_id, decision):
        self.delivered.append((employee_id, tuple(decision["fresh"])))
        return True


def test_first_run_is_cold_start_and_wakes_nobody(tmp_path):
    fake = Fake({"e1": ["old1", "old2"]})
    result = run_once(
        None,
        tmp_path,
        unread_provider=fake.provider,
        deliver=fake.deliver,
        employee_ids=["e1"],
        now=1000.0,
    )
    assert result["cold"] is True and result["woke"] == [], "cold start must not wake history"
    assert fake.delivered == []


def test_new_letter_wakes_once_then_never_again(tmp_path):
    fake = Fake({"e1": ["old1"]})
    run_once(
        None,
        tmp_path,
        unread_provider=fake.provider,
        deliver=fake.deliver,
        employee_ids=["e1"],
        now=1000.0,
    )  # 冷启动
    fake.inboxes["e1"] = ["new1", "old1"]
    first = run_once(
        None,
        tmp_path,
        unread_provider=fake.provider,
        deliver=fake.deliver,
        employee_ids=["e1"],
        now=1000.0 + STEP,
        cold_started=True,
    )
    assert first["woke"] == ["e1"], "a genuinely new letter must wake the employee"
    again = run_once(
        None,
        tmp_path,
        unread_provider=fake.provider,
        deliver=fake.deliver,
        employee_ids=["e1"],
        now=1000.0 + 2 * STEP,
        cold_started=True,
    )
    assert again["woke"] == [], "the same letter must not wake twice"
    assert len(fake.delivered) == 1


def test_failed_delivery_does_not_advance_the_watermark(tmp_path):
    fake = Fake({"e1": ["old1"]})
    run_once(
        None,
        tmp_path,
        unread_provider=fake.provider,
        deliver=fake.deliver,
        employee_ids=["e1"],
        now=0.0,
    )  # 冷启动
    fake.inboxes["e1"] = ["new1"]
    fake.deliver = lambda employee_id, decision: False  # 叫失败
    first = run_once(
        None,
        tmp_path,
        unread_provider=fake.provider,
        deliver=fake.deliver,
        employee_ids=["e1"],
        now=STEP,
        cold_started=True,
    )
    assert first["woke"] == []
    fake.deliver = Fake({"e1": []}).deliver
    retry = run_once(
        None,
        tmp_path,
        unread_provider=fake.provider,
        deliver=lambda employee_id, decision: True,
        employee_ids=["e1"],
        now=2 * STEP + 10,
        cold_started=True,
    )
    assert retry["woke"] == ["e1"], "a failed wake must be retried, not lost"


def test_two_employees_are_judged_independently(tmp_path):
    fake = Fake({"e1": ["a"], "e2": ["b"]})
    run_once(
        None,
        tmp_path,
        unread_provider=fake.provider,
        deliver=fake.deliver,
        employee_ids=["e1", "e2"],
        now=0.0,
    )
    fake.inboxes["e1"] = ["a", "a2"]
    result = run_once(
        None,
        tmp_path,
        unread_provider=fake.provider,
        deliver=fake.deliver,
        employee_ids=["e1", "e2"],
        now=STEP,
        cold_started=True,
    )
    assert result["woke"] == ["e1"], "only the employee with new mail wakes"
    assert "e2" in result["skipped"]


def test_cold_start_flag_is_explicit_not_inferred(tmp_path):
    """回归 ✓：冷启动判据必须是**显式标志** ✗ 不能看"状态里有没有员工"✓
    （实测踩到 ✓：员工为空 ⇒ 每轮都判冷启动 ⇒ **永远不叫** ✗）。"""
    fake = Fake({"e1": []})
    run_once(
        None,
        tmp_path,
        unread_provider=fake.provider,
        deliver=fake.deliver,
        employee_ids=[],
        now=0.0,
    )
    from agent_mailbox.workbench_wake import load_state

    assert load_state(tmp_path).get("cold_done") is True, "cold pass must set an explicit flag"


def test_app_does_not_force_cold_started():
    """AM-03 回归 ✓：应用**不许替持久状态决定冷启动** ✗（按 **AST 实参**判 ✓ 不靠子串 ✗）。

    ⚠️ 注意 ✓：注释里出现 `cold_started=True` 字样**不算违规** ✗（我第一版就栽在这 ✓）
    ⇒ 必须把源码**解析成 AST** 后只看 `run_once(...)` 的**关键字实参** ✓。
    """
    import ast as _ast
    import pathlib as _pathlib

    repo = _pathlib.Path(__file__).resolve().parents[1]
    source = (repo / "src" / "agent_mailbox" / "workbench.py").read_text(encoding="utf-8")
    calls = [
        node
        for node in _ast.walk(_ast.parse(source))
        if isinstance(node, _ast.Call) and getattr(node.func, "id", None) == "run_once"
    ]
    assert calls, "必须真的在调 run_once ✓"
    flags = [
        kw.value.value
        for call in calls
        for kw in call.keywords
        if kw.arg == "cold_started" and isinstance(kw.value, _ast.Constant)
    ]
    assert flags, "必须**显式**传 cold_started ✓（把决定权交给 state ✓）"
    assert all(flag is False for flag in flags), (
        "不许固定 True ✗（AM-03：空状态首启会绕过冷启动归零 ✓）"
    )


def test_empty_state_first_round_zeroes_before_waking(tmp_path):
    """AM-03 行为回归 ✓：空状态 + 有历史未读 ⇒ 这一轮只归零、**不叫人** ✓。"""
    from agent_mailbox.workbench_store import WorkbenchStore

    store = WorkbenchStore(tmp_path / "home")
    repo = tmp_path / "repo"
    repo.mkdir()
    store.create_project("P", repo)
    employee = store.create_employee("Alice", "codex", None)
    woke: list = []

    result = run_once(
        store,
        tmp_path / "wakehome",
        unread_provider=lambda _s, _e: ["hist1", "hist2"],
        deliver=lambda _e, d: woke.append(d) or True,
        employee_ids=[employee["id"]],
        cold_started=False,
    )
    assert woke == [], "首启不得把历史未读当新信 ✗"
    assert result["cold"] is True, "首启这一轮本身就是冷启动 ✓"


def test_entry_point_redelivers_pending(tmp_path):
    """AM-01 at the ENTRY level (Codex: my unit test bypassed the real entry point).

    Real polling asks `decide()` first; with the watermark already advanced it returns
    None ("no new mail"), so the round was skipped and the new recovery code never ran.
    Acceptance: provider returns NOTHING, yet the host is still called once.
    """
    import os

    from agent_mailbox.workbench_wake import hook_deliver, load_state, save_state

    home = tmp_path / "home"
    (home / "wake").mkdir(parents=True)
    employee = "employee_demo"
    assert (
        hook_deliver(home)(employee, {"reason": "new_mail", "unread": 1, "fresh": ["m-one"]})
        is False
    )
    state = load_state(home)
    state.setdefault("employees", {}).setdefault(employee, {"seen": ["m-one"], "wakes": []})
    state["cold_done"] = True
    save_state(home, state)
    hook = home / "wake" / "wake-hook.sh"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(hook, 0o755)

    calls: list[int] = []
    result = run_once(
        object(),
        home,
        unread_provider=lambda _s, _e: [],  # nothing unread => decide() returns None
        deliver=hook_deliver(
            tmp_path / "home",
            runner=lambda _c, **_k: (calls.append(1), type("D", (), {"returncode": 0})())[1],
        ),
        employee_ids=[employee],
        cold_started=True,
    )
    assert result["woke"] == [employee], f"入口层必须把补投算作本轮投递 ✓（实测 {result}）"
    assert len(calls) == 1, f"接通后必须补投 1 次 ✓（Codex 实测 0 次 ✗，实测 {len(calls)}）"


def test_entry_point_does_not_retry_unknown_delivery(tmp_path):
    """AM-02 at the entry level: a stale claim without a receipt must not re-invoke the host."""
    import os
    import time

    from agent_mailbox.workbench_wake import WAKE_LEASE_SECONDS, hook_deliver

    home = tmp_path / "home"
    claims = home / "wake" / "claims"
    claims.mkdir(parents=True)
    hook = home / "wake" / "wake-hook.sh"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(hook, 0o755)
    claim = claims / "m-stale"
    claim.write_text("", encoding="utf-8")
    (claims / "m-stale.pending").write_text("employee_demo", encoding="utf-8")
    os.utime(claim, (time.time() - WAKE_LEASE_SECONDS - 10,) * 2)

    calls: list[int] = []
    run_once(
        object(),
        home,
        unread_provider=lambda _s, _e: [],
        deliver=hook_deliver(
            home, runner=lambda _c, **_k: (calls.append(1), type("D", (), {"returncode": 0})())[1]
        ),
        employee_ids=["employee_demo"],
        cold_started=True,
    )
    assert calls == [], f"未知交付不得重投 ✗（Codex 实测 2 次外部效果，实测 {len(calls)}）"
    assert (claims / "m-stale.unknown").is_file(), "入口层也必须标 .unknown ✓"


def _scene(tmp_path, *, disabled=False, cooldown=False):
    import os
    import time

    from agent_mailbox.workbench_wake import hook_deliver, load_state, save_state

    home = tmp_path / "home"
    (home / "wake").mkdir(parents=True)
    employee = "employee_demo"
    assert (
        hook_deliver(home)(employee, {"reason": "new_mail", "unread": 1, "fresh": ["m-one"]})
        is False
    )
    state = load_state(home)
    state.setdefault("employees", {}).setdefault(employee, {"seen": ["m-one"], "wakes": []})
    state["cold_done"] = True
    if disabled:
        state["enabled"] = False
    if cooldown:
        state["employees"][employee]["wakes"] = [time.time() - 10]
    save_state(home, state)
    hook = home / "wake" / "wake-hook.sh"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(hook, 0o755)
    return home, employee


def _run(home, employee):
    from agent_mailbox.workbench_wake import hook_deliver

    calls: list[int] = []
    run_once(
        object(),
        home,
        unread_provider=lambda _s, _e: [],
        deliver=hook_deliver(
            home, runner=lambda _c, **_k: (calls.append(1), type("D", (), {"returncode": 0})())[1]
        ),
        employee_ids=[employee],
        cold_started=True,
    )
    return len(calls)


def test_recovery_respects_gates(tmp_path):
    """Codex boundary: recovery must NOT bypass disable, cooldown or the hourly cap."""
    home, employee = _scene(tmp_path / "disabled", disabled=True)
    assert _run(home, employee) == 0, "唤醒已关闭时补投也不得叫 ✗"
    home, employee = _scene(tmp_path / "cooldown", cooldown=True)
    assert _run(home, employee) == 0, "冷却中补投也不得叫 ✗"
    home, employee = _scene(tmp_path / "clear")
    assert _run(home, employee) == 1, "闸门允许时补投仍应生效 ✓"


def test_unknown_is_terminal(tmp_path):
    """Codex boundary: .unknown must block EVERY later round, not just the first."""
    import os
    import time

    from agent_mailbox.workbench_wake import WAKE_LEASE_SECONDS, hook_deliver

    home = tmp_path / "home"
    claims = home / "wake" / "claims"
    claims.mkdir(parents=True)
    hook = home / "wake" / "wake-hook.sh"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(hook, 0o755)
    claim = claims / "m-unknown"
    claim.write_text("", encoding="utf-8")
    (claims / "m-unknown.pending").write_text("employee_demo", encoding="utf-8")
    os.utime(claim, (time.time() - WAKE_LEASE_SECONDS - 10,) * 2)

    calls: list[int] = []
    deliver = hook_deliver(
        home, runner=lambda _c, **_k: (calls.append(1), type("D", (), {"returncode": 0})())[1]
    )
    for _ in range(3):
        deliver("employee_demo", {"reason": "retry", "unread": 0, "fresh": []})
    assert calls == [], (
        f"未知交付是**终态** ⇒ 任何后续轮次都不得重投 ✗（Codex 实测第二轮执行了，实测 {len(calls)}）"
    )
    assert (claims / "m-unknown.unknown").is_file()
