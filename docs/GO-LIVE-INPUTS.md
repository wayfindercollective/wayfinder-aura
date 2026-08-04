# Go-Live Inputs

These are the remaining confirmations before tagging the paid public release.
Use `docs/PROMOTION-READINESS-CHECKLIST.md` as the candidate record.

## Production License

Verified in the 2026-08-04 audit:

- `src/wayfinder/license.py` defaults to the production activation deployment.
- The embedded Ed25519 public key matches the production release configuration.
- `scripts/ci/check-release-license-defaults.py` passes.

Do not change only one side. The app activates online, then verifies the cached
offline token with the embedded public key; mismatched URL/key pairs make paid
users lose premium after activation or offline refresh.

Release tooling enforces this. `flatpak/prepare-release-manifest.py` refuses to
generate the submission manifest if checked-in defaults point at a known dev
license backend. `--allow-dev-license` is only for deliberate local dry-runs and
must not be used for a submission build.

GitHub tag releases and manual artifact builds also run
`scripts/ci/check-release-license-defaults.py` before building or publishing
artifacts, so AppImage, PyInstaller, and Flatpak release artifacts remain
blocked if those production defaults regress.

## Storefront

The configured URLs and checked-in price values are internally consistent, but
the 2026-08-04 audit found the deployed `/aura` page still promising Free GPU
support. Deploy the prepared website correction: Free is Base/Base.en on CPU,
and every GPU path requires Ultra. Then let tag CI render both pages in Chromium
and confirm the checkout still shows product identity, one-time license, card
payment, launch price `$29.99`, processing fee `$0.90`, and total `$30.89`.

- Checkout URL: `src/wayfinder/config.py` `premium_url`
- Landing/info URL: `src/wayfinder/config.py` `premium_info_url`
- Launch price: `src/wayfinder/config.py` `premium_price`
- Regular price: `src/wayfinder/config.py` `premium_price_regular`
- README pricing copy
- In-app Ultra upgrade prompts

## Release Publication

Confirm or provide:

- Public GitHub repository at `https://github.com/wayfindercollective/wayfinder-aura`.
- `v1.1.8-beta.1` on the approved prerelease commit, followed by `v1.1.8` only
  after the exact prerelease artifacts pass.
- Public screenshot URLs matching the AppStream metadata.
- AppImage and Flatpak filenames, sizes, SHA-256 hashes, and GitHub Actions run
  recorded in the promotion checklist.

## Final Hardware Signoff

Before a paid public release, perform and record:

- Full mic to transcription to injection on Wayland.
- Full mic to transcription to injection on X11.
- Steam Deck Desktop Mode trigger flow.
- Steam Deck Game Mode trigger flow.
- Dedicated GPU dictation on at least one AMD, Intel, or NVIDIA desktop.
- Tray menu Open, Reset, Quit, and icon state behavior on a real desktop shell.
- Existing-key activation on a genuinely separate clean computer, including an
  offline restart.
- One real production purchase, email/key delivery, activation, and refund-path
  verification.
