"""心跳与"没额度了"：这两件事必须能被人一眼看见（老板 2026-10-06）。"""

from __future__ import annotations

import pytest

from agent_mailbox.workbench_store import WorkbenchStore


@pytest.fixture
def store(tmp_path):
    s = WorkbenchStore(tmp_path / "data")
    d = tmp_path / "p"
    d.mkdir()
    project = s.create_project("Demo", d)
    s.create_employee("Tester", "claude", project["id"], connection_type="cli")  # 夹具要有员工
    return s


def test_quota_signals_are_classified_separately_from_ordinary_failure():
    """429/配额耗尽 ≠ 普通失败：前者要人去续费，后者要人查技术原因。"""
    c = WorkbenchStore.classify_run_output
    assert c("HTTP 429 Too Many Requests") == "quota_exhausted"
    assert c("Error: insufficient_quota") == "quota_exhausted"
    assert c("配额已用尽，请充值") == "quota_exhausted"
    assert c("TypeError: x is not a function") is None
    assert c("") is None


def test_health_reports_missing_heartbeat_as_unavailable_not_fake(store):
    """没有心跳来源时必须说"不可用"，不能给假数字（第二轮评审的教训）。"""
    rows = store.agent_health()
    assert rows, "本机至少有一个员工身份"
    for row in rows:
        assert row["heartbeat_available"] is False
        assert row["heartbeat_at"] is None
        assert row["quota_exhausted"] is False


def test_heartbeat_then_quota_marks_the_agent_as_out_of_quota(store):
    employee = store.agent_health()[0]
    store.record_heartbeat(employee["id"], detail="在跑")
    after_hb = next(r for r in store.agent_health() if r["id"] == employee["id"])
    assert after_hb["heartbeat_available"] is True
    assert after_hb["heartbeat_age_seconds"] is not None and after_hb["stale"] is False
    assert after_hb["quota_exhausted"] is False

    with store._transaction() as db:  # 模拟引擎捕获到 429
        store._governance(
            db,
            WorkbenchStore.QUOTA_TYPE,
            employee_id=employee["id"],
            project_id=None,
            task_id=None,
            actor="system",
            reason=WorkbenchStore.classify_run_output("429 rate limit"),
        )
    after_quota = next(r for r in store.agent_health() if r["id"] == employee["id"])
    assert after_quota["quota_exhausted"] is True, "额度耗尽必须被标出来（产品负责人据此上报）"


def test_heartbeat_must_not_clear_quota_state(store):
    """eng-verify 实测：写一条心跳就把 quota_exhausted 洗成 False ⇒ 教室视图假绿 ✗。

    额度只由显式 quota_restored 清除 ✓；心跳只证"活着" ✗。
    """
    from agent_mailbox.workbench_store import WorkbenchStore as WS

    employee = store.agent_health()[0]
    with store._transaction() as db:
        store._governance(
            db, WS.QUOTA_TYPE, employee_id=employee["id"], actor="system", reason="429"
        )
    assert (
        next(r for r in store.agent_health() if r["id"] == employee["id"])["quota_exhausted"]
        is True
    )

    store.record_heartbeat(employee["id"], detail="还在跑")  # 心跳 ≠ 恢复 ✗
    assert (
        next(r for r in store.agent_health() if r["id"] == employee["id"])["quota_exhausted"]
        is True
    ), "心跳把额度状态洗白了（这正是要修的假绿）"

    store.record_quota_restored(employee["id"], detail="已续费")  # 显式恢复才清 ✓
    row = next(r for r in store.agent_health() if r["id"] == employee["id"])
    assert row["quota_exhausted"] is False and row["quota_restored_at"]
