# Flathub / Discover — Owner handoff checklist

**Updated:** 2026-08-26 (v1.1.8 launch path)

> **Full engineering handoff (blockers, CI, fix order):**  
> **`docs/FLATHUB-LAUNCH-HANDOFF-2026-07-16.md`**

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

### 0c. Runtime

- Submission uses the current matched **`org.kde.Platform//6.11` +
  `com.riverbankcomputing.PyQt.BaseApp//6.11`** pair.

---

## Done in-repo (agent / CI path)

| Item | Status |
|------|--------|
| Production license URL | `shiny-goshawk-432.convex.site/activate` |
| Version | **1.1.8-beta.10** candidate; stable **1.1.8** pending hands-on signoff |
| Freemium disclosure in metainfo | Free tier + Ultra $29.99 / $60 + external checkout |
| Host model FS grants removed | Sandbox `$HOME` only |
| Compiled Python dependencies | Converted to offline source builds: shared OpenBLAS, CFFI, Cryptography, NumPy, SciPy, Pillow, Jiter, and Pydantic Core. Maturin and all locked Rust crates are source-vendored. |
| CI EGL for PyQt overlay tests | `libegl1` et al. in the consolidated Quality job |
| Screenshots on public `main` | HTTP 200 |
| AppStream / desktop validate | Clean (1 pedantic note) |
| Local Flatpak | KDE/PyQt 6.11 full source build/export plus native import/processing smoke verified 2026-08-26; exact-commit mini-inf rebuild still required before `v1.1.8` |

---

## Git release (after Quality + manual Flatpak CI are green)

```bash
cd /var/home/aenect/dev/work/wayfinder-aura
# Routine pushes run the fast Quality job only. Exercise the shared
# release-grade Flatpak path on mini-inf before tagging.
git push origin main
scripts/ci/build-flatpak-on-mini-inf.sh
# wait for Quality and the mini-inf build/smokes to pass
git tag -a v1.1.8 -m "Wayfinder Aura 1.1.8"
git push origin v1.1.8
# do NOT move or delete v1.1.0
```

The tag—not a raw `main` push—is the customer release boundary. Direct bundle
users receive the in-app GitHub Release notice. After Flathub acceptance, the
generated manifest's stable-only `x-checker-data` creates update PRs; merging a
green Flathub update PR publishes it to Discover/GNOME Software users.

Generate submission artifacts (gitignored under `flatpak/release/`):

```bash
python3 flatpak/prepare-release-manifest.py --tag v1.1.8
```

Handoff files for the Flathub fork root:

- `io.wayfindercollective.WayfinderAura.yml`
- `python-deps.json`
- `python-numpy-build-tools.json`
- `python-scipy-build-tools.json`
- `cargo-sources-maturin.json`
- `cargo-sources-cryptography.json`
- `cargo-sources-jiter.json`
- `cargo-sources-pydantic-core.json`
- `flathub.json` → `{ "only-arches": ["x86_64"] }`

---

## Human Flathub PR steps

1. Exception (0a) granted.
2. `gh repo fork --clone flathub/flathub` → branch from **`new-pr`**.
3. Copy the nine files above into the PR root.
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
| x11 / ipc | CustomTkinter UI, PyQt overlay, and xdotool injection through XWayland |
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
| **Repo / agent** | CI green, stable `v1.1.8` tag, release manifest, and exact-commit mini-inf proof build |
| **Result** | App installable via Discover from Flathub after merge + official build |

Until **M0 (exception) + green tag CI + human PR**, Discover will not list the app for other users.
