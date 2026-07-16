# Flathub / Discover — Owner handoff checklist

**Updated:** 2026-07-16 (v1.1.1 launch path; Sol-approved plan)

Discover on Bazzite/Fedora installs apps from **Flathub**. A local Flatpak on your machine does not appear for other users until Flathub publishes the app.

**Agent will not open the Flathub PR or write reviewer replies** (Flathub generative-AI policy). You author the submission.

---

## Policy gates you own (M0) — do these first

### 0a. Generative AI exception (required before Flathub PR)

Flathub policy covers app code, docs, metadata, manifests, **and** PR authorship.  
Request an exception for a mature maintained project:

- Open a human-written issue: https://github.com/flathub/flathub/issues  
  or ask in https://matrix.to/#/#flathub:matrix.org  
- State that the submission PR, descriptions, and review replies will be human-authored.
- If **denied** → stop Flathub; ship AppImage / direct `.flatpak` only.

### 0b. Ultra reviewer access

Reviewers must be able to test paid features. Have a process to issue a **temporary real Ultra license** privately (Convex licensing admin). Never put keys in the PR or git.

### 0c. Runtime note for reviewers

- KDE Platform **6.11** exists; **PyQt BaseApp 6.11** is not available yet.
- Submission stays on matched **`org.kde.Platform//6.10` + `com.riverbankcomputing.PyQt.BaseApp//6.10`**.
- Ask for a runtime exception until BaseApp 6.11 ships.

---

## Done in-repo (agent / CI path)

| Item | Status |
|------|--------|
| Production license URL | `shiny-goshawk-432.convex.site/activate` |
| Version | **1.1.1** (`pyproject`, `__init__`, metainfo, AppImage script) |
| Freemium disclosure in metainfo | Free tier + Ultra $29.99 / $60 + external checkout |
| Host model FS grants removed | Sandbox `$HOME` only |
| Platform wheels for compiled pkgs | Regenerated as **sdists** (cffi, cryptography, jiter, numpy, Pillow, pydantic-core, SciPy) |
| CI EGL for PyQt overlay tests | `libegl1` et al. in Tests job |
| Screenshots on public `main` | HTTP 200 |
| AppStream / desktop validate | Clean (1 pedantic note) |
| Local Flatpak | Previously built; release rebuild after `v1.1.1` tag |

---

## Git release (after CI green on main)

```bash
cd /var/home/bazzite/Dev/wayfinder-aura
# ensure origin/main is green
git push origin main
git tag -a v1.1.1 -m "Wayfinder Aura 1.1.1"
git push origin v1.1.1
# do NOT move or delete v1.1.0
```

Generate submission artifacts (gitignored under `flatpak/release/`):

```bash
python3 flatpak/prepare-release-manifest.py --tag v1.1.1
```

Handoff files for the Flathub fork root:

- `io.wayfindercollective.WayfinderAura.yml`
- `python-deps.json`
- `flathub.json` → `{ "only-arches": ["x86_64"] }`

---

## Human Flathub PR steps

1. Exception (0a) granted or you accept written risk.
2. `gh repo fork --clone flathub/flathub` → branch from **`new-pr`**.
3. Copy the three files above into the PR root.
4. **You** write the PR title/body (do not paste AI plan text).
5. Base branch: **`new-pr`** (never `master`). Title: `Add io.wayfindercollective.WayfinderAura`.
6. Answer reviewers yourself; provide temp Ultra key if asked; `bot, build`.
7. After merge + publish:
   ```bash
   flatpak install -y flathub io.wayfindercollective.WayfinderAura
   ```
   Confirm Discover search: “Wayfinder Aura”.

### Permissions (for your PR notes)

| Permission | Why |
|------------|-----|
| wayland / fallback-x11 / ipc | Display |
| pulseaudio | Microphone |
| dri + GGML_VK_DISABLE_COOPMAT | GPU whisper (Deck-safe) |
| network | License activate, model updates, optional cloud STT (off by default) |
| xdg-run/wayfinder-aura:create | Host/Deck trigger socket |
| Notifications / StatusNotifierWatcher | Tray + notifications |
| No full `$HOME` | Models under sandboxed app data |

---

## Bottom line

| Who | Action |
|-----|--------|
| **Owner** | AI exception, reviewer Ultra keys, human Flathub PR |
| **Repo / agent** | CI green, `v1.1.1` tag, sdist deps, release manifest, local proof build |
| **Result** | App installable via Discover from Flathub after merge + official build |

Until **M0 (exception) + green tag CI + human PR**, Discover will not list the app for other users.
