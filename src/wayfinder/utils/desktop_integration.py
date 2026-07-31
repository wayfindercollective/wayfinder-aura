"""AppImage desktop integration — make the downloaded file feel installed.

AppImages don't self-install: without help there is no app-menu entry and no
icon (2026-07 launch feedback: "I don't see an icon or install"). On AppImage
runs this module writes/refreshes the user-level desktop entry and icon so the
app shows up in menus/launchers, pointing at the CURRENT AppImage path — and
re-points it when the user moves or re-downloads the file.

Flatpak and distro installs integrate through their own packaging; this module
is a no-op for them.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

APP_ID = "io.wayfindercollective.WayfinderAura"


def _xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")))


def _desktop_entry(appimage: str) -> str:
    exec_path = appimage.replace('"', '\\"')
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Wayfinder Aura\n"
        "GenericName=Voice Dictation\n"
        "Comment=Local-first voice dictation — speak, text appears at your cursor\n"
        f'Exec="{exec_path}"\n'
        f"Icon={APP_ID}\n"
        "Terminal=false\n"
        "Categories=Utility;Accessibility;\n"
        "Keywords=voice;dictation;speech;whisper;transcription;\n"
        "StartupNotify=true\n"
        f"StartupWMClass=Wayfinder Aura\n"
        "X-AppImage-Integrated=true\n"
    )


def integrate_appimage(log=print) -> bool:
    """Write/refresh the menu entry + icon for the running AppImage.

    Idempotent: rewrites only when the stored Exec no longer matches the
    current $APPIMAGE (moved/re-downloaded file). Returns True when an
    entry exists and is current after the call. Best-effort throughout —
    integration failures must never affect the app itself.
    """
    appimage = os.environ.get("APPIMAGE", "")
    appdir = os.environ.get("APPDIR", "")
    if not appimage or not appdir or not Path(appimage).is_file():
        return False  # not an AppImage run (or an odd runtime) — nothing to do

    try:
        data = _xdg_data_home()
        desktop_dir = data / "applications"
        icon_dir = data / "icons" / "hicolor" / "256x256" / "apps"
        desktop_path = desktop_dir / f"{APP_ID}.desktop"

        content = _desktop_entry(appimage)
        if desktop_path.exists() and desktop_path.read_text(encoding="utf-8") == content:
            return True  # already integrated and current

        desktop_dir.mkdir(parents=True, exist_ok=True)
        icon_dir.mkdir(parents=True, exist_ok=True)

        bundled_icon = Path(appdir) / f"{APP_ID}.png"
        if bundled_icon.exists():
            shutil.copyfile(bundled_icon, icon_dir / f"{APP_ID}.png")

        desktop_path.write_text(content, encoding="utf-8")

        # Refresh caches so the entry appears without a relog (best-effort).
        # Host binaries — scrub the bundle's library path or they may not run.
        from wayfinder.utils.hostexec import host_env
        env = host_env()
        for cmd in (
            ["update-desktop-database", str(desktop_dir)],
            ["gtk-update-icon-cache", "-q", str(data / "icons" / "hicolor")],
        ):
            if shutil.which(cmd[0]):
                try:
                    subprocess.run(cmd, capture_output=True, timeout=10, env=env)
                except Exception:
                    pass

        log(f"🖥️ Added Wayfinder Aura to your applications menu ({desktop_path.name})")
        return True
    except Exception as e:
        try:
            log(f"⚠ Desktop integration skipped: {e}")
        except Exception:
            pass
        return False
