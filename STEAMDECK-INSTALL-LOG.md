# Steam Deck Installation Log — Wayfinder Aura

A viability assessment for shipping `wayfinder-aura` to "average user" Steam Deck owners, captured during a fresh install on **SteamOS 3.7.22 (Holo) / glibc 2.41 / Python 3.13.1 / Steam Deck OLED (Zen 2 APU)**, against `origin/main` after the macOS-port commits.

---

## TL;DR — current AppImage is **not viable** for average Steam Deck users

The bazzite-built AppImage refuses to launch (glibc symbol mismatch). The from-source path requires ~25 minutes of CLI work, three sudo blocks, building whisper.cpp inside a podman container with non-default flags, and hand-written wrapper scripts. Two of the failures are *unrecoverable for non-technical users* — they're not "missing package" errors, they're "the system you're running on doesn't support the instruction set this binary was compiled for" or "the headers your compiler needs aren't in /usr/include/."

### Hard blockers hit on this Deck

| # | Failure | Average user can recover? |
|---|---------|--------------------------|
| 1 | AppImage refuses to load `libpython3.14.so.1.0` (`GLIBC_ABI_GNU2_TLS` missing) | **No** |
| 2 | `pacman` blocked by SteamOS read-only filesystem | No (terminal + sudo) |
| 3 | First `pacman -S` fails — keyring not initialized | No (arcane error) |
| 4 | `pip install evdev` fails — kernel headers stripped | No |
| 5 | Building whisper.cpp on host fails — `stdint.h` itself stripped | No |
| 6 | Bazzite's whisper-cli SIGILL on Zen 2 — built with AVX-512 | No |
| 7 | App's setup wizard crashes with Tk threading bug | No |
| 8 | Pystray import fails — no AppIndicator namespace | No |
| 9 | Tray icon renders as blue block under xorg fallback | Cosmetic |
| 10 | `ydotool` service name mismatches install docs | No |
| 11 | Default config points at a premium-only 1.6 GB model that takes "a million years" to transcribe on Zen 2 CPU | No (UX trap) |
| 12 | App caches config in memory — file edits not picked up live | Recoverable (relaunch) |

### Tooling that *is* on the Deck (and helps a lot for v2)

| Tool | Where | Value for v2 |
|------|-------|--------------|
| `podman` | `/usr/bin/podman` (preinstalled) | We can run a fedora container locally to build anything we need with full headers |
| `distrobox` | `/usr/bin/distrobox` (preinstalled) | Cleaner dev workflow than podman raw |
| `flatpak` | preinstalled, with KDE / Discover frontend | Already how SteamOS users install GUI apps |
| `flatpak-builder` | in pacman `extra-3.7` v1.4.4 | Lets us build the Flatpak manifest the repo *already has* |
| `fuse2`, `vulkan-icd-loader`, `vulkan-headers` (file-stripped but pkg installed), `shaderc`, `cmake` | various | All available; mostly a packaging not availability problem |
| `flatpak/io.github.user.WayfinderAura.yml` | **already in the repo** | The right packaging vehicle is already designed; just not built/shipped yet |

### "Average user" success looks like

Download `.flatpak` (or click an install link in Discover) → grant permissions → app works. **None of that exists today.** Nothing in any current install doc gets a non-technical Deck owner there.

---

## Critical finding 1 — SteamOS strips dev headers system-wide

`pacman -Qkk` reveals the scale:

| Package | Total files | Files missing |
|---------|-------------|---------------|
| `glibc` | 1616 | **773 missing** (~48%) — incl. `/usr/include/stdint.h`, `/usr/include/stdio.h` |
| `linux-api-headers` | 1012 | **1010 missing** (~99.8%) — incl. `linux/input.h`, `linux/input-event-codes.h` |
| `vulkan-headers` | 75 | **50 missing** — incl. all `/usr/include/vulkan/*.h`, cmake config |

Implications:
- **No C/C++ compilation possible** on stock SteamOS — `stdint.h` itself is gone.
- **No `find_package(Vulkan)`** — headers + cmake config absent.
- **No Python C-extension wheels** that need kernel headers (`evdev`).
- These packages **report installed**. `pacman -S` won't fix it; the files were never extracted to disk.

**Workaround that works:** Run a `fedora:42` podman container, `dnf install` the build tools and headers there, mount the source via volume, build inside the container. The output binary runs fine on the host (forward-compat glibc).

**For v2:** The Flatpak path entirely sidesteps this — Flatpak runtimes ship a complete sandboxed glibc + libs. No host compilation needed. **This is the answer.**

---

## Critical finding 2 — Bazzite's binaries SIGILL on Zen 2

Symptom in the app log:
```
[21:56:16]    🔥 GPU test starting...
[21:56:16]    ⚠️ GPU failed: exit -4
[21:56:16]    🧠 CPU test starting...
[21:56:17]    ⚠️ CPU failed: exit -4
```

Exit `-4` = `SIGILL` (illegal instruction).

Diagnosed via `objdump -d`:
- Bazzite's `libggml-cpu.so.0.9.4` contains **6,565 AVX-512+ instructions** (`%zmm`, `vpermt2`, `vpdpbusd`).
- Steam Deck APU 0405 (Zen 2 mobile, 15W TDP) supports **up to AVX2**, not AVX-512.
- whisper-cli's `--help` works because it doesn't run the math; transcription does, hence SIGILL.

**Root cause:** the build was done on a CPU with AVX-512 (likely a recent desktop Ryzen or Intel), and whisper.cpp's cmake defaults enable native CPU optimization. Cross-CPU-class binary distribution silently breaks.

**Fix:** build with `-DGGML_NATIVE=OFF -DGGML_AVX512=OFF`. Tested this rebuild via `podman run fedora:42` on the Deck itself — produces a binary with **0 AVX-512 instructions** that runs cleanly. Keep the `cmake -B build -DGGML_NATIVE=OFF -DGGML_AVX512=OFF` flags in the Flatpak manifest.

**For v2:** the Flatpak runtime will guarantee a consistent build environment. Pin `-march=x86-64-v3` (covers up to AVX2/FMA, runs on every modern x86_64 CPU).

---

## Critical finding 3 — `whisper.cpp` upstream ships no Linux binaries

`gh release view ggerganov/whisper.cpp` shows only Windows (`whisper-bin-x64.zip`), Windows BLAS, Windows CUDA, and macOS xcframework + Java jar. **No Linux release binaries.**

Implication: any Linux-based product that uses whisper.cpp **must** build it themselves (or via Flatpak runtime / appimagetool / etc.).

**For v2:** include the whisper.cpp build inside the Flatpak manifest's `modules` block. The flatpak-builder runtime has compilers; this works out of the box.

---

## Issue 1 — AppImage incompatible with Steam Deck glibc *(blocker)*

```
[PYI-88192:ERROR] Failed to load Python shared library '/tmp/_MEINHqddv/libpython3.14.so.1.0':
/usr/lib/libc.so.6: version `GLIBC_ABI_GNU2_TLS' not found
```

Bazzite Python 3.14 → glibc ≥ 2.42 symbol. Deck has 2.41.

**Recommended fix:**
- Build inside `manylinux_2_28` container (glibc 2.28 — runs anywhere modern), **OR**
- Pin Python 3.13 in `pyproject.toml` and `build.sh`, **OR**
- Ship via Flatpak (runtime supplies its own glibc — issue evaporates).

---

## Issue 2 — Setup wizard threading crash *(code bug, hits every first run)*

```
File "src/wayfinder/ui/dialogs/setup_wizard.py", line 305, in _sequence
    self._recheck(dep_id)
File "src/wayfinder/ui/dialogs/setup_wizard.py", line 499, in _recheck
    self.after(200, self._update_footer_state)
RuntimeError: main thread is not in main loop
```

Background thread calls `Tk.after()` — not allowed.

**Fix:** marshal back to main thread via `root.after_idle(callback)` or a `queue.Queue` + main-loop polling. Test all 5 wizard dep states + a forced-failure case.

---

## Issue 3 — Pystray fails to import on stock SteamOS

```
ValueError: Namespace AppIndicator3 not available
ValueError: Namespace AyatanaAppIndicator3 not available
```

**Workaround:** `PYSTRAY_BACKEND=xorg` env var, then install `libayatana-appindicator` (in `extra-3.7`).

**Side observation:** with xorg backend, the tray icon renders as a blue block instead of the actual logo. The icon (`assets/icon.png`, 2048×2048 RGBA, 28 KB) is present and valid; pystray's xorg path doesn't render it correctly under KDE Plasma Wayland.

**Fix:**
```python
import os
if not os.environ.get('PYSTRAY_BACKEND'):
    try:
        import gi
        gi.require_version('AyatanaAppIndicator3', '0.1')
    except (ImportError, ValueError):
        os.environ['PYSTRAY_BACKEND'] = 'xorg'
import pystray
```

Or (better) **drop pystray entirely** in favor of `QSystemTrayIcon` from PyQt6 — already a dependency, removes the entire gtk/gobject-introspection chain, and KDE renders QTrayIcon natively.

---

## Issue 4 — ydotool service name/scope differs on Arch/SteamOS

`INSTALL-UBUNTU.md` says `sudo systemctl enable --now ydotoold`. On Arch, the unit is **user-level** at `/usr/lib/systemd/user/ydotool.service`. Right command: `systemctl --user enable --now ydotool.service` (no sudo).

**Fix:**
- Per-distro install docs.
- For AppImage/Flatpak: bundle `ydotoold` and auto-spawn it on app start with `--socket-path=$XDG_RUNTIME_DIR/ydotool.sock --socket-perm=0600`. Sidesteps service-naming and per-distro packaging entirely.

---

## Issue 5 — Kernel headers stripped → `evdev` wheel build fails

evdev's setup looks for `linux/input.h`. SteamOS strips it.

**Workaround:** install pacman's `python-evdev` 1.7.1-2; recreate venv with `--system-site-packages`; remove `evdev>=1.6.0` from `requirements.txt` before pip install.

**Fix:** Flatpak path — pre-built wheels in the runtime, or build inside the flatpak-builder sandbox.

---

## Issue 6 — Vulkan headers stripped → cmake build fails

`/usr/include/vulkan/*.h` reports installed but isn't there. Same SteamOS-strip pattern.

**Workaround:** build inside `fedora:42` podman container. Required dnf packages: `cmake gcc-c++ make vulkan-loader-devel vulkan-headers shaderc glslang`. **Note:** `shaderc-devel` does NOT exist in fedora:42 — request `shaderc` (no `-devel`) for the `glslc` binary.

---

## Issue 7 — `stdint.h` itself missing → no C compilation possible

`/usr/include/stdint.h` does not exist on stock SteamOS, despite being a fundamental C standard library header.

**No source compilation possible on the Deck without container or restored headers.** The `podman run fedora:42` workaround is the only realistic answer for the dev path. For shipping: Flatpak.

---

## Issue 8 — SteamOS read-only FS blocks `pacman` *(prereq)*

`sudo steamos-readonly disable` before any `pacman -S`. SteamOS major updates wipe `/usr/`, removing pacman-installed packages; configs survive.

---

## Issue 9 — Pacman keyring not initialized *(prereq)*

```
sudo pacman-key --init && sudo pacman-key --populate
```

before the first `pacman -S`.

---

## Issue 10 — Input group requires full KDE logout (not just lock)

`usermod -aG input` succeeds but the running KDE Plasma session won't see the new group until full logout/login. Locking does NOT refresh.

**Fix in app:** check `os.getgroups()` vs `grp.getgrnam('input').gr_mem`; if user is a member but `'input' not in os.getgroups()`, prompt for full logout. Doc must say "*log out, NOT lock*."

**Better fix (Flatpak):** sandbox + portal-based approach — no input group needed.

---

## Issue 11 — Default config uses a premium-only 1.6 GB model

`config.json` is generated with:
```json
"model_path": "~/whisper.cpp/models/ggml-large-v3-turbo.bin"
```

Issues:
- File isn't present on fresh installs.
- Model is in `PREMIUM_FEATURES["large_models"]` per `license.py` — free tier shouldn't have it as a default.
- On Zen 2 CPU it's effectively unusable: a "million years" per the user's words.

**Fix:**
- On first run, scan for `*.bin` in `~/whisper.cpp/models/` and `<XDG_DATA_HOME>/wayfinder-aura/models/`; pick the smallest available.
- If none, default to `base.en` (~142 MB) — fast enough on Steam Deck CPU.
- Free tier should default to a free-tier-eligible model.
- Premium-tier auto-upgrade flow: if user enters a Pro license, offer to download `large-v3-turbo`.

---

## Issue 12 — `whisper-cli` binary has no rpath, can't find sibling `.so`s

Whisper.cpp's cmake doesn't bake `RPATH`. Loader can't find the build-tree-relative shared libs.

**Workaround:** wrapper shim script that sets `LD_LIBRARY_PATH` and exec's the renamed real binary.

**Fix in upstream:** propose patch to whisper.cpp's `CMakeLists.txt` to set `CMAKE_INSTALL_RPATH '$ORIGIN/../lib'` + `CMAKE_BUILD_WITH_INSTALL_RPATH ON`, **OR** static-link by default.

**Fix locally:** post-process with `patchelf --set-rpath '$ORIGIN/../lib' bin/whisper-cli` after build, and lay out the `.so`s in a flat `lib/` next to `bin/`.

---

## Issue 13 — Bazzite SIGILL on Zen 2 (CPU instruction set mismatch)

Already detailed above ("Critical finding 2"). Surface here so it appears in the issue numbering. Fix: build with `-DGGML_NATIVE=OFF -DGGML_AVX512=OFF` for any binary intended to run on Steam Deck class hardware.

---

## Issue 14 — App caches config in memory; file edits not live

Editing `~/.config/wayfinder-aura/config.json` while the app is running has no effect until restart. Confused us during the model-swap test (set `model_path` to base.en, app still using the old large-v3-turbo from in-memory config).

**Fix:** simple `inotify` / `watchdog` watch on `config.json` with a debounce, OR add a "Reload config" button in Settings, OR just have the wizard "Apply" button re-read from disk.

---

## Issue 15 — Setup wizard benchmark TypeError

```
File "wayfinder_main.py", line 1051, in get_dynamic_tooltip
    time_str = f"{result['cpu_10s']:.1f}s"
TypeError: unsupported format string passed to NoneType.__format__
```

When a benchmark fails (e.g., the SIGILL we hit), `result['cpu_10s']` is `None` and the format string crashes. Compounded with issue 2, the wizard becomes a minefield on first run.

**Fix:** null-check in `get_dynamic_tooltip` and fallback string: `time_str = f"{result['cpu_10s']:.1f}s" if result.get('cpu_10s') is not None else "—"`.

---

## Issue 16 — `whisper.cpp` upstream ships no Linux binaries

Already covered ("Critical finding 3"). Surface here for the issue numbering.

**Fix:** include whisper.cpp as a build module inside the Flatpak manifest. Possibly upstream a PR to whisper.cpp adding a Linux release artifact pipeline (though this is out of scope for wayfinder-aura).

---

## Issue 17 — Performance defaults are wrong for integrated GPU class

The "default model" path assumes a discrete GPU. On a Steam Deck class device:
- CPU-only inference of `large-v3-turbo` is ~real-time × 10+ — unusable for live dictation.
- Even Vulkan-accelerated, the AMD APU is much slower than a desktop dGPU.
- A Steam Deck user opening the app and pressing F3 with default config gets a dictation latency that feels broken.

**Fix:** detect device class on first run.
- If discrete GPU + 8GB+ VRAM: default to `large-v3-turbo`.
- If integrated GPU / mobile APU: default to `base.en` or `small.en`.
- If CPU-only / underpowered: default to `base.en` and warn user about tone-mode tradeoffs.

---

## Issue 18 — Missing INSTALL-STEAMDECK.md

Currently only `INSTALL-UBUNTU.md` exists, and most of its instructions don't apply to Arch/SteamOS. Suggested file (drop in next to `INSTALL-UBUNTU.md`):

```bash
# This file is for power users / contributors.
# Most Steam Deck users should use the Flatpak — see README.md.

# 1. SteamOS unlock + keys
sudo steamos-readonly disable
sudo pacman-key --init && sudo pacman-key --populate

# 2. System packages
sudo pacman -S --needed cmake base-devel python-evdev ydotool \
                       libayatana-appindicator fuse2

# 3. ydotool USER service (note: not sudo, not 'ydotoold')
systemctl --user enable --now ydotool.service

# 4. Input group (REQUIRES FULL LOGOUT/LOGIN OF KDE)
sudo usermod -aG input $USER

# 5. whisper.cpp — MUST be built inside a container; SteamOS strips
#    /usr/include/stdint.h, vulkan headers, kernel headers, etc.
podman run --rm -v ~/whisper.cpp:/work -w /work fedora:42 bash -c "
  dnf install -y cmake gcc-c++ make vulkan-loader-devel vulkan-headers shaderc &&
  rm -rf build &&
  cmake -B build -DGGML_NATIVE=OFF -DGGML_AVX512=OFF -DGGML_VULKAN=ON -DCMAKE_BUILD_TYPE=Release &&
  cmake --build build --config Release -j\$(nproc)
"
bash ~/whisper.cpp/models/download-ggml-model.sh base.en

# 6. App: clone, venv with system-site-packages, pip install (skip evdev/llama-cpp-python)
cd ~/dev/wayfinder-aura
python3 -m venv --system-site-packages .venv
grep -vE "^(evdev|llama-cpp-python)" requirements.txt > /tmp/req.txt
.venv/bin/pip install -r /tmp/req.txt
DISPLAY=:0 .venv/bin/python main.py
```

---

## Issue 19 — `evdev` hotkey backend misses XTEST-injected keys (Steam Input on X11)

`src/wayfinder/hotkeys/__init__.py:get_best_hotkey_listener()` prefers the `evdev` backend on Linux, falling back to `pynput` only if `import evdev` fails. evdev reads `/dev/input/event*` directly, which sees real uinput keyboard events but **does not see X11 XTEST-injected key events**. Steam Input in Desktop Mode commonly emulates keystrokes via XTEST, so a controller button bound to (say) F3 may never reach wayfinder's listener — the user perceives the hotkey as silently dead even though the keystroke is being injected.

**Fix:** flip preference so pynput is tried first on Linux. pynput uses the X11 XRecord extension, which observes XTEST-injected keys *plus* real uinput events:

```python
if sys.platform.startswith('linux'):
    if is_pynput_available():
        return (pynput_hotkey_listener, "pynput")
    try:
        import evdev
        return (hotkey_listener, "evdev")
    except ImportError:
        pass
```

Trade-off: pynput has marginally higher latency than raw evdev (single-digit ms in practice — well under the perceptual threshold for hotkey-triggered dictation).

**Workaround until the code fix lands** (used on this Deck): bind controller button → unused F-key in Steam Input Desktop Layout, then add a KDE Global Shortcut that runs `toggle-recording.sh` (which calls `send_toggle()` over the Unix socket at `/tmp/wayfinder-aura.sock`). Sidesteps the keyboard-listener layer entirely.

---

## Issue 20 — Audio device picker exposes JACK-backed mic that fails stream open

PortAudio on PipeWire/SteamOS enumerates the same physical microphone under multiple host APIs (ALSA + JACK). `sounddevice.query_devices()` returns one entry per (device × host API) combination, so a single Shure MV7 appears as both:

```
[ 4] Shure MV7: USB Audio (hw:2,0)   via ALSA
[10] Shure MV7 Mono-137              via JACK Audio Connection Kit
[11] Shure MV7 Mono                  via JACK Audio Connection Kit
```

Wayfinder's settings UI lists all of them. If the user picks one of the JACK-backed indices (easy to do — the name is identical, the user has no way to know which host API is behind each), `recorder.start()` raises:

```
⚠ Microphone: Error starting stream:
Unanticipated host error [PaErrorCode -9999]: 'unknown error'
[JACK Audio Connection Kit error -1]
```

…on every recording attempt. PipeWire's libjack shim refuses to autostart a server from inside a systemd user service context, the JACK host API short-circuits, and PortAudio propagates rather than falling back to ALSA/Pulse. `JACK_NO_START_SERVER=1` in the service env does **not** prevent it — the failure happens later in the stream-open path.

User-visible symptom: F3 starts the overlay "Listening…" indicator, instantly fails, returns to Ready. No transcription, no inject. Whisper sometimes hallucinates artifacts like `"Thank you. [no audio]."` if the empty-buffer fallback path proceeds anyway, which masks the real issue.

**Fix (Linux only):** filter out JACK-backed devices when populating the device list shown to the user. The relevant call lives wherever wayfinder feeds `sounddevice.query_devices()` into the settings UI dropdown:

```python
import sounddevice as sd

def list_input_devices_linux_safe():
    hostapis = sd.query_hostapis()
    devices = []
    for idx, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] <= 0:
            continue
        if sys.platform.startswith("linux"):
            hostapi_name = hostapis[d["hostapi"]]["name"]
            if "JACK" in hostapi_name:
                continue  # JACK-backed clones fail on PipeWire/SteamOS — skip
        devices.append((idx, d))
    return devices
```

Trade-off: users running a real JACK server (rare for this app's audience) would lose visibility of JACK-routed inputs. Acceptable — they can still pick ALSA-backed devices.

**Bonus fix:** wayfinder already has a sensible auto-selection fallback (`_resolve_audio_device`-style logic — it noticed `pulse` was an output, scored devices, picked `Shure MV7 Mono-137`). On a fresh install or after device renumbering, that fallback is what saves the user. The picker filter above keeps users from getting *into* the broken state in the first place.

**Workaround used on this Deck:** set `"audio_device": 8` (the `pulse` ALSA entry) in `~/.config/wayfinder-aura/config.json`. wayfinder detects pulse is an output, auto-falls back to a working mic device, and recording works. Any ALSA-backed index would also work (4, 7, 8, or 9 in this Deck's enumeration).

---

## Issue 21 — Shipping gamepad-trigger setup by default (Steam Deck Desktop Mode)

Working chain on this Deck (after all the workarounds above): **R4 → Steam Input → keyboard `Insert` → KDE Custom Shortcut → `toggle-recording.sh` → Unix socket → wayfinder.** This delivers hands-free dictation in Desktop Mode without a keyboard. Setup is currently fully manual and requires Steam Input config + KDE Shortcuts config.

**What a Flatpak can ship by default (and what it can't):**

| Layer | Auto-shippable? | Mechanism |
|-------|-----------------|-----------|
| `toggle-recording.sh` / `trigger_record.py` | ✓ | Already in repo; Flatpak bundles them. |
| Unix socket trigger | ✓ | Already present; Flatpak just runs wayfinder normally. |
| KDE Global Shortcut binding | ✓ (via portal) | Use `org.freedesktop.portal.GlobalShortcuts` — already implemented in `src/wayfinder/hotkeys/dbus.py`. Portal lets sandboxed apps register shortcuts that KDE honors. User binds via standard System Settings → Shortcuts UI; wayfinder's shortcut entry shows up there with a default keycode wayfinder can request (e.g. `Insert`). No filesystem write to `~/.config/kglobalshortcutsrc` needed. |
| Steam Input controller layout (R4 → keyboard key) | ✗ (programmatically) | Steam Input has no public API for third-party apps to set bindings. Two practical paths: **(a) Publish a "Wayfinder Aura — Steam Deck Desktop" layout to the Steam Workshop**, link to it from wayfinder's onboarding ("Subscribe to recommended controller layout → 2 clicks"). **(b) First-run wizard** detects Steam Deck (`/sys/class/dmi/id/product_name` contains `Jupiter` or `Galileo`) and walks the user through binding R4 → Insert in Steam Input with screenshots. |

**Recommended v2 ship plan:**

1. Use the **GlobalShortcuts portal path** (already coded, just need to make it the default Linux backend on Wayland and X11+KDE; cf. Issue 19's preference fix). User binds the controller-friendly key once via System Settings; no manual `khotkeysrc` editing.
2. Publish a **Workshop controller template** with `R4 → Insert` (or whatever key the portal default uses) prebound. Onboarding wizard deep-links to it.
3. **Detect Steam Deck on first launch** and gate the onboarding hint behind that check — non-Deck Linux users don't need this whole path.

**Permission caveat for the Flatpak:** the GlobalShortcuts portal works inside the sandbox with no extra `--filesystem=` permissions — that's the whole point of the portal. Avoid asking for `--filesystem=xdg-config` access for shortcut wiring; users will (rightly) refuse, and it's unnecessary.

**What wayfinder cannot ship by default:** the Steam Input binding itself. Even an unsandboxed installer can't modify Steam's controller config without simulating user interaction in the Steam client. That layer is forever a "user follows these steps" or "user subscribes to Workshop layout" affair.

---

## Working state snapshot (this Deck, after all workarounds)

- **OS**: SteamOS 3.7.22, kernel 6.11.11-valve28, glibc 2.41
- **Python**: 3.13.1 (system)
- **whisper.cpp**: built locally inside `fedora:42` podman container with `-DGGML_NATIVE=OFF -DGGML_AVX512=OFF`. Wrapper script at `~/whisper.cpp/build/bin/whisper-cli` sets `LD_LIBRARY_PATH` and execs `whisper-cli.real`. `.so` files at `build/{src,ggml/src}/lib*.so.*` (no flat-lib layout).
- **Whisper model**: `base.en` (142 MB) — `large-v3-turbo` was unusable on CPU, hence the swap.
- **Backup**: CPU-only build saved at `~/whisper.cpp/build_cpu_backup/` before the Vulkan rebuild attempt.
- **App venv**: `python3 -m venv --system-site-packages` so pacman's `python-evdev` is visible.
- **Filtered requirements**: `requirements.txt` minus `evdev` (broken without kernel headers) and `llama-cpp-python` (user uses whisper.cpp directly).
- **System tray**: `libayatana-appindicator` installed; default pystray backend works (no need for `PYSTRAY_BACKEND=xorg` workaround anymore).
- **Working launch command**:
  ```bash
  cd ~/dev/wayfinder-aura && DISPLAY=:0 .venv/bin/python main.py
  ```
- **Tested transcription** end-to-end: 1.0s tone → 1.8s wall time (acceptable on CPU with base.en).

---

## Update — 2026-05-08: SteamOS-update regression wiped pacman packages (Issue 8 pattern)

The dev-path the "Working state snapshot" above recorded as functional broke. Same Deck, same repo, no manual changes. **A SteamOS major-version update wiped multiple pacman-installed packages from `/usr/`** — exactly the failure mode predicted by Issue 8 (*"SteamOS major updates wipe `/usr/`, removing pacman-installed packages; configs survive"*). Also lost: `deck` user's `input` group membership, which broke ydotool runtime perms.

**Casualties on this Deck:**
- `libayatana-appindicator` → pystray crashes at *import* time (Issue 3 trace)
- `ydotool` / `ydotoold` → app launches and transcribes fine, but text injection fails silently with `"⚠ Injection: ydotool not found"` in app log (Issue 4 trace)
- `deck` is no longer a member of `input` group (`groups deck` shows only `wheel steamos-log-submitter deck`) — even after reinstalling ydotool, its user service can't access `/dev/uinput` until the user re-joins `input` AND fully logs out of KDE (Issue 10)

### Symptoms

- `wayfinder-aura.service` (user systemd unit, autostart) is in a failed/restart-loop state. `systemctl --user status` shows `code=exited, status=1/FAILURE`, restart counter caps at 5 with `Start request repeated too quickly`.
- Pystray crashes at *import* time — same trace as Issue 3:
  ```
  ValueError: Namespace AppIndicator3 not available
  ValueError: Namespace AyatanaAppIndicator3 not available
  ```
  This time it's a *regression* (the package was installed and is now gone), not a fresh install.
- Running `./launch-wayfinder-aura.sh` manually produces a *different and misleading* error (`ModuleNotFoundError: No module named 'customtkinter'`) — see launcher bug below.

### Verification

- `pacman -Ql libayatana-appindicator` → `package 'libayatana-appindicator' was not found`. Compare to Working state snapshot which lists it as installed.
- `/usr/lib/girepository-1.0/` contains only `Gtk-3.0.typelib`. No `AppIndicator3.typelib`, no `AyatanaAppIndicator3.typelib`.
- `flatpak list` confirms **no Wayfinder Aura Flatpak is or has ever been installed** on this Deck — consistent with the rest of this log; noting it because it was easy to misremember.

### Quick restore on this Deck (~5 min, plus a logout)

```bash
# 1. Reinstall the wiped packages
sudo steamos-readonly disable
sudo pacman-key --init && sudo pacman-key --populate     # only if first pacman use post-update
sudo pacman -S libayatana-appindicator ydotool
sudo steamos-readonly enable

# 2. Restore input-group membership (lost across the update)
sudo gpasswd -a deck input

# 3. Enable ydotool's user service (will fail to start until step 4)
systemctl --user enable ydotool.service

# 4. Full KDE Plasma logout + login (NOT just lock — Issue 10).
#    On login, systemd --user picks up new input-group membership,
#    starts ydotool.service, and auto-restarts wayfinder-aura.service.

# 5. Verify
systemctl --user status ydotool.service wayfinder-aura.service --no-pager
```

Returns the dev-path to the "Working state snapshot" configuration. **The next SteamOS major update will wipe all of this again** — that's the structural reason the v2 Flatpak plan exists.

### Launcher script divergence (separate small bug — fix on bazzite)

`launch-wayfinder-aura.sh` only probes `venv-mac/bin/python` and `venv-gpu/bin/python`, not `.venv/bin/python`. So running it manually skips the project's actual venv and falls through to `/usr/bin/python3`, which produces the unrelated `customtkinter` error. The systemd unit invokes `~/.local/bin/wayfinder-aura-launcher` instead, which does use `.venv` correctly — that's the launcher whose pystray failure is the *real* error. **Fix:** add `.venv/bin/python` to the probe list in `launch-wayfinder-aura.sh` so manual runs match systemd's path and produce the same diagnostic.

### Misleading `dnf install` hint in app code (bazzite-side fix)

When ydotool isn't found, the app prints `Install with: sudo dnf install ydotool`. That's Fedora-only — wrong on Arch/SteamOS (which is the larger user base for this app, given Steam Deck is the headline target). Sources:

- `src/wayfinder/core/injector.py:80`
- `src/wayfinder/core/injector.py:270`
- `src/wayfinder/core/setup.py:495`
- `wayfinder_main.py:7828`

**Fix:** detect the package manager (`command -v pacman || command -v apt || command -v dnf`) and emit the right install command, or list all three. This bug actively *causes* failed installs on Steam Deck because users follow the printed advice and get "command not found" for `dnf`.

### Verified state on this Deck mid-restore (2026-05-08 23:16 CDT)

Partial restore ran in this session. Snapshot of what's confirmed working vs. pending:

| Component | State | Evidence |
|-----------|-------|----------|
| `libayatana-appindicator` | ✅ Installed (3 packages: ayatana-ido, libayatana-indicator, libayatana-appindicator) | `pacman -Ql libayatana-appindicator` succeeds; `/usr/lib/girepository-1.0/AyatanaAppIndicator3-0.1.typelib` present |
| pystray import | ✅ Works | `python -c "import pystray; print(pystray.Icon.__module__)"` → `pystray._appindicator` |
| `wayfinder-aura.service` | ✅ `active (running)` | systemd status; main + overlay subprocesses up |
| Whisper transcription (CPU + base.en) | ✅ Works | App log: `✓ Chunk 1: "Check one, two, three."` |
| Post-processing | ✅ Works | App log: `📝 "Check one, two, three."` |
| `ydotool` | ❌ Still missing | `which ydotool` → not found; app log: `⚠ Injection: ydotool not found` |
| `deck` ∈ `input` group | ❌ Not a member | `groups deck` → `wheel steamos-log-submitter deck` |
| End-to-end dictation | ❌ No keystrokes injected to focused window | follows from the two ❌ above |

**Remaining steps from the Quick restore block:** reinstall ydotool, `gpasswd -a deck input`, enable `ydotool.service`, full Plasma logout/login.

### Implication for v2 priority

This regression confirms Recommendation #1 (*Build the Flatpak*) is the real fix. Any dev-path that depends on pacman packages will silently break for every user with this layout on every SteamOS major update. The Flatpak runtime ships its own AppIndicator typelibs, so the regression class disappears.

---

## Update — 2026-05-12: Reboot validation surfaced launcher auto-rebuild bug (unfiltered `requirements.txt`)

Rebooted (post-SteamOS atomic update: kernel `valve28` → `valve29` had applied between the May 10 and May 12 boots) specifically to validate the working dictation state from earlier in the day. Service immediately entered a 6+ restart crash-loop, throwing two toast notifications per cycle: *"Wayfinder Aura crashed — see /tmp/wfa-stderr.log"* and *"Venv rebuild failed — see /tmp/wfa-stderr.log..."*.

**Root cause:** `~/.local/bin/wayfinder-aura-launcher` self-heals when its smoke test (`import customtkinter, pystray, PIL, numpy`) fails by running `pip install --quiet -r requirements.txt` against the **unfiltered** requirements file. But `requirements.txt` lists `evdev` and `llama-cpp-python`, both of which are **source-build-only** on SteamOS — exactly the conditions Issues 5 (`evdev` needs kernel headers) and 7 (no `stdint.h`, no `gcc`) document as fatal. The original install in this log (line 340-341) sidesteps this with `grep -vE "^(evdev|llama-cpp-python)" requirements.txt > /tmp/req.txt`; the auto-rebuild path doesn't mirror that filter.

So the launcher's mitigation for one SteamOS failure class (smoke-test ABI break on Python minor-version bump) immediately re-triggers a different SteamOS failure class (Issues 5/7) and bricks the service.

### Symptoms

- `systemctl --user status wayfinder-aura.service` shows `Active: activating (auto-restart)` cycling on a ~17-second loop; restart counter climbing.
- `/tmp/wfa-stderr.log` repeats:
  ```
  CMake Error at .../CMakeDetermineCCompiler.cmake:48 (message):
    Could not find the compiler specified in the environment variable CC:
    gcc.
  ERROR: Failed building wheel for llama-cpp-python
  ```
- Two `notify-send` toasts per cycle (the `OnFailure=wayfinder-aura-failed.service` plus the launcher's own "Venv rebuild failed" toast).
- `.venv` directory is left in a stub state (just `bin/{python,pip}`, no real `site-packages`) because every pip install attempt aborts mid-resolution.

### Why the smoke test was firing in the first place

Indeterminate from the available evidence — by the time the launcher's `rm -rf .venv` runs, the prior site-packages contents are gone. The atomic image swap (kernel `valve28` → `valve29` between May 10 and May 12 boots) is the most plausible trigger: even when `/usr/bin/python3` reports the same `3.13.1` post-update, the `--system-site-packages`-visible host packages can rev independently and break the compiled `.so` ABI for venv packages that link against them. The smoke test correctly detected *something* was broken; the issue is purely with what it did next.

### Fix

Patch `~/.local/bin/wayfinder-aura-launcher` so the rebuild branch mirrors the install-script filter:

```bash
SKIP_RE='^(evdev|llama-cpp-python)'
grep -vE "$SKIP_RE" requirements.txt > /tmp/wfa-req.txt
if ! ./.venv/bin/pip install --quiet -r /tmp/wfa-req.txt; then
    /usr/bin/notify-send -u critical -i dialog-error "Wayfinder Aura" "Venv rebuild failed — see /tmp/wfa-stderr.log. Run: cd ~/dev/wayfinder-aura && rm -rf .venv && python3 -m venv --system-site-packages .venv && grep -vE '$SKIP_RE' requirements.txt > /tmp/wfa-req.txt && .venv/bin/pip install -r /tmp/wfa-req.txt"
    exit 1
fi
```

Immediate restore (run once on this Deck to put the venv into the state the patched launcher would produce):

```bash
systemctl --user stop wayfinder-aura.service
cd ~/dev/wayfinder-aura
rm -rf .venv
/usr/bin/python3 -m venv --system-site-packages .venv
grep -vE '^(evdev|llama-cpp-python)' requirements.txt > /tmp/wfa-req.txt
./.venv/bin/pip install -r /tmp/wfa-req.txt
systemctl --user reset-failed wayfinder-aura.service
systemctl --user start wayfinder-aura.service
```

### Verified state on this Deck post-fix (2026-05-12 23:25 CDT)

| Component | State | Evidence |
|-----------|-------|----------|
| Filtered rebuild | ✅ Succeeds | 28 wheels installed cleanly (PyQt6, numpy, scipy, customtkinter, pystray, openai, groq, etc.) — all manylinux prebuilt, no source builds |
| Launcher smoke test (with `PYSTRAY_BACKEND=gtk`) | ✅ `smoke OK` | matches the env systemd injects |
| `wayfinder-aura.service` | ✅ `active (running)` | main `python main.py` + persistent overlay subprocess up; no restart-counter increment |
| `/tmp/wfa-stderr.log` | ✅ clean | only the benign `WAYFINDER_LICENSE_SECRET not set` dev-mode warning |

**Still pending:** end-to-end dictation test (F3 hold → speak → R4 release → text injected into focused field). The service is up; the trigger chain needs a human keypress to validate.

### Structural takeaway (relevant to v2 priority)

This is the third distinct class of SteamOS regression observed in this log on the dev-path (alongside Issue 3-style pacman-wipe and the May-8 incident). The structural pattern is consistent: **every layer that tries to self-heal from a SteamOS update assumes it can pip-build C extensions, and SteamOS guarantees it can't.** Each new mitigation in this layered design has a non-zero chance of stepping on Issues 5/7 again unless it explicitly filters the requirements. The Flatpak path (which ships pre-built wheels inside an isolated runtime) makes this entire failure surface vanish — confirms Recommendation #1 once more.

### Optional follow-up (repo-level, not yet applied)

A cleaner permanent fix is to move `evdev` and `llama-cpp-python` out of `requirements.txt` and into either a `requirements-optional.txt` or PEP 508 markers/extras_require, so neither the install script nor the launcher needs an ad-hoc grep filter. The current local patch keeps the fix self-contained to this Deck; the repo-side cleanup is a separate decision.

---

## Recommendations for v2 — the Flatpak path is the answer

The repo already has `flatpak/io.github.user.WayfinderAura.yml`, `flatpak/BUILDING.md`, `flatpak/STRATEGY.md`, `flatpak/wayfinder-aura-launcher.sh`, plus `.desktop` and `.metainfo.xml` files. **All the design work is already done.** The gap is *building, testing, and shipping it.*

### Why Flatpak solves nearly everything in this log

| Issue | Why Flatpak fixes it |
|-------|----------------------|
| #1 glibc compat | Flatpak runtime ships its own glibc; host glibc irrelevant. |
| #5 evdev wheel | Build inside flatpak-builder sandbox where headers exist. |
| #6 Vulkan headers | Same — sandbox has vulkan-headers. |
| #7 stdint.h | Same — full glibc-headers in the sandbox. |
| #8 readonly FS | User installs Flatpak via Discover/CLI, no `/usr/` writes. |
| #9 keyring | N/A — no pacman path. |
| #10 input group | Use the freedesktop GlobalShortcuts portal instead of evdev. |
| #11 default model | Flatpak runtime can prefetch `base.en` (142 MB) at install time. |
| #12 rpath | Flatpak builds whisper.cpp itself — apply `patchelf` post-step in the manifest. |
| #13 SIGILL | Flatpak builder uses generic flags; no `-march=native` leakage. |
| #16 no Linux release binaries | Doesn't matter; manifest builds whisper.cpp from source. |

### What's left to do for v2

1. **Build the Flatpak.** `flatpak-builder` is in pacman `extra-3.7`. Run on bazzite (use existing setup) or in a Fedora container.
2. **Fix the wizard threading bug (Issue 2) and the benchmark tooltip TypeError (Issue 15)** before shipping.
3. **Smarter default model selection (Issue 11/17)** — base.en for Steam-Deck-class hardware.
4. **Drop pystray for `QSystemTrayIcon`** (Issue 3) — fewer deps, cleaner KDE rendering.
5. **Auto-spawn `ydotoold`** instead of relying on user/system service (Issue 4 + 10).
6. **Live-reload config** — file watcher or "Apply" button (Issue 14).
7. **Update README + remove or rewrite INSTALL-UBUNTU.md** to point at the Flatpak. Add `INSTALL-STEAMDECK.md` only if you want to keep a developer/contributor path.
8. **CI:** add a job that builds the Flatpak and smoke-tests it inside a Steam-Deck-glibc-2.41 container. Catches regressions like Issue 1 / Issue 13 before they hit users.

### What's left to do for the *current* dev install on this Deck (before we walk away)

- [ ] Vulkan-enabled rebuild (in progress as of this log update — image cached, ~5 min)
- [ ] Smoke-test recording → transcription → cursor-typing through the actual UI with `base.en`
- [ ] Decide whether to bring in bazzite's 5 unpushed local commits (UI/AppImage fixes), or leave them stranded
- [ ] Optionally: license key entry to switch to premium tier

---

## Priority for v2 implementation work

1. **Issue 1, 7, 13, 16** — all solved by shipping a Flatpak. Build the Flatpak. ★★★
2. **Issue 2 + Issue 15** — wizard crashes on first run. Fix before ship. ★★★
3. **Issue 11 + Issue 17** — wrong defaults make the app feel broken on Deck. Smart model picker. ★★★
4. **Issue 3** — drop pystray for QSystemTrayIcon. ★★
5. **Issue 4 + Issue 10** — auto-spawn ydotoold; sidesteps service + group. ★★
6. **Issue 12** — patchelf during Flatpak build; minor. ★
7. **Issue 14** — config live reload; nice-to-have. ★
8. **Issue 18** — INSTALL-STEAMDECK.md only useful if you want a dev path. ★
9. **Issue 19** — controller-via-Steam-Input hotkey silently broken on X11. One-block edit in `hotkeys/__init__.py` to prefer pynput over evdev on Linux. ★★
10. **Issue 20** — JACK-backed mic devices in the settings picker fail on stream open. Filter `JACK Audio Connection Kit` host API entries when listing input devices on Linux. ★★★ (this one bites every Deck user who picks the obvious "Shure MV7 Mono" entry)
11. **Issue 21** — ship gamepad-trigger setup as default for Steam Deck. GlobalShortcuts portal (already coded) + Steam Workshop controller layout + first-run wizard gated on Deck detection. ★★

---

## Update — 2026-05-31: Flatpak v2 built + Steam-Deck-Optimized polish pass (all validated on the Deck)

The `integration/flatpak-v2` branch now **builds, installs, transcribes, and runs end-to-end** on the Deck (SteamOS 3.7.25, KDE/X11). The first build closed the structural blockers (#1 glibc, #5/#6/#7 headers, #13/Crit-2 SIGILL, #16 no-Linux-binaries) — `whisper-cli base.en` runs **EXIT 0** on the Zen 2 APU (re-confirmed this build: 261 ms on 1 s audio, no SIGILL). This pass then closed the three remaining sandbox-integration regressions plus reliability hardening. Plan was Codex-reviewed (gpt-5.5, 3 rounds).

### What was fixed (all proven on-device)

1. **R4 dictation ("service not running") — the IPC socket was unreachable across the sandbox.** The proven chain (`R4 → Steam Input → Insert → KDE shortcut → trigger_record.py → Unix socket`, Issue 21) broke because the socket lived at `/tmp/wayfinder-aura.sock` and a Flatpak's `/tmp` is private. **Fix:** moved `SOCKET_PATH` to `$XDG_RUNTIME_DIR/wayfinder-aura/wayfinder-aura.sock` (single source in `config.py`; `/tmp` fallback for macOS), granted `--filesystem=xdg-run/wayfinder-aura:create` (that dir is bind-mounted host↔sandbox), `os.makedirs` before bind in both socket listeners, and made `trigger_record.py`/`trigger_style.py` try the runtime-dir path then `/tmp` so one trigger works for both the Flatpak and the from-source build. Repointed the Deck's `net.local.toggle-recording.sh.desktop` Exec at the v2 trigger (daily driver untouched; backed up). **Validated:** trigger → `IDLE→RECORDING→PROCESSING→IDLE`.

2. **No tray icon (Issue 3) — runtime ships no AppIndicator/Ayatana, so `pystray` no-ops.** **Fix:** a `QSystemTrayIcon` hosted in the **already-running PyQt6 overlay subprocess** (no new process, no new dep) — KDE renders it via `org.kde.StatusNotifierWatcher` (already a `--talk-name`). Menu actions (Show/Toggle/Reset/Quit) are sent back to the app over the same Unix socket; icon availability is reported in the overlay's ready-handshake and read live so the window-close guard can't strand the UI; the overlay quits on stdin-EOF in tray mode so no orphan tray. Gated to "pystray unavailable" to avoid a double tray on desktop Linux. **Validated:** `tray: QSystemTrayIcon created and shown`; `show` verb raised the window.

3. **"Odd UI" — was NOT a Python-version difference. Two distinct causes:**
   - **Double-scaling:** the app set its own `ui_scale` but never disabled CustomTkinter's *automatic* DPI tracker, which multiplied on top. **Fix:** `ctk.deactivate_automatic_dpi_awareness()` before the CTk root is created. Layout now fits 1280×800 cleanly.
   - **★★★ Missing glyphs (the deep one): the freedesktop runtime's `libtk8.6.so` is built WITHOUT Xft.** `ldd` shows no `libXft`/`libfontconfig`; Tk falls back to the X core "fixed" bitmap font (`tkinter.font.families()` returns **1** family; every family request resolves to `fixed`). Latin renders plainly but every symbol/emoji icon (sidebar `∿ ⚓ ✎ ◷`, headers) shows as a literal `\uXXXX` escape. No font grant or font-family change can fix this — Tk ignores fontconfig's 845 fonts entirely. **Fix:** **bundle an Xft-enabled Tcl + Tk 8.6.14** (exact version match → ABI-compatible with the runtime's `_tkinter.so`) built with `--enable-xft` in the manifest, and put `/app/lib` first on `LD_LIBRARY_PATH` (+ `TCL_LIBRARY`/`TK_LIBRARY`) in the launcher so `_tkinter` loads it. **Validated:** Tk now sees **597** families, resolves real fonts (DejaVu Sans / fontconfig substitution), and renders all glyphs. (`no-debuginfo: true` on both modules — Tcl/Tk install `.so` as mode 555, which makes flatpak-builder's `eu-strip` fail with "Permission denied".)

4. **Reliability hardening (Steam-Deck-Optimized correctness):** window-close now iconifies instead of withdrawing when no tray surface is live (never orphan the UI); the hotkey supervisor re-arms the GlobalShortcuts portal listener on its existing 10 s tick (portal wrapper clears its started-flag on any exit, incl. a normal `False` return), with **no new sub-100 ms timer**.

### Resource notes (Deck Zen 2 APU)
- **Main process idle (window minimized): ~1.7 % CPU** ✓ (hero waveform pauses when hidden).
- **Persistent overlay: ~11.7 % CPU at 15 fps** on the APU (≈2 % on a desktop Ryzen). Pre-existing 15 fps design; the tray addition is event-driven and adds none. **Recommendation:** consider a Deck-specific lower idle frame-rate (or pause-when-static) for the always-on overlay — a real battery win, but it trades against the deliberate 15 fps quality target, so it's left as a follow-up decision.

### Build/deploy mechanics that work
- Scratch tree `~/dev/wayfinder-aura-v2`; sync via `tar … | ssh deck 'tar -x -C …'` (rsync absent). Build via `flatpak run org.flatpak.Builder --user --install --force-clean --ccache` (reaches `EXIT:0`). The Flatpak's `/tmp` is sandboxed — to run a probe script inside, pipe to `flatpak run --command=python3 <app-id> -`. After an app restart, poll the socket for *accept* (not just file existence) before sending verbs.

### Still pre-existing / follow-ups
- 16 unit-test failures in `test_injector`/`test_platform`/`test_e2e_*` are stale assertions from the earlier ydotool→wtype injection refactor (they assert `ydotool`, code now returns `wtype`) — not touched by this pass; should be updated to the wtype/xdotool reality.
- Portal (in-app keyboard) hotkey still unproven on the Deck KDE — R4 rides the proven socket path, so this isn't on the critical path.


## Update — 2026-06-12: USB hotplug wedges Steam Input; overlay "stuck on Listening" root-caused

**Symptom 1: R4 dictation died the moment a USB hub (Keychron dongle + Corsair
Scimitar + Shure MV7) re-enumerated.** Both the Keychron Link dongle and the
Scimitar expose joystick HID interfaces; when they appeared, Steam Input
dropped the Deck controller from PollState 2 (active) to 1 and never
re-activated it — last line in `~/.local/share/Steam/logs/controller.txt` was
`Controller PollState Changed from 2 to 1` at the exact plug-in second. Every
mapping (R4 included) goes silent while the bridge, app, and whisper all look
healthy. **Fix: restart the Steam client.** The bridge auto-reattaches to the
recreated virtual pad.

**Symptom 2: after the stop press the overlay said "Listening..." through the
whole transcription, then flashed Processing for ~800ms.** Root cause in
`overlay.py`: the command drain gated `readline()` on `select()` against the
raw fd. `readline()` pulls a whole buffered chunk off the pipe, so lines
coalesced into one read vanish from the fd's readability while sitting
unprocessed in Python's buffer — and the sender stops the 30Hz audio-level
stream *right before* writing `show processing`, making it the last write
before seconds of pipe silence. It surfaced only when `show ready` woke the
fd after transcription. Fixed with a blocking reader thread
(`src/wayfinder/ui/stdin_reader.py`) + regression test
(`tests/test_stdin_reader.py`). Diagnosis trick for next time: correlate
`~/.cache/wayfinder-aura/overlay-debug.log` RECV epochs against `[STATE]`
lines and the journal's `Toggle sent!` entries.

**R4 chain reality check:** R4 currently reaches the app via Steam key
injection → KDE custom shortcut → trigger script → Unix socket (`Toggle
sent!` in the user journal) — NOT via the r4-f3-bridge (now committed under
`scripts/steamdeck/` as a dormant backup; bind R4 → Right Joystick Click to
use it).

**Corsair Scimitar side grid:** mappings created in iCUE as software Actions
do not fire on Linux — a 5-minute evdev capture across all five HID
interfaces saw zero side-grid events. They must be saved as Hardware
Actions / onboard-memory profile. Note the flatpak's evdev listener can't see
/dev/input anyway (sandbox grants only `--device=dri`); a side-button F3
arrives via the X11 layer exactly like keyboard F3. Granting
`--device=input` in the manifest would enable the in-app evdev grab path
("MMO mouse side-grid" exclusive grab + Detect button).

**Audio:** after a hub re-plug PipeWire may fail to recreate the Shure MV7
source and the default falls back to Internal Mic. Check
`pactl list sources short`; kick with `systemctl --user restart wireplumber`.
