# Owner-only Flathub submission notes (do not paste AI plan text)

## Before opening the PR

1. Request Flathub generative-AI exception (GitHub issue or Matrix). Cover app + packaging + human-authored PR.
2. Confirm you can issue a temporary Ultra license to reviewers privately.
3. Wait for `v1.1.1` tag CI green and local release-manifest lint/build.

## Files to put in flathub/flathub PR root (branch from `new-pr`)

- `io.wayfindercollective.WayfinderAura.yml` (from `flatpak/release/` after prepare-release-manifest)
- `python-deps.json` (same dir)
- `flathub.json` (this directory)

## Facts for your human-written PR body

- App ID: `io.wayfindercollective.WayfinderAura`
- Source: https://github.com/wayfindercollective/wayfinder-aura tag `v1.1.1`
- License: Elastic-2.0 (source-available freemium)
- Free tier: local whisper.cpp dictation without payment
- Ultra: optional one-time purchase $29.99 launch / $60 regular via wayfindercollective.io
- Runtime: org.kde.Platform 6.10 + PyQt BaseApp 6.10 (BaseApp 6.11 not available yet; request exception)
- Arches: x86_64 only (flathub.json)
- Permissions: wayland, fallback-x11, ipc, pulseaudio, dri, network, xdg-run/wayfinder-aura, Notifications, StatusNotifierWatcher — no full $HOME
- Compiled Python deps built from sdist (rust-stable SDK extension)

## After merge

```bash
flatpak install -y flathub io.wayfindercollective.WayfinderAura
```

Confirm Discover search: Wayfinder Aura.


## Platform wheels (temporary)

Compiled packages currently ship as manylinux wheels (cryptography, cffi, jiter, pydantic-core, numpy, scipy, Pillow). Request a temporary Flathub exception or plan a maturin/OpenBLAS sdist follow-up before/during review.
