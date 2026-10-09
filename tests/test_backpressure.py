"""Backpressure: flooding senders are folded into one digest (rules S2/S4)."""

from __future__ import annotations

import pytest

from agent_mailbox import backpressure as bp
from agent_mailbox.workbench_store import WorkbenchStore


@pytest.fixture
def scene(tmp_path):
    store = WorkbenchStore(tmp_path / "data")
    directory = tmp_path / "project"
    directory.mkdir()
    project = store.create_project("Demo", directory)
    alice = store.create_employee("Alice", "codex", project["id"])
    bob = store.create_employee("Bob", "codex", project["id"])
    task = store.create_task(project["id"], "Build", "work", alice["id"])
    assert store.claim_task(store.local_node()["id"])["id"] == task["id"]
    store.set_status(task["id"], "running")
    return store, project, alice, bob, task


def _send(store, project, sender, recipient, task, n, *, start=0):
    out = []
    for i in range(n):
        out.append(
            store.send_message(
                project["id"],
                f"消息 {start + i}",
                f"正文 {start + i}",
                recipient_id=recipient["id"],
                sender_id=sender["id"],
                source_task_id=task["id"],
            )
        )
    return out


# ── 纯逻辑 ──────────────────────────────────────────────────────────────
def test_should_fold_boundaries():
    assert bp.should_fold(9, 10) is False  # 10th message still delivered
    assert bp.should_fold(10, 10) is True  # 11th starts folding
    assert bp.fold_limit(None) == bp.DEFAULT_FOLD_LIMIT


def test_fold_limit_env_override(monkeypatch):
    monkeypatch.setenv("AGENT_MAILBOX_FOLD_LIMIT", "3")
    assert bp.fold_limit() == 3
    monkeypatch.setenv("AGENT_MAILBOX_FOLD_LIMIT", "nonsense")
    assert bp.fold_limit() == bp.DEFAULT_FOLD_LIMIT


def test_digest_body_is_bounded_and_refreshes_count():
    body = bp.new_digest_body("第一条", "A", "B")
    assert bp.count_folded(body) == 1
    body = bp.append_folded(body, "第二条", "A", "B")
    assert bp.count_folded(body) == 2
    assert "2 条" in body.splitlines()[0]
    for i in range(bp.MAX_FOLDED_TITLES + 20):
        body = bp.append_folded(body, f"x{i}", "A", "B")
    assert bp.count_folded(body) == bp.MAX_FOLDED_TITLES  # bounded, rule S2


def test_digest_marking_and_window_cutoff():
    key = bp.digest_thread_id("A", "B")
    assert bp.is_digest(key) and not bp.is_digest("th-1")
    cutoff = bp.since("2026-10-04T08:10:24.384547+00:00", 300)
    assert cutoff.startswith("2026-10-04T08:05:24")
    assert bp.since("not-a-time", 300) == ""


# ── store 层：折叠真的发生 ────────────────────────────────────────────────
def test_flood_is_folded_into_one_digest(scene, monkeypatch):
    monkeypatch.setenv("AGENT_MAILBOX_FOLD_LIMIT", "5")
    store, project, alice, bob, task = scene
    results = _send(store, project, alice, bob, task, 9)

    delivered = [r for r in results if not r.get("folded")]
    folded = [r for r in results if r.get("folded")]
    assert len(delivered) == 5  # limit honoured
    assert len(folded) == 4  # everything after that is folded
    assert len({r["id"] for r in folded}) == 1  # ... into ONE row
    assert folded[-1]["folded_count"] == 4

    rows = [r for r in store.list_messages(project["id"]) if r["recipient_id"] == bob["id"]]
    assert len(rows) == 6  # 5 delivered + 1 digest, not 9
    digest = [r for r in rows if bp.is_digest(r["thread_id"])]
    assert len(digest) == 1
    for i in range(5, 9):
        assert f"消息 {i}" in digest[0]["body"]  # every title preserved


def test_fold_is_audited_and_reversible(scene, monkeypatch):
    monkeypatch.setenv("AGENT_MAILBOX_FOLD_LIMIT", "2")
    store, project, alice, bob, task = scene
    _send(store, project, alice, bob, task, 5)
    folds = [e for e in store.governance_events() if e["type"] == "message_folded"]
    assert len(folds) == 3  # one event per folded message
    assert "超过上限" in folds[0]["reason"]
    assert folds[0]["payload"]["folded_title"].startswith("消息")


def test_under_limit_and_human_mail_are_untouched(scene):
    store, project, alice, bob, task = scene
    _send(store, project, alice, bob, task, 3, start=100)
    assert not any(r.get("folded") for r in store.list_messages(project["id"]))

    human = store.send_message(project["id"], "人工消息", "人工直发", recipient_id=bob["id"])
    assert not human.get("folded")  # humans never get folded
    assert not any(bp.is_digest(r["thread_id"]) for r in store.list_messages(project["id"]))


def test_work_requests_are_never_folded(scene, monkeypatch):
    monkeypatch.setenv("AGENT_MAILBOX_FOLD_LIMIT", "1")
    store, project, alice, bob, task = scene
    _send(store, project, alice, bob, task, 3)
    request = store.send_message(
        project["id"],
        "请干活",
        "这是工作请求",
        recipient_id=bob["id"],
        sender_id=alice["id"],
        source_task_id=task["id"],
        request_work=True,
    )
    assert not request.get("folded")  # a work request must always arrive
    assert request.get("task_id")


# ── 规则 B 回归锁：失败绝不自动回信 ──────────────────────────────────────
def test_task_failure_never_sends_a_message(scene):
    """失败只落本地事件 —— 断掉"失败→回执→对方回信→再失败"的火药链。

    2026-10-04 事故根因：失败回执被对方当成新事件。v0.8 必须保持"失败不发信"。
    对照组：正常交付由 submit_mail_task 发投递消息（那是设计内的，不在此列）。
    """
    store, project, _alice, _bob, task = scene
    before = len(store.list_messages(project["id"]))
    finished = store.finish_task(task["id"], "failed", "", {"code": "X", "message": "boom"})
    assert finished["status"] == "failed"
    assert len(store.list_messages(project["id"])) == before  # 一封都没发
    assert store.list_messages(project["id"]) == store.list_messages(project["id"])  # 幂等读


def test_task_cancel_never_sends_a_message(scene):
    """取消只改状态/留痕，绝不发邮件（取消也要有人点，不该惊动对方）。"""
    store, project, _alice, _bob, task = scene
    before = len(store.list_messages(project["id"]))
    cancelled = store.cancel_task(task["id"])
    assert cancelled["cancel_requested"] is True or cancelled["status"] == "cancelled"
    assert len(store.list_messages(project["id"])) == before


def test_slow_drip_folds_via_long_window(scene, monkeypatch):
    """慢滴（~1 封/分）任何 5 分钟窗都不超限 —— 必须由长窗那一档接住。

    2026-10-04 回放：HS→dsh 66 封/3h、HS→ZC 45 封、HS→codex 30 封全是这种形态。
    """
    store, project, alice, bob, task = scene
    monkeypatch.delenv("AGENT_MAILBOX_FOLD_LIMIT", raising=False)
    monkeypatch.delenv("AGENT_MAILBOX_FOLD_TIERS", raising=False)
    _long_window, long_cap = bp.tiers()[1]
    assert long_cap == 30

    # 造 35 封历史：每 6 分钟一封（3.5 小时），任何 5 分钟窗内都只有 1 封
    from datetime import datetime, timedelta, timezone

    stamps = [
        (datetime.now(timezone.utc) - timedelta(minutes=6 * (35 - i))).isoformat()
        for i in range(35)
    ]
    with store._transaction() as db:
        for i, ts in enumerate(stamps):
            db.execute(
                "INSERT INTO messages(id,project_id,title,body,sender_id,recipient_id,reply_to,"
                "thread_id,request_work,source_task_id,request_id,request_digest,created_at,"
                "source_session_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"message_seed{i:04d}",
                    project["id"],
                    f"旧信 {i}",
                    "x",
                    alice["id"],
                    bob["id"],
                    None,
                    f"th-seed{i}",
                    0,
                    None,
                    None,
                    "digest",
                    ts,
                    None,
                ),
            )
    # 逐条发都不会触发短窗（间隔 6 分钟 > 5 分钟窗），但累计已超长窗上限
    folded = store.send_message(
        project["id"],
        "慢滴新信",
        "正文",
        recipient_id=bob["id"],
        sender_id=alice["id"],
        source_task_id=task["id"],
    )
    assert folded.get("folded") is True
    assert folded["folded_count"] >= 1
    assert bp.is_digest(folded["thread_id"])


def test_frozen_thread_blocks_further_sends(scene):
    """S3 执行点：回声环冻结后，回复该线程必须被挡住（不是只记一笔）。"""
    from agent_mailbox import echo_guard as eg
    from agent_mailbox.workbench_store import WorkbenchError

    store, project, alice, bob, task = scene
    # 员工发信必须绑定**自己**的执行来源 ⇒ bob 需要他自己的 task
    bob_task = store.create_task(project["id"], "Bob work", "x", bob["id"])
    store.claim_task(store.local_node()["id"])
    store.set_status(bob_task["id"], "running")
    parent = store.send_message(
        project["id"],
        "起点",
        "正文",
        recipient_id=bob["id"],
        sender_id=alice["id"],
        source_task_id=task["id"],
    )
    thread = parent["thread_id"]
    assert eg.thread_frozen(store, thread) is False

    eg.freeze_thread(store, thread, reason="合成回声环")
    with pytest.raises(WorkbenchError) as excinfo:
        store.send_message(
            project["id"],
            "回执",
            "回执正文",
            recipient_id=alice["id"],
            sender_id=bob["id"],
            source_task_id=bob_task["id"],
            reply_to=parent["id"],
        )
    assert excinfo.value.code == "THREAD_FROZEN"

    eg.unfreeze_thread(store, thread, reason="人工确认非回声")
    again = store.send_message(
        project["id"],
        "解冻后回执",
        "正文",
        recipient_id=alice["id"],
        sender_id=bob["id"],
        source_task_id=bob_task["id"],
        reply_to=parent["id"],
    )
    assert again["id"]
