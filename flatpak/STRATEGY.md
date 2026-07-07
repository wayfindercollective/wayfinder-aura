# Wayfinder Aura - Flatpak & Monetization Strategy

> Status note: this is a strategy snapshot, not the release gate. Current
> readiness and verification status lives in `SHIPPING.md` and
> `docs/SHIP-READINESS.md`.

## Distribution Plan

### Flathub

- Broadest Linux reach and automatic updates.
- Ships the same application with a useful free tier.
- Requires a public repository tag and Flathub review before submission.

### Direct Downloads

- AppImage and `.flatpak` bundle can be used for non-Flathub distribution.
- Useful for launch-day fallbacks, private testers, and users who do not want to
  add Flathub.
- AppImage support is secondary to the Flatpak release path.

### Recommended Approach

1. Publish the free tier on Flathub.
2. Sell an Ultra license through the storefront.
3. Unlock Ultra features in the same app build after license activation.

## Free vs Ultra

### Free Tier

| Feature | Details |
|---------|---------|
| Basic Transcription | Local whisper.cpp CPU mode |
| Bundled Model | Base.en |
| Hotkeys | Wayland socket path and X11 support |
| Text Injection | Local desktop injection helpers |
| UI | Full settings UI with locked Ultra controls visible |

### Ultra Tier

Regular price is **$60 one-time**. Launch price is **$29.99 one-time**.

| Feature | Value |
|---------|-------|
| GPU Acceleration | Faster transcription on supported dedicated and integrated GPUs |
| Faster-Whisper Backend | Alternative backend for GPU-heavy setups |
| Larger Models | Better accuracy when the user installs or selects larger models |
| Chunked Recording | Longer dictation sessions |
| Advanced Audio | Medium and heavy preprocessing |
| Higher Beam Search | Accuracy-focused transcription settings |
| Typing Speeds | Fast, normal, slow, and very slow simulated typing |
| Custom Vocabulary | User-defined terms and names |

## License Flow

1. User purchases a license through the storefront.
2. User enters the license key in Settings.
3. App activates online against the production license endpoint.
4. Server response includes a signed offline token.
5. App verifies the Ed25519 signature locally after activation and falls back to
   the free tier if no valid token is present.

The current app intentionally does not include a local key generator, HMAC dev
unlock path, or hard-coded test license bypass. Production release still needs
the final license server URL and public key.

## Implementation Status

### Flatpak

- [x] Generate offline Python dependency sources in `flatpak/python-deps.json`
- [x] Build and install the local Flatpak
- [x] Use the final app ID: `io.wayfindercollective.WayfinderAura`
- [x] Capture current UI screenshots for AppStream
- [x] Validate AppStream metadata with `appstreamcli validate --no-net`
- [ ] Publish public repository tag
- [ ] Generate the release manifest from that tag
- [ ] Submit to Flathub

### License System

- [x] Add license module
- [x] Add license entry and status UI
- [x] Gate Ultra features behind license state
- [x] Remove legacy dev unlock behavior
- [ ] Configure production license endpoint
- [ ] Configure production Ed25519 public key
- [ ] Verify production activation and offline-token reuse

### Sales Infrastructure

- [ ] Select and configure storefront
- [ ] Configure license delivery
- [ ] Publish support and refund policy
- [ ] Connect storefront fulfillment to the license service

## Launch Positioning

- Free tier is usable for local CPU dictation.
- Ultra is for users who dictate heavily and want GPU acceleration, larger
  models, longer recordings, and higher-accuracy settings.
- Messaging should stay precise: local mode keeps audio on device; optional
  cloud backends and online activation use network when explicitly configured or
  required.
