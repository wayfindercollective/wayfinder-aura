# Storefront + Marketing Handoff — Wayfinder Aura

**Audience:** Website / Wayfinder OS web agent (not this desktop repo)  
**Date:** 2026-07-12  
**Goal:** Fix accuracy bugs, then turn `/aura` + checkout into a page that **closes Ultra sales** for the best local Linux dictation product on the market.

**Live URLs wired in the desktop app today**

| Role in app | Config key | URL |
|-------------|------------|-----|
| **More Info** | `premium_info_url` | https://wayfindercollective.io/aura |
| **Buy Now** | `premium_url` | https://wayfindercollective.io/checkout/aura-ultra |
| Launch price | `premium_price` | `$29.99` |
| Regular price | `premium_price_regular` | `$60` |

Sources of product truth in the **desktop** repo (do not invent features):

- `README.md` — Free vs Ultra table, positioning  
- `WEBSITE_COPY_BRIEF.md` — approved copy angles  
- `PRIVACY.md` — honest privacy claims  
- `SUPPORT.md` — support path  
- `screenshots/*.png` — store-ready captures (public on GitHub `main`)  
- This file — live audit + conversion plan  

---

## Executive verdict (read this first)

| Page | Verdict |
|------|---------|
| **Checkout** | **Technically usable** for card pay. Price math correct. Stripe live. One-time + 7-day refund stated. **Does not sell Ultra** and legal links are dead. |
| **Landing `/aura`** | **Not launch-ready. Actively harmful.** Title/meta belong to Wayfinder OS (CRM). Download buttons ship the **wrong product** (`wayfinderOSdesktop` v1.0.3 `.deb`/`.dmg`/`.exe`). Claims macOS/Windows builds for Aura. Zero product story, no Free vs Ultra, no Get Ultra CTA, no screenshots. |

**Until `/aura` is fixed, “More Info” from the app is a conversion and trust landmine.** Checkout can take money, but people who click More Info first will be confused or install the wrong binary.

---

## Live audit (Playwright-rendered, 2026-07-12)

### A. More Info — `https://wayfindercollective.io/aura`

**What the user actually sees**

- Brand bar: “Wayfinder”
- H1: **Wayfinder Aura**
- Tagline only: *“Your voice, turned to text — instantly, in any app you already use.”*
- Download cards:
  - Linux “recommended” → **`.deb` 78.6 MB** from  
    `github.com/wayfindercollective/wayfinderOSdesktop/releases/.../wayfinder_1.0.3_amd64.deb`
  - macOS → `Wayfinder.dmg` (same **wayfinderOSdesktop** repo)
  - Windows → `Wayfinder-1.0.3.Setup.exe` (same repo)
  - Version line: `v1.0.3 · Apr 3, 2026` (and UI shows a double “v”: `vv1.0.3`)
- Note: “Already purchased? … paste under Settings → License.”
- Footer: Terms / Privacy / Support → all `href="#"` (dead)
- **No** price, Ultra, Free tier, GPU, Steam Deck, privacy deep-dive, screenshots, or install path for the real Aura Flatpak.

**Meta / SEO (broken brand)**

| Field | Live value | Problem |
|-------|------------|---------|
| `<title>` | Wayfinder OS | Wrong product |
| `meta description` | Multi-Tenant Revenue Management System | Completely wrong (OS/CRM product) |
| OG tags | effectively absent / inherited | Sharing looks like the CRM, not Aura |

**Accuracy failures (P0 — fix before any ads/launch posts)**

1. **Wrong download artifacts.** Aura is **Linux local dictation** (Flatpak primary, optional AppImage). Those GitHub assets are **Wayfinder OS desktop**, not Aura.  
2. **False multi-platform claim.** Marketing macOS/Windows downloads for Aura is inaccurate for the product being sold as Ultra today.  
3. **No install path that matches the product** (Flathub when live; until then GitHub releases / documented Flatpak for `io.wayfindercollective.WayfinderAura`).  
4. **No product truth** — visitor cannot answer: Is it local? Free? Why Ultra? Linux/Deck?  
5. **Dead legal/support links.**

**What is OK (keep / refine)**

- Product name “Wayfinder Aura” in the H1  
- Short voice→text tagline direction (needs a stronger Linux/local punch)  
- License-activation reminder (after install path is correct)

---

### B. Buy Now — checkout URL above

**What the user actually sees (after client render)**

- Left: Pay with card — Email, Name on card, Stripe Card element, **Pay $30.89**
- Right summary card:
  - **Wayfinder Aura**
  - One-time license  
  - Subtotal **$29.99**  
  - Processing fee (3%) **$0.90**  
  - Total **$30.89**  
  - Full refund within 7 days  
- Copy: non-subscription digital license; email used for license delivery + receipt; Stripe encrypted  
- Live Stripe + hCaptcha frames (live mode)

**Accurate / good**

| Item | Status |
|------|--------|
| Product name Wayfinder Aura | ✅ |
| One-time (not subscription) | ✅ |
| Launch subtotal $29.99 | ✅ matches app `premium_price` |
| Fee disclosed | ✅ $0.90 (3%) |
| Total $30.89 | ✅ math correct |
| Refund promise | ✅ 7 days |
| Stripe live checkout | ✅ |

**Gaps / accuracy polish**

| Item | Issue | Recommendation |
|------|--------|----------------|
| Page title / meta | Still “Wayfinder OS” / CRM description | Aura-specific title + description |
| Word **Ultra** | Missing | “Wayfinder Aura **Ultra** — one-time license” |
| Regular $60 | Missing | Optional struck-through `$60` + “launch price” if you still claim it in-app |
| What you get | Zero benefits | 4–6 bullet outcomes above fold (see copy below) |
| After pay | Unclear | “Key emailed → open Aura → Settings → License → paste” |
| Legal links | `#` placeholders | Real License / Privacy / Support URLs (Aura or site-wide) |
| Back to product | No link | “← What is Aura?” → `/aura` |
| Trust | Minimal | 7-day refund already good; add local-by-default one-liner |

**Note for release automation:** bare HTTP HTML is a SPA shell (`Loading checkout...`). Any bot that only scrapes SSR must use a real browser (your desktop repo’s `check-storefront-readiness.py` already does Playwright). Prefer SSR or static price markers in initial HTML for resilience.

---

## Product truth the website must not get wrong

### What Aura *is*
- **Local-first voice dictation for Linux**  
- Hotkey → speak → cleaned text **typed/pasted wherever the cursor is** (any app)  
- Default: **whisper.cpp + local LLM cleanup on-device** — voice does not leave the machine unless the user opts into cloud backends with **their own** keys  
- Built for **Wayland + X11**, **KDE/GNOME**, desktop **and Steam Deck** (including Game Mode workflows)  
- Glassmorphic status overlay, system tray, game-aware hotkeys (pause while GameMode game is active)  
- Free tier is a **full dictation product**, not a trial watermark demo  

### What Ultra *is* (paid, one-time)
Public tier name: **Ultra only** (not “Premium” in user-facing copy).

| Free includes | Ultra unlocks |
|---------------|---------------|
| Local whisper.cpp dictation | GPU acceleration for Small+ / long dictations (Free already has GPU on Tiny/Base) |
| Standard models (tiny / base / small) | Large models (medium, large-v3-turbo) |
| Local LLM cleanup (light models) | Large cleanup models (CDN-gated) |
| Overlay + tray | Faster-Whisper backend |
| Instant paste / typing | Cloud transcription + cleanup with **user’s** API keys |
| | Chunked / unlimited-length recording |
| | Tone presets & voice profiles |
| | Custom vocabulary |

**Pricing story (must stay consistent with the app)**

- Regular: **$60** one-time  
- Launch: **$29.99** one-time  
- Checkout currently charges **$29.99 + $0.90 fee = $30.89**  
- No subscription  
- Activate online once → works offline  
- In-app: **Get Ultra** / Buy Now → checkout; **More Info** → landing  

### Privacy claims (honest — do not oversell)

Safe:

- Local by default; no audio upload unless user enables cloud + provides keys  
- Model-update check is optional / toggleable  
- License activation contacts server with key + machine id  

Unsafe / avoid:

- “100% offline forever / never contacts network” (activation + optional model updates)  
- “Flathub available” until it actually is  
- macOS/Windows as first-class Aura platforms until builds are real  

### Install truth (2026-07-12)

- Desktop app ID: `io.wayfindercollective.WayfinderAura`  
- Primary planned store: **Flathub** (not live as of this audit)  
- Source: https://github.com/wayfindercollective/wayfinder-aura (public)  
- **Do not** link `wayfinderOSdesktop` releases as Aura downloads  
- Screenshots (public raw URLs work):  
  `https://raw.githubusercontent.com/wayfindercollective/wayfinder-aura/main/screenshots/{main-window,settings,overlay,style,welcome}.png`  

### Hotkeys to advertise
- **Super+F2** — start/stop dictation  
- **Super+F3** — cycle styles (Minimal → Professional → Casual → Dev → Personal)  

---

## Positioning: why this is the best Linux dictation app

Use this as the marketing spine (not a feature dump).

### One-liner options
1. **Press a key. Speak. Your words appear wherever your cursor is — private, local, Linux-native.**  
2. **The dictation app Linux deserved: local AI, any app, Steam Deck included.**  
3. **Cloud dictation taxes your privacy and your wallet. Aura runs on your machine.**  

### Why advanced Linux users care
- **Wayland-class citizen**, not an X11 leftover  
- **Real packaging story** (Flatpak sandbox + host Deck helpers where needed)  
- **Vulkan GPU path** when they want speed; CPU fallback when they don’t  
- **GameMode-aware** — push-to-talk doesn’t fight the game  
- **Steam Deck** as a first-class workflow, not a footnote  
- **Source-available (Elastic-2.0)** + freemium Ultra — transparent, not spyware SaaS  
- Own the stack: whisper.cpp + llama.cpp, optional *their* cloud keys  

### Why first-time dictation users care
- One mental model: **hotkey → talk → text appears**  
- Free tier works for real daily dictation  
- Cleanup removes ums and fixes punctuation without a writing degree  
- No monthly fee guilt to try it  
- Ultra is a **one-time** power-up when they want speed/accuracy/long form  

### Competitive contrast (fair, not snarky)
| Pain with alternatives | Aura answer |
|------------------------|-------------|
| Subscription cloud STT | Local default; one-time Ultra |
| Voice leaves the machine | Local pipeline by default |
| Linux/Wayland is “best effort” | Built for Linux first |
| Deck / gaming workflows ignored | Deck + Game Mode aware |
| Toy UI / abandoned projects | Polished overlay, tray, styles |

### Use cases to feature (short scenes)
1. **Dev in the terminal / IDE** — Super+F2, describe the bug, Dev style, paste as a commit-ready sentence.  
2. **Chat / Discord / email** — talk naturally; Casual or Professional cleans it.  
3. **Long-form notes** (Ultra) — chunked recording, turbo model, GPU.  
4. **Steam Deck couch** — back-button trigger, Game Mode path, text into chat/search.  
5. **Privacy-sensitive work** — legal/medical/personal drafts that should never hit a STT SaaS.  

---

## Recommended page plans (for the web agent)

### Phase 0 — P0 correctness (do first, same day if possible)

**Landing `/aura`**
1. Remove all `wayfinderOSdesktop` download links.  
2. Remove macOS/Windows download cards until real Aura builds exist (or label clearly “coming later” with **no** download button).  
3. Linux install: honest options  
   - Preferred: “Get it on Flathub” when live  
   - Interim: GitHub `wayfinder-aura` releases / install docs / “open in Discover later”  
4. Fix `<title>`, meta description, OG title/description/image to **Aura** (not CRM / Wayfinder OS).  
5. Wire Terms / Privacy / Support to real pages (can reuse site legal if accurate; else Aura-specific).  
6. Add primary CTA: **Get Ultra — $29.99 launch** → current checkout URL.  
7. Add secondary CTA: **Download free** (correct artifact only).  

**Checkout**
1. Aura-specific title/meta.  
2. Product line: **Wayfinder Aura Ultra**.  
3. Live links for License Agreement + Privacy (not `#`).  
4. Post-pay expectations line under Pay button.  
5. Optional: struck-through $60 + “launch price” if still true.  

### Phase 1 — Conversion landing (close the deal)

Suggested structure for `/aura` (single scroll, dark UI OK — match app violet `#A78BFA` energy):

1. **Hero (5-second proof)**  
   - Headline + sub  
   - 15–30s loop or static **recording screenshot** + overlay  
   - CTAs: **Download Free** | **Get Ultra $29.99**  
   - Trust strip: Local by default · One-time Ultra · Linux + Steam Deck  

2. **How it works** (3 steps)  
   Super+F2 → Speak → Text at cursor  

3. **Why local / why Linux**  
   Privacy + Wayland + no subscription tax  

4. **Free vs Ultra** table (copy from README; name tier **Ultra**)  

5. **Use cases** (dev, chat, Deck, private work)  

6. **Gallery** — use the five screenshots already on GitHub `main/screenshots/`  

7. **FAQ**  
   - Does free work offline?  
   - What leaves my machine?  
   - Steam Deck?  
   - Subscription?  
   - Refund?  

8. **Final CTA** — Get Ultra + Download Free  

### Phase 2 — Checkout as closer (not just a form)

Keep the clean form. Add a compact left or sidebar block:

**Ultra unlocks**
- Faster GPU dictation on long sessions  
- Large / turbo models  
- Tone presets & custom vocabulary  
- Unlimited-length recording  
- Optional cloud with *your* keys  

**After you pay**
1. Check email for license key  
2. Open Wayfinder Aura  
3. Settings → License → paste key  
4. Ultra works offline after activation  

Show: `$29.99` launch · was `$60` (if claimed) · + processing fee as today · 7-day refund  

### Phase 3 — Growth surfaces (after P0+P1)
- Homepage tile on wayfindercollective.io: “New: Wayfinder Aura” (today homepage is coaching/OS only)  
- OG image for social (app icon + “local dictation for Linux”)  
- `/aura` changelog or “What’s new” for launch posts  
- Optional: license re-send page  
- Analytics: CTA click, checkout start, purchase (privacy-respecting)  

---

## Suggested copy blocks (ready to paste)

### Hero
**Headline:** Press a key. Speak. Your words appear wherever your cursor is.  
**Sub:** Local-first AI dictation for Linux — private by default, polished for desktop and Steam Deck. Free to start. Ultra is a one-time upgrade, not a subscription.  

### Ultra bullets (checkout + landing)
- GPU-accelerated transcription for long sessions  
- Large & turbo models for higher accuracy  
- Tone presets (Professional, Casual, Dev, Personal)  
- Custom vocabulary for names and jargon  
- Unlimited-length / chunked recording  
- Optional cloud backends with **your** API keys  

### Free bullets
- Full local dictation with whisper.cpp  
- Local LLM cleanup (ums, punctuation, polish)  
- Status overlay + tray  
- Works in any app that accepts text  

### Privacy one-liner
By default your voice is processed on your machine. Optional cloud features are off until you turn them on and supply your own keys. License activation and optional model-update checks are the only network contacts for a normal local install.

### CTA labels
- **Get Ultra — $29.99 launch**  
- **Download free**  
- **See Free vs Ultra**  

### Social / launch post skeleton
> Wayfinder Aura is local-first voice dictation for Linux.  
> Super+F2 → talk → text at your cursor. Private by default.  
> Free is real dictation. Ultra is a one-time $29.99 launch unlock for GPU, large models, tones, and long-form.  
> Built for Wayland and Steam Deck — not a port of a Mac app.  
> [ /aura ] [ checkout ]

---

## Acceptance criteria (web agent done-when)

### P0
- [ ] No link on `/aura` points at `wayfinderOSdesktop` release assets  
- [ ] No macOS/Windows **download** for Aura unless a real Aura build exists  
- [ ] `/aura` title + meta describe **voice dictation**, not multi-tenant CRM  
- [ ] Terms / Privacy / Support are real URLs on landing **and** checkout  
- [ ] Primary **Get Ultra** CTA hits the live checkout URL used by the app  
- [ ] Linux download path is Aura-correct (or “coming to Flathub” with GitHub source)  

### P1
- [ ] Free vs Ultra table matches desktop README  
- [ ] At least 2 product screenshots (recording + overlay)  
- [ ] Price story: $29.99 launch, optional $60 regular, fee honest if charged  
- [ ] Checkout says **Ultra** + post-pay activation steps  
- [ ] Mobile layout readable (both pages already roughly ok)  

### P2
- [ ] OG image for link previews  
- [ ] Homepage discovery path to Aura  
- [ ] FAQ covers Deck, privacy, refund, offline after activate  

---

## What NOT to change without desktop coordination

- Checkout path ID currently embedded in the app (`premium_url`) — if the URL changes, update `src/wayfinder/config.py` in **wayfinder-aura** and ship a release  
- Price strings in the app (`premium_price`, `premium_price_regular`) must stay in sync with checkout  
- Do not rename public tier to “Premium” — product language is **Ultra**  
- Do not promise Flathub until the Flathub PR is live  

---

## Quick reference — assets already available

```
https://raw.githubusercontent.com/wayfindercollective/wayfinder-aura/main/screenshots/main-window.png
https://raw.githubusercontent.com/wayfindercollective/wayfinder-aura/main/screenshots/settings.png
https://raw.githubusercontent.com/wayfindercollective/wayfinder-aura/main/screenshots/overlay.png
https://raw.githubusercontent.com/wayfindercollective/wayfinder-aura/main/screenshots/style.png
https://raw.githubusercontent.com/wayfindercollective/wayfinder-aura/main/screenshots/welcome.png
https://raw.githubusercontent.com/wayfindercollective/wayfinder-aura/main/assets/icon.png
```

Repo: https://github.com/wayfindercollective/wayfinder-aura  

---

## Bottom line for sales

You already have a **working paid checkout** at the launch price.  
You do **not** yet have a **truthful marketing page**.  

Fix the wrong downloads and CRM meta **today**. Then turn `/aura` into a proof-first Linux dictation story with Free vs Ultra and a loud Get Ultra path. That is how you start closing deals without embarrassing the product.
