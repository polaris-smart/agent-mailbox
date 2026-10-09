"""outbox（外发表）+ 身份映射：幂等由**唯一索引**保证 ✓ 不靠代码自觉 ✗。"""

from __future__ import annotations

import sqlite3

from agent_mailbox.workbench_store import WorkbenchStore


def _store(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    (tmp_path / "repo").mkdir(exist_ok=True)
    project = store.create_project("P", tmp_path / "repo")
    person = store.create_employee("A", "deepseek", project["id"])
    return store, project, person


def test_same_state_hash_is_a_no_op(tmp_path):
    store, project, _person = _store(tmp_path)
    first = store.record_notification(
        "projection",
        channel="feishu",
        target_kind="dm",
        target_id="ou_x",
        state_hash="h1",
        project_id=project["id"],
    )
    again = store.record_notification(
        "projection",
        channel="feishu",
        target_kind="dm",
        target_id="ou_x",
        state_hash="h1",
        project_id=project["id"],
    )
    assert first["id"] == again["id"], "同一状态哈希不得产生新行 ✓（没变就闭嘴 ✓）"
    db = sqlite3.connect(store.db_path)
    assert db.execute("SELECT count(*) FROM notifications").fetchone()[0] == 1
    db.close()


def test_changed_hash_creates_a_new_row_and_latest_tracks_it(tmp_path):
    store, project, _person = _store(tmp_path)
    store.record_notification(
        "projection",
        channel="feishu",
        target_kind="dm",
        target_id="ou_x",
        state_hash="h1",
        project_id=project["id"],
    )
    second = store.record_notification(
        "projection",
        channel="feishu",
        target_kind="dm",
        target_id="ou_x",
        state_hash="h2",
        project_id=project["id"],
    )
    latest = store.latest_notification("projection", target_kind="dm", target_id="ou_x")
    assert latest["id"] == second["id"] and latest["state_hash"] == "h2"
    assert store.latest_notification("projection", target_kind="dm", target_id="ou_none") is None


def test_mark_notification_records_message_id_and_attempts(tmp_path):
    store, project, _person = _store(tmp_path)
    row = store.record_notification(
        "projection",
        channel="feishu",
        target_kind="dm",
        target_id="ou_x",
        state_hash="h1",
        project_id=project["id"],
    )
    sent = store.mark_notification(row["id"], status="sent", external_message_id="om_123")
    assert (
        sent["status"] == "sent"
        and sent["external_message_id"] == "om_123"
        and sent["attempts"] == 1
    )
    failed = store.mark_notification(row["id"], status="failed", last_error="boom")
    assert failed["attempts"] == 2 and failed["last_error"] == "boom"
    assert failed["external_message_id"] == "om_123", "失败不得抹掉已记的 message_id ✓"


def test_identity_link_roundtrip_and_update(tmp_path):
    store, _project, person = _store(tmp_path)
    assert store.identity_employee("feishu", "ou_x") is None  # 未绑定 ⇒ None ✓ 不猜 ✗
    store.link_identity("feishu", "ou_x", person["id"], note="老板")
    assert store.identity_employee("feishu", "ou_x") == person["id"]
    other = store.create_employee("B", "deepseek", None)
    store.link_identity("feishu", "ou_x", other["id"])  # 重复绑定 ⇒ 覆盖 ✓
    assert store.identity_employee("feishu", "ou_x") == other["id"]
    db = sqlite3.connect(store.db_path)
    assert db.execute("SELECT count(*) FROM identity_links").fetchone()[0] == 1
    db.close()


def test_empty_arguments_are_refused(tmp_path):
    import pytest

    from agent_mailbox.workbench_store import WorkbenchError

    store, _project, _person = _store(tmp_path)
    with pytest.raises(WorkbenchError):
        store.record_notification(
            "", channel="feishu", target_kind="dm", target_id="x", state_hash="h"
        )
    with pytest.raises(WorkbenchError):
        store.link_identity("", "ou_x", "employee_x")
