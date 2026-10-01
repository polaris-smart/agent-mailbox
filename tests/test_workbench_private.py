"""Native filesystem access restrictions, not Windows chmod's read-only bit."""

import os
from types import SimpleNamespace

import pytest

from agent_mailbox.workbench_private import private_access, private_mode


def test_private_file_and_directory_access(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir()
    private_mode(directory, 0o700)
    assert private_access(directory, 0o700)
    source = directory / "credential.json"
    source.write_text("private fixture")
    private_mode(source, 0o600)
    assert private_access(source, 0o600)
    assert source.read_text() == "private fixture"
    # Restricting access must not make the current user's state read-only.
    source.write_text("updated fixture")
    assert source.read_text() == "updated fixture"


def test_acl_failure_has_no_chmod_fallback(tmp_path, monkeypatch):
    from agent_mailbox import workbench_private

    source = tmp_path / "state"
    source.write_text("fixture")

    def failed():
        raise OSError("ACL fixture failure")

    monkeypatch.setattr(workbench_private, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(workbench_private, "_windows", failed)
    with pytest.raises(OSError, match="ACL fixture failure"):
        private_mode(source, 0o600)


def test_missing_private_file_fails_closed(tmp_path):
    with pytest.raises(OSError):
        private_mode(tmp_path / "missing", 0o600)


def test_acl_read_failure_does_not_attempt_permission_write(tmp_path, monkeypatch):
    from agent_mailbox import workbench_private

    source = tmp_path / "state"
    source.write_text("fixture")

    def failed(path):
        raise OSError("ACL inspection failed")

    monkeypatch.setattr(workbench_private, "os", SimpleNamespace(name="nt"))
    # No mutating API is provided: a read failure must propagate before repair.
    monkeypatch.setattr(workbench_private, "_windows", lambda: (None, None, None, None, "SID"))
    monkeypatch.setattr(workbench_private, "_native_acl", failed)
    with pytest.raises(OSError, match="ACL inspection failed"):
        private_mode(source, 0o600)


@pytest.mark.skipif(os.name != "nt", reason="Requires actual Windows security descriptors")
def test_windows_removes_everyone_ace_and_inherited_acl(tmp_path, monkeypatch):
    from agent_mailbox.workbench_private import _windows

    source = tmp_path / "credential"
    source.write_text("fixture")
    c, w, advapi, kernel, _sid = _windows()
    descriptor = c.c_void_p()
    assert advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        "D:P(A;;FA;;;WD)", 1, c.byref(descriptor), None
    )
    try:
        present, defaulted, acl = w.BOOL(), w.BOOL(), c.c_void_p()
        assert advapi.GetSecurityDescriptorDacl(
            descriptor, c.byref(present), c.byref(acl), c.byref(defaulted)
        )
        assert advapi.SetNamedSecurityInfoW(str(source), 1, 0x80000004, None, None, acl, None) == 0
    finally:
        kernel.LocalFree(descriptor)
    assert not private_access(source, 0o600)
    write = advapi.SetNamedSecurityInfoW
    writes = []

    def observed_write(*args):
        writes.append(args[0])
        return write(*args)

    monkeypatch.setattr(advapi, "SetNamedSecurityInfoW", observed_write)
    private_mode(source, 0o600)
    assert writes == [str(source)]
    assert private_access(source, 0o600)
    private_mode(source, 0o600)
    assert writes == [str(source)]  # Already private: no second ACL rewrite.
    assert source.read_text() == "fixture"


@pytest.mark.skipif(os.name != "nt", reason="Requires actual Windows security descriptors")
@pytest.mark.parametrize("directory", [False, True])
def test_windows_private_acl_is_checked_each_time_without_rewrite(tmp_path, monkeypatch, directory):
    from agent_mailbox import workbench_private

    source = tmp_path / "private"
    source.mkdir() if directory else source.write_text("fixture")
    mode = 0o700 if directory else 0o600
    private_mode(source, mode)
    _, _, advapi, _, _ = workbench_private._windows()
    inspect = workbench_private._native_acl
    reads = []

    def observed_read(path):
        reads.append(path)
        return inspect(path)

    def forbidden_write(*args):
        raise AssertionError("An already private native ACL must not be rewritten")

    monkeypatch.setattr(workbench_private, "_native_acl", observed_read)
    monkeypatch.setattr(advapi, "SetNamedSecurityInfoW", forbidden_write)
    private_mode(source, mode)
    private_mode(source, mode)
    assert reads == [source, source]
    assert private_access(source, mode)


def test_numeric_sid_acl_verification_has_no_builtin_admin_alias_ambiguity():
    from agent_mailbox.workbench_private import _allowed_acl

    owner = "S-1-5-21-123-456-789-500"
    entries = [(0, 3, 0x1F01FF, sid) for sid in (owner, "S-1-5-18", "S-1-5-32-544")]
    assert _allowed_acl(0x1004, entries, owner, directory=True)
    assert not _allowed_acl(4, entries, owner, directory=True)
    assert not _allowed_acl(0x1004, entries + [(0, 3, 0x1F01FF, "S-1-1-0")], owner)
    assert not _allowed_acl(0x1004, entries[1:], owner)
    assert not _allowed_acl(0x1004, [(0, 0, 0x1F01FF, owner)], owner, directory=True)
    assert not _allowed_acl(0x1004, [(0, 0, 0x10000000, owner)], owner)
