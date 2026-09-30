"""The native fixture must never accidentally call the default Keychain."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.macos_keychain_fixture import ScopedSecurity


@pytest.mark.parametrize("name,args,destination", [
    ("SecItemAdd", ("result",), ("use", "private")),
    ("SecItemCopyMatching", ("result",), ("match", "only-private")),
    ("SecItemUpdate", ("attributes",), ("match", "only-private")),
    ("SecItemDelete", (), ("match", "only-private")),
])
def test_every_native_call_is_scoped_and_preserves_failure(name, args, destination):
    native = Mock(return_value=-61)
    sec = SimpleNamespace(**{name: native})
    cf = SimpleNamespace(
        CFDictionaryCreateMutableCopy=Mock(return_value="copy"),
        CFDictionarySetValue=Mock(), CFRelease=Mock(),
    )
    proxy = ScopedSecurity(sec, cf, "private", "only-private", "use", "match")
    assert getattr(proxy, name)("original", *args) == -61
    cf.CFDictionaryCreateMutableCopy.assert_called_once_with(None, 0, "original")
    cf.CFDictionarySetValue.assert_called_once_with("copy", *destination)
    native.assert_called_once_with("copy", *args)
    cf.CFRelease.assert_called_once_with("copy")


def test_allocation_failure_cannot_fall_back_to_default():
    sec = SimpleNamespace(SecItemAdd=Mock())
    cf = SimpleNamespace(CFDictionaryCreateMutableCopy=Mock(return_value=None))
    proxy = ScopedSecurity(sec, cf, 1, 2, 3, 4)
    with pytest.raises(RuntimeError, match="copy"):
        proxy.SecItemAdd("query", None)
    sec.SecItemAdd.assert_not_called()


def test_native_exception_releases_only_query_copy():
    sec = SimpleNamespace(SecItemDelete=Mock(side_effect=RuntimeError("native failure")))
    cf = SimpleNamespace(
        CFDictionaryCreateMutableCopy=Mock(return_value="copy"),
        CFDictionarySetValue=Mock(), CFRelease=Mock(),
    )
    proxy = ScopedSecurity(sec, cf, 1, 2, 3, 4)
    with pytest.raises(RuntimeError, match="native failure"):
        proxy.SecItemDelete("original")
    cf.CFRelease.assert_called_once_with("copy")
