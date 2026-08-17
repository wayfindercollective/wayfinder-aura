#!/usr/bin/env python3
"""Verify every built-in catalog model against its pinned sha256.

Since the 2026-08-17 security audit (F-C) each entry in ``WHISPER_CPP_MODELS``
and ``LLM_GGUF_MODELS`` carries a ``sha256``, and downloads are refused when the
bytes do not match. That makes the pins load-bearing: if an object on the Models
CDN is ever re-uploaded with different bytes, in-app downloads of that model
start failing. Run this before cutting a release.

    # cheap: HEAD every object, compare Content-Length against the pin's size
    python3 scripts/verify-model-digests.py

    # authoritative: stream every object and hash it (tens of GB of traffic)
    python3 scripts/verify-model-digests.py --full

Ultra-gated objects need a license bearer; the script reuses the local
activation, so run it on a machine with Ultra activated to cover them. Without
one they are reported as SKIPPED (auth required), never as passing.

Exit code is non-zero if any model fails, so CI or a release script can gate on
it.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from wayfinder.core.download_guard import require_https  # noqa: E402

CHUNK = 1 << 20


def _catalogs():
    """Read the two built-in catalogs out of wayfinder_main.py without importing it.

    Importing the module would pull in tkinter and the whole UI stack; this
    script has to run headless (CI, a server, a Deck in Game Mode).
    """
    import ast

    source = (REPO_ROOT / "wayfinder_main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {"WHISPER_CPP_MODELS": "whisper", "LLM_GGUF_MODELS": "llm"}
    found: dict[str, dict] = {}

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in wanted:
                found[wanted[target.id]] = ast.literal_eval(node.value)

    missing = set(wanted.values()) - set(found)
    if missing:
        raise SystemExit(f"could not find catalog(s) in wayfinder_main.py: {sorted(missing)}")
    return [(section, found[section]) for section in ("whisper", "llm")]


FLATPAK_CONFIG_HOME = Path.home() / ".var/app/io.wayfindercollective.WayfinderAura/config"


def _bearer(explicit_config_home: str | None = None):
    """Find an Ultra bearer token, preferring an explicit config home.

    A license activated inside the shipping Flatpak lives under the sandbox's
    XDG_CONFIG_HOME, not the host's — running this on the host would otherwise
    see no activation and silently skip every license-gated object.
    `wayfinder.config` reads XDG_CONFIG_HOME at import time, so each candidate
    is tried in a fresh interpreter state.
    """
    import os

    original_config_home = os.environ.get("XDG_CONFIG_HOME")
    candidates = []
    if explicit_config_home:
        candidates.append(explicit_config_home)
    else:
        candidates.append(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config"))
        if FLATPAK_CONFIG_HOME.is_dir():
            candidates.append(str(FLATPAK_CONFIG_HOME))

    for config_home in candidates:
        if not (Path(config_home) / "wayfinder-aura" / "license.json").is_file():
            continue
        os.environ["XDG_CONFIG_HOME"] = config_home
        # The ROOT package must go too, not just `wayfinder.*`: `wayfinder.config`
        # survives as an attribute of the cached parent package, so
        # `from wayfinder import config` would keep handing back the first
        # candidate's CONFIG_DIR and the second candidate would never be tried.
        for name in [m for m in sys.modules if m == "wayfinder" or m.startswith("wayfinder.")]:
            del sys.modules[name]
        try:
            from wayfinder.license import get_feature_gate

            token = get_feature_gate().get_bearer_token()
            if token:
                print(f"using the license activated under {config_home}\n")
                return token
        except Exception:
            continue

    # Leave the environment as we found it — nothing else in this run should
    # inherit a config home we only borrowed to look for an activation.
    if original_config_home is None:
        os.environ.pop("XDG_CONFIG_HOME", None)
    else:
        os.environ["XDG_CONFIG_HOME"] = original_config_home
    return None


def _request(url: str, info: dict, bearer, *, method: str):
    from wayfinder.models_cdn import download_auth_headers

    headers = download_auth_headers(info, bearer_token=bearer, download_url=url)
    return urllib.request.Request(url, headers=headers, method=method)


def _opener(info: dict, bearer):
    """urllib opener that keeps the bearer on the CDN origin and stays on https.

    The default redirect handler copies every header — including Authorization —
    to whatever origin the redirect names. This tool runs with a real license
    token, so it needs the same rule the app enforces.
    """
    from wayfinder.core.download_guard import HttpsOnlyRedirectHandler
    from wayfinder.models_cdn import url_is_models_cdn

    class _SafeRedirect(HttpsOnlyRedirectHandler, urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            was_head = req.get_method() == "HEAD"
            new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
            if new_req is None:
                return None
            if not url_is_models_cdn(newurl):
                new_req.remove_header("Authorization")
            if was_head:
                # urllib rewrites a redirected HEAD into a GET, which would turn
                # the cheap reachability check into a full model download.
                new_req.method = "HEAD"
            return new_req

    return urllib.request.build_opener(_SafeRedirect)


def _https_opener():
    """Opener with no credentials that still refuses to leave https."""
    from wayfinder.core.download_guard import HttpsOnlyRedirectHandler

    class _SafeRedirect(HttpsOnlyRedirectHandler, urllib.request.HTTPRedirectHandler):
        pass

    return urllib.request.build_opener(_SafeRedirect)


def _hf_metadata_check(url: str, expected: str) -> tuple[str, str]:
    """Compare a pin against Hugging Face's own LFS oid — no bytes transferred.

    An LFS oid *is* the sha256 of the file, so this catches a wrong pin or an
    upstream revision that no longer holds what we pinned, for the price of one
    API call per repo. It says nothing about the CDN copies (different origin) —
    that still needs --full.
    """
    import re

    match = re.match(r"https://huggingface\.co/([^/]+/[^/]+)/resolve/([^/]+)/(.+)$", url)
    if not match:
        return "SKIP", "not a Hugging Face URL"
    repo, revision, path = match.groups()
    if revision == "main":
        return "FAIL", "pinned to mutable 'main'"

    api = f"https://huggingface.co/api/models/{repo}/tree/{revision}?recursive=1"
    try:
        require_https(api)
        with _https_opener().open(api, timeout=30) as resp:
            import json

            entries = json.load(resp)
    except Exception as e:
        return "FAIL", f"could not read HF metadata: {type(e).__name__}: {e}"

    for entry in entries:
        if entry.get("path") == path:
            oid = (entry.get("lfs") or {}).get("oid")
            if not oid:
                return "WARN", "file is not LFS — no oid to compare"
            if oid != expected:
                return "FAIL", f"HF oid {oid} != pinned {expected}"
            return "PASS", "matches HF LFS oid"
    return "FAIL", f"{path} is not present at revision {revision[:12]}"


def _check(url: str, info: dict, bearer, *, full: bool) -> tuple[str, str]:
    """Return (status, detail) for one URL.

    PASS = digest verified. OK/WARN = reachable, not hashed (no --full).
    FAIL = wrong bytes or unreachable. SKIP = needs a license we do not have.
    """
    expected = info.get("sha256")
    try:
        # Before anything else: a non-https target must never be requested with
        # a license bearer in hand. The redirect handler only guards later hops.
        require_https(url)
        req = _request(url, info, bearer, method="GET" if full else "HEAD")
        with _opener(info, bearer).open(req, timeout=60) as resp:
            want = int(info.get("size_bytes") or 0)
            if not full:
                raw_length = resp.headers.get("Content-Length")
                # `size_bytes` is the exact byte count and the app enforces it
                # before transferring, so any difference here is a real defect:
                # every client would abort this download. Distinguish "no header"
                # (nothing to compare) from "Content-Length: 0" (a real
                # disagreement that used to slip through as OK).
                if raw_length is None:
                    return "WARN", "no Content-Length to compare (use --full)"
                length = int(raw_length)
                if want and length != want:
                    return "FAIL", f"{length} bytes but the catalog pins {want}"
                if not want:
                    return "WARN", f"{length} bytes — catalog has no size_bytes"
                return "OK", f"{length} bytes reachable (use --full to hash)"

            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
            actual = digest.hexdigest()
            if actual != expected:
                return "FAIL", f"sha256 {actual} != pinned {expected}"
            if want and total != want:
                # The digest matched, so the bytes are right and the *catalog*
                # is wrong — which still aborts every client download before
                # transfer. A one-byte typo in size_bytes has to fail here.
                return "FAIL", f"sha256 ok but catalog size_bytes={want} != actual {total}"
            return "PASS", f"sha256 + size verified ({total} bytes)"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "SKIP", f"HTTP {e.code} — needs an Ultra license bearer"
        return "FAIL", f"HTTP {e.code}"
    except Exception as e:  # network, DNS, timeout
        return "FAIL", f"{type(e).__name__}: {e}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true",
                        help="download and hash every object (authoritative, slow)")
    parser.add_argument("--source", choices=("cdn", "hf", "both"), default="cdn",
                        help="which origin to check (default: cdn)")
    parser.add_argument("--only", nargs="*", metavar="ID",
                        help="limit to these model ids")
    parser.add_argument("--config-home", metavar="DIR",
                        help="XDG_CONFIG_HOME holding the activation to use "
                             "(default: host config, then the Flatpak sandbox)")
    parser.add_argument("--allow-skips", action="store_true",
                        help="exit 0 even when license-gated objects went unchecked "
                             "(NOT valid for the release gate)")
    parser.add_argument("--hf-metadata", action="store_true",
                        help="compare pins against Hugging Face LFS oids without "
                             "downloading anything (cheap drift check for CI)")
    args = parser.parse_args()

    from wayfinder.model_catalog import safe_sha256
    from wayfinder.models_cdn import resolve_download_url

    bearer = _bearer(args.config_home)
    if not bearer:
        print("note: no Ultra bearer found — license-gated objects will be SKIPPED\n")

    failures, skipped, checked = [], [], 0
    for section, catalog in _catalogs():
        for model_id, info in catalog.items():
            if args.only and model_id not in args.only:
                continue
            if not safe_sha256(info.get("sha256")):
                failures.append((model_id, "catalog entry has no valid sha256"))
                print(f"[{section}] {model_id:24s} FAIL  no pinned sha256")
                continue

            if args.hf_metadata:
                url = info.get("url") or ""
                status, detail = _hf_metadata_check(url, info["sha256"])
                checked += 1
                print(f"[{section}] {model_id:24s} {status:4s} (hf-meta) {detail}")
                if status == "FAIL":
                    failures.append((f"{model_id} [hf-meta]", detail))
                continue

            targets = []
            if args.source in ("cdn", "both"):
                cdn_url = resolve_download_url(info)
                if cdn_url:
                    targets.append(("cdn", cdn_url))
            if args.source in ("hf", "both") and info.get("url"):
                targets.append(("hf", info["url"]))

            for origin, url in targets:
                status, detail = _check(url, info, bearer, full=args.full)
                checked += 1
                print(f"[{section}] {model_id:24s} {status:4s} ({origin}) {detail}")
                if status == "FAIL":
                    failures.append((f"{model_id} [{origin}]", detail))
                elif status == "SKIP":
                    skipped.append(f"{model_id} [{origin}]")

    print(f"\nchecked {checked} url(s): {len(failures)} failed, {len(skipped)} skipped")
    if skipped:
        print("SKIPPED (not verified — rerun on a machine with Ultra activated):")
        for s in skipped:
            print(f"  - {s}")
    if failures:
        print("FAILED:")
        for model_id, detail in failures:
            print(f"  - {model_id}: {detail}")
        return 1
    if skipped and not args.allow_skips:
        # A gate that exits 0 with the paid objects unchecked is a gate that
        # certifies nothing — the seven Ultra objects are exactly the ones that
        # need covering (Sol review finding 4).
        print("\nINCOMPLETE: license-gated objects were not verified. "
              "Re-run with an Ultra activation, or pass --allow-skips to "
              "acknowledge an incomplete check.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
