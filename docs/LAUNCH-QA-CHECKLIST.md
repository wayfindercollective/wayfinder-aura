# Launch QA checklist (Phase 4)

**When:** After Phase 1–3 code is on the running build.  
**Not a substitute for Flathub review** — see `SHIPPING.md` / `docs/STOREFRONT-MARKETING-HANDOFF.md`.

## Preflight

- [ ] Running commit includes Phase 0 soft-update + Phase 1 acks (check `git rev-parse --short HEAD`).
- [ ] Aura **restarted** after pull/rebuild so the live process matches HEAD.
- [ ] `pytest tests/test_recording_watchdog.py tests/test_overlay.py -q` green.

## Desktop Wayland inject matrix

Do short dictations (2–5 words) with the cursor in the field before and after stop.

| Target | Focus stays? | Text lands? | activity.log notes |
|--------|--------------|-------------|--------------------|
| Browser text field (e.g. Gmail compose) | | | |
| Terminal (Konsole / ptyxis) | | | |
| Chat (Discord / Slack / Element) | | | |
| System Settings search | | | |

**Pass criteria**

- No `Overlay restart → processing` on every stop (only deferred after idle or real death).
- Focus stays in the target field for **20/20** short dictations on KDE Wayland.
- No stuck “Processing…” pill after normal success (ack path recovered or soft update ok).

## Steam Deck

| Mode | Dictation works | Inject path | Notes |
|------|-----------------|-------------|-------|
| Desktop Mode | | | |
| Game Mode | | type→paste fallback if needed | |

- [ ] Back-button / trigger daemon still fires (`wayfinder-trigger.service`).
- [ ] Rumble / GM cues (if enabled) still fire on start/done.

## Storefront / Flathub (separate track)

- [ ] Preview `/aura` product page (not OS desktop downloads): see Wayfinder-OS `docs/aura-storefront-preview-notes.md`.
- [ ] Buy Now → checkout product id matches in-app `premium_url`.
- [ ] Flathub packaging progress: `SHIPPING.md` (do **not** treat this checklist as Flathub submission).

## Purchase → license activate (dry-run)

- [ ] Test purchase on **preview** only (no production deploy without explicit go-ahead).
- [ ] License email / delivery page links to Aura install, not Wayfinder OS desktop feed.
- [ ] Activate Ultra offline dry-run if keys available; free tier still works without.

## Tag / ship decision

- [ ] Phase 1 residual freeze acceptable after dogfood → tag from current main **or** keep dogfood and tag after one more soak.
- [ ] Push `origin/main` when ready (local may be ahead).

## Grep helpers

```sh
# Focus / inject path
rg -n "Inject |focus drifted|deferred restart|ack timeout" ~/.cache/wayfinder-aura/activity.log | tail -40

# Overlay IPC
tail -50 ~/.cache/wayfinder-aura/overlay-debug.log
```
