# Steam Deck host-side infrastructure

Files that live OUTSIDE the flatpak on the Steam Deck. They are committed here
for disaster recovery — if the Deck is wiped, this directory plus
`STEAMDECK-INSTALL-LOG.md` is everything needed to rebuild the setup.

## Install map

| Repo file | Deck location |
|---|---|
| `wayfinder-trigger-daemon.py` | `~/.local/bin/wayfinder-trigger-daemon.py` (executable) |
| `wayfinder-mode-supervisor.py` | `~/.local/bin/wayfinder-mode-supervisor.py` (executable) |
| `wayfinder-aura-show-or-start.py` | `~/.local/bin/wayfinder-aura-show-or-start` (executable) |
| `wayfinder-aura.desktop` | `~/.local/share/applications/wayfinder-aura.desktop` |
| `systemd/wayfinder-trigger.service` | `~/.config/systemd/user/wayfinder-trigger.service` |
| `systemd/wayfinder-mode-supervisor.service` | `~/.config/systemd/user/wayfinder-mode-supervisor.service` |
| `systemd/wayfinder-aura.service` | `~/.config/systemd/user/wayfinder-aura.service` |
| `systemd/wayfinder-aura.service.d-flatpak.conf` | `~/.config/systemd/user/wayfinder-aura.service.d/flatpak.conf` |
| `systemd/wayfinder-aura-failed.service` | `~/.config/systemd/user/wayfinder-aura-failed.service` |
| `r4-f3-bridge.py` (historical only; not installed) | `~/.local/bin/r4-f3-bridge.py` |
| `systemd/r4-f3-bridge.service` (historical only; disabled) | `~/.config/systemd/user/r4-f3-bridge.service` |

Enable with:

```sh
systemctl --user daemon-reload
systemctl --user disable --now r4-f3-bridge.service   # superseded; do not run both
systemctl --user enable  --now wayfinder-aura.service wayfinder-trigger.service \
                                wayfinder-mode-supervisor.service
```

The desktop entry uses the one-shot `wayfinder-aura-show-or-start` helper. A click sends
`show` to the existing app socket and returns immediately; only a genuinely stopped or
unhealthy app touches systemd/Flatpak. The helper exits after the click and is not a daemon.

## How dictation gets triggered

Fresh app installs use **Super+F2** for record toggle and **Super+F3** for
style cycle. Existing legacy configs may still keep bare F3/F10, but the Steam
Deck path should not depend on any in-sandbox keyboard listener. Controller
and side-grid triggers go through the host-side daemon below, which sends
commands directly to the app's Unix socket.

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
  double-fire with the app's own hotkey listener in Desktop Mode, and they can't
  leak F3/F2 into whatever window has focus.
    * `F3 → socket "toggle"` (start/stop dictation)
    * `F2 → socket "style"` (cycle dictation style)
    * any other side-grid key is logged and swallowed.
* **Steam virtual "Microsoft X-Box 360 pad 0"** — *not grabbed* (Steam/games
  own it). `BTN_THUMBR` (right-stick click) `→ "toggle"`. Dormant unless the
  Steam Desktop Layout binds R4 → Right Joystick Click.

Designed to survive USB hotplug: on `device lost` it rescans every 3s and
re-grabs when the device returns. Final release signoff should still include a
fresh Deck hotplug/suspend check after packaging changes.

## Game Mode lifecycle

### `wayfinder-mode-supervisor.py` — start/stop the app per SteamOS mode

By default you don't want the dictation app eating RAM while you game: its
warm whisper/llama servers hold a meaningful chunk of the Deck's 16 GB. But
some users keybind dictation onto a controller and *do* want it live in Game
Mode. A host-side `systemd --user` daemon reconciles this every 5s. Logs to
`/tmp/wayfinder-mode-supervisor.log`.

* **Mode detection** is self-contained — it reads
  `systemctl --user is-active gamescope-session.service` (active → Game Mode,
  a clean inactive/failed → Desktop), with an exact-match `ps -eo comm`
  scan for `gamescope` as a fallback. It does **not** depend on the
  compositor, which swaps out underneath it on every Desktop↔Game transition.
* **A user toggle** at
  `~/.var/app/io.wayfindercollective.WayfinderAura/config/wayfinder-aura/game-mode-dictation`
  decides the Game-Mode behaviour: file content `1` = keep dictation on in Game
  Mode; anything else (missing/empty/`0`) = off. The supervisor still reads the
  legacy `~/.config/wayfinder-aura/game-mode-dictation` path as a fallback.
* **Lifecycle:**
    * **OFF in Game Mode** → the supervisor `stop`s `wayfinder-aura.service`,
      freeing the RAM its servers held.
    * **ON in Game Mode** → it keeps the app running.
    * **Desktop Mode (or an indeterminate read)** → it *always* keeps the app
      running. It is fail-safe by design: it never stops the app outside a
      confirmed Game-Mode-with-toggle-off state.
    * On a **mode transition** (e.g. Game → Desktop) while the app should stay
      up, it `restart`s `wayfinder-aura.service` so the app re-reads its mode.
* **Mode marker for the app:** every poll it atomically writes `game` or
  `desktop` to `$XDG_RUNTIME_DIR/wayfinder-aura/mode` (an indeterminate mode
  is published as `desktop`), so the app can read which mode it is in without
  doing its own compositor sniffing.

Safety properties (covered by `tests/test_mode_supervisor.py`): it never stops
the app unless mode is genuinely Game **and** the toggle is off; the first poll
seeds the mode and never counts as a transition (so it won't bounce an
already-correct app on supervisor startup); and a 3s min-interval guard
suppresses rapid start/stop churn.

The unit is `WantedBy=default.target` (**not** `graphical-session.target`) and
declares `Before=wayfinder-aura.service` with **no** `PartOf`/`Requires`/
`BindsTo` on the unit it controls — binding the supervisor to the app it
starts and stops would create a dependency cycle.

### Trigger daemon rebound to `default.target`

The trigger daemon was previously `PartOf=`/`WantedBy=graphical-session.target`,
which dies when the Game-Mode compositor replaces the Desktop one. Both the
trigger daemon and the mode supervisor are now `WantedBy=default.target` so
they survive the Desktop↔Game compositor swap and keep running in Game Mode —
exactly where the in-sandbox X listener can't reach.

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
  (both units declare `Conflicts=` to enforce this). The script is disabled by
  default and exits unless `WAYFINDER_ALLOW_LEGACY_R4_F3=1` is set explicitly.

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
