"""Tests for wayfinder.core.feedback — the "We'd love your feedback" client."""

from unittest.mock import MagicMock, patch

from wayfinder.core.feedback import (
    AURA_FEEDBACK_API_URL,
    FEEDBACK_MAX_CHARS,
    submit_feedback,
)


def _response(status_code=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    if json_body is None:
        resp.json.side_effect = ValueError("no body")
    else:
        resp.json.return_value = json_body
    return resp


class TestSubmitFeedbackValidation:
    def test_too_short_message_fails_without_network(self):
        with patch("requests.post") as post:
            ok, detail = submit_feedback("hi")
        assert ok is False
        assert "few words" in detail
        post.assert_not_called()

    def test_empty_and_none_fail_without_network(self):
        with patch("requests.post") as post:
            assert submit_feedback("")[0] is False
            assert submit_feedback(None)[0] is False
            assert submit_feedback("   \n  ")[0] is False
        post.assert_not_called()

    def test_overlong_message_is_truncated_not_rejected(self):
        with patch("requests.post", return_value=_response(200, {"ok": True})) as post:
            ok, _ = submit_feedback("x" * (FEEDBACK_MAX_CHARS + 500))
        assert ok is True
        sent = post.call_args.kwargs["json"]["message"]
        assert len(sent) == FEEDBACK_MAX_CHARS

    def test_astral_unicode_counts_code_points_like_the_server(self):
        # Client len() counts code points; the endpoint counts Array.from()
        # code points too — a 4000-emoji message must survive both sides.
        # (Plain JS .length would see 8000 UTF-16 units and reject it.)
        with patch("requests.post", return_value=_response(200, {"ok": True})) as post:
            ok, _ = submit_feedback("🎙" * FEEDBACK_MAX_CHARS)
        assert ok is True
        assert len(post.call_args.kwargs["json"]["message"]) == FEEDBACK_MAX_CHARS

        with patch("requests.post", return_value=_response(200, {"ok": True})) as post:
            submit_feedback("🎙" * (FEEDBACK_MAX_CHARS + 10))
        sent = post.call_args.kwargs["json"]["message"]
        # Truncation slices whole code points — never half a surrogate pair.
        assert len(sent) == FEEDBACK_MAX_CHARS
        assert sent == "🎙" * FEEDBACK_MAX_CHARS


class TestSubmitFeedbackWire:
    def test_success_posts_full_payload_to_prod_endpoint(self):
        with patch("requests.post", return_value=_response(200, {"ok": True})) as post:
            ok, detail = submit_feedback(
                "The overlay is lovely now",
                email="  peter@example.com ",
                app_version="1.1.8-beta.3",
                plan="ultra",
                platform_desc="Linux-6.8-x86_64",
            )
        assert ok is True and detail == "sent"
        assert post.call_args.args[0] == AURA_FEEDBACK_API_URL
        assert "fine-shrimp-886.convex.site" in AURA_FEEDBACK_API_URL
        payload = post.call_args.kwargs["json"]
        submission_id = payload.pop("submissionId")
        assert len(submission_id) == 32
        assert payload == {
            "message": "The overlay is lovely now",
            "email": "peter@example.com",  # stripped
            "appVersion": "1.1.8-beta.3",
            "plan": "ultra",
            "platform": "Linux-6.8-x86_64",
        }
        assert post.call_args.kwargs["timeout"] == 10

    def test_optional_fields_omitted_when_absent(self):
        with patch("requests.post", return_value=_response(200, {"ok": True})) as post:
            ok, _ = submit_feedback("just the message, thanks")
        assert ok is True
        payload = post.call_args.kwargs["json"]
        assert payload["message"] == "just the message, thanks"
        assert len(payload["submissionId"]) == 32
        assert set(payload) == {"message", "submissionId"}

    def test_email_at_254_chars_is_preserved(self):
        long_valid = f"{'a' * 64}@{'b' * 185}.com"
        assert len(long_valid) == 254
        with patch("requests.post", return_value=_response(200, {"ok": True})) as post:
            ok, _ = submit_feedback("hello there", email=long_valid)
        assert ok is True
        assert post.call_args.kwargs["json"]["email"] == long_valid

    def test_invalid_email_fails_before_network(self):
        with patch("requests.post") as post:
            ok, detail = submit_feedback("hello there", email="test")
        assert ok is False
        assert "valid email" in detail
        post.assert_not_called()


class TestSubmitFeedbackFailures:
    def test_rate_limited_gets_friendly_retry_message(self):
        with patch("requests.post", return_value=_response(429)):
            ok, detail = submit_feedback("hello there")
        assert ok is False
        assert "try again" in detail

    def test_server_error_reports_endpoint_detail(self):
        with patch("requests.post", return_value=_response(400, {"error": "message must be 3-4000 characters"})):
            ok, detail = submit_feedback("hello there")
        assert ok is False
        assert "message must be 3-4000 characters" in detail

    def test_server_error_without_json_reports_status(self):
        with patch("requests.post", return_value=_response(500)):
            ok, detail = submit_feedback("hello there")
        assert ok is False
        assert "HTTP 500" in detail

    def test_network_exception_never_raises(self):
        with patch("requests.post", side_effect=OSError("no route to host")):
            ok, detail = submit_feedback("hello there")
        assert ok is False
        assert "connection" in detail
