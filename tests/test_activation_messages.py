"""Activation failures must tell a paying customer what to do next.

`not_found` rendered as "License key not found", which is a dead end: it
cannot distinguish a typo from a key that was never issued for this
deployment. The project owner hit exactly this with a real purchase and could
not diagnose it from the message alone.

Unknown server codes are deliberately NOT echoed back — see
activation_error_message.
"""

from wayfinder.license import (
    _ACTIVATION_ERRORS,
    SUPPORT_EMAIL,
    activation_error_message,
)


def test_not_found_tells_the_customer_what_to_do():
    message = activation_error_message("not_found")

    assert SUPPORT_EMAIL in message
    assert "order" in message.lower(), "must ask for the order number"
    # A bare restatement of the server code helps nobody.
    assert message.strip().lower() != "license key not found"


def test_a_brace_in_a_server_reason_does_not_crash_activation():
    """Guards against reintroducing a server-controlled failure path.

    Nothing server-supplied is formatted or echoed, so braces are inert; this
    pins that property rather than describing past behaviour.
    """
    for hostile in ("{support}", "{0}", "weird{", "}weird", "{__class__}"):
        message = activation_error_message(hostile)
        assert SUPPORT_EMAIL in message
        assert hostile not in message


def test_unknown_reasons_still_route_to_support():
    """An unmapped server code must not become a dead end either.

    The code itself is NOT shown: it is unrecognised server input, and no
    shape check can prove it is not a token or key. Support reads the server
    logs for the code.
    """
    message = activation_error_message("some_new_server_code")

    assert SUPPORT_EMAIL in message
    assert "some_new_server_code" not in message


def test_known_reasons_keep_their_specific_guidance():
    assert "Activation limit" in activation_error_message("activation_limit")
    assert "revoked" in activation_error_message("revoked").lower()
    assert "refunded" in activation_error_message("refunded").lower()


def test_every_message_routes_the_user_to_support():
    """No message may leave the user with nothing to do.

    Note missing_fields deliberately does NOT ask for an order number: it
    signals an app bug, not a problem with the key.
    """
    for reason in ("not_found", "activation_limit", "revoked", "refunded",
                   "missing_fields", "totally_unknown"):
        message = activation_error_message(reason)
        assert SUPPORT_EMAIL in message, f"{reason} gives the user no next step"


def test_mapped_messages_are_static_text():
    """Every MAPPED message is a fixed string with only the support address
    interpolated, so none can carry server input into a log or screenshot.
    Unknown reasons are not echoed either — they are replaced wholesale, which
    is covered separately below."""
    for reason in _ACTIVATION_ERRORS:
        message = activation_error_message(reason)
        assert reason not in message or reason in ("revoked", "refunded")
        assert "WV-" not in message


def test_a_non_string_reason_does_not_raise():
    """Malformed JSON can make `reason` a list or dict; an unhashable value
    raised TypeError at the mapping lookup before the message was built."""
    for hostile in ([1, 2], {"a": 1}, None, 42):
        message = activation_error_message(hostile)
        assert SUPPORT_EMAIL in message


def test_a_key_shaped_reason_is_not_echoed_back():
    """Real server codes are short lowercase identifiers. Anything else could
    be a token or key echoed into the UI and then into a screenshot, so it is
    replaced rather than displayed."""
    secret = "WV-AAAA-BBBB-CCCC-DDDD"
    message = activation_error_message(secret)

    assert secret not in message
    assert "WV-AAAA" not in message
    assert SUPPORT_EMAIL in message


def test_unknown_codes_are_never_echoed_even_when_they_look_legitimate():
    """A shape check cannot prove an unknown server string is not a secret, so
    nothing unrecognised is rendered. A trailing newline slipped past `$`, and
    a 40-char token matched the "looks like a code" shape."""
    for unknown in ("seat_limit_exceeded", "seat_limit_exceeded\n",
                    "WV-AAAA-BBBB-CCCC-DDDD", "a" * 40):
        message = activation_error_message(unknown)
        assert unknown.strip() not in message
        assert SUPPORT_EMAIL in message
        assert "\n" not in message
