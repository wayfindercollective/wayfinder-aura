"""
Safe overlay subprocess lifecycle helpers.

Never uses pattern-based pkill. Kill only:
- an explicit PID we own, or
- a pidfile PID after verifying same uid + cmdline contains our overlay script path.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path
from typing import Iterable, Optional


def overlay_script_candidates(project_root: Optional[Path] = None) -> list[Path]:
    """Resolved overlay script paths we may launch."""
    roots: list[Path] = []
    if project_root is not None:
        roots.append(Path(project_root))
    # Package-relative: utils -> wayfinder -> src -> project
    here = Path(__file__).resolve()
    pkg_root = here.parents[2]  # .../src or project if installed differently
    # src/wayfinder/utils -> parents[0]=utils, [1]=wayfinder, [2]=src, [3]=project
    try:
        roots.append(here.parents[3])
    except IndexError:
        pass
    roots.append(pkg_root)
    out: list[Path] = []
    seen = set()
    for root in roots:
        for rel in (
            Path("src") / "wayfinder" / "ui" / "overlay.py",
            Path("status_overlay.py"),
            Path("wayfinder") / "ui" / "overlay.py",
        ):
            p = (root / rel).resolve()
            if p.exists() and str(p) not in seen:
                seen.add(str(p))
                out.append(p)
    # Also the package overlay path next to this tree
    pkg_overlay = (here.parents[1] / "ui" / "overlay.py").resolve()
    if pkg_overlay.exists() and str(pkg_overlay) not in seen:
        out.append(pkg_overlay)
    return out


def _runtime_dir() -> Path:
    rd = os.environ.get("XDG_RUNTIME_DIR")
    if rd and os.path.isdir(rd):
        return Path(rd) / "wayfinder-aura"
    return Path(os.environ.get("TMPDIR", "/tmp")) / f"wayfinder-aura-{os.getuid()}"


def overlay_pidfile_path() -> Path:
    return _runtime_dir() / "overlay.pid"


def write_overlay_pidfile(pid: int) -> None:
    path = overlay_pidfile_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.name == "wayfinder-aura":
            try:
                os.chmod(path.parent, 0o700)
            except OSError:
                pass
        path.write_text(str(int(pid)), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def clear_overlay_pidfile() -> None:
    try:
        overlay_pidfile_path().unlink(missing_ok=True)
    except OSError:
        pass


def _read_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")
    except OSError:
        return ""


def _pid_uid(pid: int) -> Optional[int]:
    try:
        return Path(f"/proc/{pid}").stat().st_uid
    except OSError:
        return None


def is_our_overlay_pid(
    pid: int,
    script_paths: Optional[Iterable[Path]] = None,
) -> bool:
    """True if pid is running, same uid, and cmdline references an overlay script."""
    if pid <= 0:
        return False
    uid = _pid_uid(pid)
    if uid is None or uid != os.getuid():
        return False
    cmdline = _read_cmdline(pid)
    if not cmdline:
        return False
    scripts = list(script_paths) if script_paths is not None else overlay_script_candidates()
    for script in scripts:
        # Exact path match preferred; also allow basename overlay.py / status_overlay.py
        # only when full resolved path appears in cmdline.
        s = str(script)
        if s in cmdline:
            return True
    return False


def kill_pid_if_our_overlay(pid: int, sig: int = signal.SIGKILL) -> bool:
    """Signal pid only after is_our_overlay_pid. Returns True if signal sent."""
    if not is_our_overlay_pid(pid):
        return False
    try:
        os.kill(pid, sig)
        return True
    except OSError:
        return False


def kill_tracked_process(process, sig: int = signal.SIGKILL) -> bool:
    """Kill a subprocess.Popen if still alive (no pattern match)."""
    if process is None:
        return False
    try:
        if process.poll() is not None:
            return False
        pid = process.pid
        try:
            process.kill()
        except Exception:
            try:
                os.kill(pid, sig)
            except OSError:
                return False
        try:
            process.wait(timeout=0.5)
        except Exception:
            pass
        return True
    except Exception:
        return False


def kill_stale_overlay_from_pidfile(
    script_paths: Optional[Iterable[Path]] = None,
) -> bool:
    """Read overlay.pid and kill only if verification passes."""
    path = overlay_pidfile_path()
    try:
        text = path.read_text(encoding="utf-8").strip()
        pid = int(text)
    except (OSError, ValueError):
        return False
    killed = kill_pid_if_our_overlay(pid, signal.SIGKILL)
    clear_overlay_pidfile()
    return killed
