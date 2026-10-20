"""First-run config safety: real temp files, no host or model execution."""

import base64
import concurrent.futures
import json
import os
import stat

import pytest

from agent_mailbox import workbench_codex_config as config


def install_plan(before=None, path="/private/synthetic/session.json"):
    return config.plan_install(
        before, command="/synthetic/Agent Mailbox", args=["mailbox-mcp", "--session-file", path]
    )


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    journal = tmp_path / "private"
    journal.mkdir(mode=0o700)
    return root, journal


@pytest.mark.parametrize(
    "before",
    [
        b"",
        b"# user comment, no trailing newline",
        b"# CRLF\r\n[features]\r\nthing = true\r\n",
        b'[mcp_servers.other]\ncommand = "keep"\nargs = ["quoted\\"arg"]\n',
        b'mcp_servers.other.command = "keep"\n',
        b"[mcp_servers]\n",
    ],
)
def test_existing_bytes_and_semantics_preserved_and_exact_removal(before):
    plan = install_plan(before)
    assert plan.after.startswith(before)
    parsed = config.tomllib.loads(plan.after.decode())
    assert parsed["mcp_servers"][config.SERVER]["args"][-1] == "/private/synthetic/session.json"
    assert config.plan_remove(plan.after, owned_block=plan.owned_block).after == before


@pytest.mark.parametrize(
    "before",
    [
        b'[mcp_servers.agent-mailbox-project]\ncommand="other"\n',
        b'[mcp_servers.an_alias]\ncommand="/bin/python"\nargs=["-m","agent_mailbox"]\n',
        b'[mcp_servers.disabled_old]\nenabled=false\ncommand="mailbox-mcp"\n',
        b'[mcp_servers.renamed]\ncommand="wrapper"\nargs=["--session-file","/private/other.json"]\n',
    ],
)
def test_no_silent_identity_overwrite_even_alias_or_disabled(before):
    with pytest.raises(ValueError, match="identity_conflict"):
        install_plan(before)


@pytest.mark.parametrize("before", [b"bad=[", b"\xff", b"mcp_servers=3", b"mcp_servers={a=1}"])
def test_invalid_input_is_not_repaired_or_overwritten(before):
    with pytest.raises(ValueError):
        install_plan(before)


def test_closed_inline_table_is_explicit_manual_merge():
    with pytest.raises(ValueError, match="manual_merge"):
        install_plan(b'mcp_servers={other={command="keep"}}\n')


def test_preview_does_not_leak_existing_secret():
    plan = install_plan(b'[mcp_servers.other.env]\nSECRET="private-sentinel"\n')
    preview = json.dumps(plan.preview())
    assert "private-sentinel" not in preview
    assert "confirmation_required" in preview
    assert "workspace_codex_sessions" in preview


def test_paths_cannot_inject_another_setting():
    path = '/private/名字/"\n[permissions]\nallow=true\t\\.json'
    plan = install_plan(path=path)
    parsed = config.tomllib.loads(plan.after.decode())
    assert set(parsed) == {"mcp_servers"}
    assert parsed["mcp_servers"][config.SERVER]["args"][-1] == path


@pytest.mark.parametrize(
    "args",
    [
        ["--token", "secret"],
        ["mailbox-mcp", "--token", "/secret"],
        ["-m", "wrong", "mailbox-mcp", "--session-file", "/a"],
        ["mailbox-mcp", "--session-file", "relative"],
    ],
)
def test_only_private_session_file_command_shape(args):
    with pytest.raises(ValueError, match="invalid_command"):
        config.plan_install(None, command="/exe", args=args)


def test_isolated_python_mailbox_command_is_accepted():
    args = ["-E", "-m", "agent_mailbox", "mailbox-mcp", "--session-file", "/private/s.json"]
    plan = config.plan_install(None, command="/synthetic/python", args=args)
    parsed = config.tomllib.loads(plan.after.decode())
    assert parsed["mcp_servers"][config.SERVER]["args"] == args


def test_remove_preserves_later_independent_changes():
    original = b"[features]\nkeep = true\n"
    plan = install_plan(original)
    later = b'\n[mcp_servers.other]\ncommand="do-not-remove"\n'
    removal = config.plan_remove(plan.after + later, owned_block=plan.owned_block)
    assert removal.after == original + later


def test_renewal_is_one_plan_and_preserves_later_settings(workspace):
    root, journal = workspace
    old = install_plan()
    config.apply_plan(root, old, journal_directory=journal)
    file = root / ".codex/config.toml"
    later = b'\n[mcp_servers.other]\ncommand="keep"\n'
    file.write_bytes(old.after + later)
    renewal = config.plan_replace(
        file.read_bytes(),
        owned_block=old.owned_block,
        command="/synthetic/new-version",
        args=["mailbox-mcp", "--session-file", "/new.json"],
    )
    assert file.read_bytes() == old.after + later  # Planning never removes the live entry.
    config.apply_plan(root, renewal, journal_directory=journal)
    parsed = config.tomllib.loads(file.read_text())
    assert parsed["mcp_servers"][config.SERVER]["args"][-1] == "/new.json"
    assert parsed["mcp_servers"]["other"]["command"] == "keep"
    assert old.owned_block not in file.read_bytes()


def test_too_large_config_rejected():
    with pytest.raises(ValueError, match="config_size"):
        install_plan(b"#" + b"x" * config.LIMIT)


@pytest.mark.parametrize(
    "change",
    [
        lambda data: data.replace(b"/private/synthetic", b"/different"),
        lambda data: data + b"enabled = false\n",
        lambda data: data + b'\n[mcp_servers.another]\ncommand="mailbox-mcp"\n',
    ],
)
def test_remove_rejects_changed_owned_meaning(change):
    plan = install_plan()
    with pytest.raises(ValueError, match="owned_block_changed"):
        config.plan_remove(change(plan.after), owned_block=plan.owned_block)


def test_preview_is_zero_write_then_install_and_revoke_roundtrip(workspace):
    root, journal = workspace
    assert config.read_config(root) is None
    assert list(root.iterdir()) == []
    plan = install_plan()
    result = config.apply_plan(root, plan, journal_directory=journal)
    assert result["state"] == "written_waiting_for_host"
    file = root / ".codex/config.toml"
    assert file.read_bytes() == plan.after
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    records = list(journal.glob("*.json"))
    assert len(records) == 1 and stat.S_IMODE(records[0].stat().st_mode) == 0o600
    record = json.loads(records[0].read_text())
    assert record["before"] is None and record["state"] == "prepared"
    assert base64.b64decode(record["owned_block"]) == plan.owned_block
    later = b'\n[mcp_servers.keep]\ncommand="unchanged"\n'
    file.write_bytes(plan.after + later)
    removal = config.plan_remove(file.read_bytes(), owned_block=plan.owned_block)
    config.apply_plan(root, removal, journal_directory=journal)
    assert file.read_bytes() == later


def test_drift_after_preview_preserved_and_no_backup_created(workspace):
    root, journal = workspace
    plan = install_plan()
    (root / ".codex").mkdir()
    file = root / ".codex/config.toml"
    file.write_bytes(b"# user just changed this\n")
    with pytest.raises(ValueError, match="changed_refresh_preview"):
        config.apply_plan(root, plan, journal_directory=journal)
    assert file.read_bytes() == b"# user just changed this\n"
    assert list(journal.iterdir()) == []


def test_external_change_during_prepare_is_not_overwritten(workspace, monkeypatch):
    root, journal = workspace
    original_read = config._read
    reads = 0
    changed = b"# external edit during preparation\n"

    def edit_before_second_read(fd):
        nonlocal reads
        reads += 1
        if reads == 2:
            (root / ".codex/config.toml").write_bytes(changed)
        return original_read(fd)

    monkeypatch.setattr(config, "_read", edit_before_second_read)
    with pytest.raises(ValueError, match="changed_refresh_preview"):
        config.apply_plan(root, install_plan(), journal_directory=journal)
    assert (root / ".codex/config.toml").read_bytes() == changed
    assert len(list(journal.glob("*.json"))) == 1  # Prepared backup, no blind rollback.
    assert not list((root / ".codex").glob("*.tmp"))


def test_directory_replacement_during_prepare_rejected(workspace, monkeypatch):
    root, journal = workspace
    original_read = config._read
    reads = 0

    def replace_directory(fd):
        nonlocal reads
        reads += 1
        if reads == 2:
            (root / ".codex").rename(root / "old-codex")
            (root / ".codex").mkdir()
        return original_read(fd)

    monkeypatch.setattr(config, "_read", replace_directory)
    with pytest.raises(ValueError, match="directory_changed"):
        config.apply_plan(root, install_plan(), journal_directory=journal)
    assert list((root / ".codex").iterdir()) == []
    assert list((root / "old-codex").iterdir()) == []


def test_existing_permissions_backup_and_write_failure(workspace, monkeypatch):
    root, journal = workspace
    (root / ".codex").mkdir()
    file = root / ".codex/config.toml"
    original = b"# keep exactly\n"
    file.write_bytes(original)
    file.chmod(0o640)
    plan = install_plan(config.read_config(root))
    real_replace = config.os.replace

    def fail(*args, **kwargs):
        raise OSError("injected storage failure")

    monkeypatch.setattr(config.os, "replace", fail)
    with pytest.raises(OSError, match="storage failure"):
        config.apply_plan(root, plan, journal_directory=journal)
    assert file.read_bytes() == original
    assert list((root / ".codex").iterdir()) == [file]
    record = json.loads(next(journal.glob("*.json")).read_text())
    assert base64.b64decode(record["before"]) == original
    monkeypatch.setattr(config.os, "replace", real_replace)
    config.apply_plan(root, plan, journal_directory=journal)
    assert file.read_bytes() == plan.after
    assert stat.S_IMODE(file.stat().st_mode) == 0o640


@pytest.mark.parametrize("kind", ["symlink-file", "hardlink", "fifo", "symlink-dir", "writable"])
def test_unsafe_paths_rejected_without_touching_target(workspace, kind):
    root, journal = workspace
    external = root.parent / "external"
    external.write_bytes(b"# untouched\n")
    directory = root / ".codex"
    if kind == "symlink-dir":
        directory.symlink_to(journal, target_is_directory=True)
    else:
        directory.mkdir()
        file = directory / "config.toml"
        if kind == "symlink-file":
            file.symlink_to(external)
        elif kind == "hardlink":
            os.link(external, file)
        elif kind == "fifo":
            os.mkfifo(file)
        else:
            file.write_bytes(b"")
            file.chmod(0o666)
    with pytest.raises((ValueError, OSError)):
        config.apply_plan(root, install_plan(), journal_directory=journal)
    assert external.read_bytes() == b"# untouched\n"
    assert list(journal.iterdir()) == []


def test_concurrent_apply_only_one_writer(workspace):
    root, journal = workspace
    plan = install_plan()

    def apply():
        try:
            return config.apply_plan(root, plan, journal_directory=journal)["state"]
        except (BlockingIOError, ValueError):
            return "rejected"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(lambda _: apply(), range(2)))
    assert sorted(result) == ["rejected", "written_waiting_for_host"]
    assert config.read_config(root) == plan.after
    assert len(list(journal.glob("*.json"))) == 1
