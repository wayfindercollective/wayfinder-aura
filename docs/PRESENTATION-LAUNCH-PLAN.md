# Plan: Wayfinder Aura Pre-Launch Presentation Refinement (v3 — post-Codex R2)

**Status:** Implemented in-tree (2026-07-08) except **live store screenshot recapture** (still manual — recipe in `screenshots/README.md`). Codex review: APPROVED.

**Project:** `/var/home/bazzite/Dev/wayfinder-aura`  
**Goal:** Refine how the app *presents* itself before paid public launch — store surfaces, first-open UX, brand consistency, and conversion copy.  
**Out of scope:** Settings mouse-scroll fix (owned by another agent). Production license backend keys, Flathub tag packaging, and full hardware QA remain ship blockers but are *not* this plan's implementation work.  
**Skip-perf-check:** UI chrome/copy/screenshots only; no 3D/render pipeline changes.  
**Skip-asset-reuse-check:** No new 3D/mesh/animation content. New screenshots and optional icon recolor are presentation assets only.

## Background (current state)

- UI system is coherent: deep ink `#0D1117`, soft violet accent `#A78BFA`, bento cards, liquid-ribbon hero (`theme.py`, `hero_render.py`, `wayfinder_main.py`).
- Brand voice is distinctive: lowercase wordmark, mono captions, calm welcome flow (`welcome.py`).
- Store/screenshot surface is weak: only `screenshots/main-window.png` and `settings.png` (idle free-tier 1920×1080). No recording state, overlay, Style tab, or welcome card.
- Brand color drift: app icon is flat blue on black; UI is violet; AppStream branding uses cyan `#00D4FF` (`flatpak/io.wayfindercollective.WayfinderAura.metainfo.xml`).
- Tier naming drift (public strings): Free / Premium / Ultra. Confirmed public "Premium" in `README.md`, `PRIVACY.md`, `SUPPORT.md`, `WEBSITE_COPY_BRIEF.md`.
- Dictate empty state: large dead space under Quick Tips; **returning** startup still does `self.active_tab = "settings"` + `_switch_tab("settings")` (`wayfinder_main.py` ~4522–4530). First-run welcome already switches to Dictate (`show_welcome_pane` → `_switch_tab("dictate")`).
- Sidebar footer shows muted `free` vs celebrated Ultra gold glow + 😇.
- `WEBSITE_COPY_BRIEF.md` is stale. README is stronger source of truth.
- Model default trap: Flatpak path uses `ggml-base.en.bin`; non-Flatpak/source defaults to `ggml-large-v3-turbo.bin` (`config.py`). Screenshots must force a free-legal profile.

## Design principles

1. **One public tier name:** **Ultra** (map all public "Premium" → Ultra). Internal `is_premium` / `premium_url` / `premium_price` stay.
2. **Evidence over claims:** Flathub must show recording + overlay.
3. **Calm free tier, celebrate Ultra.**
4. **Returning users open ready-to-speak** (Dictate), not Settings.
5. **Do not touch settings scroll behavior** — hard no-hunks rule (below).
6. **CLAUDE.md icon rules:** Lucide icons only for decorative chrome; sanctioned exceptions 😇 (Ultra) and 🎭 (caricature). Touched UI must not introduce or leave decorative emoji (e.g. 💡 Quick Tips, ⚡/☁ benefit row) — replace with Lucide or plain text.

---

## Hard constraints (concurrency + conventions)

### Settings scroll no-hunks rule
Another agent owns settings scroll. **This plan may not edit:**
- Scroll/momentum/wheel/trackpad handlers
- Scroll constants, canvas scroll bindings, `_settings_scroll` physics
- Any hunk whose sole purpose is scroll feel

**Allowed in `wayfinder_main.py` only for:** dictate empty state, default tab, free badge, upgrade prompt copy/icons, header/sidebar tier labels, small Settings help one-liner if needed. Prefer surgical string/widget changes away from the settings scroll block (~6080 region).

### Workstream E dropped as code work
No Settings IA restructuring, no Advanced accordion, no “mentally order essentials” as code. Settings presentation for launch = **screenshot composition only** (clean free profile, essentials visible in the still). Optional help one-liner for overlay is the only Settings text add if missing.

---

## Workstreams

### A. Store screenshots (critical) — **5 required**

**Surfaces:** `screenshots/`, `flatpak/io.wayfindercollective.WayfinderAura.metainfo.xml`, `screenshots/README.md`.

| # | File | Content | Caption angle | Required in AppStream |
|---|------|---------|---------------|------------------------|
| 1 | `main-window.png` | Dictate **mid-recording**: live waveform, recording state, short Last Transcription | Dictate into any app | **Yes (default)** |
| 2 | `settings.png` | Free-legal defaults: **base.en** (not Turbo), Local mode, privacy banner, mic | Private local processing | **Yes** |
| 3 | `overlay.png` | Glassmorphic status overlay over real desktop (browser/IDE) | Always-visible status | **Yes** |
| 4 | `style.png` | Style tab, one clear preset (e.g. Professional); no caricature lead | Tone-aware cleanup | **Yes** |
| 5 | `welcome.png` | Welcome card (hotkey step preferred) | Guided first run | **Yes** |

**Capture recipe (mandatory):**

Shared for all shots:
- Clean temporary free-tier profile, **no license**.
- Force free-legal model: **`ggml-base.en.bin` / base.en** — do **not** capture from default source/dev config (turbo). Prefer installed Flatpak or an explicit config override.
- 1920×1080 PNG on KDE Wayland (or document DE).
- Prefer Lucide/plain chrome in stills; no decorative emoji clutter.
- Document full recipe in `screenshots/README.md`.

Profile flags by shot (welcome is incompatible with a completed welcome flag):
| Shots | `setup_completed` | `welcome_completed` | Notes |
|-------|-------------------|---------------------|-------|
| `main-window`, `settings`, `overlay`, `style` | `true` | `true` | Returning free user; no welcome card |
| `welcome.png` | `true` | **`false`** | Startup shows welcome via `first_run_plan` / `show_welcome_pane`, **or** explicitly invoke `show_welcome_pane` and document that path |

**Metainfo:** all 5 listed under `<screenshots>` with outcome-led captions. Recording + overlay are non-negotiable entries.

### B. Brand color alignment (high)

**Decision:** Launch primary = soft violet `#A78BFA`. Ultra gold `#E5AC2A` unchanged.

1. Recolor/regenerate `assets/icon.png` (+ Flatpak resize pipeline uses this source via `flatpak/resize-icons.py`) so tray/store sizes read on brand (not pure blue-on-black void). Keep arrow silhouette.
2. AppStream `<branding>`: replace cyan `#00D4FF` / `#0099CC` with violet family (dark primary `#A78BFA`; light primary darker violet e.g. `#6D28D9` — final pair WCAG-reasonable).
3. Free header logo keeps cosmic trail; Ultra keeps gold glow.

### C. Free badge + tier presentation (high)

1. Sidebar footer: soft pill **`Free`** (border, slight elevation) **or** hide free and only show Ultra when licensed — prefer soft Free pill.
2. Public casing: badges/status use `Free` / `Ultra`. Wordmark stays lowercase.
3. Upgrade panel (`_show_premium_prompt`): outcome copy + **Lucide or plain-text row markers** (no ⚡/☁ decorative emoji). Sanctioned 😇 in Ultra title only.

| Current benefit lead | Target |
|----------------------|--------|
| Groq/OpenAI + GPT/Claude… | Optional cloud speed & polish with your own keys |
| Large & Turbo models… | Faster GPU transcription, including Steam Deck |
| Tone Presets… | Keep (clear) |
| Chunked Recording… | Keep, plain language |
| Large Models + High Accuracy… | Higher-accuracy models, beam search, custom vocabulary |

4. **Public Premium → Ultra file list (required edits):**
   - `README.md` (Free vs Premium table header and body)
   - `PRIVACY.md` (user-facing Premium → Ultra)
   - `SUPPORT.md`
   - `WEBSITE_COPY_BRIEF.md`
   - In-app user-visible strings that say Premium where Ultra is intended
   - **Leave alone:** `is_premium`, `premium_url`, `premium_price`, `premium_price_regular`, `premium_info_url`, feature gate internals, checkout URL path segments

### D. Dictate first-open + returning default (high)

**Corrected target (Codex R1):**
- First-run welcome **already** lands on Dictate while the pane is active.
- **Required change:** when `setup_completed=true` **and** `welcome_completed=true`, **app startup lands on Dictate**, not Settings (`_switch_tab("dictate")` instead of `"settings"` at init).

**Also:**
1. Hide/collapse **Quick Tips** after welcome complete **or** first successful dictation (not permanent empty-state filler).
2. Replace permanent tips dominance with a compact **setup row**: model · Local/Remote · hotkey (e.g. `base.en · Local · Super+F2`). Use Lucide/plain text, not 💡.
3. Larger mono hotkey token under Ready state (mirror welcome step 2).
4. Optional one-liner under idle hero until first transcript; remove after first success.
5. New widgets only from existing toolkit + tokens (`CTkFrame` / `CTkLabel` / `CTkButton`, `COLORS`/`RADIUS`/`SPACING`).

### E. Settings code work — **cancelled**

Screenshot composition only. No scroll hunks. No IA restructure in this plan.

### F. Store + docs + web copy (medium–high)

1. AppStream `<summary>` outcome-led (one final line, e.g. local-first voice dictation / text at cursor).
2. Public 1.1.0 release blurb: human outcomes, not internal build notes.
3. Rewrite `WEBSITE_COPY_BRIEF.md` from README: Super+F2/F3, Ultra tier, local LLM cleanup, Free vs Ultra, privacy caveats.
4. Landing `/aura` requirements for web owner (may be outside this repo): hero proof 5s, privacy, price, install.
5. Note business confirmation: checkout fee `$0.90` vs regular `$60` claims — do not show claims checkout doesn't support unless intentional.

### G. Welcome mic step verification

Manual KDE Wayland check of “speak and watch the hero waveform.” If invalid: **prefer copy fallback** to Mic Test rather than large engineering.

### H. Overlay product surface

1. `overlay.png` (workstream A).
2. One Settings help line only if missing: status indicator while dictating.
3. Spot-check overlay palette vs main theme; fix only obvious mismatch.

### I. Style tab presentation

Style screenshot without caricature lead; store copy leads with named presets.

---

## Implementation order

1. Public string pass (C + F.1–F.3 + emoji→Lucide on touched chrome)  
2. Dictate empty state + **returning startup → Dictate** (D)  
3. Brand icon + AppStream branding colors (B)  
4. Screenshot recapture with free-legal recipe (A)  
5. Welcome verify + overlay help line if needed (G, H)  
6. Web brief + landing handoff (F.4–F.5)  

## Acceptance criteria (measurable)

- [ ] AppStream lists **exactly 5** screenshots; files exist on disk; each ≥1248×702 (existing dimension floor)  
- [ ] Required captions/entries include **recording (main-window)** and **overlay**  
- [ ] Settings still uses free-legal **base.en** (not Turbo) in the published settings shot  
- [ ] Capture README documents free-tier profile + model override / Flatpak-like path, and **split profile flags** (welcome shot uses `welcome_completed=false` or explicit `show_welcome_pane`)  
- [ ] Icon + AppStream branding + UI accent are violet-family; **no cyan AppStream primary**  
- [ ] Public docs in the required file list say **Ultra** not Premium (internal `premium_*` allowed)  
- [ ] Free badge is intentional pill **or** omitted; not raw muted `free` debug-looking text  
- [ ] Startup with `setup_completed=true` and `welcome_completed=true` → active tab **Dictate**  
- [ ] Quick Tips not permanently dominating post-welcome empty Dictate  
- [ ] Upgrade benefits outcome-led; no decorative emoji in that panel except sanctioned 😇  
- [ ] Touched decorative 💡/benefit emoji replaced with Lucide or plain text  
- [ ] WEBSITE_COPY_BRIEF hotkeys/tier match README  
- [ ] Settings scroll handlers/constants **git-diff clean** relative to this plan's branch intent (no intentional scroll edits)  
- [ ] Release metadata tests green; new guards below green  

## Tests (tighten, don't duplicate)

**Already exists:** `test_metainfo_screenshots_are_local_pngs_with_release_sized_dimensions` (URL → file exists + size).

**Add/adjust (useful deltas only):**
1. Metainfo screenshot **count == 5** and required caption substrings or filenames for recording + overlay (or stable file basenames `main-window.png`, `overlay.png`, …).  
2. AppStream branding primary colors **must not** be `#00D4FF` / `#0099CC` (cyan regression guard).  
3. Narrow public-string guard: docs (`README.md`, `PRIVACY.md`, `SUPPORT.md`, `WEBSITE_COPY_BRIEF.md`) and selected UI user-visible strings must not say “Premium” as the product tier; **whitelist** `premium_url`, `premium_price`, `is_premium`, etc.  
4. Startup tab: with completed setup+welcome config fixture, initial tab is Dictate (extend welcome / main launch tests if patterns exist).  
5. No new perf budget items.

## Risks

| Risk | Mitigation |
|------|------------|
| Turbo in free screenshot | Capture recipe forces base.en / Flatpak-like profile |
| Concurrent settings-scroll agent | No-hunks rule; avoid scroll region |
| Icon recolor shock | Keep arrow silhouette; recolor/field only |
| Default Dictate annoys Settings power users | Accept for launch story; optional last-tab memory is follow-up |
| Broken dictation blocks recording shot | Capture from known-good Flatpak build |

## Success metric

A stranger viewing Flathub + first 60s in-app understands: hotkey dictation, local/private by default, Linux-native, optional Ultra — without reading README.

## Simpler path (north star)

Default returning app to Dictate → replace permanent Quick Tips with compact setup/hotkey/model row → public Ultra string pass + Lucide on touched chrome → violet icon/AppStream → capture 5 free-legal screenshots. No Settings restructure.
