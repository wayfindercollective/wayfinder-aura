"""Real, private Keychains for native tests; never change the login search list.

SecItemAdd accepts kSecUseKeychain; search/update/delete accept
kSecMatchSearchList. Only these destinations are added to the application's
real CF dictionaries. Its serialization and Security.framework calls remain
under test. No test password is sent through argv or saved outside Keychain.
"""

import ctypes
import secrets
import uuid
from contextlib import contextmanager


class ScopedSecurity:
    """Forward native calls with a per-query destination, preserving OSStatus."""

    def __init__(self, sec, cf, keychain, search_list, use_keychain, match_list):
        self.sec = sec
        self.cf = cf
        self.keychain = keychain
        self.search_list = search_list
        self.use_keychain = use_keychain
        self.match_list = match_list

    def _call(self, name, query, *args):
        scoped = self.cf.CFDictionaryCreateMutableCopy(None, 0, query)
        if not scoped:
            raise RuntimeError("Could not copy the test Keychain query")
        try:
            key, value = ((self.use_keychain, self.keychain) if name == "SecItemAdd"
                          else (self.match_list, self.search_list))
            self.cf.CFDictionarySetValue(scoped, key, value)
            return getattr(self.sec, name)(scoped, *args)
        finally:
            self.cf.CFRelease(scoped)

    def SecItemAdd(self, query, result):
        return self._call("SecItemAdd", query, result)

    def SecItemCopyMatching(self, query, result):
        return self._call("SecItemCopyMatching", query, result)

    def SecItemUpdate(self, query, attributes):
        return self._call("SecItemUpdate", query, attributes)

    def SecItemDelete(self, query):
        return self._call("SecItemDelete", query)


def _configure(sec, cf):
    vp = ctypes.c_void_p
    signatures = (
        (sec, "SecKeychainCreate", ctypes.c_int32,
         [ctypes.c_char_p, ctypes.c_uint32, vp, ctypes.c_ubyte, vp, ctypes.POINTER(vp)]),
        (sec, "SecKeychainDelete", ctypes.c_int32, [vp]),
        (sec, "SecKeychainCopyDefault", ctypes.c_int32, [ctypes.POINTER(vp)]),
        (sec, "SecKeychainCopySearchList", ctypes.c_int32, [ctypes.POINTER(vp)]),
        (sec, "SecKeychainGetUserInteractionAllowed", ctypes.c_int32,
         [ctypes.POINTER(ctypes.c_ubyte)]),
        (sec, "SecKeychainSetUserInteractionAllowed", ctypes.c_int32, [ctypes.c_ubyte]),
        (cf, "CFEqual", ctypes.c_ubyte, [vp, vp]),
        (cf, "CFArrayCreate", vp, [vp, ctypes.POINTER(vp), ctypes.c_long, vp]),
        (cf, "CFDictionaryCreateMutableCopy", vp, [vp, ctypes.c_long, vp]),
        (cf, "CFDictionarySetValue", None, [vp, vp, vp]),
    )
    for lib, name, restype, argtypes in signatures:
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = restype, argtypes


@contextmanager
def _preferences(sec, cf):
    """Hold opaque metadata refs, without reading or reporting any credentials."""
    default, search_list = ctypes.c_void_p(), ctypes.c_void_p()
    try:
        status = sec.SecKeychainCopyDefault(ctypes.byref(default))
        assert status in (0, -25307), f"SecKeychainCopyDefault: {status}"
        search_status = sec.SecKeychainCopySearchList(ctypes.byref(search_list))
        assert search_status == 0, f"SecKeychainCopySearchList: {search_status}"
        assert search_list.value
        yield status, default.value, search_list.value
    finally:
        for ref in (search_list.value, default.value):
            if ref:
                cf.CFRelease(ref)


def _assert_preferences(sec, cf, before):
    with _preferences(sec, cf) as after:
        assert before[0] == after[0], "Default Keychain availability changed"
        if before[0] == 0:
            assert cf.CFEqual(before[1], after[1]), "Default Keychain changed"
        assert cf.CFEqual(before[2], after[2]), "Keychain search list changed"


@contextmanager
def private_keychain(module, tmp_path):
    """Create/delete only our fresh test DB, retaining native CRUD coverage.

    Apple's StorageManager::shouldAddToSearchList excludes private filenames;
    only login/System keychains are automatically added. Assert that boundary
    before yielding and after deletion. Never 'restore' user preferences.
    """
    libs = module._load()
    assert libs is not None, "Native Security.framework unavailable"
    sec, cf, consts = libs
    _configure(sec, cf)
    directory = tmp_path / ("aura-keychain-" + uuid.uuid4().hex)
    directory.mkdir(mode=0o700)
    path = directory / "isolated.keychain"
    assert "/login.keychain" not in str(path)
    password = secrets.token_bytes(32)
    ref, search_list = ctypes.c_void_p(), None
    created = False
    interaction = ctypes.c_ubyte()
    assert sec.SecKeychainGetUserInteractionAllowed(ctypes.byref(interaction)) == 0
    with _preferences(sec, cf) as before:
        assert sec.SecKeychainSetUserInteractionAllowed(False) == 0
        try:
            status = sec.SecKeychainCreate(
                str(path).encode(), len(password), password, False, None, ctypes.byref(ref)
            )
            assert status == 0, f"Private SecKeychainCreate: {status}"
            created = True
            assert ref.value
            _assert_preferences(sec, cf, before)
            callbacks = ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeArrayCallBacks"))
            search_list = cf.CFArrayCreate(None, (ctypes.c_void_p * 1)(ref.value), 1, callbacks)
            assert search_list
            scoped = ScopedSecurity(
                sec, cf, ref.value, search_list,
                ctypes.c_void_p.in_dll(sec, "kSecUseKeychain").value,
                ctypes.c_void_p.in_dll(sec, "kSecMatchSearchList").value,
            )
            yield scoped, cf, consts
        finally:
            try:
                if created:
                    status = sec.SecKeychainDelete(ref.value)
                    assert status == 0, f"Private SecKeychainDelete: {status}"
                    assert not path.exists() and not path.with_suffix(".keychain-db").exists()
            finally:
                if search_list:
                    cf.CFRelease(search_list)
                if ref.value:
                    cf.CFRelease(ref.value)
                assert sec.SecKeychainSetUserInteractionAllowed(interaction.value) == 0
                _assert_preferences(sec, cf, before)
