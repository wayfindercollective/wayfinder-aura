# Launch hardening plan — focus, inject, overlay recovery

**Status:** Phase 0 shipped at `b517549` (soft overlay update + deferred restart). Next: execute phases below after compact.  
**Owner context:** Codex Sol (xhigh) **REVISE** on the soft-update fix; user confirmed dictation works again; launching soon.  
**Branch:** `main` (wayfinder-aura) — local commit `b517549`; push to origin optional until dogfood OK.

---

## Handoff for next session (after compact)

**Do this first, then code:**

1. **Restart Aura** so the running process is on `b517549` before more dogfood (soft-update is useless if the old always-restart binary is still live).
2. **Execute in this order only:**
   1. **Phase 1** — Overlay ack recovery (Sol High) — full section below
   2. **Phase 2.3** — Drain overlay stderr after start (small, high value)
   3. **Phase 2.1–2.2** — KWin geometry once + script reuse/unload
3. Then Phase 3 product decisions, then Phase 4 QA.

**Do not:** re-do Phase 0; hard-restart mid-PASTING; production-deploy Wayfinder-OS storefront (preview only until separate go-ahead).

---

## Already committed (do not re-do) — `b517549`

### Focus / inject (this commit)

1. **Soft leave-listen** — `OverlayController.update()` no longer always kills the overlay when leaving `listening` (was stealing KDE Wayland focus every dictation).
2. **Deferred restart** — failed writes during dictation set `_restart_pending`; `flush_deferred_restart()` runs after inject → IDLE.
3. **`allow_restart=False`** on processing indicator path.
4. **Health + display-wake** do not hard-refresh mid RECORDING/PROCESSING/PASTING.
5. **Inject logging** — backend name (`via ydotool`), honest focus-drift (no fake retarget on Wayland).
6. **Final generation gate** immediately before `inject_text()`.
7. **Overlay min-display** — no `QTimer(0)` storms (`remaining <= 0` apply now; else ≥1ms).
8. **Tray slightly smaller / taskbar icon larger** — tray render 96 + glyph inset; window icon 128.
9. **Tests** — soft path + defer restart coverage in `tests/test_recording_watchdog.py`.

### Related product work already on main / elsewhere

- Game Mode three fixes (rumble, type→paste, light ASR) — `04ed9b7`.
- Storefront marketing handoff doc (optional companion): `docs/STOREFRONT-MARKETING-HANDOFF.md`.
- Wayfinder-OS Signatures preview storefront (separate repo; not this commit).

---

## Phase 1 — Overlay ack recovery (Sol High) — **do next**

**Goal:** Soft-update must not leave a wedged-but-alive Qt loop forever.

| Step | Work | Done when |
|------|------|-----------|
| 1.1 | Overlay emits JSON ack on stdout when a `show`/`ping` command is **handled on the Qt thread** (not just stdin read). Include `nonce` + `state` + `ok`. | Protocol documented in code comment |
| 1.2 | `_send_command(critical=True)` optionally waits for matching nonce ack (timeout ~150–300ms), continuous stdout drain (reader thread or non-blocking read). | No buffer-block; timeouts logged |
| 1.3 | Missed ack → set `_restart_pending` (never hard-restart during PASTING); `flush_deferred_restart()` still owns recycle. | Unit/mock tests |
| 1.4 | `is_healthy()` remains process-liveness; document that freeze detection is ack-based only on critical path. | Tests + short note in DEVELOPMENT or SUPPORT |

**Out of scope for 1:** rewriting the whole overlay IPC.

**Risk if skipped:** rare stuck “Processing…” pill until next idle defer/health cycle.

---

## Phase 2 — KWin / geometry freeze root causes (Sol Medium)

**Goal:** Reduce original freeze that motivated always-restart.

| Step | Work |
|------|------|
| 2.1 | Call KWin positioning **once per completed state transition**, not every animation frame (`_position_at_bottom` / Wayland path). |
| 2.2 | Load KWin script once; unload or reuse — stop load-per-call leak (`_force_kde_window_position`). |
| 2.3 | Drain overlay **stderr** after startup so a chatty child cannot block on full pipe. |
| 2.4 | Optional: rate-limit geometry scripts under load. |

**Risk if skipped:** rare Qt wedge; deferred restart still recovers after inject.

---

## Phase 3 — Inject honesty & optional focus assist (Sol Medium)

| Step | Work |
|------|------|
| 3.1 | Keep backend + drift logging (done). Add `activity.log` line when deferred restart flushes. |
| 3.2 | Evaluate **desktop** clipboard+Ctrl+V fallback when focus drift is *detected and* backend is ydotool (today GM-only). Product decision: may type into wrong field less often if user refocused; may also paste into wrong place — gate carefully. |
| 3.3 | Document in SUPPORT: Wayland cannot retarget window; keep focus in the text field. |

---

## Phase 4 — Ship / release hygiene (product launch)

| Step | Work |
|------|------|
| 4.1 | Physical QA matrix: Desktop Wayland inject (browser, terminal, chat); Steam Deck Desktop + Game Mode; no `Overlay restart → processing` every stop. |
| 4.2 | Tag release commit after Phase 1 if freeze residual unacceptable; else ship Phase 0 + dogfood. |
| 4.3 | Flathub / production storefront (Wayfinder-OS) — separate plan (`docs/STOREFRONT-MARKETING-HANDOFF.md`, OS `docs/aura-storefront-preview-notes.md`). |
| 4.4 | Purchase → license activate → offline Ultra dry-run. |

---

## Execution order after compact

0. **Restart Aura on `b517549`** (or later HEAD after Phase 1 lands).  
1. **Phase 1** — Qt-thread nonce acks for `show`/`ping`; critical `_send_command` waits + stdout drain; missed ack → `_restart_pending` (never mid-PASTING); tests soft success / ack timeout → pending.  
2. **Phase 2.3** — stderr drain after overlay start.  
3. **Phase 2.1–2.2** — geometry once per transition; KWin script load once / unload-reuse.  
4. **Phase 3** — optional clipboard fallback (product decision); SUPPORT focus note.  
5. **Phase 4** — physical QA matrix; Flathub/storefront; purchase→activate dry-run; tag.

## Success metrics

- No per-dictation `Overlay restart → processing` in activity.log (except deferred after idle or real death).  
- Focus stays in target field for 20/20 short dictations on KDE Wayland.  
- Intentional freeze simulation (if we add a debug hang) recovers within one deferred restart after IDLE.  
- `pytest tests/test_recording_watchdog.py` green; add Phase 1 tests for ack timeout → pending.

## Explicit non-goals (this plan)

- Reverting soft-update to always-restart.  
- Full DRM / license hardening.  
- Flathub submission (tracked elsewhere).
