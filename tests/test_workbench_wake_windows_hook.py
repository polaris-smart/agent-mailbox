"""Windows hook argv, trusted interpreter discovery, and real native execution."""

import json
import sys
from types import SimpleNamespace

import pytest

from agent_mailbox import workbench_wake as wk


def test_windows_interpreter_comes_from_system_directory_not_environment(tmp_path, monkeypatch):
    import ctypes

    system = tmp_path / "system"
    binary = system / "WindowsPowerShell/v1.0/powershell.exe"
    binary.parent.mkdir(parents=True)
    binary.touch()
    monkeypatch.setenv("SystemRoot", str(tmp_path / "untrusted"))
    monkeypatch.setenv("PATH", str(tmp_path / "untrusted"))
    monkeypatch.chdir(tmp_path)

    def system_directory(buffer, size):
        buffer.value = str(system)
        return len(buffer.value)

    kernel = SimpleNamespace(GetSystemDirectoryW=system_directory)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: kernel, raising=False)
    assert wk._windows_powershell() == str(binary)


def test_windows_hook_command_uses_literal_arguments_and_binary_capture(tmp_path, monkeypatch):
    monkeypatch.setattr(wk.sys, "platform", "win32")
    binary = tmp_path / "trusted powershell.exe"
    monkeypatch.setattr(wk, "_windows_powershell", lambda: str(binary))
    hook = tmp_path / "wake" / wk.WINDOWS_HOOK_FILENAME
    hook.parent.mkdir()
    hook.write_text("exit 0\n", encoding="utf-8")
    hook.chmod(0o600)  # PS1 scripts do not need POSIX execute bits.
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        assert kwargs == {"capture_output": True, "timeout": 30}
        return SimpleNamespace(returncode=0, stdout=b"\xff\xfe\x80", stderr=b"")

    assert wk.hook_deliver(tmp_path, runner=runner)(
        "employee_demo", {"reason": "new_mail", "fresh": ["m1"]}
    )
    prefix = [str(binary), "-NoProfile", "-NonInteractive", "-File", str(hook.resolve())]
    assert calls[0][:-2] == prefix
    assert calls[0][-2] == "employee_demo"
    assert calls[0][-1].startswith("wake-employee_demo-")
    assert (tmp_path / "wake/claims/m1.ok").is_file()


def test_missing_windows_interpreter_records_pending_without_delivery(tmp_path, monkeypatch):
    monkeypatch.setattr(wk.sys, "platform", "win32")
    monkeypatch.setattr(wk, "_windows_powershell", lambda: None)
    hook = tmp_path / "wake" / wk.WINDOWS_HOOK_FILENAME
    hook.parent.mkdir()
    hook.write_text("exit 0\n", encoding="utf-8")

    def runner(*args, **kwargs):
        pytest.fail("a missing interpreter must never invoke the host")

    assert not wk.hook_deliver(tmp_path, runner=runner)(
        "employee_demo", {"reason": "new_mail", "fresh": ["m1"]}
    )
    assert (tmp_path / "wake/claims/m1.pending").is_file()
    assert not (tmp_path / "wake/claims/m1.ok").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="requires real WindowsPowerShell on Windows")
@pytest.mark.parametrize("returncode", [0, 7])
def test_real_windows_powershell_hook_reports_receipt_or_failure(tmp_path, returncode):
    home = tmp_path / "宿主 with space ' quote"
    hook = home / "wake" / wk.WINDOWS_HOOK_FILENAME
    hook.parent.mkdir(parents=True)
    hook.write_text(
        "param([string]$EmployeeId, [string]$DeliveryId)\n"
        "$ErrorActionPreference = 'Stop'\n"
        "$data = @($EmployeeId, $DeliveryId) | ConvertTo-Json -Compress\n"
        "[System.IO.File]::WriteAllText((Join-Path $PSScriptRoot 'received.json'), "
        "$data, [System.Text.UTF8Encoding]::new($false))\n"
        "[Console]::OpenStandardOutput().Write([byte[]](255, 254, 128), 0, 3)\n"
        f"exit {returncode}\n",
        encoding="utf-8-sig",
    )
    assert wk._windows_powershell() is not None, "the native Windows interpreter must be available"
    employee = "employee_接收者"
    delivered = wk.hook_deliver(home)(employee, {"reason": "new_mail", "fresh": ["m1"]})
    assert delivered is (returncode == 0)
    markers = list((home / "wake/wake-outbox").glob("*.json"))
    assert len(markers) == 1
    received = json.loads((hook.parent / "received.json").read_text(encoding="utf-8"))
    assert received == [employee, markers[0].stem]
    assert (home / "wake/claims/m1.ok").exists() is (returncode == 0)
    if returncode:
        assert not (home / "wake/claims/m1").exists()
