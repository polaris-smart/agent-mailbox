"""Private POSIX modes or protected Windows DACLs for workbench secrets.

Windows chmod only changes a read-only bit. Restrict actual access to the current
user, SYSTEM and Administrators instead; fail closed if ACL operations fail.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _windows():
    import ctypes
    from ctypes import wintypes as w

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    signatures = {
        "OpenProcessToken": ([w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)], w.BOOL),
        "GetTokenInformation": (
            [w.HANDLE, ctypes.c_int, pointer, w.DWORD, ctypes.POINTER(w.DWORD)],
            w.BOOL,
        ),
        "ConvertSidToStringSidW": ([pointer, ctypes.POINTER(w.LPWSTR)], w.BOOL),
        "ConvertStringSecurityDescriptorToSecurityDescriptorW": (
            [w.LPCWSTR, w.DWORD, ctypes.POINTER(pointer), ctypes.POINTER(w.DWORD)],
            w.BOOL,
        ),
        "GetSecurityDescriptorDacl": (
            [pointer, ctypes.POINTER(w.BOOL), ctypes.POINTER(pointer), ctypes.POINTER(w.BOOL)],
            w.BOOL,
        ),
        "SetNamedSecurityInfoW": (
            [w.LPWSTR, ctypes.c_int, w.DWORD, pointer, pointer, pointer, pointer],
            w.DWORD,
        ),
        "GetNamedSecurityInfoW": (
            [
                w.LPCWSTR,
                ctypes.c_int,
                w.DWORD,
                pointer,
                pointer,
                pointer,
                pointer,
                ctypes.POINTER(pointer),
            ],
            w.DWORD,
        ),
        "ConvertSecurityDescriptorToStringSecurityDescriptorW": (
            [pointer, w.DWORD, w.DWORD, ctypes.POINTER(w.LPWSTR), ctypes.POINTER(w.DWORD)],
            w.BOOL,
        ),
    }
    for name, (args, result) in signatures.items():
        function = getattr(advapi, name)
        function.argtypes, function.restype = args, result
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [pointer], pointer
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [w.HANDLE], w.BOOL
    token = w.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    text = w.LPWSTR()
    try:
        needed = w.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, buffer, needed.value, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = pointer.from_buffer(buffer).value
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        current_sid = text.value
    finally:
        if text:
            kernel.LocalFree(ctypes.cast(text, pointer))
        kernel.CloseHandle(token)
    return ctypes, w, advapi, kernel, current_sid


def private_mode(path, mode):
    path = Path(path)
    if os.name != "nt":
        path.chmod(mode)
        return
    c, w, advapi, kernel, sid = _windows()
    descriptor = c.c_void_p()
    inherit = "OICI" if path.is_dir() else ""
    sddl = "D:P" + "".join(f"(A;{inherit};FA;;;{who})" for who in (sid, "SY", "BA"))
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, c.byref(descriptor), None
    ):
        raise c.WinError(c.get_last_error())
    try:
        present, defaulted, acl = w.BOOL(), w.BOOL(), c.c_void_p()
        if (
            not advapi.GetSecurityDescriptorDacl(
                descriptor, c.byref(present), c.byref(acl), c.byref(defaulted)
            )
            or not present.value
            or not acl
        ):
            raise OSError("Private Windows ACL could not be constructed")
        code = advapi.SetNamedSecurityInfoW(str(path), 1, 0x80000004, None, None, acl, None)
        if code:
            raise c.WinError(code)
    finally:
        kernel.LocalFree(descriptor)
    if not private_access(path, mode):
        raise OSError("Private Windows ACL verification failed")


def private_access(path, mode):
    """Inspect actual access, allowing only owner, SYSTEM and Administrators."""
    path = Path(path)
    if os.name != "nt":
        return path.stat().st_mode & 0o777 == mode
    c, w, advapi, kernel, sid = _windows()
    descriptor, text = c.c_void_p(), w.LPWSTR()
    code = advapi.GetNamedSecurityInfoW(
        str(path), 1, 4, None, None, None, None, c.byref(descriptor)
    )
    if code:
        raise c.WinError(code)
    try:
        if not advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, 1, 4, c.byref(text), None
        ):
            raise c.WinError(c.get_last_error())
        sddl = text.value
        entries = re.findall(r"\(([^()]*)\)", sddl)
        owner_present = False
        if not entries or not sddl.startswith("D:P"):
            return False
        for entry in entries:
            fields = entry.split(";")
            if len(fields) != 6 or fields[0] != "A" or fields[5] not in {sid, "SY", "BA"}:
                return False
            if fields[2] not in {"FA", "0x1f01ff"}:
                return False
            owner_present |= fields[5] == sid
        return owner_present
    finally:
        if text:
            kernel.LocalFree(c.cast(text, c.c_void_p))
        kernel.LocalFree(descriptor)
