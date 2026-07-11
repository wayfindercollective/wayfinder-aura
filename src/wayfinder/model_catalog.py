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

import json
import logging
import os
import time
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# Fields we accept from a remote catalog (never execute / never trust arbitrary keys).
_ALLOWED_ENTRY_KEYS = frozenset({
    "name",
    "size",
    "size_bytes",
    "url",
    "filename",
    "cdn_object",
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


def merge_section(builtin: dict, remote_section: dict) -> dict:
    """Merge remote section into a deep copy of builtin.

    - New remote ids are added (if valid full entries).
    - Existing ids get field overlays from remote.
    - ``disabled: true`` removes the id from the result.
    """
    out = deepcopy(builtin)
    for mid, entry in (remote_section or {}).items():
        if entry.get("disabled") is True:
            out.pop(mid, None)
            continue
        if mid in out:
            merged = dict(out[mid])
            for k, v in entry.items():
                if k == "disabled":
                    continue
                merged[k] = v
            # still need filename/name after merge
            if merged.get("filename") and merged.get("name"):
                out[mid] = merged
        else:
            # full entry required for brand-new models
            if entry.get("filename") and entry.get("name") and (
                entry.get("url") or entry.get("cdn_object")
            ):
                out[mid] = dict(entry)
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

    new_w = merge_section(builtin_w, remote.get("whisper") or {})
    new_l = merge_section(builtin_l, remote.get("llm") or {})

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
