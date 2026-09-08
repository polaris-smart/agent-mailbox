"""Tests for the task board: create/move/list, state machine, auto-notify."""

import threading

import pytest

from agent_mailbox.store import MailboxError, MailStore


@pytest.fixture()
def store(tmp_path):
    return MailStore(root=tmp_path / "mail")


# ------------------------------------------------------------------ create

def test_task_create_defaults(store):
    task = store.task_create("write docs", "ZC", "HS")
    assert task["id"] == "t-1"
    assert task["status"] == "todo"
    assert task["assignee"] == "ZC"
    assert task["created_by"] == "HS"
    assert (store.root / "tasks.json").exists()


def test_task_create_increments_ids(store):
    store.task_create("a", "ZC", "HS")
    store.task_create("b", "ZC", "HS")
    assert [t["id"] for t in store.task_list()] == ["t-1", "t-2"]


def test_task_create_rejects_blank_title(store):
    with pytest.raises(MailboxError):
        store.task_create("   ", "ZC", "HS")


def test_task_create_rejects_bad_ids(store):
    with pytest.raises(MailboxError):
        store.task_create("t", "../evil", "HS")
    with pytest.raises(MailboxError):
        store.task_create("t", "ZC", "a/b")


# -------------------------------------------------------------- state machine

def test_full_lifecycle_happy_path(store):
    store.task_create("ship v0.3.0", "ZC", "HS")
    for status in ("doing", "review", "done"):
        task = store.task_move("t-1", status, moved_by="HS")
        assert task["status"] == status
    assert len(task["history"]) == 3


def test_illegal_skip_rejected(store):
    store.task_create("t", "ZC", "HS")
    with pytest.raises(MailboxError, match="illegal transition"):
        store.task_move("t-1", "review", moved_by="HS")
    assert store.task_list()[0]["status"] == "todo"


def test_illegal_skip_allowed_with_force(store):
    store.task_create("t", "ZC", "HS")
    task = store.task_move("t-1", "done", moved_by="HS", force=True)
    assert task["status"] == "done"


def test_backward_move_needs_force(store):
    store.task_create("t", "ZC", "HS")
    store.task_move("t-1", "doing", moved_by="HS")
    with pytest.raises(MailboxError, match="illegal transition"):
        store.task_move("t-1", "todo", moved_by="HS")
    assert store.task_move("t-1", "todo", moved_by="HS", force=True)["status"] == "todo"


def test_same_status_rejected(store):
    store.task_create("t", "ZC", "HS")
    with pytest.raises(MailboxError, match="already in"):
        store.task_move("t-1", "todo", moved_by="HS")


def test_done_is_terminal_even_with_force(store):
    store.task_create("t", "ZC", "HS")
    store.task_move("t-1", "done", moved_by="HS", force=True)
    with pytest.raises(MailboxError, match="terminal"):
        store.task_move("t-1", "doing", moved_by="HS", force=True)


def test_move_rejects_bad_status_and_unknown_task(store):
    store.task_create("t", "ZC", "HS")
    with pytest.raises(MailboxError):
        store.task_move("t-1", "archived", moved_by="HS")
    with pytest.raises(MailboxError):
        store.task_move("t-99", "doing", moved_by="HS")


# ---------------------------------------------------------------- auto-notify

def test_move_auto_messages_assignee(store):
    store.register("HS")
    store.register("ZC")
    store.task_create("review this", "ZC", "HS", notify=False)
    store.task_move("t-1", "doing", moved_by="HS", notify=False)
    store.task_move("t-1", "review", moved_by="HS")
    msgs = store.check("ZC")
    assert len(msgs) == 1
    assert msgs[0]["subject"] == "[task#t-1 → review] review this"
    assert "移至 review" in msgs[0]["body"]


def test_create_auto_messages_assignee(store):
    store.register("HS")
    store.register("ZC")
    store.task_create("new job", "ZC", "HS")
    msgs = store.check("ZC")
    assert len(msgs) == 1
    assert msgs[0]["subject"] == "[task#t-1 → todo] new job"
    assert msgs[0]["from"] == "HS"


def test_self_assign_sends_no_mail(store):
    store.register("ZC")
    store.task_create("my own task", "ZC", "ZC")
    store.task_move("t-1", "doing", moved_by="ZC")
    assert store.list_messages("ZC") == []


def test_notify_false_sends_no_mail(store):
    store.register("HS")
    store.register("ZC")
    store.task_create("quiet move", "ZC", "HS", notify=False)
    store.task_move("t-1", "doing", moved_by="HS", notify=False)
    assert store.list_messages("ZC") == []


def test_move_note_lands_in_mail(store):
    store.register("HS")
    store.register("ZC")
    store.task_create("t", "ZC", "HS", notify=False)
    store.task_move("t-1", "doing", moved_by="HS", note="please focus on tests")
    body = store.check("ZC")[0]["body"]
    assert "please focus on tests" in body


def test_move_note_recorded_in_history(store):
    store.task_create("t", "ZC", "HS")
    store.task_move("t-1", "doing", moved_by="ZC", note="starting")
    entry = store.task_list()[0]["history"][-1]
    assert entry["note"] == "starting"
    assert entry["by"] == "ZC"


# ------------------------------------------------------------------ reassign

def test_move_reassigns_and_notifies_new_owner(store):
    store.register("HS")
    store.register("ZC")
    store.register("WB")
    store.task_create("t", "ZC", "HS", notify=False)
    task = store.task_move("t-1", "doing", moved_by="HS", assignee="WB")
    assert task["assignee"] == "WB"
    msgs = store.check("WB")
    assert len(msgs) == 1
    assert "负责人已从 ZC 转派给 WB" in msgs[0]["body"]
    assert store.list_messages("ZC") == []  # old owner stays silent


def test_reassign_to_same_owner_keeps_card(store):
    store.task_create("t", "ZC", "HS")
    task = store.task_move("t-1", "doing", moved_by="HS", assignee="ZC")
    assert task["assignee"] == "ZC"


# --------------------------------------------------------------------- list

def test_task_list_filters(store):
    store.task_create("a", "ZC", "HS")
    store.task_create("b", "WB", "HS")
    store.task_create("c", "ZC", "HS")
    store.task_move("t-1", "doing", moved_by="HS", notify=False)
    assert [t["id"] for t in store.task_list(assignee="ZC")] == ["t-1", "t-3"]
    assert [t["id"] for t in store.task_list(status="todo")] == ["t-2", "t-3"]
    assert [t["id"] for t in store.task_list(assignee="WB", status="todo")] == ["t-2"]


def test_task_list_rejects_bad_filter(store):
    with pytest.raises(MailboxError):
        store.task_list(status="wip")


def test_corrupt_tasks_json_raises(store):
    store.task_create("t", "ZC", "HS")
    (store.root / "tasks.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(MailboxError):
        store.task_list()


# ---------------------------------------------------------------- concurrency

def test_concurrent_moves_no_corruption(store):
    store.task_create("t", "ZC", "HS", notify=False)
    errors = []

    def worker(i):
        try:
            for j in range(10):
                store.task_create(f"m{i}-{j}", "ZC", "HS", notify=False)
        except (OSError, MailboxError) as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(store.task_list()) == 41  # t-1 + 40 concurrent creates
