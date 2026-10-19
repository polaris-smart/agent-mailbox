"""The Human data-layer intersection; owner HTTP remains local-only."""

import sqlite3

import pytest

from agent_mailbox.workbench_human_spaces import (
    add_space_member,
    authenticate_human_session,
    claim_human_invitation,
    create_human,
    create_human_invitation,
    create_managed_private_project,
    create_private_project,
    create_shared_space,
    grant_project,
    grant_review_authority,
    issue_human_session,
    list_project_grants_for_human,
    list_projects_for_human,
    list_spaces_for_human,
    move_project_to_shared_space,
    project_role,
    require_project_role,
    require_review_authority,
    revoke_human_session,
    revoke_project,
    revoke_review_authority,
    revoke_space_member,
)
from agent_mailbox.workbench_store import SCHEMA_VERSION, WorkbenchError, WorkbenchStore


def owner_id(store):
    with store._transaction(readonly=True) as db:
        return db.execute(
            "SELECT owner_human_id FROM human_bootstrap WHERE singleton=1"
        ).fetchone()[0]


def visible_ids(store, human_id):
    return {row["id"] for row in list_projects_for_human(store, human_id)}


def can_review(store, human_id, project_id):
    project = next(
        row for row in list_projects_for_human(store, human_id) if row["id"] == project_id
    )
    return bool(project["can_review"])


def test_review_authority_does_not_revive_after_project_member_or_space_change(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    owner = owner_id(store)
    guest = create_human(store, "Reviewer")["human_id"]
    project = store.create_project("Shared", tmp_path)["id"]
    space = create_shared_space(store, owner, "Family")["space_id"]
    add_space_member(store, owner, space, guest)
    move_project_to_shared_space(store, owner, project, space)
    grant_project(store, owner, project, guest, "viewer")
    assert not can_review(store, guest, project)
    grant_review_authority(store, owner, project, guest)
    assert can_review(store, guest, project)
    with store._transaction(readonly=True) as db:
        require_review_authority(db, guest, project)
        with pytest.raises(WorkbenchError):
            require_project_role(db, guest, project, ("owner",))

    revoke_review_authority(store, owner, project, guest)
    assert not can_review(store, guest, project)
    grant_review_authority(store, owner, project, guest)
    revoke_project(store, owner, project, guest)
    grant_project(store, owner, project, guest, "viewer")
    assert not can_review(store, guest, project)

    grant_review_authority(store, owner, project, guest)
    revoke_space_member(store, owner, space, guest)
    add_space_member(store, owner, space, guest)
    grant_project(store, owner, project, guest, "viewer")
    assert not can_review(store, guest, project)

    grant_review_authority(store, owner, project, guest)
    new_space = create_shared_space(store, owner, "Other family")["space_id"]
    move_project_to_shared_space(store, owner, project, new_space)
    add_space_member(store, owner, new_space, guest)
    grant_project(store, owner, project, guest, "viewer")
    assert not can_review(store, guest, project)


def test_schema_17_upgrade_adds_review_authority_without_changing_existing_access(tmp_path):
    home = tmp_path / "home"
    store = WorkbenchStore(home)
    owner = owner_id(store)
    project = store.create_project("Existing private", tmp_path)["id"]
    with sqlite3.connect(store.db_path) as db:
        db.execute("DROP TABLE project_human_reviewers")
        db.execute("PRAGMA user_version=17")
    upgraded = WorkbenchStore(home)
    assert SCHEMA_VERSION == 18
    assert upgraded.migration_backup_path is not None
    assert upgraded.migration_backup_path.is_file()
    assert visible_ids(upgraded, owner) == {project}
    with upgraded._transaction(readonly=True) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 18
        assert db.execute("SELECT count(*) FROM project_human_reviewers").fetchone()[0] == 0
        assert db.execute("PRAGMA foreign_key_check").fetchone() is None


def test_managed_private_project_does_not_leave_a_directory_for_unknown_human(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    with pytest.raises(WorkbenchError, match="不存在或已停用"):
        create_managed_private_project(store, "human_unknown", "Should not exist")
    assert not any((store.directory / "human-projects" / "human_unknown").glob("*"))
    with store._transaction(readonly=True) as db:
        assert db.execute("SELECT count(*) FROM projects").fetchone()[0] == 0


def test_private_project_requires_own_human_and_shared_project_requires_both_grants(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    owner = owner_id(store)
    owner_project = store.create_project("Owner private", tmp_path)["id"]
    second = create_human(store, "Another Human")["human_id"]
    second_project = create_private_project(store, second, "Second private", tmp_path)["project_id"]

    assert visible_ids(store, owner) == {owner_project}
    assert visible_ids(store, second) == {second_project}
    with store._transaction(readonly=True) as db:
        assert project_role(db, second, owner_project) is None
        assert project_role(db, owner, second_project) is None
        assert project_role(db, second, "project_missing") is None

    shared = create_shared_space(store, owner, "Family work")["space_id"]
    add_space_member(store, owner, shared, second)
    assert visible_ids(store, second) == {second_project}  # space entry is not project access
    with pytest.raises(WorkbenchError, match="私人项目"):
        grant_project(store, owner, owner_project, second, "viewer")
    move_project_to_shared_space(store, owner, owner_project, shared)
    assert visible_ids(store, second) == {second_project}  # moving is not granting
    grant_project(store, owner, owner_project, second, "viewer")
    assert visible_ids(store, second) == {second_project, owner_project}
    assert {
        row["human_id"]: row["role"]
        for row in list_project_grants_for_human(store, owner, owner_project)
    } == {owner: "owner", second: "viewer"}
    with pytest.raises(WorkbenchError, match="不存在或没有访问权限"):
        list_project_grants_for_human(store, second, owner_project)
    with pytest.raises(WorkbenchError, match="不存在或没有访问权限"):
        list_project_grants_for_human(store, owner, second_project)
    with store._transaction(readonly=True) as db:
        assert require_project_role(db, second, owner_project) == "viewer"
        with pytest.raises(WorkbenchError):
            require_project_role(db, second, owner_project, ("owner", "editor"))
    revoke_project(store, owner, owner_project, second)
    assert visible_ids(store, second) == {second_project}
    assert [
        row["human_id"] for row in list_project_grants_for_human(store, owner, owner_project)
    ] == [owner]
    grant_project(store, owner, owner_project, second, "viewer")
    revoke_space_member(store, owner, shared, second)
    assert visible_ids(store, second) == {second_project}
    add_space_member(store, owner, shared, second)
    assert visible_ids(store, second) == {second_project}  # old grant stays revoked
    grant_project(store, owner, owner_project, second, "viewer")
    assert visible_ids(store, second) == {second_project, owner_project}


def test_private_space_stays_hidden_if_membership_and_grant_rows_are_corrupt(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    private_project = store.create_project("Owner private", tmp_path)["id"]
    guest = create_human(store, "Guest")["human_id"]
    with store._transaction() as db:
        private_space = db.execute(
            "SELECT private_space_id FROM human_bootstrap WHERE singleton=1"
        ).fetchone()[0]
        db.execute(
            "INSERT INTO space_memberships VALUES(?,?,?,?,NULL)",
            (private_space, guest, "member", "2026-10-16T00:00:00Z"),
        )
        db.execute(
            "INSERT INTO project_human_grants VALUES(?,?,?,?,NULL)",
            (private_project, guest, "viewer", "2026-10-16T00:00:00Z"),
        )

    # A malformed relationship must not turn someone else's private scope public.
    assert private_project not in visible_ids(store, guest)
    assert private_space not in {space["id"] for space in list_spaces_for_human(store, guest)}


def test_removing_space_member_revokes_all_project_grants_and_rejoin_does_not_restore(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    owner = owner_id(store)
    guest = create_human(store, "Guest")["human_id"]
    first = store.create_project("First", tmp_path)["id"]
    second = store.create_project("Second", tmp_path)["id"]
    space = create_shared_space(store, owner, "Family")["space_id"]
    add_space_member(store, owner, space, guest)
    for project_id in (first, second):
        move_project_to_shared_space(store, owner, project_id, space)
        grant_project(store, owner, project_id, guest, "viewer")
    assert {first, second}.issubset(visible_ids(store, guest))
    revoke_space_member(store, owner, space, guest)
    assert not ({first, second} & visible_ids(store, guest))
    add_space_member(store, owner, space, guest)
    assert not ({first, second} & visible_ids(store, guest))
    for project_id in (first, second):
        assert [
            row["human_id"] for row in list_project_grants_for_human(store, owner, project_id)
        ] == [owner]


def test_legacy_schema_upgrade_keeps_project_private_and_backfills_owner(tmp_path):
    home = tmp_path / "home"
    store = WorkbenchStore(home)
    project = store.create_project("Pre-0.8.3 data", tmp_path)
    employee = store.create_employee("Synthetic", "codex", project["id"])
    with sqlite3.connect(store.db_path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        for name in (
            "human_enrollments",
            "human_bootstrap",
            "project_human_grants",
            "project_spaces",
            "space_memberships",
            "spaces",
            "humans",
        ):
            db.execute(f"DROP TABLE {name}")
        db.execute("PRAGMA user_version=13")
    upgraded = WorkbenchStore(home)
    assert SCHEMA_VERSION >= 16
    assert upgraded.migration_backup_path is not None
    assert upgraded.migration_backup_path.is_file()
    assert visible_ids(upgraded, owner_id(upgraded)) == {project["id"]}
    with upgraded._transaction(readonly=True) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert db.execute("SELECT id FROM employees WHERE id=?", (employee["id"],)).fetchone()
        assert db.execute("PRAGMA foreign_key_check").fetchone() is None
    other = create_human(upgraded, "Unrelated Human")["human_id"]
    assert visible_ids(upgraded, other) == set()


def test_owner_cannot_be_self_revoked_or_private_space_shared(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    owner = owner_id(store)
    project = store.create_project("Owner project", tmp_path)["id"]
    another = create_human(store, "Another Human")["human_id"]
    with pytest.raises(WorkbenchError):
        revoke_project(store, owner, project, owner)
    with store._transaction(readonly=True) as db:
        private_space = db.execute(
            "SELECT private_space_id FROM human_bootstrap WHERE singleton=1"
        ).fetchone()[0]
    with pytest.raises(WorkbenchError):
        add_space_member(store, owner, private_space, another)
    with pytest.raises(WorkbenchError):
        grant_project(store, owner, project, owner, "viewer")
    assert visible_ids(store, owner) == {project}
    assert visible_ids(store, another) == set()


def test_moving_shared_project_does_not_reactivate_old_grant(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    owner = owner_id(store)
    another = create_human(store, "Another Human")["human_id"]
    project = store.create_project("Shared work", tmp_path)["id"]
    first = create_shared_space(store, owner, "First")["space_id"]
    second = create_shared_space(store, owner, "Second")["space_id"]
    add_space_member(store, owner, first, another)
    move_project_to_shared_space(store, owner, project, first)
    grant_project(store, owner, project, another, "viewer")
    assert project in visible_ids(store, another)
    move_project_to_shared_space(store, owner, project, second)
    add_space_member(store, owner, second, another)
    assert project not in visible_ids(store, another)
    grant_project(store, owner, project, another, "viewer")
    assert project in visible_ids(store, another)


def test_current_schema_missing_human_scope_fails_closed_without_reassignment(tmp_path):
    home = tmp_path / "home"
    store = WorkbenchStore(home)
    project = store.create_project("Private", tmp_path)["id"]
    with sqlite3.connect(store.db_path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("DELETE FROM project_spaces WHERE project_id=?", (project,))
    with pytest.raises(WorkbenchError) as error:
        WorkbenchStore(home)
    assert error.value.code == "HUMAN_SCOPE_CORRUPT"
    with sqlite3.connect(store.db_path) as db:
        assert (
            db.execute("SELECT 1 FROM project_spaces WHERE project_id=?", (project,)).fetchone()
            is None
        )


def test_human_session_only_resolves_active_human_and_revocation_is_immediate(tmp_path):
    store = WorkbenchStore(tmp_path / "home")
    human_id = create_human(store, "Another Human")["human_id"]
    session = issue_human_session(store, human_id)
    assert authenticate_human_session(store, session["token"])["human_id"] == human_id
    with sqlite3.connect(store.db_path) as db:
        raw = db.execute(
            "SELECT token_sha256 FROM human_sessions WHERE id=?", (session["session_id"],)
        ).fetchone()[0]
        assert session["token"] not in raw
        db.execute("UPDATE humans SET disabled_at='2026-10-16' WHERE id=?", (human_id,))
    with pytest.raises(WorkbenchError) as error:
        authenticate_human_session(store, session["token"])
    assert error.value.code == "UNAUTHORIZED"
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE humans SET disabled_at=NULL WHERE id=?", (human_id,))
    assert revoke_human_session(store, session["session_id"]) == {"revoked": True}
    with pytest.raises(WorkbenchError) as error:
        authenticate_human_session(store, session["token"])
    assert error.value.code == "UNAUTHORIZED"
    assert revoke_human_session(store, session["session_id"]) == {"revoked": False}


def test_intermediate_schema_upgrade_preserves_explicit_shared_grants(tmp_path):
    home = tmp_path / "home"
    store = WorkbenchStore(home)
    owner = owner_id(store)
    second = create_human(store, "Second Human")["human_id"]
    project = store.create_project("Shared work", tmp_path)["id"]
    space = create_shared_space(store, owner, "Shared")["space_id"]
    add_space_member(store, owner, space, second)
    move_project_to_shared_space(store, owner, project, space)
    grant_project(store, owner, project, second, "viewer")
    with sqlite3.connect(store.db_path) as db:
        db.execute("DROP TABLE human_sessions")
        db.execute("PRAGMA user_version=14")
    upgraded = WorkbenchStore(home)
    assert upgraded.migration_backup_path is not None
    assert visible_ids(upgraded, second) == {project}
    with upgraded._transaction(readonly=True) as db:
        assert project_role(db, second, project) == "viewer"
        assert db.execute("PRAGMA foreign_key_check").fetchone() is None
    expired = issue_human_session(upgraded, second)
    with sqlite3.connect(upgraded.db_path) as db:
        db.execute(
            "UPDATE human_sessions SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (expired["session_id"],),
        )
    with pytest.raises(WorkbenchError) as error:
        authenticate_human_session(upgraded, expired["token"])
    assert error.value.code == "UNAUTHORIZED"


def test_schema_15_upgrade_keeps_claimed_identity_and_existing_session(tmp_path):
    home = tmp_path / "home"
    store = WorkbenchStore(home)
    second = create_human(store, "Second Human")["human_id"]
    legacy_session = issue_human_session(store, second)
    with sqlite3.connect(store.db_path) as db:
        db.execute("DROP TABLE human_enrollments")
        db.execute("PRAGMA user_version=15")
    upgraded = WorkbenchStore(home)
    assert upgraded.migration_backup_path is not None
    assert authenticate_human_session(upgraded, legacy_session["token"])["human_id"] == second
    with pytest.raises(WorkbenchError) as error:
        create_human_invitation(upgraded, second)
    assert error.value.code == "HUMAN_ALREADY_ENROLLED"
    with upgraded._transaction(readonly=True) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert db.execute("SELECT 1 FROM human_enrollments").fetchone() is None


def test_current_schema_missing_enrollment_ledger_fails_closed(tmp_path):
    home = tmp_path / "home"
    store = WorkbenchStore(home)
    second = create_human(store, "Second Human")["human_id"]
    invite = create_human_invitation(store, second)
    claim_human_invitation(store, invite["invitation"])
    with sqlite3.connect(store.db_path) as db:
        db.execute("DROP TABLE human_enrollments")
    with pytest.raises(WorkbenchError) as error:
        WorkbenchStore(home)
    assert error.value.code == "HUMAN_SCOPE_CORRUPT"


def test_intermediate_schema_orphan_does_not_become_local_owner_project(tmp_path):
    home = tmp_path / "home"
    store = WorkbenchStore(home)
    second = create_human(store, "Second Human")["human_id"]
    project = create_private_project(store, second, "Private", tmp_path)["project_id"]
    with sqlite3.connect(store.db_path) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("DELETE FROM project_spaces WHERE project_id=?", (project,))
        db.execute("DROP TABLE human_sessions")
        db.execute("PRAGMA user_version=14")
    before_backups = list(store.directory.glob("state-v14-before-v16-*.sqlite"))
    with pytest.raises(WorkbenchError) as error:
        WorkbenchStore(home)
    assert error.value.code == "HUMAN_SCOPE_CORRUPT"
    assert list(store.directory.glob("state-v14-before-v16-*.sqlite")) == before_backups
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 14
        assert (
            db.execute("SELECT 1 FROM project_spaces WHERE project_id=?", (project,)).fetchone()
            is None
        )
