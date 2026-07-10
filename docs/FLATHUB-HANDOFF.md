# Flathub / Discover — Your handoff checklist

**Updated:** 2026-07-09  
**What was done for you:** store screenshots recaptured; packaging validated; dry-run release manifest generated; remaining steps that only you can do listed below.

Discover on Bazzite/Fedora installs apps from **Flathub**. A local Flatpak on your machine does not appear for other users until Flathub publishes the app.

---

## Done locally (2026-07-09)

| Item | Status |
|------|--------|
| **Store screenshots** | All five recaptured at **1920×1080** free-tier profile via `scripts/capture_store_screenshots.py` |
| Files | `screenshots/main-window.png`, `settings.png`, `overlay.png`, `style.png`, `welcome.png` |
| AppStream metainfo | `appstreamcli validate --no-net` → success (1 pedantic note only) |
| Desktop file | `desktop-file-validate` → clean |
| Local Flatpak manifest | Structure + `flatpak-builder-lint manifest` exercised |
| Dry-run **release** manifest | `flatpak/release/io.wayfindercollective.WayfinderAura.yml` (gitignored) with **git tag+commit** source instead of `type: dir` — **not for submission** until prod license + real tag |
| python-deps | Copied beside release yml in dry-run |

Screenshot URLs in metainfo already point at:

`https://raw.githubusercontent.com/wayfindercollective/wayfinder-aura/main/screenshots/<file>.png`

They only work for Flathub after those files exist on the **public** default branch / tag.

---

## What **you** must do (in order)

### A — Product / money path (cannot automate)

1. **Production license pair** (both together) in `src/wayfinder/license.py`:
   - `LICENSE_API_URL` → production Convex activate URL  
   - `LICENSE_PUBLIC_KEY_HEX` → matching production Ed25519 public key  
   - See `docs/GO-LIVE-INPUTS.md`  
   - Until this is done, release tooling **refuses** a real submission build (by design).

2. **Confirm storefront** (browser):
   - Checkout / landing URLs, price, fee copy final  
   - In-app “Get Ultra” matches live checkout  
   - Values live in `src/wayfinder/config.py` (`premium_url`, `premium_info_url`, prices)

3. **Quick hardware smoke** (you at the keyboard):
   - Wayland: hotkey → speak → text pastes  
   - Optional: X11, tray Open/Reset/Quit, Deck if you market Deck  

### B — Git release (public source for Flathub)

4. **Review + commit** all ship work (security hardening, overlay position, screenshots, docs, …).  
   Your tree was dirty / ahead of `origin` when this handoff was written — Flathub only sees **pushed** commits.

5. **Push** to `https://github.com/wayfindercollective/wayfinder-aura`  
   - Repo must be **public** for Flathub to clone.

6. **Tag** the release commit (version is **1.1.0**):
   ```bash
   git tag -a v1.1.0 -m "Wayfinder Aura 1.1.0"
   git push origin main
   git push origin v1.1.0
   ```

7. **Generate the real Flathub manifest** (no dirty, no dev license):
   ```bash
   python3 flatpak/prepare-release-manifest.py --tag v1.1.0
   # writes flatpak/release/io.wayfindercollective.WayfinderAura.yml + python-deps.json
   ```

8. **Optional but recommended:** prove a clean build of that release YAML (CI tag job, or local `flatpak-builder` against the release file).

9. **Confirm screenshot URLs return HTTP 200** after push:
   ```bash
   curl -sI https://raw.githubusercontent.com/wayfindercollective/wayfinder-aura/main/screenshots/main-window.png | head -1
   ```

### C — Flathub submission (store process)

10. Follow official submission: https://docs.flathub.org/docs/for-app-authors/submission  
    Typical path:
    - New app repo named `io.wayfindercollective.WayfinderAura` under Flathub org (via PR process)
    - Manifest filename = app id: `io.wayfindercollective.WayfinderAura.yml`
    - Include `python-deps.json` (and any other files the manifest references)
    - Open PR / submission; answer reviewer questions

11. **Permissions story** (reviewers often ask):
    - Network: license activation, model updates, **optional** cloud (off by default)
    - PulseAudio: recording  
    - DRI: GPU whisper  
    - `xdg-run/wayfinder-aura`: host hotkey / Deck trigger socket  
    - No full `$HOME`  

12. After merge + first Flathub build: install from Flathub and confirm it appears in **Discover**.

---

## Commands cheat sheet (after A+B)

```bash
cd /path/to/wayfinder-aura

# Validate packaging files
appstreamcli validate --no-net flatpak/io.wayfindercollective.WayfinderAura.metainfo.xml
desktop-file-validate flatpak/io.wayfindercollective.WayfinderAura.desktop
flatpak run --command=flatpak-builder-lint org.flatpak.Builder \
  manifest flatpak/release/io.wayfindercollective.WayfinderAura.yml

# Recapture screenshots later (KDE Wayland session)
python3 scripts/capture_store_screenshots.py
```

---

## What is **not** required for Flathub

- Model SHA digests, socket auth tokens, OS keyring  
- AppImage (optional second channel)  
- Perfect DRM  

---

## Bottom line

| Who | Action |
|-----|--------|
| **Already done here** | Screenshots + packaging validation + dry-run release yml shape |
| **Only you** | Prod license keys, push/tag public repo, real release manifest, Flathub PR |
| **Result** | App installable via Discover from Flathub |

Until **prod license + public `v1.1.0` tag** exist, do not open a Flathub PR that claims production readiness.
