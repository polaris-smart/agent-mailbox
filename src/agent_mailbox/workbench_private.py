"""Private POSIX modes or protected Windows DACLs for workbench secrets.

Windows chmod only changes a read-only bit. Restrict actual access to the current
user, SYSTEM and Administrators instead; fail closed if ACL operations fail.
"""

from __future__ import annotations

import os
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
        "GetSecurityDescriptorControl": (
            [pointer, ctypes.POINTER(w.WORD), ctypes.POINTER(w.DWORD)],
            w.BOOL,
        ),
        "GetAclInformation": ([pointer, pointer, w.DWORD, ctypes.c_int], w.BOOL),
        "GetAce": ([pointer, w.DWORD, ctypes.POINTER(pointer)], w.BOOL),
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
    control, entries = _native_acl(path)
    if not _allowed_acl(control, entries, sid, path.is_dir()):
        diagnostic = ""
        if os.environ.get("CI"):
            diagnostic = f" (SID={sid}, control={control:#x}, ACEs={entries!r})"
        raise OSError("Private Windows ACL verification failed" + diagnostic)


def _allowed_acl(control, entries, sid, directory=False):
    # Numeric SID identity avoids canonical SDDL aliases such as LA for the
    # built-in Administrator account (renamed runneradmin on some CI hosts).
    if not control & 0x1000 or not entries:
        return False  # SE_DACL_PROTECTED: never inherit wider parent grants.
    allowed = {sid, "S-1-5-18", "S-1-5-32-544"}
    for ace_type, flags, mask, identity in entries:
        if ace_type != 0 or identity not in allowed or mask != 0x1F01FF:
            return False  # ACCESS_ALLOWED_ACE, FILE_ALL_ACCESS, exact identities.
        if directory and flags & 3 != 3:
            return False  # Object/container inheritance protects newly created secrets.
    return any(identity == sid for _, _, _, identity in entries)


def _native_acl(path):
    c, w, advapi, kernel, _sid = _windows()
    descriptor = c.c_void_p()
    code = advapi.GetNamedSecurityInfoW(
        str(path), 1, 4, None, None, None, None, c.byref(descriptor)
    )
    if code:
        raise c.WinError(code)
    try:
        control, revision = w.WORD(), w.DWORD()
        if not advapi.GetSecurityDescriptorControl(descriptor, c.byref(control), c.byref(revision)):
            raise c.WinError(c.get_last_error())
        present, defaulted, acl = w.BOOL(), w.BOOL(), c.c_void_p()
        if not advapi.GetSecurityDescriptorDacl(
            descriptor, c.byref(present), c.byref(acl), c.byref(defaulted)
        ):
            raise c.WinError(c.get_last_error())
        if not present.value or not acl:
            return control.value, []

        class AclSize(c.Structure):
            _fields_ = (("count", w.DWORD), ("used", w.DWORD), ("free", w.DWORD))

        info = AclSize()
        if not advapi.GetAclInformation(acl, c.byref(info), c.sizeof(info), 2):
            raise c.WinError(c.get_last_error())
        if info.count > 64:
            return control.value, []
        entries = []
        for index in range(info.count):
            ace = c.c_void_p()
            if not advapi.GetAce(acl, index, c.byref(ace)):
                raise c.WinError(c.get_last_error())
            ace_type = c.c_ubyte.from_address(ace.value).value
            flags = c.c_ubyte.from_address(ace.value + 1).value
            size = w.WORD.from_address(ace.value + 2).value
            if ace_type != 0 or size < 20:
                return control.value, []
            mask = w.DWORD.from_address(ace.value + 4).value
            identity = w.LPWSTR()
            if not advapi.ConvertSidToStringSidW(ace.value + 8, c.byref(identity)):
                raise c.WinError(c.get_last_error())
            try:
                entries.append((ace_type, flags, mask, identity.value))
            finally:
                kernel.LocalFree(c.cast(identity, c.c_void_p))
        return control.value, entries
    finally:
        kernel.LocalFree(descriptor)


def private_access(path, mode):
    """Inspect native protected ACLs, allowing only owner, SYSTEM and Administrators."""
    path = Path(path)
    if os.name != "nt":
        return path.stat().st_mode & 0o777 == mode
    *_api, sid = _windows()
    control, entries = _native_acl(path)
    return _allowed_acl(control, entries, sid, path.is_dir())
