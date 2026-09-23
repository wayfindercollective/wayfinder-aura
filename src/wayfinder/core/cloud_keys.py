"""Cloud API key help: where to get a key, a free "Verify" call, sane models.

Verification lists the provider's models (free, no audio or text sent) straight
to the provider's official HTTPS host. Redirects are refused so the key can
never be forwarded to another host, and nothing here logs the key.

Used by the macOS key panels; the Linux panels are unchanged.
"""

from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    key: str             # config key name
    name: str
    console_url: str
    console_label: str
    steps: str
    prefix: str          # every key from this provider starts with it
    models_url: str


PROVIDERS = {
    "groq": Provider(
        key="groq_api_key",
        name="Groq",
        console_url="https://console.groq.com/keys",
        console_label="console.groq.com/keys",
        steps=("1. Sign in at console.groq.com (free account)\n"
               "2. Open API Keys → Create API Key\n"
               "3. Copy the key and paste it here (⌘V)"),
        prefix="gsk_",
        models_url="https://api.groq.com/openai/v1/models",
    ),
    "openai": Provider(
        key="openai_api_key",
        name="OpenAI",
        console_url="https://platform.openai.com/api-keys",
        console_label="platform.openai.com/api-keys",
        steps=("1. Sign in at platform.openai.com\n"
               "2. Open API keys → Create new secret key\n"
               "3. Copy the key (it is shown once) and paste it here (⌘V)"),
        prefix="sk-",
        models_url="https://api.openai.com/v1/models",
    ),
    "anthropic": Provider(
        key="anthropic_api_key",
        name="Anthropic",
        console_url="https://console.anthropic.com/settings/keys",
        console_label="console.anthropic.com/settings/keys",
        steps=("1. Sign in at console.anthropic.com\n"
               "2. Open Settings → API Keys → Create Key\n"
               "3. Copy the key and paste it here (⌘V)"),
        prefix="sk-ant-",
        models_url="https://api.anthropic.com/v1/models",
    ),
}

# Current models (checked 2026-09 against the providers' deprecation pages).
# Every Anthropic ID the app shipped (Claude 3 / 3.5) is retired and fails.
# gpt-4-turbo / gpt-3.5-turbo shut down 2026-10-23.
OPENAI_CLEANUP_MODELS = ["gpt-4o-mini", "gpt-4.1-mini", "gpt-4o"]
ANTHROPIC_CLEANUP_MODELS = ["claude-haiku-4-5-20251001", "claude-sonnet-4-6"]
RETIRED_MODEL_REPLACEMENTS = {
    "anthropic_model": {
        "claude-3-haiku-20240307": "claude-haiku-4-5-20251001",
        "claude-3-5-haiku-20241022": "claude-haiku-4-5-20251001",
        "claude-3-sonnet-20240229": "claude-sonnet-4-6",
        "claude-3-5-sonnet-20241022": "claude-sonnet-4-6",
        "claude-3-7-sonnet-20250219": "claude-sonnet-4-6",
    },
    "openai_model": {
        "gpt-4-turbo": "gpt-4o-mini",
        "gpt-3.5-turbo": "gpt-4o-mini",
    },
}


def clean_key(text: str) -> str:
    """A key never contains whitespace; pasted ones often carry some."""
    return "".join(str(text or "").split())


def _a(name: str) -> str:
    return f"{'an' if name[:1] in 'AEIOU' else 'a'} {name}"


def format_warning(provider: str, key: str) -> str | None:
    """A hint when the key obviously belongs to another provider."""
    info = PROVIDERS.get(provider)
    key = clean_key(key)
    if info is None or not key:
        return None
    if not key.startswith(info.prefix):
        for other in PROVIDERS.values():
            if other.key != info.key and key.startswith(other.prefix) and (
                other.prefix != "sk-" or not key.startswith("sk-ant-")
            ):
                return f"This looks like {_a(other.name)} key, not {_a(info.name)} key."
        return f"{info.name} keys start with “{info.prefix}”. Check you copied the whole key."
    if provider == "openai" and key.startswith("sk-ant-"):
        return "This looks like an Anthropic key, not an OpenAI key."
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None  # never forward a key to wherever a redirect points


def _request(provider: str, key: str) -> urllib.request.Request:
    info = PROVIDERS[provider]
    headers = {"User-Agent": "Wayfinder-Aura-KeyCheck/1"}
    if provider == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    else:
        headers["Authorization"] = f"Bearer {key}"
    return urllib.request.Request(info.models_url, headers=headers, method="GET")


def verify_key(provider: str, key: str, timeout: float = 10.0, opener=None) -> tuple[bool, str]:
    """(works, one-line message). Makes one free request; never raises."""
    info = PROVIDERS.get(provider)
    key = clean_key(key)
    if info is None:
        return False, "Unknown provider."
    if not key:
        return False, "Paste a key first."
    if opener is None:
        try:  # idempotent; the app already did this at startup
            from wayfinder.tls import configure_tls_ca_bundle
            configure_tls_ca_bundle()
        except Exception:
            pass
        opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(_request(provider, key), timeout=timeout) as response:
            status = getattr(response, "status", 200)
            body = response.read(512 * 1024)
        if status == 200:
            try:
                count = len(json.loads(body or b"{}").get("data") or [])
            except Exception:
                count = 0
            suffix = f" ({count} models available)" if count else ""
            return True, f"Key works — connected to {info.name}{suffix}."
        return False, f"{info.name} answered HTTP {status}."
    except urllib.error.HTTPError as exc:
        code = exc.code
        if code in (401, 403):
            return False, (f"{info.name} rejected this key. Copy it again from "
                           f"{info.console_label} (or create a new one).")
        if code == 429:
            return True, f"Key works — {info.name} is rate-limiting right now, try again shortly."
        if 300 <= code < 400:
            return False, f"{info.name} redirected the check; the key was not sent on."
        if code >= 500:
            return False, f"{info.name} is having problems (HTTP {code}). Try again in a minute."
        return False, f"{info.name} answered HTTP {code}."
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        if isinstance(exc, ssl.SSLError) or isinstance(getattr(exc, "reason", None), ssl.SSLError):
            return False, (f"Couldn't make a secure connection to {info.name}. A network "
                           "filter or proxy may be intercepting HTTPS.")
        return False, f"Couldn't reach {info.name}. Check your internet connection."
