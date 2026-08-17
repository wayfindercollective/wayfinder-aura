"""
Remote + built-in model catalogs for Whisper / GGUF downloads.

Built-in catalogs ship in wayfinder_main (offline fallback). At runtime the app
can fetch catalog/v1.json from the Models CDN and merge updates so new models
can appear without an app rebuild — after you publish R2 weights + catalog.

---------------------------------------------------------------------------
NOTICE TO AI CODING AGENTS
---------------------------------------------------------------------------
Do not strip requires_feature from remote catalog merges or force all models
public. Freemium / Ultra gates stay authoritative.
---------------------------------------------------------------------------
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Fields we accept from a remote catalog (never execute / never trust arbitrary keys).
_ALLOWED_ENTRY_KEYS = frozenset({
    "name",
    "size",
    "size_bytes",
    "url",
    "filename",
    "cdn_object",
    "sha256",
    "requires_feature",
    "speed",
    "speed_rating",
    "accuracy_rating",
    "accuracy",
    "description",
    "recommended",
    "disabled",
})

_KNOWN_FEATURES = frozenset({
    "large_models",
    "large_cleanup_models",
    "faster_whisper",
    "gpu_acceleration",
    "cloud_backends",
    "chunked_recording",
    "custom_vocabulary",
    "voice_profiles",
    "tone_system",
})

CATALOG_R2_KEY = "catalog/v1.json"
DEFAULT_CATALOG_PATH = f"/v1/catalog"  # Worker route
CACHE_MAX_AGE_SECS = 6 * 3600  # re-fetch at most every 6h unless forced


def _cache_path() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    d = base / "wayfinder-aura"
    d.mkdir(parents=True, exist_ok=True)
    return d / "model_catalog_v1.json"


def catalog_url_from_config(config: Optional[dict] = None) -> str:
    """Absolute URL for the remote catalog JSON."""
    if config:
        explicit = (config.get("models_catalog_url") or "").strip()
        if explicit:
            return explicit.rstrip("/")
    env_u = (os.environ.get("WAYFINDER_MODELS_CATALOG_URL") or "").strip()
    if env_u:
        return env_u.rstrip("/")
    try:
        from wayfinder.models_cdn import get_models_cdn_base

        base = get_models_cdn_base(config)
    except Exception:
        base = ""
    if not base:
        return ""
    return f"{base.rstrip('/')}{DEFAULT_CATALOG_PATH}"


def _safe_model_id(model_id: str) -> bool:
    if not model_id or len(model_id) > 64:
        return False
    # ids like tiny.en, large-v3-turbo-q5_0, qwen3-4b-2507
    for ch in model_id:
        if not (ch.isalnum() or ch in "._-"):
            return False
    return True


# Catalog `filename` values are joined onto the models dir and written to, so
# they must be bare basenames. A remote catalog is ours, but it is only as
# trustworthy as the Worker/R2 credentials behind it — an unsanitized name lets
# whoever can publish catalog/v1.json write the download body anywhere the app
# can reach (security audit 2026-08-17, F-B). First char must be alphanumeric,
# which also rules out dotfiles and leading-dash names.
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_FILENAME_LEN = 128
_MAX_CDN_OBJECT_LEN = 256


def safe_model_filename(filename: Any) -> bool:
    """True when *filename* is a bare basename safe to join onto a models dir."""
    if not isinstance(filename, str) or not filename:
        return False
    if len(filename) > _MAX_FILENAME_LEN:
        return False
    if not _SAFE_FILENAME_RE.match(filename):
        return False
    # Belt and braces: the pattern already excludes separators and "..".
    return Path(filename).name == filename


def safe_download_url(url: Any) -> bool:
    """True when *url* is an https URL with a host.

    https only: the whisper downloader runs on urllib, which will happily read
    ``file://`` (local-file exfil into the models dir), and any plaintext scheme
    is downgradeable. Built-in and published catalog entries are already https.
    """
    if not isinstance(url, str) or not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme.lower() == "https" and bool(parsed.netloc)


def safe_cdn_object(obj: Any) -> bool:
    """True when *obj* is a relative R2 key with no traversal or empty segments."""
    if not isinstance(obj, str) or not obj:
        return False
    if len(obj) > _MAX_CDN_OBJECT_LEN or obj.startswith("/") or "\\" in obj:
        return False
    return all(seg and seg not in (".", "..") for seg in obj.split("/"))


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def safe_sha256(value: Any) -> Optional[str]:
    """Return a normalized lowercase sha256 hex digest, or None if malformed."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if v.startswith("sha256:"):
        v = v[len("sha256:"):]
    return v if _SHA256_RE.match(v) else None


class DigestCheckCancelled(Exception):
    """Raised when a caller's ``should_cancel()`` returns True mid-hash."""


class ModelDigestMismatch(ValueError):
    """A downloaded file did not match its pinned digest.

    Carries the values so callers can log the hex and show the user prose,
    instead of three copies of the same string-parsing.
    """

    def __init__(self, expected: str, actual: str, *, size: Optional[int] = None):
        self.expected = expected
        self.actual = actual
        self.size = size
        super().__init__(
            f"checksum mismatch (expected {expected[:12]}…, got {actual[:12]}…)"
        )

    @property
    def user_message(self) -> str:
        """Plain-language text for a UI label — no hex, and an actual next step."""
        return (
            "This model file doesn't match what this version of Wayfinder Aura "
            "expects, so it was not installed. That's a problem on our end, not "
            "yours — check for an app update, and let support know if it keeps "
            "happening."
        )


def shipped_trust_map(builtin: dict) -> dict:
    """filename → the facts we shipped for it, keyed by FILENAME not model id.

    A remote catalog may not weaken any of these for a filename we ship, and
    only these values authorize an alternate download origin — a digest the
    remote catalog supplied cannot vouch for the remote catalog's own URL.
    """
    trusted: dict[str, dict] = {}
    for info in (builtin or {}).values():
        if not isinstance(info, dict):
            continue
        filename = info.get("filename")
        digest = safe_sha256(info.get("sha256"))
        if not filename or not digest:
            continue
        trusted[filename] = {
            "sha256": digest,
            "size_bytes": info.get("size_bytes"),
            "requires_feature": info.get("requires_feature"),
            "url": info.get("url") if safe_download_url(info.get("url")) else None,
        }
    return trusted


def fallback_url_for(model_info: dict, shipped: dict) -> Optional[str]:
    """The one alternate origin a model may use when its primary fails.

    Only an **ungated** model whose filename we ship a pin for may fall back,
    and only to the revision-pinned URL *we* shipped — never to a URL the remote
    catalog supplied. Gated models stay on the authenticated CDN: sending their
    traffic to a public mirror would route paid weights around the Bearer check
    (see the licensing notice at the top of models_cdn.py).
    """
    trusted = shipped.get(model_info.get("filename"))
    if not trusted:
        return None
    if trusted.get("requires_feature") or model_info.get("requires_feature"):
        return None
    return trusted.get("url")


def sha256_file(path: Any, *, chunk_size: int = 1 << 20, should_cancel=None) -> str:
    """Stream a file and return its lowercase sha256 hex digest.

    ``should_cancel`` is polled per chunk so a multi-GB hash stays interruptible
    — without it, Cancel appears to do nothing while a large model is verified.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            if should_cancel is not None and should_cancel():
                raise DigestCheckCancelled()
            digest.update(chunk)
    return digest.hexdigest()


def verify_model_digest(path: Any, expected: Any, *, should_cancel=None) -> None:
    """Raise ValueError unless *path* hashes to *expected*.

    No-op when *expected* is absent — catalogs may add models we have no pinned
    digest for. Built-in entries all carry one (security audit 2026-08-17, F-C).
    Raises DigestCheckCancelled if ``should_cancel`` fires during hashing.
    """
    want = safe_sha256(expected)
    if not want:
        return
    got = sha256_file(path, should_cancel=should_cancel)
    if got != want:
        try:
            size = Path(path).stat().st_size
        except OSError:
            size = None
        raise ModelDigestMismatch(want, got, size=size)


def resolve_model_dest(models_dir: Any, filename: str, *, suffix: str = "") -> Path:
    """Join a catalog *filename* onto *models_dir*, refusing anything that escapes.

    Use this instead of ``models_dir / filename`` for every catalog-supplied
    name. ``suffix`` is for caller-owned temp extensions (".downloading").

    Raises ValueError when the name is unsafe or the join leaves *models_dir*.
    """
    if not safe_model_filename(filename):
        raise ValueError(f"unsafe model filename: {filename!r}")
    base = Path(models_dir).resolve()
    dest = base / f"{filename}{suffix}"
    # Holds by construction (the name is separator-free); this catches a
    # regression in the check above rather than trusting it twice.
    if dest.parent.resolve() != base:
        raise ValueError(f"model destination escapes {base}: {dest}")
    return dest


def sanitize_entry(raw: Any) -> Optional[dict]:
    """Return a safe catalog entry dict, or None if unusable."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k not in _ALLOWED_ENTRY_KEYS:
            continue
        out[k] = v
    if out.get("disabled") is True:
        return {"disabled": True}
    filename = out.get("filename")
    name = out.get("name")
    if not filename or not name:
        return None
    if not isinstance(filename, str) or not isinstance(name, str):
        return None
    if not safe_model_filename(filename):
        log.warning("catalog entry rejected: unsafe filename %r", filename)
        return None
    # Drop unusable download locations before requiring that one survives.
    # An empty/invalid cdn_object is dropped rather than honored as "clear the
    # CDN override" — that clear is what turns a catalog compromise into a
    # redirect away from the authenticated origin. To retire a model, publish
    # `disabled: true` (and re-add under a new id if it moves hosts).
    if "url" in out and not safe_download_url(out.get("url")):
        log.warning("catalog entry %r: dropping non-https url", name)
        out.pop("url", None)
    if "cdn_object" in out and not safe_cdn_object(out.get("cdn_object")):
        out.pop("cdn_object", None)
    if "sha256" in out:
        normalized = safe_sha256(out.get("sha256"))
        if normalized:
            out["sha256"] = normalized
        else:
            out.pop("sha256", None)
    # Must have at least one download location
    if not out.get("url") and not out.get("cdn_object"):
        return None
    feat = out.get("requires_feature")
    if feat is not None:
        # Non-empty strings only (unknown feature ids stay — gate denies until known).
        if not isinstance(feat, str) or not feat.strip():
            out.pop("requires_feature", None)
        else:
            out["requires_feature"] = feat.strip()
    if "size_bytes" in out:
        try:
            out["size_bytes"] = int(out["size_bytes"])
        except (TypeError, ValueError):
            out.pop("size_bytes", None)
        else:
            # A declared size is a download bound, so an absurd one is worse
            # than none — drop it and let the transfer ceiling apply instead.
            if not 0 < out["size_bytes"] <= (8 << 30):
                out.pop("size_bytes", None)
    for rk in ("speed_rating", "accuracy_rating"):
        if rk in out:
            try:
                out[rk] = int(out[rk])
            except (TypeError, ValueError):
                out.pop(rk, None)
    if "recommended" in out:
        out["recommended"] = bool(out["recommended"])
    return out


def validate_remote_document(doc: Any) -> Optional[dict]:
    """Validate top-level remote catalog document."""
    if not isinstance(doc, dict):
        return None
    version = doc.get("version", 1)
    try:
        if int(version) < 1:
            return None
    except (TypeError, ValueError):
        return None
    whisper = doc.get("whisper") or {}
    llm = doc.get("llm") or {}
    if not isinstance(whisper, dict) or not isinstance(llm, dict):
        return None
    clean_w: dict[str, dict] = {}
    clean_l: dict[str, dict] = {}
    for mid, entry in whisper.items():
        if not isinstance(mid, str) or not _safe_model_id(mid):
            continue
        se = sanitize_entry(entry)
        if se is not None:
            clean_w[mid] = se
    for mid, entry in llm.items():
        if not isinstance(mid, str) or not _safe_model_id(mid):
            continue
        se = sanitize_entry(entry)
        if se is not None:
            clean_l[mid] = se
    return {
        "version": int(version),
        "updated_at": doc.get("updated_at"),
        "whisper": clean_w,
        "llm": clean_l,
    }


def merge_section(
    builtin: dict, remote_section: dict, shipped_trust: Optional[dict] = None
) -> dict:
    """Merge remote section into a deep copy of builtin.

    - New remote ids are added (if valid full entries).
    - Existing ids get field overlays from remote.
    - ``disabled: true`` removes the id from the result.
    """
    out = deepcopy(builtin)
    # Trust is keyed by filename across the WHOLE app, not per section: the
    # whisper and llm catalogs are merged separately, so a section-local map
    # let an llm entry claim a shipped *whisper* filename (`ggml-base.bin`) with
    # its own digest and origin, and the llm downloader — finding no llm trust
    # for it — verified the attacker's digest. Callers pass the combined map.
    shipped = shipped_trust if shipped_trust is not None else shipped_trust_map(builtin)

    def _weakens_shipped(candidate: dict) -> Optional[str]:
        """Reason a candidate may not claim a shipped filename, or None.

        The pin follows the FILENAME, not the model id: otherwise a compromised
        catalog disables `base.en` and adds id `replacement` reusing
        `ggml-base.en.bin` with its own url and digest, and the shipped pin is
        never consulted. The tuple has to carry entitlement and size too — an
        alias that keeps the digest but drops `requires_feature` would hand a
        paid model away, and one that inflates `size_bytes` would loosen the
        download bound.
        """
        trusted = shipped.get(candidate.get("filename"))
        if not trusted:
            return None
        if trusted.get("size_bytes") and not candidate.get("size_bytes"):
            # Dropping the size removes the pre-hash download bound.
            return "size"
        if candidate.get("sha256") != trusted["sha256"]:
            return "digest"
        if candidate.get("requires_feature") != trusted.get("requires_feature"):
            return "entitlement"
        if trusted.get("size_bytes") and candidate.get("size_bytes") != trusted["size_bytes"]:
            return "size"
        return None

    for mid, entry in (remote_section or {}).items():
        if entry.get("disabled") is True:
            out.pop(mid, None)
            continue
        if mid in out:
            merged = dict(out[mid])
            # A built-in digest is authoritative: a remote catalog may add a
            # sha256 for a model that has none, but may not replace or drop one
            # we shipped. That is what keeps built-in models tamper-evident even
            # if the catalog *and* the object store are both compromised
            # (security audit 2026-08-17, F-C).
            pinned_digest = merged.get("sha256")
            for k, v in entry.items():
                if k == "disabled":
                    continue
                if k == "sha256" and pinned_digest:
                    continue
                merged[k] = v
            # still need filename/name after merge, and the overlay must not be
            # able to swap in a name that escapes the models dir (callers may
            # merge a section that never went through sanitize_entry)
            if merged.get("filename") and merged.get("name"):
                if not safe_model_filename(merged.get("filename")):
                    continue  # keep the built-in entry untouched
                # Renaming a shipped entry's file walks it out of the trust map
                # entirely — no digest, no size bound, any origin. Refuse.
                original_filename = (out.get(mid) or {}).get("filename")
                if (
                    original_filename in shipped
                    and merged.get("filename") != original_filename
                ):
                    log.warning(
                        "catalog entry %r rejected: cannot rename shipped file %r",
                        mid, original_filename,
                    )
                    continue
                weakened = _weakens_shipped(merged)
                if weakened:
                    log.warning(
                        "catalog entry %r rejected: reuses shipped filename %r "
                        "but weakens its %s", mid, merged.get("filename"), weakened,
                    )
                    continue
                out[mid] = merged
        else:
            # Full entry required for brand-new models — and a model we never
            # shipped must come from the Models CDN with a digest. Its digest is
            # still verified, but since we did not ship it, it cannot vouch for
            # an arbitrary origin; the CDN is then the only trust available.
            if (
                safe_model_filename(entry.get("filename"))
                and entry.get("name")
                and entry.get("cdn_object")
                and safe_sha256(entry.get("sha256"))
                # An exact size is required too: it is the pre-hash bound that
                # stops an endless stream from filling the disk before the
                # digest can reject it.
                and isinstance(entry.get("size_bytes"), int)
                and entry["size_bytes"] > 0
            ):
                weakened = _weakens_shipped(entry)
                if weakened:
                    log.warning(
                        "catalog entry %r rejected: reuses shipped filename %r "
                        "but weakens its %s", mid, entry.get("filename"), weakened,
                    )
                    continue
                # CDN-only: keeping a `url` would let it be selected whenever the
                # CDN base is unset, which is the origin trust we just required.
                fresh = {k: v for k, v in entry.items() if k != "url"}
                out[mid] = fresh
    return out


def build_export_document(whisper: dict, llm: dict) -> dict:
    """Build a publishable catalog document from in-app catalogs."""
    from datetime import datetime, timezone

    def strip(section: dict) -> dict:
        clean = {}
        for mid, info in section.items():
            if not _safe_model_id(mid):
                continue
            se = sanitize_entry(info)
            if se and not se.get("disabled"):
                clean[mid] = se
        return clean

    return {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "whisper": strip(whisper),
        "llm": strip(llm),
    }


def load_cached_catalog() -> Optional[dict]:
    path = _cache_path()
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return validate_remote_document(doc)
    except Exception as e:
        log.debug("catalog cache unreadable: %s", e)
        return None


def save_cached_catalog(doc: dict) -> None:
    path = _cache_path()
    try:
        path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        # also store fetch meta
        meta = path.with_suffix(".meta.json")
        meta.write_text(
            json.dumps({"fetched_at": time.time()}),
            encoding="utf-8",
        )
    except Exception as e:
        log.debug("catalog cache write failed: %s", e)


def _cache_is_fresh(max_age: float = CACHE_MAX_AGE_SECS) -> bool:
    meta = _cache_path().with_suffix(".meta.json")
    if not meta.exists():
        return False
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
        return (time.time() - float(data.get("fetched_at", 0))) < max_age
    except Exception:
        return False


def fetch_remote_catalog(
    config: Optional[dict] = None,
    *,
    timeout: float = 8.0,
    force: bool = False,
) -> Optional[dict]:
    """Fetch + validate remote catalog; uses disk cache within TTL."""
    if not force and _cache_is_fresh():
        cached = load_cached_catalog()
        if cached is not None:
            return cached

    url = catalog_url_from_config(config)
    if not url:
        return load_cached_catalog()

    try:
        # Use a browser-class UA: some edge Bot Fight configs 404/block
        # short custom UAs on non-object routes (downloads still use Wayfinder-Aura/1.0).
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; Wayfinder-Aura/1.0; "
                    "+https://wayfindercollective.io)"
                ),
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        doc = json.loads(raw.decode("utf-8"))
        clean = validate_remote_document(doc)
        if clean is None:
            log.warning("remote catalog failed validation")
            return load_cached_catalog()
        save_cached_catalog(clean)
        return clean
    except Exception as e:
        log.info("remote catalog fetch failed (%s); using cache/builtin", e)
        return load_cached_catalog()


def apply_remote_to_globals(
    whisper_models: dict,
    llm_models: dict,
    config: Optional[dict] = None,
    *,
    force: bool = False,
) -> dict:
    """
    Merge remote catalog into the given mutable global catalog dicts.

    Returns status dict: {source, whisper_count, llm_count, remote_applied}.
    """
    status = {
        "source": "builtin",
        "remote_applied": False,
        "whisper_count": len(whisper_models),
        "llm_count": len(llm_models),
        "catalog_url": catalog_url_from_config(config),
    }
    # Snapshot builtins before merge (for return of merged sizes)
    remote = fetch_remote_catalog(config, force=force)
    if not remote:
        return status

    # Rebuild from original builtin snapshots stored on first call
    builtin_w = getattr(apply_remote_to_globals, "_builtin_whisper", None)
    builtin_l = getattr(apply_remote_to_globals, "_builtin_llm", None)
    if builtin_w is None:
        apply_remote_to_globals._builtin_whisper = deepcopy(whisper_models)  # type: ignore[attr-defined]
        apply_remote_to_globals._builtin_llm = deepcopy(llm_models)  # type: ignore[attr-defined]
        builtin_w = apply_remote_to_globals._builtin_whisper  # type: ignore[attr-defined]
        builtin_l = apply_remote_to_globals._builtin_llm  # type: ignore[attr-defined]

    # One trust map spanning both catalogs — see the note in merge_section.
    combined_trust = shipped_trust_map({**builtin_w, **builtin_l})
    new_w = merge_section(builtin_w, remote.get("whisper") or {}, combined_trust)
    new_l = merge_section(builtin_l, remote.get("llm") or {}, combined_trust)

    whisper_models.clear()
    whisper_models.update(new_w)
    llm_models.clear()
    llm_models.update(new_l)

    status.update(
        {
            "source": "remote+builtin",
            "remote_applied": True,
            "whisper_count": len(whisper_models),
            "llm_count": len(llm_models),
            "updated_at": remote.get("updated_at"),
        }
    )
    return status
