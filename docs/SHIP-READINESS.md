# Ship Readiness — Wayfinder Aura

_Last updated: 2026-06-28. Tracks what's left before a paid public release, and — at the
top — what's needed from you (Peter). Complements `SHIPPING.md`._

---

## ⛳ What I need from YOU (these block shipping)

1. **Production license backend — the #1 blocker.** `src/wayfinder/license.py` still
   defaults to the **DEV** Convex deployment, so **no purchased key will work in
   production**. I need either:
   - the production activate URL (the `shiny-goshawk-432` endpoint) **and** the
     production **Ed25519 public key** (hex), so I can set them as the defaults, **or**
   - your OK to read them from env (`WAYFINDER_LICENSE_API_URL`, `WAYFINDER_LICENSE_PUBKEY`)
     baked into the release build.
   - ⚠️ The URL **and** the pubkey must change **together** — switching only the URL makes
     production-signed tokens fail offline verification (premium silently lost after grace).
   - Evidence: `src/wayfinder/license.py` `LICENSE_API_URL` (~L80) + `LICENSE_PUBLIC_KEY_HEX` (~L74),
     with the `# ...DEV deployment for now — switch ... at go-live` comment.

2. **Green-light to remove DEV-UNLOCK.** The Settings → "🛠 Developer → Unlock all premium
   features" toggle is a deliberate **testing backdoor** (free bypass of all premium, also via
   `WAYFINDER_DEV_UNLOCK=1` / `dev_unlock_all` in config). Say the word once you've finished
   testing premium and I'll strip it — everything is tagged `DEV-UNLOCK`, one commit removes it
   (license.py + wayfinder_main.py + tests/test_dev_unlock.py).

3. **Screenshots — DONE (drafts).** ✅ Real captures of the running app are committed at
   `screenshots/main-window.png` (Dictate/home) + `screenshots/settings.png` (Settings/Audio),
   so the metainfo URLs now resolve. They show your live state (turbo-q5, 95% zoom, a leftover
   "Thank you." transcription) — **replace with polished marketing shots before submission** if
   you want, but this no longer blocks a build/listing.

4. **Confirm the storefront.** Is `https://wayfinder.dev/premium` live, and is the price
   ("$20 launch / reg. $40") correct? It's hardcoded in the upgrade prompts
   (`config.py` `premium_url`, `license.py` `get_upgrade_message`, the Settings/banner UI).

5. **Pick the ship channel(s).** Flathub (free tier) + a self-hosted repo / direct `.flatpak`
   bundle (premium)? AppImage? PyInstaller? This decides how much packaging work remains —
   Flathub is by far the most demanding (see Remaining → Flathub).

6. **License-secret cleanup (quick decision).** The legacy offline-HMAC path
   (`WAYFINDER_LICENSE_SECRET` + `validate_license_key`/`generate_license_key`) is **dead code** —
   real activation uses the Convex/Ed25519 path. Recommend I just delete it (also removes the
   scary, now-false "secret MUST be set" startup warning). OK to remove?

---

## ✅ Done this session (all pushed to `origin/main`)

| Area | What | Commit |
|------|------|--------|
| GPU on the Deck | whisper.cpp **v1.9.1 + `GGML_VK_DISABLE_COOPMAT=1`** → working Vulkan on RDNA2. base.en 0.61s GPU vs 1.76s CPU; turbo-q5 **3.7s** (now usable, was ~31s) | `0a17dc5` |
| GPU = premium | Gated at the backend factory (`get_backend`) so a config.json edit can't bypass it; live CPU↔GPU toggle (no app restart); upgrade prompt for free users | `6b36a8d` |
| Remote inference | Groq + OpenAI keys pulled from Bazzite, validated live + via the app's bundled SDKs, written to config (backend left on local-GPU; remote selectable) | config only |
| Whisper download | Fixed read-only-filesystem failure (writable grant + path resolution) | `958ed75` |
| Benchmark | "Test Current Model" no longer crashes on the Deck (uses the CPU binary; reports GPU unavailable cleanly) | `1688e9e` |
| Dev testing | DEV-UNLOCK premium toggle (temporary — see #2) | `7afa1aa` |
| Pre-ship hardening | version 1.0.0→1.1.0 coherent; config/license files chmod 600; false "encrypted" comment fixed; **4 Deck-only test failures fixed → 837/837 green**; portable `.desktop` install; AppImage source pins; license classifier fixed | `eb0ae01`, `1dd2f9c` |

---

## 🔧 Remaining work (I can do — triggered by the items above)

- **Switch license backend to production** — needs #1.
- **Remove DEV-UNLOCK** — needs #2 (do right before GA).
- **Flathub submission packaging** (a dedicated, separately-tested pass; only if Flathub is a
  channel, #5):
  - Offline, SHA256-hashed `python-deps` via `flatpak-pip-generator` + the **PyQt6 BaseApp**
    (`com.riverbankcomputing.PyQt.BaseApp`) — the current manifest does `pip install` over the
    network, which Flathub forbids.
  - Commit-pin the 8 `git` sources (add `commit:` next to each `tag:`).
  - Switch the app module from `type: dir` to `git` + `tag` + `commit` (do at tag time).
  - Screenshots: done (drafts committed, #3) — swap for polished shots if desired.
- **Tag `v1.1.0`** at actual ship time (metainfo release + the Flathub app-source pin need it).
- **On-device QA:** a real GPU dictation run end-to-end, and the **Game-Mode / DAoC live test**
  (designed but never validated on hardware).
- **CI gap (optional):** CI runs only on `ubuntu-latest` and never the Steam Deck — the 4 tests
  we just fixed were Deck-only. Consider a Deck / self-hosted runner so Deck-specific
  regressions get caught automatically.

---

## Audit reference (2026-06-28)

Four parallel audits — licensing/security, code-sweep, packaging/Flathub, tests. Net verdict:
**the core app is solid** (837 tests green, broad coverage, CI + tag-triggered release pipeline
already exist, AppStream metadata essentially Flathub-shaped, no hidden backdoors beyond the
intentional DEV-UNLOCK). The remaining blockers are the **DEV license backend** (#1), the
**DEV-UNLOCK backdoor** (#2), and **Flathub packaging plumbing** (#5) — everything else is
should-fix/minor and largely handled above.
