# Shipping Aura on Linux for gamers

Plan and status for making the Linux build dependable on every desktop a
player has, not only on the machine it was developed on. Written 2026-09-28;
status is updated as items land.

## Why

Inside the Flatpak (X11 socket only) Aura typed with xdotool, which reaches
X11/XWayland windows only. Wine/Proton games and some apps are XWayland, so
dictation worked there; native Wayland apps (most KDE/GNOME apps, Firefox on
Wayland) got nothing without host-side workarounds (ydotool, input group).
X11 sessions are being retired, so Wayland has to be the primary path.

## Input backends

| Session | Typing | Enter / held keys | Game detection |
|---|---|---|---|
| KDE Plasma 6 / GNOME 45+ (Wayland) | RemoteDesktop portal (keyboard), approved once, remembered with a restore token | same portal | X11 window of the game (XWayland) |
| X11 session (KDE X11 etc.) | xdotool (XTEST) | xdotool | X11 window |
| SteamOS Game Mode (gamescope) | xdotool on gamescope's X server | xdotool | X11 window + STEAM_GAME |
| Portal missing or declined | xdotool (XWayland windows only), with a hint | xdotool | X11 window |

Wine and Proton games are XWayland windows in every session type, so game
detection (WM_CLASS exe name, `steam_app_<id>`, gamescope's `STEAM_GAME`)
behaves the same everywhere.

## Work items

1. **RemoteDesktop portal typing** (`core/portal_keyboard.py`, done
   2026-09-28): keyboard-only session, `persist_mode=2`, restore token in
   `portal-keyboard.json` next to the config, started at app launch on
   Wayland desktops so the approval dialog appears once, never mid-dictation.
   Text is typed as keysyms; Enter, Ctrl+V and Gamer mode's held keys too.
   Characters the keyboard layout has no key for (é on a US layout, emoji) are
   pasted: the Tk window owns the X11 clipboard, KWin hands it to Wayland
   apps, the portal presses Ctrl+V. Falls back to xdotool when the portal is
   missing or declined; a failure after some keys landed is never retyped.
   Settings → System → "Type into every app" turns it off (or asks again after
   a decline).
   KDE shows a "Remote Control Started" notice when the session starts and a
   "Remote Control" tray icon while Aura runs: the portal's own indicator.
2. **xdotool hardening (done 2026-09-28)**: a stale XWayland modifier bit no
   longer blocks typing; Shift is released again after every shifted run
   (a dropped release left dictations shifted: "... BUGS>").
3. **Gamer mode on Linux (done 2026-09-28)**: `core/linux_game_chat.py`,
   Games tab, Dark Age of Camelot (Eden, Lutris) typed + sent, Proton titles
   by Steam app id.
4. **Wine focus misrouting (done 2026-09-28)**: before Gamer mode types, if
   the X input focus is on another window than the active game window (Wine
   handed focus to a hidden launcher window), the game window gets it back
   (`linux_game_chat.ensure_game_focus`). The DAoC note in the Games tab names
   the permanent fix (Wine `UseTakeFocus=N`).
5. **Update banner on Linux (done 2026-09-28)**: the `.flatpak` from GitHub
   has no update channel (built without `--repo-url`), so "update via your
   software center" was wrong. Get Update now downloads the release's
   `.flatpak` (or the AppImage for AppImage installs) and the banner says to
   open the file; installing a newer bundle over a bundle install updates it
   in place (checked with a throwaway app in an isolated FLATPAK_USER_DIR).
   `.flatpak` files open in Discover, GNOME Software or (Bazzite) Warehouse.
   Real automatic updates need Flathub or a hosted repo + `--repo-url`.
6. **Release candidate**: version bump, release notes, bundle built and
   installed from the bundle, smoke-tested.

## Off-screen KWin rig

`kwin_wayland --virtual --xwayland` inside `dbus-run-session`, started under
`env -i` with private XDG config/data/cache/state/runtime dirs so the rig's
portal, permission store and shortcut state are its own (D-Bus-activated
services inherit the bus daemon's environment, not the launching script's).
GTK4 windows (Wayland and X11) record what they receive; `spectacle -b` takes
screenshots of the virtual screen. A second, restored portal session presses
keys on the first-run dialog, so Approve and Deny are testable without a
person. Results 2026-09-28 (KWin 6.7.5, xdg-desktop-portal 1.22.1): xdotool
reached no Wayland window; the portal typed exactly into both; restore token
skipped the dialog; Escape = declined; X11 clipboard pasted into a Wayland
window with é/ï/emoji intact; layout-less characters are dropped when typed.

## Test matrix

Automated: unit tests (fake D-Bus for the portal), off-screen gamescope
harness (dictation, Game Mode, cancel, stand-in game windowed / fullscreen).
Manual: a short pass on real hardware and games.

| Setup | How it is covered | Status |
|---|---|---|
| KDE Plasma 6 Wayland (desktop) | off-screen KWin rig (same KWin/portal versions) + live on Peter's machine | rig passed; live pending |
| KDE Plasma 6 Wayland (SteamOS desktop mode) | Steam machine | pending |
| GNOME 45+ Wayland | not available here: unit tests + portal spec; needs a manual pass | open |
| KDE X11 | nested X server harness (xdotool path) | covered by harness |
| SteamOS Game Mode | gamescope harness; Steam machine | pending |
| Proton game | Path of Exile 2 (Steam) | pending |
| Lutris / Wine game | Dark Age of Camelot (Eden) | typing verified 2026-09-28 |
| Native Linux game | stand-in Tk game | harness |
| Windowed / borderless / fullscreen | stand-in game in each mode; DAoC | pending |
