# Go-Live Inputs

These are the remaining values or confirmations needed from Peter before tagging
the paid public release. Everything here is intentionally left until the end.

## Production License

Provide both values together:

- Production activation URL for `src/wayfinder/license.py` `LICENSE_API_URL`.
- Matching production Ed25519 public key hex for `src/wayfinder/license.py`
  `LICENSE_PUBLIC_KEY_HEX`.

Do not change only one side. The app activates online, then verifies the cached
offline token with the embedded public key; mismatched URL/key pairs make paid
users lose premium after activation or offline refresh.

Release tooling now enforces this. `flatpak/prepare-release-manifest.py` refuses
to generate the submission manifest while the checked-in defaults still point at
the dev license backend. `--allow-dev-license` exists only for local dry-runs and
must not be used for a submission build.

GitHub tag releases and manual artifact builds also run
`scripts/ci/check-release-license-defaults.py` before building or publishing
artifacts, so AppImage, PyInstaller, and Flatpak release artifacts are blocked
until these production license defaults are set.

## Storefront

Confirm these values are final and live. The 2026-07-07 external audit reached
both configured URLs. The Aura landing route exposed the expected Wayfinder Aura
shell, but the checkout route exposed only a `Loading checkout...` shell and no
`$29.99`, `$60`, or Ultra checkout copy in the server-rendered payload. Release
tooling now installs Playwright Chromium and probes the rendered pages for those
positive markers before tag/manual artifacts. Local rendered verification reached
the checkout card form and showed subtotal `$29.99`, processing fee `$0.90`, and
total `$30.89`; confirm that fee treatment and checkout copy are final.

- Checkout URL: `src/wayfinder/config.py` `premium_url`
- Landing/info URL: `src/wayfinder/config.py` `premium_info_url`
- Launch price: `src/wayfinder/config.py` `premium_price`
- Regular price: `src/wayfinder/config.py` `premium_price_regular`
- README pricing copy
- In-app Ultra upgrade prompts

## Release Publication

Confirm or provide:

- Public GitHub repository at `https://github.com/wayfindercollective/wayfinder-aura`.
- `v1.1.0` tag on the final release commit.
- Public screenshot URLs matching the AppStream metadata.

## Final Hardware Signoff

Before a paid public release, perform and record:

- Full mic to transcription to injection on Wayland.
- Full mic to transcription to injection on X11.
- Steam Deck Desktop Mode trigger flow.
- Steam Deck Game Mode trigger flow.
- Dedicated GPU dictation on at least one AMD, Intel, or NVIDIA desktop.
- Tray menu Open, Reset, Quit, and icon state behavior on a real desktop shell.
