"""Cloud API keys in Windows Credential Manager (generic credentials).

The Windows counterpart of ``macos_keychain``, with the same get/set/delete
contract so ``config.py`` treats both stores alike. Values never touch argv or
config.json; they are protected by the user's Windows login (DPAPI) and appear
under Control Panel > Credential Manager > Windows Credentials as
``Wayfinder Aura/<config key>``. Talks to advapi32 through ctypes, so no extra
dependency ships in the app.

Persisted per user on this machine only (``CRED_PERSIST_LOCAL_MACHINE``: not
roamed to other PCs). Every function returns quietly on failure; callers keep
a working fallback (config.json) rather than losing the key.
Windows-only: every function is a no-op elsewhere.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading

SERVICE = os.environ.get("WAYFINDER_KEYCHAIN_SERVICE") or "Wayfinder Aura"

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168
_CRED_MAX_BLOB = 5 * 512

_lock = threading.Lock()
_api = None


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


def _load():
    global _api
    if sys.platform != "win32":
        return None
    if _api is None:
        try:
            advapi = ctypes.WinDLL("advapi32", use_last_error=True)
            advapi.CredReadW.argtypes = [
                ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
            ]
            advapi.CredReadW.restype = ctypes.c_int
            advapi.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), ctypes.c_uint32]
            advapi.CredWriteW.restype = ctypes.c_int
            advapi.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
            advapi.CredDeleteW.restype = ctypes.c_int
            advapi.CredFree.argtypes = [ctypes.c_void_p]
            advapi.CredFree.restype = None
            _api = advapi
        except (AttributeError, OSError):
            return None
    return _api


def available() -> bool:
    return sys.platform == "win32" and _load() is not None


def _target(account: str) -> str:
    return f"{SERVICE}/{account}"


def get(account: str) -> str | None:
    """The stored value, "" if there is no credential, None if the store failed."""
    api = _load()
    if api is None:
        return None
    with _lock:
        out = ctypes.POINTER(_CREDENTIALW)()
        if not api.CredReadW(_target(account), _CRED_TYPE_GENERIC, 0, ctypes.byref(out)):
            if ctypes.get_last_error() == _ERROR_NOT_FOUND:
                return ""
            return None
        try:
            cred = out.contents
            raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        finally:
            api.CredFree(out)
    return raw.decode("utf-8", errors="replace")


def set(account: str, value: str) -> bool:  # noqa: A001 - mirrors get/delete
    """Store (add or replace) a value. True on success."""
    api = _load()
    if api is None:
        return False
    blob = value.encode("utf-8")
    if len(blob) > _CRED_MAX_BLOB:
        return False  # CredWrite refuses it; config.json keeps the key
    with _lock:
        buf = (ctypes.c_ubyte * max(1, len(blob))).from_buffer_copy(blob or b"\0")
        cred = _CREDENTIALW()
        cred.Type = _CRED_TYPE_GENERIC
        cred.TargetName = _target(account)
        cred.Comment = f"{SERVICE} cloud API key"
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = account
        return bool(api.CredWriteW(ctypes.byref(cred), 0))


def delete(account: str) -> bool:
    """Remove the credential. True when it is gone (or never existed)."""
    api = _load()
    if api is None:
        return False
    with _lock:
        if api.CredDeleteW(_target(account), _CRED_TYPE_GENERIC, 0):
            return True
        return ctypes.get_last_error() == _ERROR_NOT_FOUND
