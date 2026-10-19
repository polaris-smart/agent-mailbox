"""Crash-safe first-contact admission intent; synthetic files only."""

import json
import os
from types import SimpleNamespace

import pytest

from agent_mailbox import workbench_hermes_admission_journal as journal


def sample(tmp_path):
    store = SimpleNamespace(root=tmp_path / "mailbox")
    (store.root / "wake").mkdir(parents=True, mode=0o700)
    profile = tmp_path / "hermes"
    control = SimpleNamespace(
        profile=profile,
        domain=f"gui/{os.getuid()}",
        plist_digest=bytes.fromhex("a" * 64),
        command_digest=bytes.fromhex("b" * 64),
    )
    runtime = {"pid": 123, "start_time": 456}
    plan = "c" * 32
    scope = {
        "employee_id": "employee_1",
        "project_id": "project_1",
        "profile_id": "profile_1",
        "profile": profile,
    }
    return store, control, runtime, plan, scope


def test_absent_first_contact_journal_is_a_zero_write_read(tmp_path):
    home = tmp_path / "fresh-home"
    home.mkdir(mode=0o700)
    assert journal.unfinished_exists(home) is False
    assert not (home / "wake").exists()


def test_admission_journal_persists_each_external_intent_and_complete_history(tmp_path):
    store, control, runtime, plan, scope = sample(tmp_path)
    assert not journal.unfinished_exists(store.root)
    first = journal.begin(
        store,
        plan,
        scope["employee_id"],
        scope["project_id"],
        scope["profile_id"],
        control,
        runtime,
    )
    assert first["phase"] == "stop_intent"
    assert journal.path(store.root, plan).stat().st_mode & 0o077 == 0
    assert journal.unfinished_exists(store.root)
    with pytest.raises(ValueError, match="requires_inspection"):
        journal.begin(
            store,
            "d" * 32,
            scope["employee_id"],
            scope["project_id"],
            scope["profile_id"],
            control,
            runtime,
        )
    for old, new in (
        ("stop_intent", "admission_intent"),
        ("admission_intent", "restart_intent"),
        ("restart_intent", "complete"),
    ):
        assert journal.advance(store, plan, old, new, **scope)["phase"] == new
        assert journal.read(store, plan, **scope)["phase"] == new
    assert not journal.unfinished_exists(store.root)
    with pytest.raises(ValueError, match="transition_unconfirmed"):
        journal.advance(store, plan, "restart_intent", "complete", **scope)


def test_pre_admission_restore_is_terminal_but_never_replayable(tmp_path):
    store, control, runtime, plan, scope = sample(tmp_path)
    journal.begin(
        store,
        plan,
        scope["employee_id"],
        scope["project_id"],
        scope["profile_id"],
        control,
        runtime,
    )
    journal.advance(store, plan, "stop_intent", "pre_admission_restore_intent", **scope)
    assert journal.unfinished_exists(store.root)
    journal.advance(store, plan, "pre_admission_restore_intent", "pre_admission_restored", **scope)
    assert not journal.unfinished_exists(store.root)
    assert journal.reserved_plan(store.root, plan)
    with pytest.raises(ValueError, match="requires_inspection"):
        journal.begin(
            store,
            plan,
            scope["employee_id"],
            scope["project_id"],
            scope["profile_id"],
            control,
            runtime,
        )


def test_admission_journal_rejects_changed_scope_and_orphaned_temporary(tmp_path):
    store, control, runtime, plan, scope = sample(tmp_path)
    journal.begin(
        store,
        plan,
        scope["employee_id"],
        scope["project_id"],
        scope["profile_id"],
        control,
        runtime,
    )
    with pytest.raises(ValueError, match="unconfirmed"):
        journal.read(store, plan, **{**scope, "project_id": "another"})
    record = journal.path(store.root, plan)
    value = json.loads(record.read_text())
    value["original_pid"] = True
    record.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="unconfirmed"):
        journal.read(store, plan, **scope)
    record.unlink()
    orphan = record.with_name("." + record.name + ".orphan.tmp")
    orphan.write_text("incomplete")
    with pytest.raises(ValueError, match="unconfirmed"):
        journal.unfinished_exists(store.root)


def test_admission_journal_rejects_unsafe_identity_before_stop(tmp_path):
    store, control, runtime, plan, scope = sample(tmp_path)
    control.domain = "system"
    with pytest.raises(ValueError, match="evidence_unconfirmed"):
        journal.begin(
            store,
            plan,
            scope["employee_id"],
            scope["project_id"],
            scope["profile_id"],
            control,
            runtime,
        )
    assert not journal.path(store.root, plan).exists()
    control.domain = f"gui/{os.getuid()}"
    runtime["pid"] = True
    with pytest.raises(ValueError, match="evidence_unconfirmed"):
        journal.begin(
            store,
            plan,
            scope["employee_id"],
            scope["project_id"],
            scope["profile_id"],
            control,
            runtime,
        )
