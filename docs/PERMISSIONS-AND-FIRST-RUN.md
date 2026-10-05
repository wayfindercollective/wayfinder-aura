# Permissions and first run, per platform and distro

Audit of 2026-10-04 (branch `sched/7860e492-linux-ship-audit`). What Aura asks
the system for, when, what the user sees, what a decline does and how to
recover, for each package and desktop. "Verified" means read in code and
covered by a test or checked on the KDE Plasma 6.7 desktop; "unverified" needs
a live machine (see the checklist at the end).

## Linux: what is asked, per package and desktop

| | Flatpak, KDE Plasma (Wayland) | Flatpak, GNOME 48+ | Flatpak, GNOME 45–47, Sway | Flatpak, X11 (Cinnamon, XFCE, MATE, KDE X11) | AppImage / source, Wayland | AppImage / source, X11 | SteamOS Game Mode |
|---|---|---|---|---|---|---|---|
| **Typing into every app** (RemoteDesktop portal) | one dialog ("Control input devices", KDE: Remote Control), then a saved token; verified | one dialog expected; unverified | GNOME 45–47: one dialog expected, unverified. Sway: not offered, xdotool (X11 windows only) | not asked; xdotool | one dialog; whether the token persists for a non-Flatpak app is unverified | not asked; xdotool (host), ydotool/wtype fallbacks | not asked; xdotool on gamescope |
| **Global shortcut** | GlobalShortcuts portal; the desktop owns the binding | GlobalShortcuts portal (bind dialog expected, unverified) | no portal support: Aura says to bind a desktop shortcut to `flatpak run io.wayfindercollective.WayfinderAura --toggle` | GlobalShortcuts on KDE X11; elsewhere the X11 listener (XRecord), no prompt | evdev; needs the `input` group (log out and in once) | evdev with the `input` group, else the X11 listener, no group needed | Steam trigger service |
| **Microphone** | no prompt (PulseAudio/PipeWire socket) | no prompt | no prompt | no prompt | no prompt | no prompt | no prompt |
| **Root / password** | never | never | never | never | AppImage: never. Source Setup: polkit for distro packages only where needed (ydotool is skipped while the portal types) | same | never |

Files: config, license, restore token (`portal-keyboard.json`) and logs are
owner-only (0600); the runtime dir is 0700 and its socket 0600. There is no
autostart on Linux except the Steam Deck service.

## First run, in order

**Flatpak** (Setup checks run, then the tour):
1. At launch the global-shortcut binding is requested (KDE, GNOME 48+). On
   desktops without the portal Aura picks the X11 listener (X11) or logs the
   `--toggle` advice (Wayland) instead of retrying a portal that cannot work.
2. About a second later the desktop asks whether Aura may control input
   devices (Wayland KDE/GNOME). Both dialogs can be on screen together; nothing
   in the tour announces them yet (open item).
3. Setup finds everything bundled (engines, Base model) and hands over to the
   tour: mic test, shortcut, a test dictation into the card.

**AppImage** (no Setup, tour only):
1. Adds a menu entry. On Wayland the typing dialog appears as above.
2. Tour. The AppImage carries no speech model, so the tour's first step after
   the mic test downloads the free Base model (as on macOS and Windows); a user
   who skipped the tour gets the model panel at the next launch.
3. Shortcut: evdev with the `input` group; on X11 without it, the X11 listener.

**Source**: Setup checks packages (per-distro commands, rpm-ostree on
Bazzite/Silverblue, zypper on openSUSE), builds whisper.cpp, then the tour.

## Declines and recovery

| What happened | What Aura shows | How to recover |
|---|---|---|
| Typing dialog declined (Esc counts) | Dictate-tab banner; the "Type into every app" switch turns off; typing reaches X11 apps only | Settings → System → Type into every app: turn it on, the desktop asks again |
| Typing session failed or ended by the desktop | Dictate-tab banner | Turn the switch off and on |
| Portal stops taking keys mid-session | log line, then the normal tools (ydotool/wtype/xdotool) type | Turn the switch off and on |
| No global-shortcut portal (Wayland) | activity log names the `--toggle` command | bind a desktop shortcut to it |
| No `input` group (AppImage/source, Wayland) | activity log | `usermod -aG input $USER` as root, log out and in |
| GameMode game running (AppImage/source) | single-key shortcuts pause, first ignored press logged; Ctrl/Alt shortcuts keep working with Gamer mode on | use a Ctrl/Alt shortcut (the default) or the tray |

## Distros

| Distro | Flatpak | AppImage | Source |
|---|---|---|---|
| Fedora (GNOME, KDE) | works; two desktop approvals | `input` group on Wayland; download Base | `dnf install python3-tkinter`; Setup per-distro |
| Bazzite, Silverblue, Kinoite | primary path | `input` group on Wayland | `rpm-ostree install python3-tkinter` + reboot, or use the Flatpak |
| Ubuntu 22.04/24.04, Debian 12 | install `flatpak` first; GNOME 46 has no global-shortcut portal: bind `--toggle` | `input` group; no usable ydotool (0.1.8) | PEP 668: use a venv; `python3-tk` |
| Debian 13 (GNOME 48) | install `flatpak` first | as above | as above |
| Mint (Cinnamon X11), Pop!_OS 22.04 | Flatpak preinstalled; X11 listener for the shortcut | nothing extra on X11 | venv, `python3-tk` |
| Arch, EndeavourOS, CachyOS | `pacman -S flatpak` | `input` group on Wayland | `pacman -S tk` |
| openSUSE Tumbleweed | `zypper in flatpak` | as above | `zypper install python3-tk` |
| SteamOS (Deck) | rootless installer script; Game Mode uses the Steam trigger service | not recommended | unsupported |

## macOS and Windows (same branch, separate releases)

macOS asks for Microphone, Accessibility and Input Monitoring through the
Welcome checklist on a true first run. Fixed here: a `~/Applications` install
counts as installed, and a failed Keychain read can no longer delete stored
cloud API keys. Open (needs a Mac): returning users get bare system prompts at
launch before any window; the tour's first card does not check the install
location before asking; the shortcut step has no way forward while a
permission is missing; the hotkey does not recover after a late Accessibility
grant; whether Input Monitoring is needed at all.

Windows asks for nothing at install (per-user, no UAC). Fixed here: a failed
paste leaves the dictation on the clipboard. Open: the installer is unsigned
(Smart App Control blocks it; a public-release blocker), a blocked Windows
microphone dead-ends the tour, elevated (admin) target windows are not
detected before pasting.

## Open items

1. Sequence the two Flatpak dialogs and announce them in the tour (one line
   before they fire, start the typing session after the shortcut bind).
   Decided 2026-10-04: ships as a known issue in the 1.2.0 betas.
2. AppImage: register a host app id with the portal (`org.freedesktop.host.portal.Registry`)
   and confirm the restore token survives a restart.
3. Retry a typing session that failed to start (not only the probe).
4. Steam Deck installer: fail with a clear message when `/dev/input` is not
   readable; make the Scimitar grab and the R3 trigger opt-in.

## Live checks still needed

- GNOME 48+: both dialogs, typing into a GTK4 Wayland app, the token on restart.
- GNOME 46 (Ubuntu 24.04): the log names `--toggle`; a desktop shortcut running it records.
- Cinnamon or XFCE (X11) Flatpak: the shortcut works through the X11 listener.
- AppImage on KDE Wayland: typing dialog once, token after restart; X11 AppImage
  without the `input` group: the shortcut works.
- Declining the typing dialog shows the banner and turns the switch off;
  turning it on asks again.
