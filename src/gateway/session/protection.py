"""Local secret protection. Windows ctypes is confined to the DPAPI boundary."""

import csv
import ctypes
import os
import stat
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from gateway.contracts import GatewayError


def restricted(path: Path, *, create: bool = False, directory: bool = False) -> None:
    """Create private directories, or validate existing owner-only storage."""
    if path.is_symlink() or path.is_junction():
        raise GatewayError("unsafe secret storage path")
    if create:
        path.mkdir(mode=0o700)
        if os.name == "nt":
            subprocess.run(
                [
                    "icacls",
                    str(path),
                    "/inheritance:r",
                    "/grant:r",
                    f"*{_current_sid()}:(OI)(CI)F",
                ],
                check=True,
                capture_output=True,
            )
    if sys.platform == "win32":
        _windows_restricted(path)
    else:
        info = path.stat(follow_symlinks=False)
        expected = 0o700 if directory else 0o600
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != expected:
            raise GatewayError("unsafe secret storage permissions")
    if directory != path.is_dir():
        raise GatewayError("invalid secret storage path")


class Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]


@lru_cache(maxsize=1)
def _current_sid() -> str:
    result = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True
    )
    return next(csv.reader(result.stdout.strip().splitlines()))[-1]


def _windows_restricted(path: Path) -> None:
    """Inspect the actual DACL through Win32, including inherited allow entries."""
    if sys.platform != "win32":
        raise GatewayError("Windows ACL requires Windows")
    api: Any = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetNamedSecurityInfoW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    api.GetNamedSecurityInfoW.restype = ctypes.c_uint32
    api.GetAce.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    api.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor, dacl, owner = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    if api.GetNamedSecurityInfoW(
        str(path),
        1,
        5,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    ):
        raise GatewayError("secret storage ACL unavailable")
    allowed = {_current_sid(), "S-1-5-18", "S-1-5-32-544"}

    def sid_text(sid: int | None) -> str:
        converted = ctypes.c_wchar_p()
        if not api.ConvertSidToStringSidW(sid, ctypes.byref(converted)):
            raise GatewayError("invalid secret storage ACL")
        try:
            return converted.value or ""
        finally:
            kernel.LocalFree(converted)

    try:
        if not dacl.value or sid_text(owner.value) not in allowed:
            raise GatewayError("unsafe secret storage owner")
        count = ctypes.c_uint16.from_address(dacl.value + 4).value
        for index in range(count):
            ace = ctypes.c_void_p()
            if not api.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
                raise GatewayError("invalid secret storage ACL")
            kind = ctypes.c_ubyte.from_address(ace.value).value
            if kind == 1:
                continue
            if kind != 0 or sid_text(ace.value + 8) not in allowed | {"S-1-3-4"}:
                raise GatewayError("unsafe secret storage permissions")
    finally:
        kernel.LocalFree(descriptor)


def dpapi(data: bytes, *, protect: bool) -> bytes:
    """Current-user DPAPI, UI forbidden; never use the machine-wide flag."""
    if sys.platform != "win32":
        raise GatewayError("DPAPI requires Windows")
    crypt: Any = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    operation = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    operation.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(Blob),
    ]
    operation.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise GatewayError("local key protection failed")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.memset(target.data, 0, target.size)
        kernel.LocalFree(target.data)
