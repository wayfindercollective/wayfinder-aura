"""
Wayfinder Aura — "We'd love your feedback" client.

POSTs one JSON message to the Wayfinder-OS Convex HTTP endpoint, which stores
it and pings the team in Slack. The endpoint is public and rate-limited
server-side; nothing secret ships in the app. Called from a worker thread by
the settings-panel form — submit_feedback() never raises and never blocks
longer than the HTTP timeout.
"""

import os

# Production Wayfinder-OS Convex deployment (fine-shrimp-886) — httpActions are
# served from the .convex.site domain. Override for local/dev testing.
AURA_FEEDBACK_API_URL = os.environ.get(
    "WAYFINDER_AURA_FEEDBACK_URL",
    "https://fine-shrimp-886.convex.site/api/aura/feedback",
)
FEEDBACK_HTTP_TIMEOUT = 10  # seconds
FEEDBACK_MAX_CHARS = 4000  # keep in sync with the endpoint's server-side cap


def submit_feedback(
    message: str,
    email: "str | None" = None,
    app_version: "str | None" = None,
    plan: "str | None" = None,
    platform_desc: "str | None" = None,
) -> "tuple[bool, str]":
    """Send one feedback message. Returns (ok, detail). Never raises.

    *detail* is user-facing: "sent" on success, otherwise a short sentence
    suitable for the form's status label.
    """
    message = (message or "").strip()
    if len(message) < 3:
        return False, "Please write a few words first."
    if len(message) > FEEDBACK_MAX_CHARS:
        message = message[:FEEDBACK_MAX_CHARS]

    payload = {"message": message}
    if email and email.strip():
        payload["email"] = email.strip()[:254]
    if app_version:
        payload["appVersion"] = str(app_version)[:100]
    if plan:
        payload["plan"] = str(plan)[:40]
    if platform_desc:
        payload["platform"] = str(platform_desc)[:200]

    try:
        import requests

        resp = requests.post(
            AURA_FEEDBACK_API_URL, json=payload, timeout=FEEDBACK_HTTP_TIMEOUT
        )
        if resp.status_code == 200:
            return True, "sent"
        if resp.status_code == 429:
            return False, "Easy there — please try again in a minute."
        try:
            detail = resp.json().get("error") or f"HTTP {resp.status_code}"
        except Exception:
            detail = f"HTTP {resp.status_code}"
        return False, f"Couldn't send feedback ({detail})."
    except Exception:
        return False, "Couldn't reach the feedback service — check your connection."
