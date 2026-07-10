# Implementation Plan: Security Hardening Tier A (+ B2/B3 hygiene)

**Status:** Codex **APPROVED** (gpt-5.5, 2026-07-09, review id `2304586e`, session `019f4805-9de8-72d3-a033-572257b23948`)  
**Source:** `docs/SECURITY-REVIEW.md` (F1–F8; Flatpak cross-check)  
**Out of scope this PR:** model SHA digests (B1), socket auth token (B4), OS keyring (C1), Flatpak network split (C3), DRM.

Non-goals (do not re-litigate): plaintext API keys with 0600, client-side license, Flatpak `--share=network`, same-user host IPC for KDE/Deck triggers.

---

## Codex R1 → R2

R1 **REVISE** (all accepted): live socket duplicate in `wayfinder_main.py`; all `pkill -f overlay.py` sites; log rollover modes; F6 app-owned temp dir; tone matrix test run; atomic 0600 writes; parent chmod guard; both overlay modules for F8.

R2 **APPROVED** with implementation guardrails (must follow):

1. `OwnerOnlyRotatingFileHandler`: create files owner-only at open (`os.open(..., 0o600)` / opener), not create-then-chmod only.
2. Import package `socket_listener` inside `_ensure_socket_listener` (avoid local function shadowing).
3. App temp-dir cleanup only **after** single-instance check, only inside app-owned `0700` dir.
4. Overlay pidfile cleanup: same uid **and** exact resolved overlay script path before signal.
5. Structural `pkill` regression test should catch split/import-aliased uses if practical.

---

## Goals

1. Transcript/secret-adjacent files owner-only via atomic create (0600 before replace); repair on load.
2. Control socket `0600` after bind on the **live app path** (package listener only). Flatpak xdg-run + plain commands unchanged.
3. License gate fails **closed** for cloud when gate cannot run.
4. Recordings in app-owned `0700` temp dir; startup cleans only that dir (after single-instance).
5. Zero `pkill -f overlay.py`; PID / verified pidfile only.
6. No `shell=True` desktop probes in `overlay.py` or `status_overlay.py`.
7. Tests on real entry points; `tests/test_tone_eval.py` in required run.

---

## Architecture

### Shared helper — `src/wayfinder/utils/fs_security.py`

- `restrict_owner_only(path)`
- `atomic_write_text` / `atomic_write_json` (tmp + chmod 0600 + replace)

### Live socket (critical)

- Harden `src/wayfinder/hotkeys/socket.py` (chmod 0600 after bind; parent `0o700` only if basename is `wayfinder-aura`).
- `wayfinder_main._ensure_socket_listener` imports package listener; remove/thin-wrap local duplicate (~2235).

### App-owned recording temp

- e.g. `$XDG_CACHE_HOME/wayfinder-aura/tmp` mode `0700`.
- Recorder WAVs + audited `wayfinder_main` benchmark temps under that dir.
- Startup cleanup of that dir only, after single-instance lock held.

### Overlay kill

- Track Popen PID; optional pidfile with uid + exact script path verification.
- Grep-clean all `pkill`+overlay sites in `wayfinder_main.py`.

---

## Work items

| ID | Finding | Action |
|----|---------|--------|
| A1 | F1 | Voice profile atomic 0600 + repair on load |
| A2 | F4 | Config `config.json*` repair; OwnerOnlyRotatingFileHandler; overlay-debug 0600 |
| A3 | F2 partial | Socket 0600 + migrate live app to package listener |
| A4 | F6 | App temp dir + cleanup (honest F6 status) |
| A5 | F5 | Gate fail-closed to local/minimal for cloud |
| B2 | F7 | Remove all overlay pattern pkill |
| B3 | F8 | Both overlay modules: env desktop, no shell=True |

---

## Implementation order

1. `fs_security`  
2. A1 → A2 → A3 (socket migrate) → A5 → A4 → B2 → B3  
3. Tests + tone_eval  
4. Update `SECURITY-REVIEW.md` / `PRIVACY.md`  

---

## Required tests

```bash
python3 -m pytest \
  tests/test_voice_profile.py \
  tests/test_config.py \
  tests/test_hotkeys.py \
  tests/test_postprocessor.py \
  tests/test_recorder.py \
  tests/test_license.py \
  tests/test_tone_eval.py \
  tests/test_utils.py \
  -v --tb=short
```

Plus `tests/test_fs_security.py` if added. Manual: modes 600, socket ping, no finish-args change, `rg pkill.*overlay` empty.

---

## Non-goals this PR

B1 model hashes, B4 socket token, C1 keyring, Flatpak network removal, sweeping arbitrary `/tmp/tmp*.wav`.
