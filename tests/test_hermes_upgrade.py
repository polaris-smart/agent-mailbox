"""Existing Hermes receiver replacement stays reversible and never edits config."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "hermes_upgrade_setup", ROOT / "integrations/hermes-mailbox-host/setup.py"
)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


@pytest.fixture
def case(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o700)
    socket = tmp_path / "receiver.sock"
    config = profile / "config.yaml"
    value = {
        "plugins": {
            "entries": {
                "agent-mailbox-host": {
                    "allow_gateway_injection": True,
                    "settings": {
                        "socket_path": str(socket),
                        "home": str(tmp_path / "mailbox"),
                        "sessions": {"synthetic": {"session_id": "existing", "platform": "feishu"}},
                    },
                }
            }
        },
    }
    config.write_text(json.dumps(value))
    config.chmod(0o600)
    api = SimpleNamespace(
        config=SimpleNamespace(
            get_config_path=lambda: config,
            require_readable_config_before_write=lambda p: json.loads(p.read_text()),
        )
    )
    replacement = setup.Replacement(profile, api=api)
    replacement.plugins.mkdir(mode=0o700)
    replacement.target.mkdir(mode=0o700)
    old = b"synthetic-legacy-plugin"
    monkeypatch.setattr(setup.Replacement, "LEGACY_INIT", setup.digest(old))
    (replacement.target / "__init__.py").write_bytes(old)
    (replacement.target / "plugin.yaml").write_bytes(
        (replacement.preparation.source / "plugin.yaml").read_bytes()
    )
    (replacement.target / "README.md").write_bytes(b"synthetic legacy readme")
    return SimpleNamespace(replacement=replacement, config=config, socket=socket, old=old)


def test_existing_component_preview_apply_and_rollback(case):
    before = case.config.read_bytes()
    result = case.replacement.preview()
    assert result["state"] == "preview"
    assert result["requires_stopped_gateway"]
    assert case.config.read_bytes() == before

    plan = result["plan_id"]
    with pytest.raises(ValueError, match="confirmation"):
        case.replacement.apply(plan)
    applied = case.replacement.apply(plan, confirmed=True, gateway_stopped=True)
    assert applied["state"] == "published_runtime_unverified"
    assert not applied["runtime_verified"]
    assert case.config.read_bytes() == before
    for name in setup.FILES:
        assert (case.replacement.target / name).read_bytes() == (
            case.replacement.preparation.source / name
        ).read_bytes()
    restored = case.replacement.rollback(plan, confirmed=True, gateway_stopped=True)
    assert restored["state"] == "rolled_back"
    assert (case.replacement.target / "__init__.py").read_bytes() == case.old
    assert case.config.read_bytes() == before


def test_known_five_file_component_can_preview_but_changed_bytes_refuse(case, monkeypatch):
    target = case.replacement.target
    (target / "pinned.py").write_bytes(b"known pinned")
    (target / "live.py").write_bytes(b"known live")
    known = {
        name: setup.digest((target / name).read_bytes())
        for name in ("__init__.py", "pinned.py", "live.py", "plugin.yaml", "README.md")
    }
    monkeypatch.setattr(setup.Replacement, "PRE_REAUTH", known)
    plan = case.replacement.preview()["plan_id"]
    assert case.replacement.status(plan)["state"] == "preview"
    (target / "pinned.py").write_bytes(b"changed pinned")
    with pytest.raises(ValueError, match="installed_component_changed_refresh_required"):
        case.replacement.apply(plan, confirmed=True, gateway_stopped=True)
    assert not (case.replacement.records / plan / "intent.json").exists()


def test_known_082_five_file_predecessor_can_preview_without_broad_acceptance(case, monkeypatch):
    target = case.replacement.target
    (target / "pinned.py").write_bytes(b"known 082 pinned")
    (target / "live.py").write_bytes(b"known 082 live")
    known = {
        name: setup.digest((target / name).read_bytes())
        for name in ("__init__.py", "pinned.py", "live.py", "plugin.yaml", "README.md")
    }
    monkeypatch.setattr(setup.Replacement, "PRE_PRIVATE_082", known)
    assert case.replacement.preview()["state"] == "preview"
    (target / "live.py").write_bytes(b"locally changed live")
    with pytest.raises(ValueError, match="unrecognized_hermes_component_version"):
        case.replacement.preview()


def test_known_six_file_authority_predecessor_can_preview_only_exact_bytes(case, monkeypatch):
    target = case.replacement.target
    for name in ("pinned.py", "live.py", "private_platform.py"):
        (target / name).write_bytes((case.replacement.preparation.source / name).read_bytes())
    known = {name: setup.digest((target / name).read_bytes()) for name in setup.FILES}
    monkeypatch.setattr(setup.Replacement, "PRE_AUTHORITY_WRAPPER", known)
    assert case.replacement.preview()["state"] == "preview"
    (target / "pinned.py").write_bytes(b"locally changed pinned")
    with pytest.raises(ValueError, match="unrecognized_hermes_component_version"):
        case.replacement.preview()


def test_authority_admission_with_old_metadata_can_upgrade_only_exact_bytes(case, monkeypatch):
    target = case.replacement.target
    for name in ("pinned.py", "private_platform.py"):
        (target / name).write_bytes((case.replacement.preparation.source / name).read_bytes())
    (target / "live.py").write_bytes(b"previous metadata reader")
    known = {name: setup.digest((target / name).read_bytes()) for name in setup.FILES}
    monkeypatch.setattr(setup.Replacement, "PRE_AUTHORITY_METADATA", known)
    assert case.replacement.preview()["state"] == "preview"
    (target / "live.py").write_bytes(b"unrecognized local metadata change")
    with pytest.raises(ValueError, match="unrecognized_hermes_component_version"):
        case.replacement.preview()


@pytest.mark.parametrize("changed", ["config", "old_component", "new_component", "socket"])
def test_changed_or_live_receiver_refuses_without_replacing(case, changed, monkeypatch):
    plan = case.replacement.preview()["plan_id"]
    if changed == "config":
        case.config.write_bytes(case.config.read_bytes() + b" ")
    elif changed == "old_component":
        (case.replacement.target / "__init__.py").write_bytes(b"third party")
    elif changed == "new_component":
        source = case.replacement.preparation.source_files()
        monkeypatch.setattr(
            case.replacement.preparation,
            "source_files",
            lambda: {**source, "README.md": b"changed"},
        )
    else:
        case.socket.touch()
    with pytest.raises(ValueError):
        case.replacement.apply(plan, confirmed=True, gateway_stopped=True)
    assert (case.replacement.target / "__init__.py").exists()
    assert not (case.replacement.records / plan / "intent.json").exists()


def test_interrupted_after_backup_has_explicit_rollback(case, monkeypatch):
    plan = case.replacement.preview()["plan_id"]
    rename = setup.os.rename
    staging = case.replacement.records / plan / "next"

    def fail_second(source, target):
        if Path(source) == staging:
            raise OSError("synthetic interruption")
        return rename(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(setup.os, "rename", fail_second)
        with pytest.raises(OSError, match="synthetic interruption"):
            case.replacement.apply(plan, confirmed=True, gateway_stopped=True)
    assert case.replacement.status(plan)["state"] == "interrupted_before_publication"
    assert (
        case.replacement.rollback(plan, confirmed=True, gateway_stopped=True)["state"]
        == "rolled_back"
    )
    assert (case.replacement.target / "__init__.py").read_bytes() == case.old


def test_rollback_record_interruption_can_be_finalized_without_replacing_again(case, monkeypatch):
    plan = case.replacement.preview()["plan_id"]
    case.replacement.apply(plan, confirmed=True, gateway_stopped=True)
    write = setup.write_new

    def fail_record(path, raw):
        if Path(path).name == "rolled_back.json":
            raise OSError("synthetic record interruption")
        return write(path, raw)

    with monkeypatch.context() as patch:
        patch.setattr(setup, "write_new", fail_record)
        with pytest.raises(OSError, match="record interruption"):
            case.replacement.rollback(plan, confirmed=True, gateway_stopped=True)
    assert case.replacement.status(plan)["state"] == "rollback_completed_unrecorded"
    assert (
        case.replacement.rollback(plan, confirmed=True, gateway_stopped=True)["state"]
        == "rolled_back"
    )
    assert (case.replacement.target / "__init__.py").read_bytes() == case.old


def test_broken_symlink_after_interruption_is_not_overwritten(case, monkeypatch):
    plan = case.replacement.preview()["plan_id"]
    rename = setup.os.rename
    staging = case.replacement.records / plan / "next"

    def fail_second(source, target):
        if Path(source) == staging:
            raise OSError("synthetic interruption")
        return rename(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(setup.os, "rename", fail_second)
        with pytest.raises(OSError):
            case.replacement.apply(plan, confirmed=True, gateway_stopped=True)
    case.replacement.target.symlink_to(case.replacement.profile / "unknown")
    assert case.replacement.status(plan)["state"] == "inspection_required"
    with pytest.raises(ValueError, match="state_conflict"):
        case.replacement.rollback(plan, confirmed=True, gateway_stopped=True)
    assert (case.replacement.records / plan / "previous" / "__init__.py").read_bytes() == case.old


def test_identical_bytes_in_replaced_old_directory_do_not_pass_apply(case):
    plan = case.replacement.preview()["plan_id"]
    displaced = case.replacement.profile / "displaced"
    case.replacement.target.rename(displaced)
    case.replacement.target.mkdir(mode=0o700)
    for child in displaced.iterdir():
        (case.replacement.target / child.name).write_bytes(child.read_bytes())
    with pytest.raises(ValueError, match="installed_component_changed_refresh_required"):
        case.replacement.apply(plan, confirmed=True, gateway_stopped=True)
    assert not (case.replacement.records / plan / "intent.json").exists()


def test_identical_bytes_in_replaced_new_directory_do_not_pass_rollback(case):
    plan = case.replacement.preview()["plan_id"]
    case.replacement.apply(plan, confirmed=True, gateway_stopped=True)
    displaced = case.replacement.profile / "displaced"
    case.replacement.target.rename(displaced)
    case.replacement.target.mkdir(mode=0o700)
    for child in displaced.iterdir():
        (case.replacement.target / child.name).write_bytes(child.read_bytes())
    assert case.replacement.status(plan)["state"] == "inspection_required"
    with pytest.raises(ValueError, match="state_conflict"):
        case.replacement.rollback(plan, confirmed=True, gateway_stopped=True)
    assert (case.replacement.records / plan / "previous" / "__init__.py").read_bytes() == case.old


def test_crash_after_new_directory_publication_can_be_rolled_back(case, monkeypatch):
    plan = case.replacement.preview()["plan_id"]
    write = setup.write_new

    def fail_published(path, raw):
        if Path(path).name == "published.json":
            raise OSError("synthetic publication record interruption")
        return write(path, raw)

    with monkeypatch.context() as patch:
        patch.setattr(setup, "write_new", fail_published)
        with pytest.raises(OSError, match="publication record interruption"):
            case.replacement.apply(plan, confirmed=True, gateway_stopped=True)
    assert case.replacement.status(plan)["state"] == "published_runtime_unverified"
    assert (
        case.replacement.rollback(plan, confirmed=True, gateway_stopped=True)["state"]
        == "rolled_back"
    )
