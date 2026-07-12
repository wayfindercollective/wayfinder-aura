# Support

Thanks for using Wayfinder Aura. This page explains how to get help, what to
try first, and exactly what to include when you report a bug.

## Where to file issues

Bug reports and feature requests go to the GitHub issue tracker:

**<https://github.com/wayfindercollective/wayfinder-aura/issues>**

Please search existing issues first — your question may already be answered.

## What to include in a bug report

A good report gets a fast fix. Please include:

1. What you did, what you expected, and what actually happened.
2. Your platform: Linux distro + desktop (KDE/GNOME), or **Steam Deck**
   (Desktop Mode vs Game Mode).
3. How you installed it: Flatpak bundle, Flathub, or from source.
4. The relevant **logs** (below).

### Logs to attach

**All installs — the app's own diagnostic log:**

```
~/.cache/wayfinder-aura/activity.log
```

> Note: this log may contain snippets of your transcribed text. Skim it and
> redact anything private before attaching it to a public issue.

**Steam Deck (systemd user services)** — the host-side services log here:

```
# The dictation app itself
journalctl --user -u wayfinder-aura.service --no-pager -n 200

# The host-side trigger daemon (back button / side-grid keys)
/tmp/wayfinder-trigger.log

# The Game Mode / Desktop Mode lifecycle supervisor
/tmp/wayfinder-mode-supervisor.log
```

The app service also mirrors stdout/stderr to `/tmp/wfa-stdout.log` and
`/tmp/wfa-stderr.log`, which are worth attaching if the app crashes on launch.

To grab the trigger/supervisor logs and the recent journal in one shot:

```sh
journalctl --user -u wayfinder-aura.service --no-pager -n 200 > ~/wfa-journal.txt
cp /tmp/wayfinder-trigger.log /tmp/wayfinder-mode-supervisor.log ~/  2>/dev/null
```

## Troubleshooting checklist

Try these before filing an issue — they cover the most common problems.

### "No audio detected" after recording

- Your mic is muted, or the wrong input device is selected.
- Open **Settings → Audio** and pick your microphone, or choose
  **Auto-detect**.
- The app stores your mic **by name**, so it survives PipeWire device
  renumbering. If you recently changed audio hardware, re-select it.

### Hotkey does nothing

- **On Wayland (desktop):** the app uses the GlobalShortcuts portal. Approve
  the shortcut prompt from your desktop, or bind it in **System Settings →
  Shortcuts**. The default hotkeys are **Super+F2** (start/stop) and
  **Super+F3** (cycle style).
- **"No input devices found" (from-source installs):** add yourself to the
  `input` group, then log out and back in:
  ```sh
  sudo usermod -aG input $USER
  ```
- **On the Steam Deck:** dictation is triggered by a host-side daemon
  (`wayfinder-trigger-daemon.py`) that talks to the app over a Unix socket, so
  it works in both Desktop Mode and Game Mode. If the button stops firing:
  - Check the daemon is running:
    `systemctl --user status wayfinder-trigger.service`
    and read `/tmp/wayfinder-trigger.log`.
  - **USB hotplug can wedge Steam Input.** Plugging in devices that expose a
    joystick HID interface (e.g. a Keychron Link dongle or Corsair Scimitar)
    can knock the Deck controller from PollState 2 → 1, silencing every Steam
    Input mapping (including a back button bound to dictation). **Fix: restart
    the Steam client.** (The Scimitar side-grid F3/F2 path does not depend on
    Steam Input, so it keeps working through this.)

### First dictation after sleep is slow

- The warm whisper-server does not survive a long suspend/overnight. On the
  next press the app re-launches its CPU whisper server — a one-time ~0.5s
  delay on that first post-suspend dictation, then it's warm again. This is
  expected, not a crash.

### Transcription is slow / GPU not being used

- Wayfinder Aura uses Vulkan GPU acceleration where available and
  **automatically falls back to CPU** when GPU inference isn't available.
  Dictation still works on CPU — it's just slower.
- A GPU→CPU fallback (and the reason) is recorded in
  `~/.cache/wayfinder-aura/activity.log`. Check there if transcription is
  unexpectedly slow.
- GPU acceleration and the large models are Ultra features; the free tier
  runs the standard models on CPU.

### UI is too small (e.g. 4K)

- Press **Ctrl + Plus**, or open **Settings → UI Scale**.

### Text typed into the wrong place (or nowhere) on Wayland

- **Wayland cannot retarget a window.** ydotool/wtype type into whatever
  surface currently has keyboard focus. Aura cannot “click back” into the
  field you were in when recording started (that only works with X11 + xdotool).
- **Keep focus in the text field** while you dictate and until text appears.
- Aura’s overlay uses a **soft update** path so leaving “Listening…” does not
  recycle the pill/tray every dictation (that used to steal KDE focus).
- If you still see systematic focus loss, check `activity.log` for
  `focus drifted` and `Overlay deferred restart`. Optional advanced config
  `desktop_paste_on_focus_drift` (default **off**) pastes via clipboard when
  drift is detected — it can still paste into the wrong app if you switched
  windows on purpose.

### Overlay stuck on “Processing…” / tray frozen

- Use **tray → Reset (unstick overlay)** or the reset hotkey path.
- Missed overlay acks defer a restart until you are idle (never mid-paste).
- Attach `~/.cache/wayfinder-aura/activity.log` and
  `~/.cache/wayfinder-aura/overlay-debug.log` when reporting.

## Steam Deck setup and recovery

The Steam Deck host-side setup (systemd user services + the trigger daemon) is
documented in `scripts/steamdeck/README.md`, and can be installed/removed with
`scripts/steamdeck/install-steamdeck.sh` and
`scripts/steamdeck/uninstall-steamdeck.sh`.

## More documentation

- `README.md` — overview, install, quick start, troubleshooting.
- `PRIVACY.md` — what data the app handles and where it goes.
- `scripts/steamdeck/README.md` — Steam Deck host-side architecture.
