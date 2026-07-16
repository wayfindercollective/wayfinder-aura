# Handoff: Wayfinder Aura Flathub / Discover launch

**Date:** 2026-07-16  
**Repo:** https://github.com/wayfindercollective/wayfinder-aura  
**App ID:** `io.wayfindercollective.WayfinderAura`  
**Release tag (target):** `v1.1.2` (follow-up after v1.1.1 packaging; see git tags)  
**Goal:** Ship on Flathub so the app appears in KDE Discover / Bazaar.

This doc is for the next person fixing blockers and finishing submission. It is not the Flathub PR text (that must be **human-authored**).

---

## TL;DR

| Area | State |
|------|--------|
| Version / packaging for Flathub | **Mostly ready** (1.1.1, release manifest, freemium metainfo, sandbox grants cleaned) |
| `main` CI (incl. Flatpak) | **Green** |
| Tag CI for `v1.1.1` | **Not fully green** — Flatpak + PyInstaller + storefront OK; **AppImage smoke fails** → GitHub Release skipped |
| Flathub PR | **Not opened** (must be human; AI policy) |
| Discover listing | **Not live** until Flathub merge + official build |

**Highest-value fixes next:**

1. Unblock **tag CI AppImage** (metainfo validate exit code) so `Create Release` runs.  
2. Owner: **Flathub AI exception** + **human submission PR**.  
3. Follow-ups: **Vulkan** in Flatpak, **sdist/wheels** policy, **runtime 6.11** when BaseApp exists.

---

## Current refs (verify before acting)

```bash
cd /path/to/wayfinder-aura
git fetch origin --tags
git rev-parse origin/main
git rev-parse v1.1.1^{commit}
# Expect both at 3e54b34c64ff0ecba749daa31fb53780a5d30ea9 as of handoff write-up
```

Latest tag CI run (failed overall):  
https://github.com/wayfindercollective/wayfinder-aura/actions/runs/29531551300

| Job | Result |
|-----|--------|
| Tests | success |
| Release readiness (license + storefront browser) | success |
| Build (Flatpak) | **success** |
| Build (PyInstaller) | success |
| Build (AppImage) | **failure** |
| Create Release | skipped (needs all artifact jobs) |

---

## What was already done

### Product / packaging

- Production license defaults: `https://shiny-goshawk-432.convex.site/activate` (+ Ed25519 pubkey in `src/wayfinder/license.py`). Gate: `scripts/ci/check-release-license-defaults.py`.
- Version **1.1.1**: `pyproject.toml`, `src/wayfinder/__init__.py`, metainfo release entry, AppImage `VERSION`.
- Metainfo freemium disclosure (free tier + Ultra $29.99 / $60, external checkout).
- Removed host filesystem grants for model dirs; models use sandboxed `$HOME` under Flatpak.
- `flathub.json` intent: **x86_64 only** (file in staging dir).
- Release generator: `python3 flatpak/prepare-release-manifest.py --tag v1.1.1` → gitignored `flatpak/release/`.
- Storefront checker markers aligned to live `/aura` hero:  
  `"Press a key. Speak. Your words land at your cursor."`  
  (`scripts/ci/check-storefront-readiness.py`).
- CI Tests: `libegl1` etc. for PyQt overlay imports; design-ratchet optical glyph comment on hide-to-tray button.

### Flatpak engines (important product caveat)

- **Vulkan is OFF** for Flatpak whisper + llama (CPU only).
- Reason: whisper.cpp **v1.9.1** + KDE/Freedesktop SDK **glslc 2025.x** links with undefined `matmul_id_subgroup_*` symbols after shader gen. Tried SPIRV-Headers 1.3.290 vs 1.4.321, `-j1`; still failed.
- Free-tier CPU dictation works; Ultra GPU path is **not** honestly delivered in Flatpak until Vulkan is fixed.

### Python deps

- `flatpak/python-deps.json` regenerated with **platform wheels** for compiled packages (cryptography, cffi, jiter, pydantic-core, numpy, scipy, Pillow).
- Full sdist offline builds need maturin/OpenBLAS and failed in CI; **Flathub “build from source” policy** may require exception or a maturin pipeline.

### Staging files (local, gitignored)

Directory: `.tmp-flathub-handoff/` (also listed in `.gitignore` as `.tmp-flathub-handoff/`).

| File | Purpose |
|------|---------|
| `io.wayfindercollective.WayfinderAura.yml` | Release manifest (tag+commit app source) |
| `python-deps.json` | Pip module sources |
| `flathub.json` | `{ "only-arches": ["x86_64"] }` |
| `OWNER-FLATHUB-PR.md` | Short owner PR notes (outdated SHA in places—prefer this doc) |

Regenerate release YAML after any tag move:

```bash
python3 flatpak/prepare-release-manifest.py --tag v1.1.1
cp flatpak/release/io.wayfindercollective.WayfinderAura.yml \
   flatpak/release/python-deps.json \
   .tmp-flathub-handoff/
```

### Related docs (older / overlapping)

- `docs/FLATHUB-HANDOFF.md` — owner checklist (updated 2026-07-16 path; may lag this file).
- `SHIPPING.md` — shipping gap tracker.
- `flatpak/BUILDING.md` — local build notes.
- Official Flathub submission: https://docs.flathub.org/docs/for-app-authors/submission  
- Flathub requirements (AI, monetisation, source builds, runtime): https://docs.flathub.org/docs/for-app-authors/requirements  

---

## Open issues to fix (ordered)

### P0 — AppImage tag CI / GitHub Release

**Symptom:** AppImage job fails at smoke step:

```text
appstreamcli validate --no-net squashfs-root/.../io.wayfindercollective.WayfinderAura.metainfo.xml
✘ Validation failed: warnings: 1, infos: 1, pedantic: 1
# seen: url-invalid-type vcs-browser; unknown-tag developer
```

AppImage **build + binary extract often succeeds**; exit code 3 is from stricter `appstreamcli` on **ubuntu-22.04** vs host/Bazzite (where validate often only pedantic).

**Also in logs:** early CMake Vulkan errors (`find_package(Vulkan … glslangValidator)`) during native builds; script may fall back to CPU. `glslang-tools` + `install-glslc-if-needed.sh` (`glslc_exe` target) were added—still verify clean Vulkan path if desired.

**Fix options (pick one or combine):**

1. Soften AppImage smoke: accept pedantic/warnings for known AppStream 1.0 tags, or pin a newer `appstreamcli`.
2. Adjust metainfo for older validators (e.g. URL types / developer element) without breaking Flathub modern validation.
3. Make `Create Release` not hard-require AppImage if Flatpak is the launch channel (workflow `needs:` change—product decision).

**Files:** `.github/workflows/ci.yml` (AppImage job), `flatpak/io.wayfindercollective.WayfinderAura.metainfo.xml`, `scripts/build-appimage.sh`, `scripts/ci/install-glslc-if-needed.sh`.

**Success:** Tag workflow fully green + GitHub Release with artifacts (optional for Flathub, required if you want GH release automation).

---

### P0 — Flathub submission (human-owned)

Flathub **forbids AI-generated submission PRs, descriptions, and reviewer replies**. Agents must not open the PR or paste plan text.

**Owner must:**

1. Request **generative AI exception** (GitHub issue / Matrix) for a mature project with AI-assisted history; commit to human-authored PR/replies.  
   Policy: https://docs.flathub.org/docs/for-app-authors/requirements#generative-ai-policy  
2. Be ready to issue a **temporary Ultra license** to reviewers privately (Convex licensing)—never commit keys.  
3. Open PR on `flathub/flathub` against base branch **`new-pr`** (not `master`):  
   - Title: `Add io.wayfindercollective.WayfinderAura`  
   - Root files: release yml + `python-deps.json` + `flathub.json`  
4. Answer reviewers; trigger `bot, build` when allowed.  
5. After merge: accept org invite (2FA), wait for official build, then:

```bash
flatpak install -y flathub io.wayfindercollective.WayfinderAura
# Confirm Discover search: "Wayfinder Aura"
```

**Permissions story for reviewers:**

| Permission | Why |
|------------|-----|
| wayland / fallback-x11 / ipc | Display |
| pulseaudio | Mic |
| dri | GPU path (when re-enabled) / graphics |
| network | License activate, model updates, optional cloud STT (off by default) |
| `xdg-run/wayfinder-aura:create` | Host/Deck trigger socket |
| Notifications / StatusNotifierWatcher | Tray + notifications |
| No full `$HOME` | Models under sandbox data |

**Likely review friction:**

- Runtime 6.10 while 6.11 exists (PyQt BaseApp 6.11 missing → request exception).  
- Platform wheels for compiled Python packages.  
- Freemium / Elastic-2.0 (disclose free vs Ultra; metainfo already has pricing).  
- CPU-only engines if reviewers test GPU Ultra claims.

---

### P1 — Re-enable Vulkan in Flatpak

**Symptom:** Link failure on `whisper-cli` with undefined `matmul_id_subgroup_*_{len,data}` after vulkan-shaders-gen (v1.9.1 + SDK glslc 2025).

**Ideas:**

- Newer whisper.cpp / ggml once tagged; or pin older glslc that matches generator.  
- Confirm SPIRV-Headers pin matches SDK Vulkan (currently module can use `vulkan-sdk-1.4.321.0`).  
- Upstream issue search; avoid claiming GPU in Flathub listing until fixed.

**Files:** `flatpak/io.wayfindercollective.WayfinderAura.yml` (whisper-cpp / llama-cpp modules, `GGML_VULKAN`).

---

### P1 — Python deps “build from source”

- Prefer regenerating via `flatpak/generate-pip-sources.sh` with sdist for pure C where possible.  
- Rust packages (cryptography, jiter, pydantic-core) need maturin + cargo offline (or temporary Flathub exception listing each wheel).  
- CustomTkinter pinned `<6` in requirements generator.

---

### P2 — Runtime / BaseApp 6.11

When `com.riverbankcomputing.PyQt.BaseApp//6.11` exists: bump Platform, Sdk, BaseApp, CI installs, pip generator `--runtime`, tests, BUILDING.md together.

---

### P2 — Tag hygiene

`v1.1.1` was moved several times during launch work (no prior GH Release). Prefer **immutable tags** after the next green full tag CI; use `v1.1.2` for further bumps rather than force-moving.

---

## How to verify (cheat sheet)

```bash
# License + packaging unit tests
python3 scripts/ci/check-release-license-defaults.py
python3 -m pytest tests/test_flatpak_release_manifest.py tests/test_release_metadata.py tests/test_version.py -q

# Storefront (needs Playwright + chromium for --browser)
python3 scripts/ci/check-storefront-readiness.py --browser --timeout 45

# Metainfo / desktop
appstreamcli validate --no-net flatpak/io.wayfindercollective.WayfinderAura.metainfo.xml
desktop-file-validate flatpak/io.wayfindercollective.WayfinderAura.desktop

# Release manifest
python3 flatpak/prepare-release-manifest.py --tag v1.1.1
flatpak run --command=flatpak-builder-lint org.flatpak.Builder \
  manifest flatpak/release/io.wayfindercollective.WayfinderAura.yml
# Expect warning: runtime-update-available-to-org.kde.Platform-6.11 only

# Local Flatpak (Bazzite: use org.flatpak.Builder if host builder missing)
# See flatpak/BUILDING.md
```

CI: https://github.com/wayfindercollective/wayfinder-aura/actions  

---

## Critical file map

| Path | Role |
|------|------|
| `flatpak/io.wayfindercollective.WayfinderAura.yml` | Local-build manifest (`type: dir`) |
| `flatpak/prepare-release-manifest.py` | Tag → Flathub YAML |
| `flatpak/python-deps.json` | Pip modules |
| `flatpak/generate-pip-sources.sh` | Regenerate deps |
| `flatpak/io.wayfindercollective.WayfinderAura.metainfo.xml` | AppStream |
| `.github/workflows/ci.yml` | Tests, release-readiness, Flatpak, AppImage, Release |
| `scripts/ci/check-storefront-readiness.py` | Storefront gates |
| `scripts/ci/check-release-license-defaults.py` | Prod license gate |
| `scripts/ci/install-glslc-if-needed.sh` | AppImage glslc (`glslc_exe`) |
| `src/wayfinder/license.py` | License API + pubkey |
| `src/wayfinder/config.py` | `premium_url` / prices |

---

## Suggested fix sequence for the next engineer

1. **Reproduce AppImage smoke failure** on ubuntu-22.04 or CI logs; fix metainfo and/or validate flags.  
2. Push → retag **only if needed** as `v1.1.2` (preferred) or wait for green `v1.1.1` rebuild via workflow_dispatch if you avoid retagging.  
3. Confirm tag CI: Flatpak + AppImage + Create Release green.  
4. Regenerate `flatpak/release/` + refresh `.tmp-flathub-handoff/`.  
5. Hand to **owner** for AI exception + human Flathub PR.  
6. Parallel track: Vulkan re-enable + sdist plan (can ship Flathub free tier without Vulkan if messaging is honest).

---

## Explicit non-goals / do-nots

- Do not open Flathub PRs or post reviewer replies via AI/agent.  
- Do not paste this handoff or Sol plan text into the Flathub PR body.  
- Do not put license keys or secrets in git.  
- Do not claim GPU Ultra performance on Flathub until Vulkan Flatpak works.  
- Do not force-move `v1.1.0` (older tag history).

---

## Contact / context

Launch work executed 2026-07-16 in Grok session against local clone  
`/var/home/bazzite/Dev/wayfinder-aura`, remote `wayfindercollective/wayfinder-aura`.  
Sol (gpt-5.6-sol) reviewed the launch plan (APPROVED after revisions emphasizing AI policy, no retag of `v1.1.0`, wheels, reviewer Ultra access).

Questions: start from this file + `SHIPPING.md` + latest failed Actions log for AppImage.
