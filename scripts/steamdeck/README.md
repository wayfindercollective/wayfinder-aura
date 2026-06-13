# Steam Deck host-side infrastructure

Files that live OUTSIDE the flatpak on the Steam Deck. They are committed here
for disaster recovery — if the Deck is wiped, this directory plus
`STEAMDECK-INSTALL-LOG.md` is everything needed to rebuild the setup.

## Install map

| Repo file | Deck location |
|---|---|
| `wayfinder-trigger-daemon.py` | `~/.local/bin/wayfinder-trigger-daemon.py` (executable) |
| `systemd/wayfinder-trigger.service` | `~/.config/systemd/user/wayfinder-trigger.service` |
| `systemd/wayfinder-aura.service` | `~/.config/systemd/user/wayfinder-aura.service` |
| `systemd/wayfinder-aura.service.d-flatpak.conf` | `~/.config/systemd/user/wayfinder-aura.service.d/flatpak.conf` |
| `systemd/wayfinder-aura-failed.service` | `~/.config/systemd/user/wayfinder-aura-failed.service` |
| `r4-f3-bridge.py` (superseded) | `~/.local/bin/r4-f3-bridge.py` |
| `systemd/r4-f3-bridge.service` (superseded) | `~/.config/systemd/user/r4-f3-bridge.service` |

Enable with:

```sh
systemctl --user daemon-reload
systemctl --user disable --now r4-f3-bridge.service   # superseded; do not run both
systemctl --user enable  --now wayfinder-aura.service wayfinder-trigger.service
```

## How dictation gets triggered

The app itself listens for **F3** (its default record hotkey). In the Flatpak
that runs via the pynput XRecord listener on X11 (the GlobalShortcuts portal
once `dbus-python` is bundled). That covers a plain keyboard F3 in Desktop
Mode. The host-side daemon below covers everything the in-sandbox listener
can't reach.

### `wayfinder-trigger-daemon.py` — the primary, mode-agnostic path

A host-side evdev daemon (the Flatpak sandbox can't read `/dev/input`; the host
can) that talks straight to the app's Unix socket
`$XDG_RUNTIME_DIR/wayfinder-aura/wayfinder-aura.sock`. **No X11/Wayland
involvement at all**, which is the whole point: it works identically in Desktop
Mode and Game Mode. Logs to `/tmp/wayfinder-trigger.log`.

It watches two devices:

* **Corsair Scimitar "Keyboard" HID interface** — *grabbed exclusively*. The
  side grid is programmed (iCUE Hardware Actions, onboard memory) to emit
  F3/F2. The grab means those presses reach ONLY the daemon: they can't
  double-fire with the app's own F3 listener in Desktop Mode, and they can't
  leak F3/F2 into whatever window has focus.
    * `F3 → socket "toggle"` (start/stop dictation)
    * `F2 → socket "style"` (cycle dictation style)
    * any other side-grid key is logged and swallowed.
* **Steam virtual "Microsoft X-Box 360 pad 0"** — *not grabbed* (Steam/games
  own it). `BTN_THUMBR` (right-stick click) `→ "toggle"`. Dormant unless the
  Steam Desktop Layout binds R4 → Right Joystick Click.

Survives USB hotplug: on `device lost` it rescans every 3s and re-grabs when
the device returns (validated across the Deck's flaky hub dropping mid-session
and an overnight suspend — the daemon reattached and re-grabbed each time).

### Why a host daemon and not just an X listener (Game Mode)

Game Mode is gamescope, not KDE/X11. gamescope routes real input on the
Wayland side; an XRecord/pynput listener on its Xwayland sees **nothing**
(verified 2026-06-12 in a nested gamescope: identical pynput listener + key
injection that works on `:0` sees zero events under gamescope). XTEST
*injection* into a client still works, but *listening* does not — so any
trigger that depends on catching a global keypress in X is dead in Game Mode.
The socket, reached via host evdev, is the only path that works in every
session type.

### Superseded: KDE custom shortcut + `r4-f3-bridge.py`

Earlier designs, kept in the repo for history, **disabled** in favour of the
daemon:

* **KDE custom shortcut** (`~/.config/kglobalshortcutsrc`) ran a trigger script
  on an injected key → socket. KDE-only, so dead in Game Mode.
* **`r4-f3-bridge.py`** caught `BTN_THUMBR` and fired `xdotool key F3` — i.e. it
  depended on an X listener downstream, so it too died wherever X listening
  dies. The daemon's pad watch replaces it, writing to the socket directly.
  Do not run `r4-f3-bridge.service` alongside `wayfinder-trigger.service`
  (the unit declares `Conflicts=` to enforce this).

## Known failure mode: USB hotplug wedges Steam Input

Plugging in devices that expose joystick HID interfaces (Keychron Link dongle,
Corsair Scimitar) can knock the Deck controller from PollState 2 → 1
permanently (`~/.local/share/Steam/logs/controller.txt`); every Steam Input
mapping, R4 included, goes silent while everything downstream looks healthy.
**Fix: restart the Steam client.** (The Scimitar F3/F2 path does not depend on
Steam Input at all, so it keeps working through this.)

## Known behaviour: whisper-server is cold after suspend

The warm whisper-server does not survive a long suspend/overnight. The app's
fallback re-launches `whisper-server-cpu` on the next press — a ~0.5s one-time
delay on the first post-suspend dictation, then warm again. Graceful, not a
crash; noted so it isn't mistaken for a regression.
