"""Cloud API keys in the macOS login Keychain (generic passwords).

Values never touch argv (the ``security`` CLI would expose them to ``ps``) or
disk outside the Keychain. Talks to Security.framework directly through
ctypes, so no extra dependency ships in the app.

One item per key, service ``SERVICE``, account = the config key name
(``groq_api_key``...). Every function returns quietly on failure; callers keep
a working fallback (config.json, owner-only) rather than losing the key.
macOS-only: every function is a no-op elsewhere.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading

SERVICE = os.environ.get("WAYFINDER_KEYCHAIN_SERVICE") or "Wayfinder Aura"

_ERR_SEC_SUCCESS = 0
_ERR_SEC_ITEM_NOT_FOUND = -25300
_ERR_SEC_DUPLICATE_ITEM = -25299
_UTF8 = 0x08000100  # kCFStringEncodingUTF8

_lock = threading.Lock()
_libs = None


class KeychainError(RuntimeError):
    pass


def available() -> bool:
    return sys.platform == "darwin" and _load() is not None


def _load():
    global _libs
    if _libs is not None:
        return _libs or None
    try:
        sec = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        vp = ctypes.c_void_p
        cf.CFStringCreateWithCString.restype = vp
        cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFDataCreate.restype = vp
        cf.CFDataCreate.argtypes = [vp, ctypes.c_char_p, ctypes.c_long]
        cf.CFDataGetLength.restype = ctypes.c_long
        cf.CFDataGetLength.argtypes = [vp]
        cf.CFDataGetBytePtr.restype = vp
        cf.CFDataGetBytePtr.argtypes = [vp]
        cf.CFDictionaryCreate.restype = vp
        cf.CFDictionaryCreate.argtypes = [vp, ctypes.POINTER(vp), ctypes.POINTER(vp), ctypes.c_long, vp, vp]
        cf.CFRelease.argtypes = [vp]
        for name in ("SecItemAdd", "SecItemCopyMatching"):
            fn = getattr(sec, name)
            fn.restype = ctypes.c_int32
            fn.argtypes = [vp, ctypes.POINTER(vp)]
        sec.SecItemUpdate.restype = ctypes.c_int32
        sec.SecItemUpdate.argtypes = [vp, vp]
        sec.SecItemDelete.restype = ctypes.c_int32
        sec.SecItemDelete.argtypes = [vp]

        def const(lib, name):
            return vp.in_dll(lib, name).value

        consts = {
            name: const(sec, name) for name in (
                "kSecClass", "kSecClassGenericPassword", "kSecAttrService",
                "kSecAttrAccount", "kSecAttrLabel", "kSecValueData",
                "kSecReturnData", "kSecMatchLimit", "kSecMatchLimitOne",
            )
        }
        consts["kCFBooleanTrue"] = const(cf, "kCFBooleanTrue")
        consts["keyCallbacks"] = ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"))
        consts["valueCallbacks"] = ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks"))
        _libs = (sec, cf, consts)
    except Exception:
        _libs = False
        return None
    return _libs


class _Scope:
    """Owns the CF objects created for one call and releases them after."""

    def __init__(self, cf):
        self.cf = cf
        self.owned = []

    def string(self, text: str):
        ref = self.cf.CFStringCreateWithCString(None, text.encode("utf-8"), _UTF8)
        self.owned.append(ref)
        return ref

    def data(self, raw: bytes):
        ref = self.cf.CFDataCreate(None, raw, len(raw))
        self.owned.append(ref)
        return ref

    def dict(self, consts, pairs):
        n = len(pairs)
        keys = (ctypes.c_void_p * n)(*[k for k, _ in pairs])
        values = (ctypes.c_void_p * n)(*[v for _, v in pairs])
        ref = self.cf.CFDictionaryCreate(None, keys, values, n,
                                         consts["keyCallbacks"], consts["valueCallbacks"])
        self.owned.append(ref)
        return ref

    def close(self):
        for ref in self.owned:
            if ref:
                self.cf.CFRelease(ref)


def _base(scope, consts, account: str):
    return [
        (consts["kSecClass"], consts["kSecClassGenericPassword"]),
        (consts["kSecAttrService"], scope.string(SERVICE)),
        (consts["kSecAttrAccount"], scope.string(account)),
    ]


def get(account: str) -> str | None:
    """The stored value, "" if there is no item, None if the Keychain failed."""
    libs = _load() if sys.platform == "darwin" else None
    if libs is None:
        return None
    sec, cf, consts = libs
    with _lock:
        scope = _Scope(cf)
        try:
            query = scope.dict(consts, _base(scope, consts, account) + [
                (consts["kSecReturnData"], consts["kCFBooleanTrue"]),
                (consts["kSecMatchLimit"], consts["kSecMatchLimitOne"]),
            ])
            out = ctypes.c_void_p()
            status = sec.SecItemCopyMatching(query, ctypes.byref(out))
            if status == _ERR_SEC_ITEM_NOT_FOUND:
                return ""
            if status != _ERR_SEC_SUCCESS or not out.value:
                return None
            try:
                length = cf.CFDataGetLength(out.value)
                raw = ctypes.string_at(cf.CFDataGetBytePtr(out.value), length)
            finally:
                cf.CFRelease(out.value)
            return raw.decode("utf-8", errors="replace")
        finally:
            scope.close()


def set(account: str, value: str) -> bool:  # noqa: A001 - mirrors get/delete
    """Store (add or replace) a value. True on success."""
    libs = _load() if sys.platform == "darwin" else None
    if libs is None:
        return False
    sec, cf, consts = libs
    with _lock:
        scope = _Scope(cf)
        try:
            secret = scope.data(value.encode("utf-8"))
            add = scope.dict(consts, _base(scope, consts, account) + [
                (consts["kSecAttrLabel"], scope.string(f"{SERVICE} — {account}")),
                (consts["kSecValueData"], secret),
            ])
            status = sec.SecItemAdd(add, None)
            if status == _ERR_SEC_DUPLICATE_ITEM:
                query = scope.dict(consts, _base(scope, consts, account))
                update = scope.dict(consts, [(consts["kSecValueData"], secret)])
                status = sec.SecItemUpdate(query, update)
            return status == _ERR_SEC_SUCCESS
        finally:
            scope.close()


def delete(account: str) -> bool:
    """Remove the item. True when it is gone (or never existed)."""
    libs = _load() if sys.platform == "darwin" else None
    if libs is None:
        return False
    sec, cf, consts = libs
    with _lock:
        scope = _Scope(cf)
        try:
            status = sec.SecItemDelete(scope.dict(consts, _base(scope, consts, account)))
            return status in (_ERR_SEC_SUCCESS, _ERR_SEC_ITEM_NOT_FOUND)
        finally:
            scope.close()
