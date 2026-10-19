"""Workspace onboarding orchestration, isolated homes; never a real Codex process."""

import copy
import hashlib
import json
import os
import threading
import urllib.error
import urllib.request

import pytest

from agent_mailbox import workbench_codex_installer as installer
from agent_mailbox.workbench_mail_config import export_session
from agent_mailbox.workbench_mail_sessions import list_sessions, revoke_session
from agent_mailbox.workbench_store import WorkbenchError, WorkbenchStore


@pytest.fixture
def setup(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    store = WorkbenchStore(tmp_path / "home")
    project = store.create_project("Synthetic onboarding", workspace)
    employee = store.create_employee("Synthetic Codex", "codex", project["id"])
    session = export_session(store, employee["id"], project["id"], "Synthetic owned session")
    inspection = {
        "config_digest": "a" * 64,
        "effective_servers": [],
        "runtime_servers": [],
        "layers": [],
    }
    calls = []

    def inspect(s, thread, path):
        assert s is store and thread == "selected-thread" and path == str(workspace)
        calls.append(thread)
        return copy.deepcopy(inspection)

    monkeypatch.setattr(installer, "_inspect", inspect)

    def act(action="preview", **kwargs):
        return installer.installation(
            store,
            employee["id"],
            project["id"],
            action,
            **{"thread_id": "selected-thread", "session_id": session["id"], **kwargs},
        )

    return store, project, employee, session, workspace, inspection, calls, act


def test_preview_does_not_issue_or_write_config_and_apply_is_idempotent(setup):
    store, project, employee, session, workspace, _, calls, act = setup
    preview = act()
    assert preview["state"] == "confirmation_required" and not preview["connection_verified"]
    assert not (workspace / ".codex").exists()
    assert "token" not in json.dumps(preview)
    before = list_sessions(store, employee["id"], project["id"])
    assert len(before) == 1
    with pytest.raises(WorkbenchError, match="explicit_confirmation"):
        act("apply", plan_id=preview["plan_id"])
    result = act("apply", plan_id=preview["plan_id"], confirmed=True)
    assert result["state"] == "written_waiting_for_host" and not result["connection_verified"]
    target = workspace / ".codex/config.toml"
    content, stamp = target.read_bytes(), target.stat().st_mtime_ns
    assert session["id"].encode() in content
    assert act("apply", plan_id=preview["plan_id"], confirmed=True) == result
    assert target.stat().st_mtime_ns == stamp and calls == ["selected-thread"] * 2
    assert list_sessions(store, employee["id"], project["id"]) == before
    assert act("status", plan_id=preview["plan_id"])["state"] == "written_waiting_for_host"
    directory = store.directory / "codex-installs"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in directory.iterdir())


@pytest.mark.parametrize(
    "source", ["effective", "inherited", "disabled", "runtime", "offline", "missing", "untrusted"]
)
def test_conflicts_and_unknown_inventory_never_install(setup, source):
    store, _, _, _, workspace, inspection, _, act = setup
    mailbox = {"name": "alias", "mailbox_like": True, "enabled": True}
    if source == "effective":
        inspection["effective_servers"] = [mailbox]
    elif source in {"inherited", "disabled", "untrusted"}:
        inspection["layers"] = [
            {
                "scope": "user" if source == "inherited" else "project",
                "disabled": source != "inherited",
                "workspace_layer": source == "untrusted",
                "servers": [] if source == "untrusted" else [mailbox],
            }
        ]
    elif source == "runtime":
        inspection["runtime_servers"] = [
            {"name": "wrapper", "mailbox_like": True, "connected": True}
        ]
    elif source == "offline":
        inspection["runtime_servers"] = [
            {"name": "unknown", "mailbox_like": False, "connected": False}
        ]
    else:
        inspection["effective_servers"] = [
            {"name": "unknown", "mailbox_like": False, "enabled": True}
        ]
    with pytest.raises(WorkbenchError):
        act()
    assert not (workspace / ".codex").exists() and not (store.directory / "codex-installs").exists()


@pytest.mark.parametrize("fault", ["scope", "revoked", "link", "public", "wrong_token", "home"])
def test_invalid_session_never_reaches_host(setup, fault):
    store, project, employee, session, _, _, calls, act = setup
    path = store.directory / "mail-sessions" / (session["id"] + ".json")
    if fault in {"scope", "home", "wrong_token"}:
        record = json.loads(path.read_text())
        record[{"scope": "project_id", "home": "home", "wrong_token": "token"}[fault]] = (
            "not-the-registered-value"
        )
        path.write_text(json.dumps(record))
    elif fault == "revoked":
        revoke_session(store, session["id"])
    elif fault == "link":
        other = path.with_suffix(".backup")
        path.rename(other)
        path.symlink_to(other)
    else:
        path.chmod(0o644)
    with pytest.raises(WorkbenchError):
        act()
    assert calls == [] and len(list_sessions(store, employee["id"], project["id"])) == 1


@pytest.mark.parametrize("fault", ["disk", "host", "expired", "revoked", "workspace"])
def test_confirm_rechecks_preview_and_does_not_overwrite(setup, monkeypatch, fault):
    store, project, _, session, workspace, inspection, _, act = setup
    preview = act()
    if fault == "disk":
        (workspace / ".codex").mkdir()
        (workspace / ".codex/config.toml").write_text("# newer owner edit\n")
    elif fault == "host":
        inspection["config_digest"] = "b" * 64
    elif fault == "expired":
        monkeypatch.setattr(installer.time, "time", lambda: preview["expires_at"] + 1)
    elif fault == "revoked":
        revoke_session(store, session["id"])
    else:
        with store._transaction() as db:
            db.execute(
                "UPDATE projects SET path=? WHERE id=?", (str(workspace.parent), project["id"])
            )
    with pytest.raises(WorkbenchError):
        act("apply", plan_id=preview["plan_id"], confirmed=True)
    assert installer.config.read_config(workspace) == (
        b"# newer owner edit\n" if fault == "disk" else None
    )


@pytest.mark.parametrize("after_write", [False, True])
def test_unknown_result_reconciles_without_retry_or_reissue(setup, monkeypatch, after_write):
    store, project, employee, _, workspace, _, _, act = setup
    preview = act()
    real = installer.config.apply_plan
    calls = []

    def lose_ack(*args, **kwargs):
        calls.append(1)
        if after_write:
            real(*args, **kwargs)
        raise OSError("PRIVATE synthetic lost result")

    monkeypatch.setattr(installer.config, "apply_plan", lose_ack)
    with pytest.raises(WorkbenchError) as error:
        act("apply", plan_id=preview["plan_id"], confirmed=True)
    assert "PRIVATE" not in str(error.value)
    result = act("apply", plan_id=preview["plan_id"], confirmed=True)
    assert result["state"] == (
        "written_waiting_for_host" if after_write else "outcome_requires_review"
    )
    assert calls == [1] and len(list_sessions(store, employee["id"], project["id"])) == 1
    assert (installer.config.read_config(workspace) is not None) == after_write


def test_remove_owns_only_added_block_and_does_not_revoke_credential(setup):
    store, project, employee, _, workspace, _, _, act = setup
    preview = act()
    act("apply", plan_id=preview["plan_id"], confirmed=True)
    target = workspace / ".codex/config.toml"
    target.write_bytes(target.read_bytes() + b"\n[features]\nsynthetic = true\n")
    assert act("remove", plan_id=preview["plan_id"], confirmed=True)["state"] == "removed"
    assert target.read_bytes() == b"\n[features]\nsynthetic = true\n"
    assert act("remove", plan_id=preview["plan_id"], confirmed=True)["state"] == "removed"
    assert list_sessions(store, employee["id"], project["id"])[0]["active"]


def test_remove_refuses_user_changed_owned_entry(setup):
    _, _, _, _, workspace, _, _, act = setup
    preview = act()
    act("apply", plan_id=preview["plan_id"], confirmed=True)
    target = workspace / ".codex/config.toml"
    content = target.read_bytes().replace(
        b"# agent-mailbox managed MCP end", b"enabled = false\n# agent-mailbox managed MCP end"
    )
    target.write_bytes(content)
    with pytest.raises(WorkbenchError):
        act("remove", plan_id=preview["plan_id"], confirmed=True)
    assert target.read_bytes() == content


def test_http_auth_scope_and_argument_boundary(setup):
    from agent_mailbox.workbench import WorkbenchHTTP

    store, project, employee, session, workspace, _, calls, _ = setup
    server = WorkbenchHTTP(store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    credential = json.loads(
        (store.directory / "mail-sessions" / (session["id"] + ".json")).read_text()
    )["token"]

    def request(token, **kwargs):
        body = {
            "employee_id": employee["id"],
            "project_id": project["id"],
            "action": "preview",
            "thread_id": "selected-thread",
            "session_id": session["id"],
            **kwargs,
        }
        req = urllib.request.Request(
            server.endpoint + "/api/workbench/mailbox/installers/codex",
            data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    try:
        for token in ("", "wrong", credential):
            assert request(token)[0] == 401
        assert calls == []
        assert request(server.token, command="/untrusted")[0] >= 400
        other = store.create_project("Other", workspace.parent)
        assert request(server.token, project_id=other["id"])[0] >= 400
        code, preview = request(server.token)
        assert code == 200 and preview["state"] == "confirmation_required"
        code, result = request(
            server.token, action="apply", plan_id=preview["plan_id"], confirmed=True
        )
        assert code == 200 and result["state"] == "written_waiting_for_host"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_cross_employee_plan_and_untrusted_plan_paths_rejected(setup):
    store, project, _, _, workspace, _, _, act = setup
    preview = act()
    second = store.create_employee("Other employee", "codex", project["id"])
    with pytest.raises(WorkbenchError, match="scope_mismatch"):
        installer.installation(
            store, second["id"], project["id"], "apply", plan_id=preview["plan_id"], confirmed=True
        )
    for plan_id in ("../config", None, "z" * 32):
        with pytest.raises(WorkbenchError, match="plan_required"):
            act("status", plan_id=plan_id)
    assert not (workspace / ".codex").exists()


def test_private_record_hardlink_rejected(setup):
    store, _, _, _, workspace, _, _, act = setup
    preview = act()
    record = store.directory / "codex-installs" / (preview["plan_id"] + ".json")
    os.link(record, record.with_suffix(".link"))
    with pytest.raises(WorkbenchError, match="private_record_required"):
        act("apply", plan_id=preview["plan_id"], confirmed=True)
    assert not (workspace / ".codex").exists()


def test_history_scoped_without_secrets_and_handles_expired_preview(setup, monkeypatch):
    store, project, _, _, workspace, _, calls, act = setup
    assert act("history") == {"plans": []}
    assert not (store.directory / "codex-installs").exists()
    preview = act()
    history = act("history")["plans"]
    assert len(history) == 1 and history[0]["plan_id"] == preview["plan_id"]
    assert not ({"preview", "before", "after", "owned_block", "token"} & set(history[0]))
    other = store.create_employee("Other", "codex", project["id"])
    assert installer.installation(store, other["id"], project["id"], "history")["plans"] == []
    monkeypatch.setattr(installer.time, "time", lambda: preview["expires_at"] + 1)
    assert act("history")["plans"][0]["state"] == "preview_expired"
    assert calls == ["selected-thread"] and not (workspace / ".codex").exists()


def test_remove_lost_ack_reconciles_from_exact_postimage(setup, monkeypatch):
    _, _, _, _, workspace, _, _, act = setup
    preview = act()
    act("apply", plan_id=preview["plan_id"], confirmed=True)
    original = installer.config.apply_plan
    calls = []

    def lose_ack(*args, **kwargs):
        calls.append(1)
        original(*args, **kwargs)
        raise OSError("lost removal acknowledgement")

    monkeypatch.setattr(installer.config, "apply_plan", lose_ack)
    with pytest.raises(WorkbenchError):
        act("remove", plan_id=preview["plan_id"], confirmed=True)
    assert act("remove", plan_id=preview["plan_id"], confirmed=True)["state"] == "removed"
    assert calls == [1] and installer.config.read_config(workspace) == b""


def identity_control(setup, monkeypatch):
    store, project, employee, session, workspace, _, _, act = setup
    preview = act()
    act("apply", plan_id=preview["plan_id"], confirmed=True)
    receiver = installer.receiver
    monkeypatch.setattr(receiver, "_paths", lambda root: (store.directory, store.directory))
    monkeypatch.setattr(receiver, "_installed", lambda root: True)
    monkeypatch.setattr(receiver, "_codex_launcher_endpoint", lambda *args: "synthetic-only")
    calls = []
    reply = {
        "thread_id": "fresh-thread",
        "cwd": str(workspace),
        "identity": {
            "protocol": "agent-mailbox.identity.v1",
            "employee_id": employee["id"],
            "project_id": project["id"],
            "session_id": session["id"],
        },
    }

    def control(endpoint, operation, thread_id, *, entry_digest):
        assert (
            endpoint == "synthetic-only" and operation == "identity" and thread_id == "fresh-thread"
        )
        command, args = installer._credential(store, employee["id"], project["id"], session["id"])
        expected = hashlib.sha256(
            json.dumps(
                {"command": command, "args": args},
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        assert entry_digest == expected
        calls.append(operation)
        return copy.deepcopy(reply)

    monkeypatch.setattr(receiver, "_control", control)
    return preview, calls, reply


def test_verify_current_identity_is_not_persistent_or_notification_acceptance(setup, monkeypatch):
    store, _, _, _, workspace, _, _, act = setup
    preview, calls, _ = identity_control(setup, monkeypatch)
    directory = store.directory / "codex-installs"
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    config_before = installer.config.read_config(workspace)
    result = act("verify", plan_id=preview["plan_id"], thread_id="fresh-thread")
    assert result["connection_verified"] is True
    assert result["verified_thread_id"] == "fresh-thread" and result["checked_at"] > 0
    assert result["verification_scope"] == "mailbox_identity_only" and calls == ["identity"]
    assert act("status", plan_id=preview["plan_id"])["connection_verified"] is False
    assert act("history")["plans"][0]["connection_verified"] is False
    assert installer.config.read_config(workspace) == config_before
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before


@pytest.mark.parametrize(
    "fault", ["employee_id", "project_id", "session_id", "protocol", "cwd", "thread_id", "extra"]
)
def test_verify_rejects_wrong_actual_identity_and_thread_without_echoing_content(
    setup, monkeypatch, fault
):
    *_, act = setup
    preview, calls, reply = identity_control(setup, monkeypatch)
    if fault in {"cwd", "thread_id"}:
        reply[fault] = "/PRIVATE" if fault == "cwd" else "PRIVATE"
    else:
        reply["identity"][fault] = "PRIVATE"
    with pytest.raises(WorkbenchError, match="identity_scope_mismatch") as error:
        act("verify", plan_id=preview["plan_id"], thread_id="fresh-thread")
    assert "PRIVATE" not in str(error.value) and calls == ["identity"]


@pytest.mark.parametrize("fault", ["revoked", "config", "session", "removed"])
def test_verify_requires_current_owned_config_and_live_credential_before_host_call(
    setup, monkeypatch, fault
):
    store, _, _, session, workspace, _, _, act = setup
    preview, calls, _ = identity_control(setup, monkeypatch)
    kwargs = {}
    if fault == "revoked":
        revoke_session(store, session["id"])
    elif fault == "config":
        target = workspace / ".codex/config.toml"
        target.write_bytes(target.read_bytes() + b"# edited\n")
    elif fault == "removed":
        act("remove", plan_id=preview["plan_id"], confirmed=True)
    else:
        kwargs["session_id"] = "mail_session_other"
    with pytest.raises(WorkbenchError):
        act("verify", plan_id=preview["plan_id"], thread_id="fresh-thread", **kwargs)
    assert calls == []


@pytest.mark.parametrize("fault", ["config", "revoked"])
def test_verify_rechecks_after_host_call(setup, monkeypatch, fault):
    store, _, _, session, workspace, _, _, act = setup
    preview, _, _ = identity_control(setup, monkeypatch)
    original = installer.receiver._control

    def drift(*args, **kwargs):
        result = original(*args, **kwargs)
        if fault == "config":
            (workspace / ".codex/config.toml").write_text("# concurrent owner edit\n")
        else:
            revoke_session(store, session["id"])
        return result

    monkeypatch.setattr(installer.receiver, "_control", drift)
    with pytest.raises(WorkbenchError):
        act("verify", plan_id=preview["plan_id"], thread_id="fresh-thread")


def replacement_fixture(setup):
    store, project, employee, _session, workspace, inspection, _, act = setup
    old = act()
    act("apply", plan_id=old["plan_id"], confirmed=True)
    entry = installer.config._parse(installer.config.read_config(workspace))["mcp_servers"][
        installer.config.SERVER
    ]
    row = {
        "name": installer.config.SERVER,
        "enabled": True,
        "mailbox_like": True,
        "entry_digest": hashlib.sha256(
            json.dumps(entry, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    inspection.update(
        effective_servers=[row],
        layers=[
            {
                "scope": "project",
                "disabled": False,
                "workspace_layer": True,
                "servers": [copy.deepcopy(row)],
            }
        ],
        runtime_servers=[
            {
                "name": installer.config.SERVER,
                "mailbox_like": True,
                "connected": False,
                "tools_available": False,
            }
        ],
    )
    new = export_session(store, employee["id"], project["id"], "Synthetic replacement")
    return old, new


@pytest.mark.parametrize("old_state", ["active", "revoked", "expired"])
def test_replace_owned_session_once_preserves_edits_and_old_record_cannot_remove_new(
    setup, monkeypatch, old_state
):
    store, project, employee, session, workspace, _, _, act = setup
    old, new = replacement_fixture(setup)
    if old_state == "revoked":
        revoke_session(store, session["id"])
    elif old_state == "expired":
        with store._transaction() as db:
            db.execute(
                "UPDATE mailbox_sessions SET expires_at=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", session["id"]),
            )
    target = workspace / ".codex/config.toml"
    addition = b"\n[features]\nsynthetic = true\n"
    target.write_bytes(target.read_bytes() + addition)
    before = target.read_bytes()
    assert act("replaceable", session_id=new["id"])["plans"][0]["plan_id"] == old["plan_id"]
    preview = act("preview_replace", plan_id=old["plan_id"], session_id=new["id"])
    assert target.read_bytes() == before and preview["replaces"] == old["plan_id"]
    with pytest.raises(WorkbenchError, match="explicit_confirmation"):
        act("apply", plan_id=preview["plan_id"], session_id=new["id"])
    real, writes = installer.config.apply_plan, []

    def apply(*args, **kwargs):
        writes.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(installer.config, "apply_plan", apply)
    result = act("apply", plan_id=preview["plan_id"], session_id=new["id"], confirmed=True)
    assert result["state"] == "written_waiting_for_host" and not result["connection_verified"]
    assert addition in target.read_bytes() and new["id"].encode() in target.read_bytes()
    assert session["id"].encode() not in target.read_bytes()
    assert act("status", plan_id=old["plan_id"])["state"] == "replaced"
    assert act("remove", plan_id=old["plan_id"], confirmed=True)["state"] == "replaced"
    assert (
        act("apply", plan_id=preview["plan_id"], confirmed=True)["state"]
        == "written_waiting_for_host"
    )
    assert writes == [1] and len(list_sessions(store, employee["id"], project["id"])) == 2
    with pytest.raises(WorkbenchError, match="written_configuration_required"):
        act("verify", plan_id=old["plan_id"])
    assert act("remove", plan_id=preview["plan_id"], confirmed=True)["state"] == "removed"
    assert target.read_bytes() == addition


@pytest.mark.parametrize(
    "fault",
    [
        "missing_digest",
        "wrong_digest",
        "inherited",
        "alias",
        "disabled",
        "duplicate_layer",
        "unknown_offline",
        "tampered",
        "new_revoked",
        "cross_employee",
    ],
)
def test_replacement_rejects_unowned_or_invalid_configuration_and_new_credentials(setup, fault):
    store, project, _, _, workspace, inspection, _, act = setup
    old, new = replacement_fixture(setup)
    if fault == "missing_digest":
        inspection["effective_servers"][0].pop("entry_digest")
    elif fault == "wrong_digest":
        inspection["effective_servers"][0]["entry_digest"] = "0" * 64
    elif fault == "inherited":
        inspection["layers"][0].update(scope="user", workspace_layer=False)
    elif fault == "alias":
        inspection["runtime_servers"].append(
            {"name": "other", "mailbox_like": True, "connected": True, "tools_available": True}
        )
    elif fault == "disabled":
        inspection["layers"][0]["disabled"] = True
    elif fault == "duplicate_layer":
        inspection["layers"] *= 2
    elif fault == "unknown_offline":
        inspection["runtime_servers"].append(
            {"name": "other", "mailbox_like": False, "connected": False, "tools_available": False}
        )
    elif fault == "tampered":
        target = workspace / ".codex/config.toml"
        target.write_bytes(target.read_bytes().replace(b"command =", b"enabled = false\ncommand ="))
    elif fault == "new_revoked":
        revoke_session(store, new["id"])
    else:
        other = store.create_employee("Other", "codex", project["id"])
        new = export_session(store, other["id"], project["id"], "Other identity")
    before = installer.config.read_config(workspace)
    with pytest.raises(WorkbenchError):
        act("preview_replace", plan_id=old["plan_id"], session_id=new["id"])
    assert installer.config.read_config(workspace) == before


@pytest.mark.parametrize("after_write", [False, True])
def test_replacement_unknown_outcome_never_rewrites_or_allows_old_removal(
    setup, monkeypatch, after_write
):
    _, _, _, _, workspace, _, _, act = setup
    old, new = replacement_fixture(setup)
    preview = act("preview_replace", plan_id=old["plan_id"], session_id=new["id"])
    original, calls = installer.config.apply_plan, []

    def lose(*args, **kwargs):
        calls.append(1)
        if after_write:
            original(*args, **kwargs)
        raise OSError("lost replacement result")

    monkeypatch.setattr(installer.config, "apply_plan", lose)
    with pytest.raises(WorkbenchError):
        act("apply", plan_id=preview["plan_id"], confirmed=True)
    content = installer.config.read_config(workspace)
    assert act("apply", plan_id=preview["plan_id"], confirmed=True)["state"] == (
        "written_waiting_for_host" if after_write else "outcome_requires_review"
    )
    assert act("remove", plan_id=old["plan_id"], confirmed=True)["state"] == (
        "replaced" if after_write else "outcome_requires_review"
    )
    with pytest.raises(WorkbenchError, match="replacement_source_required"):
        act("preview_replace", plan_id=old["plan_id"], session_id=new["id"])
    assert calls == [1] and installer.config.read_config(workspace) == content


def test_competing_replacement_previews_cannot_both_write(setup):
    _, _, _, _, workspace, _, _, act = setup
    old, new = replacement_fixture(setup)
    a = act("preview_replace", plan_id=old["plan_id"], session_id=new["id"])
    b = act("preview_replace", plan_id=old["plan_id"], session_id=new["id"])
    act("apply", plan_id=a["plan_id"], confirmed=True)
    content = installer.config.read_config(workspace)
    with pytest.raises(WorkbenchError, match="replacement_source_required"):
        act("apply", plan_id=b["plan_id"], confirmed=True)
    assert installer.config.read_config(workspace) == content


@pytest.mark.parametrize(
    "fault", ["disk", "effective", "revoked", "revoked_during_inspection", "expired_preview"]
)
def test_replacement_revalidates_before_single_write(setup, monkeypatch, fault):
    store, _, _, _, workspace, inspection, _, act = setup
    old, new = replacement_fixture(setup)
    preview = act("preview_replace", plan_id=old["plan_id"], session_id=new["id"])
    if fault == "disk":
        target = workspace / ".codex/config.toml"
        target.write_bytes(target.read_bytes() + b"# concurrent owner edit\n")
    elif fault == "effective":
        inspection["config_digest"] = "b" * 64
    elif fault == "revoked":
        revoke_session(store, new["id"])
    elif fault == "revoked_during_inspection":
        original = installer._inspect

        def inspect(*args):
            result = original(*args)
            revoke_session(store, new["id"])
            return result

        monkeypatch.setattr(installer, "_inspect", inspect)
    else:
        monkeypatch.setattr(installer.time, "time", lambda: preview["expires_at"] + 1)
    content = installer.config.read_config(workspace)
    with pytest.raises(WorkbenchError):
        act("apply", plan_id=preview["plan_id"], confirmed=True)
    assert installer.config.read_config(workspace) == content


@pytest.mark.parametrize("replacement", [False, True])
@pytest.mark.parametrize("after_write", [False, True])
def test_explicit_recovery_observes_disk_without_rewriting_or_reviving_plan(
    setup, monkeypatch, replacement, after_write
):
    store, _, _, session, workspace, _, _, act = setup
    old = None
    if replacement:
        old, session = replacement_fixture(setup)
        preview = act("preview_replace", plan_id=old["plan_id"], session_id=session["id"])
    else:
        preview = act()
    real = installer.config.apply_plan

    def lose(*args, **kwargs):
        if after_write:
            real(*args, **kwargs)
        raise OSError("lost result")

    monkeypatch.setattr(installer.config, "apply_plan", lose)
    with pytest.raises(WorkbenchError):
        act("apply", plan_id=preview["plan_id"], confirmed=True)
    revoke_session(store, session["id"])
    content = installer.config.read_config(workspace)
    monkeypatch.setattr(
        installer.config,
        "apply_plan",
        lambda *a, **k: pytest.fail("recovery must not write config"),
    )
    monkeypatch.setattr(
        installer, "_inspect", lambda *a: pytest.fail("recovery must not call host")
    )
    with pytest.raises(WorkbenchError, match="explicit_confirmation"):
        act("recover", plan_id=preview["plan_id"])
    result = act("recover", plan_id=preview["plan_id"], confirmed=True)
    expected = "written_waiting_for_host" if after_write else "closed_at_original"
    assert result["state"] == expected and not result["connection_verified"]
    assert result["recovery_observation"] == (
        "current_matches_after" if after_write else "current_matches_before"
    )
    assert act("recover", plan_id=preview["plan_id"], confirmed=True)["state"] == expected
    assert act("apply", plan_id=preview["plan_id"], confirmed=True)["state"] == expected
    assert installer.config.read_config(workspace) == content
    if old:
        assert act("status", plan_id=old["plan_id"])["state"] == (
            "replaced" if after_write else "written_waiting_for_host"
        )


@pytest.mark.parametrize("after_write", [False, True])
def test_removal_recovery_keeps_user_edits_and_never_repeats_removal(
    setup, monkeypatch, after_write
):
    _, _, _, _, workspace, _, _, act = setup
    preview = act()
    act("apply", plan_id=preview["plan_id"], confirmed=True)
    target = workspace / ".codex/config.toml"
    target.write_bytes(target.read_bytes() + b"# unrelated owner edit\n")
    real = installer.config.apply_plan

    def lose(*args, **kwargs):
        if after_write:
            real(*args, **kwargs)
        raise OSError("lost removal")

    monkeypatch.setattr(installer.config, "apply_plan", lose)
    with pytest.raises(WorkbenchError):
        act("remove", plan_id=preview["plan_id"], confirmed=True)
    content = target.read_bytes()
    monkeypatch.setattr(
        installer.config, "apply_plan", lambda *a, **k: pytest.fail("recovery must not write")
    )
    result = act("recover", plan_id=preview["plan_id"], confirmed=True)
    assert result["state"] == ("removed" if after_write else "configuration_changed")
    assert result["recovery_observation"] == (
        "current_matches_removed" if after_write else "current_matches_removal_before"
    )
    assert target.read_bytes() == content


def test_recovery_record_interruption_is_resumable_and_cannot_release_another_plan(
    setup, monkeypatch
):
    store, _, _, _, workspace, _, _, act = setup
    old, new = replacement_fixture(setup)
    preview = act("preview_replace", plan_id=old["plan_id"], session_id=new["id"])
    monkeypatch.setattr(
        installer.config,
        "apply_plan",
        lambda *a, **k: (_ for _ in ()).throw(OSError("before write")),
    )
    with pytest.raises(WorkbenchError):
        act("apply", plan_id=preview["plan_id"], confirmed=True)
    content = installer.config.read_config(workspace)
    write = installer._write

    def fail_parent(directory, name, value, **kwargs):
        if name == old["plan_id"] + ".json" and not value.get("replacement_id"):
            raise OSError("lost parent write")
        return write(directory, name, value, **kwargs)

    monkeypatch.setattr(installer, "_write", fail_parent)
    with pytest.raises(WorkbenchError):
        act("recover", plan_id=preview["plan_id"], confirmed=True)
    assert act("status", plan_id=preview["plan_id"])["state"] == "closed_at_original"
    assert act("status", plan_id=old["plan_id"])["state"] == "outcome_requires_review"
    monkeypatch.setattr(installer, "_write", write)
    assert (
        act("recover", plan_id=preview["plan_id"], confirmed=True)["state"] == "closed_at_original"
    )
    assert act("status", plan_id=old["plan_id"])["state"] == "written_waiting_for_host"
    parent_file = store.directory / "codex-installs" / (old["plan_id"] + ".json")
    parent = json.loads(parent_file.read_text())
    parent["replacement_id"] = "f" * 32
    parent_file.write_text(json.dumps(parent))
    with pytest.raises(WorkbenchError, match="replacement_record_conflict"):
        act("recover", plan_id=preview["plan_id"], confirmed=True)
    assert json.loads(parent_file.read_text())["replacement_id"] == "f" * 32
    assert installer.config.read_config(workspace) == content


def test_recovery_never_overwrites_third_party_configuration(setup, monkeypatch):
    store, _, _, _, workspace, _, _, act = setup
    preview = act()

    def fail(*args, **kwargs):
        raise OSError("before write")

    monkeypatch.setattr(installer.config, "apply_plan", fail)
    with pytest.raises(WorkbenchError):
        act("apply", plan_id=preview["plan_id"], confirmed=True)
    (workspace / ".codex").mkdir()
    target = workspace / ".codex/config.toml"
    target.write_text("# New owner configuration\n")
    before = {p.name: p.read_bytes() for p in (store.directory / "codex-installs").iterdir()}
    assert (
        act("recover", plan_id=preview["plan_id"], confirmed=True)["state"]
        == "configuration_changed"
    )
    assert target.read_text() == "# New owner configuration\n"
    assert {
        p.name: p.read_bytes() for p in (store.directory / "codex-installs").iterdir()
    } == before


@pytest.mark.parametrize("fault", ["employee", "project", "workspace", "symlink", "public"])
def test_recovery_requires_scoped_private_record(setup, monkeypatch, fault):
    store, _, _, _, workspace, _, _, act = setup
    preview = act()
    path = store.directory / "codex-installs" / (preview["plan_id"] + ".json")
    record = json.loads(path.read_text())
    record["state"] = "applying"
    if fault in {"employee", "project", "workspace"}:
        record[
            {"employee": "employee_id", "project": "project_id", "workspace": "workspace"}[fault]
        ] = "other"
    path.write_text(json.dumps(record))
    if fault == "symlink":
        other = path.with_suffix(".backup")
        path.rename(other)
        path.symlink_to(other)
    elif fault == "public":
        path.chmod(0o644)
    before = path.read_bytes()
    monkeypatch.setattr(
        installer.config, "apply_plan", lambda *a, **k: pytest.fail("must not write")
    )
    with pytest.raises(WorkbenchError):
        act("recover", plan_id=preview["plan_id"], confirmed=True)
    assert path.read_bytes() == before and installer.config.read_config(workspace) is None


@pytest.mark.parametrize("current", ["before", "after", "absent"])
def test_legacy_removal_recovery_does_not_invent_missing_preimage(setup, monkeypatch, current):
    store, _, _, _, workspace, _, _, act = setup
    preview = act()
    act("apply", plan_id=preview["plan_id"], confirmed=True)
    path = store.directory / "codex-installs" / (preview["plan_id"] + ".json")
    record = json.loads(path.read_text())
    record.update(state="removing", removed_sha256=hashlib.sha256(b"").hexdigest())
    path.write_text(json.dumps(record))
    target = workspace / ".codex/config.toml"
    if current == "after":
        target.write_bytes(b"")
    elif current == "absent":
        target.unlink()
    before = installer.config.read_config(workspace)
    monkeypatch.setattr(
        installer.config, "apply_plan", lambda *a, **k: pytest.fail("must not write")
    )
    result = act("recover", plan_id=preview["plan_id"], confirmed=True)
    assert result["state"] == ("removed" if current == "after" else "configuration_changed")
    assert installer.config.read_config(workspace) == before
    if current == "absent":
        assert act("status", plan_id=preview["plan_id"])["state"] == "configuration_changed"


def test_recovery_serializes_duplicate_requests_and_old_plan_cannot_affect_replacement(
    setup, monkeypatch
):
    store, _, _, _, workspace, _, _, act = setup
    old, new = replacement_fixture(setup)
    preview = act("preview_replace", plan_id=old["plan_id"], session_id=new["id"])
    act("apply", plan_id=preview["plan_id"], confirmed=True)
    content = installer.config.read_config(workspace)
    directory = store.directory / "codex-installs"
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    assert act("recover", plan_id=old["plan_id"], confirmed=True)["state"] == "replaced"
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
    # Synthetic crash after configuration write but before recording its result.
    path = directory / (preview["plan_id"] + ".json")
    record = json.loads(path.read_text())
    record["state"] = "applying"
    path.write_text(json.dumps(record))
    monkeypatch.setattr(
        installer.config, "apply_plan", lambda *a, **k: pytest.fail("must not write")
    )
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: act("recover", plan_id=preview["plan_id"], confirmed=True), range(2))
        )
    assert all(r["state"] == "written_waiting_for_host" for r in results)
    assert installer.config.read_config(workspace) == content
