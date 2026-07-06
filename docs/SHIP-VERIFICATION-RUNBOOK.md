# Ship Verification Runbook — Wayfinder Aura

_How to prove the build is ship-ready. Complements `docs/SHIP-READINESS.md` (release
blockers) and `SHIPPING.md` (Flathub). Automated layers run themselves; the on-device
rows are yours (Peter) to work on a real Deck before tagging `v1.1.0`._

---

## 1. Automated test layers

```sh
# Fast gate (what CI runs) — must be green, ~30s:
QT_QPA_PLATFORM=offscreen PYTHONPATH=src .venv/bin/pytest \
    tests/ -m "not ui and not slow and not network and not perf" -q

# Perf budgets (load-sensitive; run on an idle machine):
QT_QPA_PLATFORM=offscreen PYTHONPATH=src .venv/bin/pytest -m perf -q

# Golden-audio ASR accuracy (needs whisper-cli + base.en; ~3 min on CPU):
PYTHONPATH=src python tests/golden_audio/generate.py      # once, regenerates the corpus
WAYFINDER_GOLDEN=1 QT_QPA_PLATFORM=offscreen PYTHONPATH=src .venv/bin/pytest \
    tests/test_golden_asr.py -q
PYTHONPATH=src:scripts python scripts/eval_asr.py         # human-readable WER report

# Soak / leak harness (pre-ship smoke = 20 iters; nightly = 500):
PYTHONPATH=src python scripts/soak.py --iters 20 --orphan-check
```

The golden corpus is **synthetic** (espeak-ng, regenerable via `generate.py`) — license-clean
and safe in the public repo, no recorded voice. Install `piper` for more natural clips
(`generate.py --voice piper --piper-model <onnx>`).

## 2. Live smoke (running app, on the Deck)

```sh
# With the app running, verify the control socket AND that tabs actually switch
# (the status breadcrumb at $XDG_RUNTIME_DIR/wayfinder-aura/status.json):
WAYFINDER_LIVE=1 PYTHONPATH=src .venv/bin/pytest tests/test_live_smoke.py -v
```

## 3. Mic → inject end-to-end (manual — injection targets the FOCUSED window)

This is the one path no headless test can own (text lands in whatever window has focus).
Do it by hand on the Deck:

1. Regenerate a golden clip if needed: `tests/golden_audio/long_clean.wav`.
2. Create a PipeWire loopback so a played file becomes the app's mic input:
   ```sh
   pactl load-module module-null-sink sink_name=wf_test \
       sink_properties=device.description=wf_test
   # In the app: Settings → Audio → pick "Monitor of wf_test" as the input device.
   ```
3. Focus a text field (a terminal running `cat > /tmp/wf_out.txt`, or any editor).
4. Trigger record (F2 / R4), then play the clip into the sink:
   `paplay --device=wf_test tests/golden_audio/long_clean.wav`
5. Trigger stop. Confirm the transcription lands in the focused field and reads back the
   clip's key phrases ("voice dictation", "press the button", "never lose a single word").
6. Unload the loopback: `pactl unload-module module-null-sink`.

## 4. On-device verification matrix (fresh Deck, before tag)

| Check | Expected | How |
|-------|----------|-----|
| **No config** | Recreated (chmod 600) + welcome tour; no crash | `mv ~/.config/wayfinder-aura ~/.config/wayfinder-aura.bak` then launch |
| **No models (Flatpak)** | base.en resolves from the bundle, dictates with zero downloads | fresh Flatpak install, dictate |
| **Broken/So GPU** | Auto CPU fallback still produces text; activity.log shows the fallback line | dictate on the Deck; `tail ~/.cache/wayfinder-aura/activity.log` |
| **No mic** | Friendly "no audio" message, no crash | disable input, record |
| **Desktop + Game Mode** | R4/back-button trigger works in both | switch modes, trigger |
| **No license** | Free tier fully works; "Get Ultra" opens storefront; premium truly locked | fresh profile, no license.json |
| **Upgrade** | Old config merges (keys kept, URL migrated, stays 600); models persist | keep an old config.json, update, `systemctl --user restart wayfinder-aura.service` |
| **Packaging** | `/app/bin` has all 6 inference binaries + wtype + xdotool + base.en model | `flatpak run --command=sh …WayfinderAura -c 'ls /app/bin /app/share/whisper-models'` |
| **GPU dictation** | Fast + accurate turbo dictation (your standing gate) | one real long dictation on the Deck |
| **Soak (Mode B)** | No orphaned whisper-server/llama after SIGKILL; RSS/VRAM stable | `python scripts/soak.py --mode socket --pid <app-pid> --minutes 30 --orphan-check` |

Deck systemd + host-trigger install is automated: `scripts/steamdeck/install-steamdeck.sh`
(and `uninstall-steamdeck.sh`).

## 5. Release blockers (see `docs/SHIP-READINESS.md`)

Still gated on you and NOT covered by the automated layers above:
- License → production (`LICENSE_API_URL` **and** `LICENSE_PUBLIC_KEY_HEX` together).
- Strip DEV-UNLOCK; delete the dead HMAC path.
- Confirm the storefront URL + price; then the activation matrix (locked / activate / offline-grace).
- Bump the metainfo `<release>` date at tag time (`test_metainfo_release_matches_pyproject`
  guards the version; the date is manual).
- Author terms/refund for the paid tier; the privacy + support notices are in
  `PRIVACY.md` / `SUPPORT.md`.
