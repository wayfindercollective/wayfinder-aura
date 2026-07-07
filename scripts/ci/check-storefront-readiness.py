#!/usr/bin/env python3
"""Fail release artifact builds when Ultra storefront links are not release-ready."""

from __future__ import annotations

import argparse
import ast
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

REQUIRED_CONFIG_KEYS = {
    "premium_url",
    "premium_info_url",
    "premium_price",
    "premium_price_regular",
}


def config_default_literals(config_file: Path) -> dict[str, str]:
    tree = ast.parse(config_file.read_text(encoding="utf-8"), filename=str(config_file))
    for node in tree.body:
        value: ast.AST | None = None
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "DEFAULT_CONFIG" for target in node.targets
        ):
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "DEFAULT_CONFIG":
            value = node.value
        if value is None:
            continue
        if not isinstance(value, ast.Dict):
            raise ValueError("DEFAULT_CONFIG must be a dict literal")

        values: dict[str, str] = {}
        for key, item in zip(value.keys, value.values):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            if key.value not in REQUIRED_CONFIG_KEYS:
                continue
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                values[key.value] = item.value
        return values
    raise ValueError("DEFAULT_CONFIG assignment not found")


def _https_url(url: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{url!r} is not an absolute HTTPS URL")
    return parsed


def static_readiness_errors(
    *,
    defaults: dict[str, str],
    readme_text: str,
    main_text: str,
) -> list[str]:
    errors: list[str] = []
    missing = sorted(REQUIRED_CONFIG_KEYS - defaults.keys())
    if missing:
        errors.append(f"missing storefront defaults: {', '.join(missing)}")
        return errors

    checkout = defaults["premium_url"]
    info_url = defaults["premium_info_url"]
    launch_price = defaults["premium_price"]
    regular_price = defaults["premium_price_regular"]

    try:
        checkout_url = _https_url(checkout)
        if checkout_url.netloc != "wayfindercollective.io" or not checkout_url.path.startswith("/checkout/"):
            errors.append("premium_url must point at https://wayfindercollective.io/checkout/<product>")
    except ValueError as exc:
        errors.append(f"premium_url: {exc}")

    try:
        info = _https_url(info_url)
        if info.netloc != "wayfindercollective.io" or info.path != "/aura":
            errors.append("premium_info_url must point at https://wayfindercollective.io/aura")
    except ValueError as exc:
        errors.append(f"premium_info_url: {exc}")

    for price in (launch_price, regular_price):
        if not price.startswith("$") or len(price) < 2:
            errors.append(f"invalid price literal: {price!r}")
        if price not in readme_text:
            errors.append(f"README.md is missing storefront price {price!r}")

    if "wayfinder.dev" in readme_text or "wayfinder.dev" in main_text:
        errors.append("stale wayfinder.dev storefront URL remains in release surfaces")

    for key, value in defaults.items():
        fallback = f'self.config.get("{key}", "{value}")'
        if fallback not in main_text:
            errors.append(f"wayfinder_main.py fallback for {key} is not synced with DEFAULT_CONFIG")

    return errors


def fetch_url(url: str, timeout: float) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "WayfinderAuraReleaseCheck/1.0",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", response.getcode())
        body = response.read(1_000_000).decode("utf-8", "replace")
    return int(status), body


def render_url_text(url: str, timeout: float) -> str:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "browser storefront checks require Playwright; install with "
            "`python -m pip install playwright` and `python -m playwright install chromium`"
        ) from exc

    timeout_ms = int(timeout * 1000)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except PlaywrightTimeoutError:
                # Checkout pages often keep analytics or realtime connections open.
                pass

            deadline = time.monotonic() + timeout
            text = page.locator("body").inner_text(timeout=timeout_ms)
            while time.monotonic() < deadline:
                if "Loading checkout" not in text and "Loading the latest release" not in text:
                    break
                time.sleep(0.5)
                text = page.locator("body").inner_text(timeout=timeout_ms)

            title = page.title()
            html = page.content()
            return "\n".join((title, text, html))
        finally:
            browser.close()


def live_readiness_errors(
    *,
    defaults: dict[str, str],
    timeout: float = 20.0,
    fetcher: Callable[[str, float], tuple[int, str]] = fetch_url,
    renderer: Callable[[str, float], str] = render_url_text,
    browser: bool = False,
) -> list[str]:
    errors: list[str] = []
    checkout = defaults.get("premium_url", "")
    info_url = defaults.get("premium_info_url", "")
    launch_price = defaults.get("premium_price", "")
    regular_price = defaults.get("premium_price_regular", "")

    pages = {
        "premium_info_url": (
            info_url,
            ("Wayfinder Aura", "Your voice, turned to text"),
        ),
        "premium_url": (
            checkout,
            ("Wayfinder Aura", "One-time license", "Pay with card", launch_price),
        ),
    }

    for key, (url, markers) in pages.items():
        try:
            status, body = fetcher(url, timeout)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            errors.append(f"{key} {url} is unreachable: {exc}")
            continue

        if status < 200 or status >= 300:
            errors.append(f"{key} {url} returned HTTP {status}")
            continue

        marker_source = body
        if browser:
            try:
                marker_source = renderer(url, timeout)
            except Exception as exc:
                errors.append(f"{key} {url} browser render failed: {exc}")
                continue

        missing_markers = [marker for marker in markers if marker and marker not in marker_source]
        if missing_markers:
            joined = ", ".join(repr(marker) for marker in missing_markers)
            source_name = "rendered page" if browser else "HTML"
            errors.append(f"{key} {url} {source_name} is missing release markers: {joined}")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-file", type=Path, default=Path("src/wayfinder/config.py"))
    parser.add_argument("--readme-file", type=Path, default=Path("README.md"))
    parser.add_argument("--main-file", type=Path, default=Path("wayfinder_main.py"))
    parser.add_argument("--skip-network", action="store_true", help="validate only checked-in release surfaces")
    parser.add_argument("--browser", action="store_true", help="render storefront pages in headless Chromium")
    parser.add_argument("--timeout", type=float, default=20.0, help="seconds per storefront URL")
    args = parser.parse_args(argv)

    try:
        defaults = config_default_literals(args.config_file)
        readme_text = args.readme_file.read_text(encoding="utf-8")
        main_text = args.main_file.read_text(encoding="utf-8")
    except (OSError, SyntaxError, ValueError) as exc:
        print(f"error: cannot inspect storefront release surfaces: {exc}", file=sys.stderr)
        return 1

    errors = static_readiness_errors(defaults=defaults, readme_text=readme_text, main_text=main_text)
    if not args.skip_network:
        errors.extend(live_readiness_errors(defaults=defaults, timeout=args.timeout, browser=args.browser))

    if errors:
        print("error: storefront is not release-ready:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 2

    print("storefront release surfaces are ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
