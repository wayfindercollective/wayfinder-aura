"""Install an app update in place: download, verify, swap, relaunch.

The update banner's button used to open the release download in the browser,
leaving the user to drag a DMG into Applications or run an installer. Here the
app does it itself, where it safely can:

- macOS: the DMG is mounted read-only, and its app must carry the running
  app's bundle ID and Developer ID team and pass Gatekeeper (notarized) before
  it is staged next to the installed app. A helper waits for this process to
  exit, swaps the bundles and opens the new one. The swap never happens while
  Aura runs: a frozen app reads modules from its own bundle lazily, so a
  bundle replaced under a running process could mix two versions. Same team
  and bundle ID, so macOS keeps the Microphone/Accessibility/Input Monitoring
  grants.
- Windows: the per-user Setup exe runs silently (no UAC: PrivilegesRequired=
  lowest); it closes Aura itself and, with /relaunch=1, starts the new one.
- Linux AppImage: the new AppImage replaces the running file once it exits,
  keeping its path (desktop entries and launchers keep working).

Everything else falls back to the old behaviour, so the user is never stuck:
a Flatpak (no permission to install bundles from the sandbox), a Mac app the
user cannot write over (non-admin account, app quarantined/translocated), an
ad-hoc signed or source build, or any failed check. ``prepare_update`` never
raises: it returns an Outcome the UI turns into a sentence.

Only downloads from this repository's GitHub releases are accepted, the same
rule app_updates applies when it picks the download.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

DOWNLOAD_PREFIX = "https://github.com/wayfindercollective/wayfinder-aura/releases/download/"
MAX_DOWNLOAD_BYTES = 1_500_000_000
_CHUNK = 1 << 20
APP_BUNDLE_NAME = "Wayfinder Aura.app"


@dataclass
class Outcome:
    """What prepare_update achieved.

    ``ready``: the update is staged; call ``finish()`` and quit right away.
    ``fallback``: hand the user ``fallback_path`` (a downloaded DMG/installer)
    or, when that is empty, the download URL, as before.
    """

    ready: bool = False
    message: str = ""
    fallback_path: str = ""
    finish: Callable[[], None] | None = field(default=None, repr=False)


Progress = Callable[[float], None] | None


# --- what this install can do ---------------------------------------------------

def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def mac_bundle_path() -> Path | None:
    """The running .app bundle, or None for a source run."""
    if sys.platform != "darwin" or not _frozen():
        return None
    for parent in Path(sys.executable).resolve().parents:
        if parent.suffix == ".app":
            return parent
    return None


def appimage_path() -> Path | None:
    from ..utils.platform import is_appimage

    if not sys.platform.startswith("linux") or not is_appimage():
        return None
    path = os.environ.get("APPIMAGE", "")
    return Path(path) if path and os.path.isfile(path) else None


_in_place_failed = False  # the last swap failed: hand over downloads this launch


def _failure_marker() -> Path:
    from ..utils.platform import get_cache_dir

    return get_cache_dir() / "update-install-failed"


def install_mode() -> str | None:
    """"mac", "windows" or "appimage" when an in-place install is possible."""
    if _in_place_failed:
        return None
    if sys.platform == "darwin":
        bundle = mac_bundle_path()
        if bundle is None or "/AppTranslocation/" in str(bundle):
            return None
        return "mac" if os.access(bundle.parent, os.W_OK) else None
    if sys.platform == "win32":
        return "windows" if _frozen() else None
    target = appimage_path()
    if target is not None and os.access(target.parent, os.W_OK):
        return "appimage"
    return None


# --- download -----------------------------------------------------------------------

def _updates_dir() -> Path:
    from ..utils.platform import get_cache_dir

    path = get_cache_dir() / "updates"
    path.mkdir(parents=True, exist_ok=True)
    for old in path.iterdir():  # one update at a time; drop leftovers
        if old.is_file():
            try:
                old.unlink()
            except OSError:
                pass
    return path


def download(url: str, dest_dir: Path, progress: Progress = None) -> Path:
    """Stream ``url`` (this repo's release asset) to dest_dir; returns the file."""
    if not url.startswith(DOWNLOAD_PREFIX):
        raise ValueError("not a Wayfinder Aura release download")
    name = url.rsplit("/", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ValueError("unexpected download file name")
    import requests

    partial = dest_dir / (name + ".part")
    with requests.get(url, stream=True, timeout=30) as response:
        response.raise_for_status()
        total = int(response.headers.get("Content-Length") or 0)
        if total > MAX_DOWNLOAD_BYTES:
            raise ValueError("download is unexpectedly large")
        received = 0
        with open(partial, "wb") as fh:
            for chunk in response.iter_content(_CHUNK):
                received += len(chunk)
                if received > MAX_DOWNLOAD_BYTES:
                    raise ValueError("download is unexpectedly large")
                fh.write(chunk)
                if progress and total:
                    progress(min(received / total, 1.0))
    if total and received != total:
        raise OSError(f"download incomplete ({received} of {total} bytes)")
    final = dest_dir / name
    os.replace(partial, final)
    return final


# --- macOS ---------------------------------------------------------------------------

def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def signing_identity(app: Path) -> tuple:
    """(bundle identifier, team ID) from codesign, ('', '') when unsigned."""
    info = _run(["/usr/bin/codesign", "-dv", "--verbose=2", str(app)])
    text = info.stderr + info.stdout
    ident = re.search(r"^Identifier=(.+)$", text, re.MULTILINE)
    team = re.search(r"^TeamIdentifier=(.+)$", text, re.MULTILINE)
    team_id = team[1].strip() if team else ""
    return (ident[1].strip() if ident else "",
            "" if team_id == "not set" else team_id)


def verify_mac_app(candidate: Path, running: Path) -> str | None:
    """None when ``candidate`` may replace ``running``, else the reason."""
    want_id, want_team = signing_identity(running)
    if not want_team:
        return "this copy of Aura is not Developer ID signed"
    strict = _run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(candidate)])
    if strict.returncode != 0:
        return "the update's signature does not verify"
    got_id, got_team = signing_identity(candidate)
    if (got_id, got_team) != (want_id, want_team):
        return "the update is signed by someone else"
    gatekeeper = _run(["/usr/sbin/spctl", "--assess", "--type", "execute", str(candidate)])
    if gatekeeper.returncode != 0:
        return "macOS did not accept the update (not notarized)"
    return None


def _mac_helper_script() -> str:
    # $1 pid, $2 installed bundle, $3 staged bundle, $4 backup path, $5 opener,
    # $6 failure marker. Waits for Aura to exit, swaps, opens the new copy; on
    # any failure (e.g. macOS refusing to modify the bundle) puts the old one
    # back, leaves the marker so the next launch hands over the download
    # instead of retrying, and opens the old copy: the user always gets an app.
    return (
        'while kill -0 "$1" 2>/dev/null; do sleep 0.2; done; '
        'rm -rf "$4"; '
        'if mv "$2" "$4" && mv "$3" "$2"; then '
        'rm -rf "$4"; '
        'else [ -e "$2" ] || mv "$4" "$2"; rm -rf "$3"; : > "$6"; fi; '
        'exec "$5" "$2"'
    )


def _prepare_mac(url: str, progress: Progress) -> Outcome:
    running = mac_bundle_path()
    dmg = download(url, _updates_dir(), progress)
    mount = Path(tempfile.mkdtemp(prefix="aura-update-"))
    staged = running.parent / f".{running.name}.update"
    try:
        attach = _run(["/usr/bin/hdiutil", "attach", "-nobrowse", "-readonly",
                       "-noautoopen", "-mountpoint", str(mount), str(dmg)], timeout=300)
        if attach.returncode != 0:
            return Outcome(message="Couldn't open the update.", fallback_path=str(dmg))
        try:
            candidate = mount / APP_BUNDLE_NAME
            if not candidate.is_dir():
                return Outcome(message="The update has no app inside.", fallback_path=str(dmg))
            problem = verify_mac_app(candidate, running)
            if problem:
                return Outcome(message=f"Not installed: {problem}.", fallback_path=str(dmg))
            if staged.exists():
                shutil.rmtree(staged)
            copy = _run(["/usr/bin/ditto", str(candidate), str(staged)], timeout=600)
            if copy.returncode != 0 or verify_mac_app(staged, running):
                shutil.rmtree(staged, ignore_errors=True)
                return Outcome(message="Couldn't copy the update.", fallback_path=str(dmg))
        finally:
            _run(["/usr/bin/hdiutil", "detach", str(mount), "-force"], timeout=60)
    finally:
        shutil.rmtree(mount, ignore_errors=True)

    backup = running.parent / f".{running.name}.previous"

    def finish() -> None:
        subprocess.Popen(
            ["/bin/sh", "-c", _mac_helper_script(), "wayfinder-update",
             str(os.getpid()), str(running), str(staged), str(backup), "/usr/bin/open",
             str(_failure_marker())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        try:
            dmg.unlink()
        except OSError:
            pass

    return Outcome(ready=True, message="Update ready: restarting Aura.", finish=finish)


# --- Windows -------------------------------------------------------------------------

SILENT_INSTALL_ARGS = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/relaunch=1"]


def _prepare_windows(url: str, progress: Progress) -> Outcome:
    installer = download(url, _updates_dir(), progress)

    def finish() -> None:
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([str(installer), *SILENT_INSTALL_ARGS],
                         creationflags=flags, close_fds=True)

    return Outcome(ready=True, message="Installing the update: Aura will restart.",
                   finish=finish)


# --- Linux AppImage ----------------------------------------------------------------

def _appimage_helper_script() -> str:
    # $1 pid, $2 running AppImage, $3 staged AppImage, $4 failure marker.
    return ('while kill -0 "$1" 2>/dev/null; do sleep 0.2; done; '
            'mv -f "$3" "$2" || { rm -f "$3"; : > "$4"; }; exec "$2"')


def _prepare_appimage(url: str, progress: Progress) -> Outcome:
    target = appimage_path()
    downloaded = download(url, _updates_dir(), progress)
    staged = target.parent / f".{target.name}.update"
    shutil.move(str(downloaded), staged)
    staged.chmod(0o755)
    with open(staged, "rb") as fh:
        head = fh.read(12)
    if head[:4] != b"\x7fELF" or head[8:11] != b"AI\x02":  # type-2 AppImage magic
        staged.unlink()
        return Outcome(message="The download is not an AppImage.")

    def finish() -> None:
        from ..utils.hostexec import host_env

        subprocess.Popen(
            ["/bin/sh", "-c", _appimage_helper_script(), "wayfinder-update",
             str(os.getpid()), str(target), str(staged), str(_failure_marker())],
            env=host_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )

    return Outcome(ready=True, message="Update ready: restarting Aura.", finish=finish)


# --- leftovers -------------------------------------------------------------------------

def cleanup_stale_staging() -> bool:
    """Remove a staged update this launch did not use (Aura quit before it
    restarted into it) and a backup a failed swap left behind. Safe at startup:
    the swap helper finishes before it opens the app.

    Returns True when the last in-place install failed; this launch then hands
    over the download instead (the next launch tries in place again).
    """
    global _in_place_failed
    try:
        marker = _failure_marker()
        if marker.exists():
            _in_place_failed = True
            marker.unlink()
    except OSError:
        pass
    paths = []
    bundle = mac_bundle_path()
    if bundle is not None:
        paths += [bundle.parent / f".{bundle.name}.update", bundle.parent / f".{bundle.name}.previous"]
    image = appimage_path() if sys.platform.startswith("linux") else None
    if image is not None:
        paths.append(image.parent / f".{image.name}.update")
    for path in paths:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        except OSError:
            pass
    return _in_place_failed


# --- entry point ----------------------------------------------------------------------

def prepare_update(download_url: str, progress: Progress = None) -> Outcome:
    """Download and stage the update for this install. Never raises.

    Run off the Tk thread. On ``ready`` the caller calls ``finish()`` and then
    quits the app at once; the helper takes it from there.
    """
    mode = install_mode()
    if mode is None:
        return Outcome(message="")
    try:
        if mode == "mac":
            return _prepare_mac(download_url, progress)
        if mode == "windows":
            return _prepare_windows(download_url, progress)
        return _prepare_appimage(download_url, progress)
    except Exception as exc:  # network, disk, tools: the user still gets the download
        return Outcome(message=f"Couldn't install the update ({exc}).")
