# Steam Deck host-side infrastructure

Files that live OUTSIDE the flatpak on Peter's Steam Deck. They are committed
here for disaster recovery — if the Deck is wiped, this directory plus
`STEAMDECK-INSTALL-LOG.md` is everything needed to rebuild the setup.

## Install map

| Repo file | Deck location |
|---|---|
| `r4-f3-bridge.py` | `~/.local/bin/r4-f3-bridge.py` (executable) |
| `systemd/r4-f3-bridge.service` | `~/.config/systemd/user/r4-f3-bridge.service` |
| `systemd/wayfinder-aura.service` | `~/.config/systemd/user/wayfinder-aura.service` |
| `systemd/wayfinder-aura.service.d-flatpak.conf` | `~/.config/systemd/user/wayfinder-aura.service.d/flatpak.conf` |
| `systemd/wayfinder-aura-failed.service` | `~/.config/systemd/user/wayfinder-aura-failed.service` |

Enable with `systemctl --user daemon-reload && systemctl --user enable --now
wayfinder-aura.service r4-f3-bridge.service`.

## How R4 triggers dictation (two designs, both kept)

**Active path (2026-06-12):** Steam Desktop Layout binds R4 to a keyboard key →
KDE custom shortcut (`~/.config/kglobalshortcutsrc`) runs the toggle script →
Unix socket `$XDG_RUNTIME_DIR/wayfinder-aura/wayfinder-aura.sock` → app
toggles. Shows up as `Toggle sent!` in the user journal.

**Backup path (dormant):** `r4-f3-bridge.service`. Bind R4 → "Right Joystick
Click" in the Steam Desktop Layout; Steam's virtual "Microsoft X-Box 360 pad 0"
emits BTN_THUMBR; the bridge fires `xdotool key F3`. Pure evdev + XTEST — no
KDE dependency. Built when Steam's keyboard injection was broken; keep it
enabled, it idles harmlessly and logs to `/tmp/r4-f3-bridge.log`.

## Known failure mode: USB hotplug wedges Steam Input

Plugging in devices that expose joystick HID interfaces (Keychron Link dongle,
Corsair Scimitar) can knock the Deck controller from PollState 2 → 1
permanently (`~/.local/share/Steam/logs/controller.txt`); every Steam Input
mapping, R4 included, goes silent while everything downstream looks healthy.
**Fix: restart the Steam client.**
