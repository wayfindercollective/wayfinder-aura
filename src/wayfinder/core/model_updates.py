"""
Model update detection for Wayfinder Aura.

Checks HuggingFace for newer versions of whisper and LLM models on app startup.
Results are cached for 24 hours to avoid unnecessary API calls.
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import CONFIG_DIR

# Cache file for update check results
UPDATE_CACHE_FILE = CONFIG_DIR / "model_updates_cache.json"

# Check interval: once per week (seconds)
CHECK_INTERVAL = 604800

# HuggingFace repos to monitor for updates. Keep in sync with the download
# catalog (LLM_GGUF_MODELS in wayfinder_main.py / LLM_MODELS in core/setup.py) —
# every downloadable post-processing model should be monitored so new revisions
# are detected and offered for download.
MONITORED_MODELS = {
    "llm_gemma3_1b": {
        "repo_id": "bartowski/google_gemma-3-1b-it-GGUF",
        "description": "Gemma 3 1B (post-processing, recommended)",
        "current_filename": "google_gemma-3-1b-it-Q4_K_M.gguf",
        "category": "llm",
    },
    "llm_qwen35": {
        "repo_id": "unsloth/Qwen3.5-2B-GGUF",
        "description": "Qwen 3.5 2B (post-processing)",
        "current_filename": "Qwen3.5-2B-Q4_K_M.gguf",
        "category": "llm",
    },
    "llm_lfm2_5": {
        "repo_id": "LiquidAI/LFM2.5-1.2B-Instruct-GGUF",
        "description": "LFM2.5 1.2B (post-processing)",
        "current_filename": "LFM2.5-1.2B-Instruct-Q4_K_M.gguf",
        "category": "llm",
    },
    "llm_qwen25": {
        "repo_id": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        "description": "Qwen 2.5 1.5B (legacy post-processing)",
        "current_filename": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
        "category": "llm",
    },
    "whisper": {
        "repo_id": "ggerganov/whisper.cpp",
        "description": "Whisper transcription models",
        "category": "whisper",
    },
}


def check_for_updates(force: bool = False) -> Dict[str, Any]:
    """
    Check monitored HuggingFace repos for model updates.

    Uses cached results unless force=True or cache is older than CHECK_INTERVAL.

    Returns dict with:
        - updates_available: bool
        - models: list of update info dicts
        - last_checked: ISO timestamp
        - error: optional error message
    """
    if not force:
        cached = _load_cache()
        if cached and _is_cache_fresh(cached):
            return cached

    results = {
        "updates_available": False,
        "models": [],
        "last_checked": datetime.now().isoformat(),
        "error": None,
    }

    try:
        import requests
    except ImportError:
        results["error"] = "requests library not available"
        return results

    for model_key, model_info in MONITORED_MODELS.items():
        try:
            repo_data = _fetch_repo_info(model_info["repo_id"])
            if repo_data:
                last_modified = repo_data.get("lastModified", "")
                model_id = repo_data.get("modelId", model_info["repo_id"])

                update_entry = {
                    "key": model_key,
                    "repo": model_info["repo_id"],
                    "description": model_info["description"],
                    "category": model_info["category"],
                    "last_modified": last_modified,
                    "model_id": model_id,
                }

                # Check if this is newer than what we last saw
                cached = _load_cache()
                if cached:
                    prev = _find_cached_model(cached, model_key)
                    if prev and prev.get("last_modified") != last_modified:
                        update_entry["has_update"] = True
                        results["updates_available"] = True

                results["models"].append(update_entry)
        except Exception as e:
            results["models"].append({
                "key": model_key,
                "repo": model_info["repo_id"],
                "description": model_info["description"],
                "error": str(e),
            })

    _save_cache(results)
    return results


def get_available_upgrades(current_model_path: str) -> Optional[Dict[str, Any]]:
    """
    Check if there's a newer model family available for the user's current model.

    Args:
        current_model_path: Path to the user's current LLM model file

    Returns:
        Dict with upgrade info if available, None otherwise
    """
    filename = os.path.basename(current_model_path).lower()

    # Detect if user is on Qwen 2.5 and Qwen 3.5 is available
    if "qwen2.5" in filename:
        return {
            "current": "Qwen 2.5",
            "available": "Qwen 3.5",
            "reason": "Improved instruction following and accuracy",
            "download_key": "Qwen3.5-2B-Q4_K_M",
            "message": "Qwen 3.5 2B is available with improved dictation cleanup quality.",
        }

    return None


def _fetch_repo_info(repo_id: str) -> Optional[Dict[str, Any]]:
    """Query HuggingFace API for repo metadata."""
    import requests

    url = f"https://huggingface.co/api/models/{repo_id}"
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return response.json()


def _find_cached_model(cache: Dict, model_key: str) -> Optional[Dict]:
    """Find a model entry in cached results."""
    for model in cache.get("models", []):
        if model.get("key") == model_key:
            return model
    return None


def _load_cache() -> Optional[Dict[str, Any]]:
    """Load cached update check results."""
    try:
        if UPDATE_CACHE_FILE.exists():
            with open(UPDATE_CACHE_FILE) as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError):
        pass
    return None


def _save_cache(data: Dict[str, Any]) -> None:
    """Save update check results to cache."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(UPDATE_CACHE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except IOError:
        pass


def _is_cache_fresh(cache: Dict[str, Any]) -> bool:
    """Check if cache is within CHECK_INTERVAL."""
    last_checked = cache.get("last_checked", "")
    if not last_checked:
        return False
    try:
        checked_time = datetime.fromisoformat(last_checked)
        age = (datetime.now() - checked_time).total_seconds()
        return age < CHECK_INTERVAL
    except (ValueError, TypeError):
        return False
