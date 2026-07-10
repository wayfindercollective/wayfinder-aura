# Wayfinder Aura — Security Review & Remediation Plan

**Date:** 2026-07-09  
**Scope:** Local desktop dictation app (source under `src/wayfinder/`, `wayfinder_main.py`, Flatpak finish-args).  
**Method:** Code review of trust boundaries + live on-disk permission samples on a developer install.  
**Goal of this doc:** Finish the security check and leave an ordered remediation plan. **No code patches are required by this review** (plan only).

Threat model: single-user desktop; primary concerns are **local secret/transcript leakage**, **unauthenticated same-user control plane**, and **model supply-chain integrity**. Not a full pen test or dependency CVE audit.

---

## Strengths (not open defects)

These already match `PRIVACY.md` and should not be re-opened as bugs:

| Strength | Evidence |
|----------|----------|
| Config/API keys written owner-only | `src/wayfinder/config.py` `save_config` → `os.chmod(tmp_file, 0o600)` before replace. Live: `config.json` mode `600`. |
| License token owner-only + repair on load | `src/wayfinder/license.py` `_restrict_owner_only` / load path. Live: `license.json` mode `600`. |
| Activity log owner-only | `wayfinder_main.py` `_append_activity_log` → `os.chmod(..., 0o600)`. Live: `activity.log` mode `600`. |
| Local-by-default processing | `DEFAULT_CONFIG["processing_mode"] = "local"`; cloud keys empty by default. |
| Temp recordings intended ephemeral | `recorder.py` `NamedTemporaryFile` + `cleanup()` / `unlink`. |
| Whisper server client uses loopback | `transcriber.py` health/inference via `127.0.0.1`; upstream whisper-server default host is localhost. |
| License offline crypto | Ed25519 verify with embedded public key; private key not shipped. |
| Flatpak least privilege (home) | No full `$HOME`; model dirs + `xdg-run/wayfinder-aura` only. |
| No telemetry | Confirmed in product privacy posture / network surface limited to license, model checks, opt-in cloud. |

---

## Findings

Each finding: **severity**, **area**, **status**, **checkable observation**.

### F1 — Voice profile stores transcripts as world-readable  
| | |
|--|--|
| **Severity** | **High** (local privacy / multi-user or world-readable home shares) |
| **Area** | `voice_profile.json` file permissions |
| **Status** | **Fixed** (2026-07-09) — atomic `0600` write + repair on load via `fs_security` |
| **Observation** | Was live `644`; `_save` had no chmod. Now `atomic_write_json(..., mode=0o600)` and load-time `restrict_owner_only`. |

### F2 — Unauthenticated Unix control socket (quit / record / show)  
| | |
|--|--|
| **Severity** | **High** (same-user control plane; lower if only multi-user isolation is considered) |
| **Area** | IPC control socket |
| **Status** | **Partial fixed** (2026-07-09) — socket/file modes `0600` + parent app dir `0700`; plain same-user commands remain (B4 token deferred) |
| **Observation** | Live app now uses package `socket_listener` (no duplicate). After bind: `chmod 0600`; app subdir `wayfinder-aura` only → `0700`. Auth token still out of scope. |

### F3 — Model downloads: size checks only, no content hash  
| | |
|--|--|
| **Severity** | **Medium** (supply chain) |
| **Area** | Model download integrity |
| **Status** | **Open** |
| **Observation** | `src/wayfinder/core/setup.py` `_download_model_file`: verifies Content-Length match and `min_bytes`; **no** `sha256` / checksum field in catalog or verify step. HTTPS mitigates path MITM; does not pin a known-good model revision. |

### F4 — Config backups and structured logs not owner-only  
| | |
|--|--|
| **Severity** | **Medium** (local privacy) |
| **Area** | Config backups / structured logs |
| **Status** | **Fixed** (2026-07-09) — load repairs `config.json*`; OwnerOnlyRotatingFileHandler; overlay-debug open `0600` |
| **Observation** | Was live backups/logs `644`. Now `load_config` chmods `config.json*`; logs use owner-only create/rollover. |

### F5 — Feature gate fails open on exception (cloud / tone)  
| | |
|--|--|
| **Severity** | **Medium** (license + accidental cloud send) |
| **Area** | License feature gate (post-processing factory) |
| **Status** | **Fixed** (2026-07-09) — fail-closed to local/minimal on gate exception |
| **Observation** | Except branch now forces `llama_cpp` for cloud backends and `minimal` tone. |

### F6 — Orphan temp audio residual after crash/kill  
| | |
|--|--|
| **Severity** | **Medium** (residual privacy; mode often OK) |
| **Area** | Temp WAV cleanup |
| **Status** | **Fixed for app-managed temps** (2026-07-09) — recordings under app `0700` temp dir; startup cleanup of that dir only after single-instance |
| **Observation** | Historical host `/tmp/tmp*.wav` outside the app dir are not swept (by design). New WAVs use `get_app_temp_dir()`. |

### F7 — Broad `pkill -f overlay.py`  
| | |
|--|--|
| **Severity** | **Low** |
| **Area** | Overlay process lifecycle |
| **Status** | **Fixed** (2026-07-09) — all pattern pkill removed; tracked PID + verified pidfile only |
| **Observation** | `wayfinder.utils.overlay_process` kills only after same-uid + exact overlay script path check. |

### F8 — `shell=True` on fixed KDE blur probe  
| | |
|--|--|
| **Severity** | **Low** |
| **Area** | Overlay subprocess |
| **Status** | **Fixed** (2026-07-09) — both overlay modules use `os.environ.get("XDG_CURRENT_DESKTOP")` |
| **Observation** | No `shell=True` desktop probe remains. |

### F9 — API keys / license token plaintext on disk  
| | |
|--|--|
| **Severity** | **Low** (documented tradeoff) |
| **Area** | Secret storage |
| **Status** | **Deferred** — intentional for v1 |
| **Observation** | Documented in `PRIVACY.md`. Mitigated by `0600` on live config/license. No keyring integration. |

### F10 — Client-side license enforcement  
| | |
|--|--|
| **Severity** | **Low** (product/DRM, not user-privacy) |
| **Area** | Licensing |
| **Status** | **Deferred** — intentional offline design |
| **Observation** | Offline Ed25519 tokens + online activate/refresh. Not DRM-hard; acceptable for desktop Ultra. |

### F11 — Flatpak always shares network  
| | |
|--|--|
| **Severity** | **Low** (sandbox tradeoff) |
| **Area** | Flatpak finish-args |
| **Status** | **Deferred** — intentional |
| **Observation** | `--share=network` for license, model update check, optional cloud. App defaults remain local. |

### F12 — Config-controlled executable paths  
| | |
|--|--|
| **Severity** | **Low** (same-user trust) |
| **Area** | Subprocess binary paths |
| **Status** | **Deferred** — expected for local tool paths |
| **Observation** | `whisper_binary` / llama binary from config are executed. Same-user write to config ≡ code exec; not a remote input path. |

---

## Remediation plan (ordered)

Priority = impact × ease for local privacy first; larger supply-chain/product work after.

### Tier A — Quick local privacy hardening (do first)

| # | Priority | Intended outcome | Addresses |
|---|----------|------------------|-----------|
| A1 | P0 | Voice profile file is owner-only on write and repaired on load (same pattern as license). | F1 |
| A2 | P0 | Config backups and any file that may hold keys/transcripts are created/repaired as owner-only; structured app logs under cache use owner-only modes. | F4 |
| A3 | P1 | Control socket is not world-connectable within the runtime dir group/other bits (`0600` after bind); document that host triggers must run as the same user. Optional later: shared secret file for `quit`/`toggle`. | F2 (partial) |
| A4 | P1 | Temp audio cleanup runs on all exit/error/timeout paths that drop a WAV; optional startup sweep of known empty/orphan chunk temps owned by the app. | F6 |
| A5 | P1 | When the license gate cannot run, cloud post-processing fails closed to local/minimal rather than keeping a cloud backend. | F5 |

### Tier B — Supply-chain and control-plane depth (next)

| # | Priority | Intended outcome | Addresses |
|---|----------|------------------|-----------|
| B1 | P2 | Downloaded Whisper/LLM models are verified against known digests (or pinned HF revision + hash) before rename into place. | F3 |
| B2 | P2 | Overlay lifecycle never needs pattern-based `pkill`; only tracked PIDs are signaled. | F7 |
| B3 | P3 | Remove unnecessary `shell=True` probes; use env reads. | F8 |
| B4 | P3 | Optional socket auth token (file next to socket, `0600`) required for destructive commands (`quit`, maybe `toggle`). | F2 (full) |

### Tier C — Product / platform (out of this review’s implementation scope)

| # | Priority | Intended outcome | Addresses |
|---|----------|------------------|-----------|
| C1 | Later | Optional OS keyring for API keys (libsecret / Keychain). | F9 |
| C2 | Never required for privacy | Hard DRM / server-only license. | F10 |
| C3 | Product call | Narrow Flatpak network (split optional cloud) if stores allow. | F11 |

**Ordering rule for implementers:** complete Tier A (especially A1–A2) before B1 (hashes) or C1 (keyring). Do not block ship on C-tier.

---

## Non-goals and intentional tradeoffs

Do **not** re-litigate these as open security defects without a product decision:

1. **Plaintext API keys and license token on disk with `0600`** — documented in `PRIVACY.md`; acceptable for v1 single-user desktops.
2. **Client-side / offline-first license** — Ed25519 tokens are integrity, not anti-tamper DRM.
3. **Flatpak `--share=network`** — required for activation, model-update check, and opt-in cloud; app-level defaults stay local.
4. **Unauthenticated same-user IPC for triggers** — required for KDE shortcuts / Deck R4 host scripts; harden modes/token over time, do not remove the socket without a replacement.
5. **This review does not implement patches** — remediation is planned only unless a separate implementation goal is opened.
6. **Out of scope:** dependency CVE bulk audit, Flathub review, full red-team, store screenshot / presentation work.

---

## Verification evidence (2026-07-09)

Captured under implementer scratch as `security-check-notes.txt` (modes + greps). Summary:

```
voice_profile.json     644   OPEN (F1)
config.json            600   strength
license.json           600   strength
config.json.bak-*      644   OPEN (F4)
activity.log           600   strength
wayfinder.log          644   OPEN (F4)
overlay-debug.log      644   OPEN (F4)
wayfinder-aura.sock    srwxr-xr-x  OPEN (F2)
/tmp/tmp*.wav leftovers 600  residual OPEN (F6)
```

Source anchors:

- Socket commands: `src/wayfinder/hotkeys/socket.py` (toggle/show/quit unauthenticated)
- Voice profile write: `src/wayfinder/core/voice_profile.py` `_save` (no chmod)
- Download integrity: `src/wayfinder/core/setup.py` `_download_model_file` (size only)
- Gate fail-open: `src/wayfinder/core/postprocessor.py` except branch “keeping configured backend”
- Strengths: `config.py` / `license.py` `0o600`; `wayfinder_main.py` activity log `0o600`; `processing_mode: local`

---

## Checklist for a future implementation PR (not this review)

- [x] A1 voice_profile `0600` + repair on load + test  
- [x] A2 backups/logs `0600`  
- [x] A3 socket `chmod` after bind (+ live app uses package listener)  
- [x] A4 app-owned temp dir + startup cleanup (that dir only)  
- [x] A5 gate fail-closed for cloud  
- [ ] B1 model digests (deferred)  
- [x] B2/B3 hygiene (pkill removal + shell=True)  
- [ ] B4 socket auth token (deferred)  

Implementation plan (Codex-approved): `docs/SECURITY-HARDENING-PLAN.md`.

---

*End of security review & remediation plan.*
