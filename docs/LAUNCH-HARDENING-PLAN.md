# Launch hardening plan — focus, inject, overlay recovery

**Status:** Phases **1–3 implemented in tree**; Phase **4** is the QA checklist (manual).  
**Branch:** `main` (wayfinder-aura)

---

## Handoff

1. **Restart Aura** after pulling these commits before dogfood.
2. Run **Phase 4** from `docs/LAUNCH-QA-CHECKLIST.md`.
3. Do **not** production-deploy Wayfinder-OS storefront without separate go-ahead.

---

## Already done

### Phase 0 — Soft leave-listen (`b517549`)

1. Soft leave-listen (no always-restart on leave listen).
2. Deferred restart (`_restart_pending` / `flush_deferred_restart`).
3. `allow_restart=False` on processing path.
4. Health + display-wake skip hard-refresh mid RECORDING/PROCESSING/PASTING.
5. Inject backend + focus-drift logging; final generation gate.
6. Overlay min-display (no `QTimer(0)` storms).
7. Tray slightly smaller / taskbar icon larger.
8. Soft/defer tests.

### Phase 1 — Overlay ack recovery

| Step | Work | Status |
|------|------|--------|
| 1.1 | Qt-thread JSON ack on stdout for `show`/`ping` (`nonce` + `state` + `ok`) | **Done** |
| 1.2 | Critical `_send_command` waits for matching nonce (~250ms); continuous stdout drain | **Done** |
| 1.3 | Missed ack → `_restart_pending` when `allow_restart=False`; never mid-PASTING | **Done** |
| 1.4 | `is_healthy()` = process liveness; freeze via ack path; SUPPORT note | **Done** |

### Phase 2 — KWin / freeze roots

| Step | Work | Status |
|------|------|--------|
| 2.1–2.2 | “Geometry once per frame” / script-unload rewrite | **Wrong direction** — intermediate attempts either leaked KWin scripts or left Qt `setGeometry` fighting KWin |
| 2.1′ | **Final rule (dogfood OK):** Wayland on-screen place = **KWin only** (no `setGeometry`/`move`); snap width on state change; X11 keeps Qt geometry and skips KWin spam | **Done** (`662d24b`) — see `DEVELOPMENT.md` (overlay Wayland positioning) |
| 2.3 | Drain overlay **stderr** after start (reader thread) | **Done** (in OverlayController) |
| 2.4 | Optional rate-limit under load | Deferred (width snap reduces KWin call rate) |

### Phase 3 — Inject honesty & optional focus assist

| Step | Work | Status |
|------|------|--------|
| 3.1 | activity.log line on deferred restart flush | **Done** |
| 3.2 | Opt-in `desktop_paste_on_focus_drift` (default **False**) | **Done** |
| 3.3 | SUPPORT: Wayland cannot retarget; keep focus | **Done** |

### Phase 4 — Ship / release hygiene

| Step | Work | Status |
|------|------|--------|
| 4.1 | Physical QA matrix | **Checklist** → `docs/LAUNCH-QA-CHECKLIST.md` |
| 4.2 | Tag when dogfood OK | Manual |
| 4.3 | Flathub / production storefront | Separate (`SHIPPING.md`, OS preview notes) |
| 4.4 | Purchase → activate dry-run | Checklist item (preview only) |

---

## Success metrics

- No per-dictation `Overlay restart → processing` in activity.log (except deferred after idle or real death).
- Focus stays in target field for 20/20 short dictations on KDE Wayland.
- Intentional freeze / missed ack recovers via deferred restart after IDLE.
- `pytest tests/test_recording_watchdog.py tests/test_overlay.py` green.

## Explicit non-goals

- Reverting soft-update to always-restart.
- Full DRM / license hardening.
- Flathub submission (tracked in `SHIPPING.md`).
- Enabling `desktop_paste_on_focus_drift` by default.
