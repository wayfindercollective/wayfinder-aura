"""
Audio ducking utility for Wayfinder Aura.

Automatically reduces system audio when recording, then restores it afterwards.
Uses pactl (PulseAudio CLI) which works on both PipeWire and PulseAudio systems.
Falls back to osascript on macOS when pactl is not available.
"""

import platform
import re
import subprocess
from typing import Optional


def is_pactl_available() -> bool:
    """Check if pactl is available on the system."""
    try:
        result = subprocess.run(
            ["which", "pactl"],
            capture_output=True,
            timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def is_macos() -> bool:
    """Check if running on macOS."""
    return platform.system() == "Darwin"


def _get_macos_volume() -> Optional[int]:
    """Get current macOS output volume (0-100). Returns None on failure."""
    try:
        result = subprocess.run(
            ["osascript", "-e", "output volume of (get volume settings)"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return int(result.stdout.strip())
    except Exception:
        pass
    return None


def _set_macos_volume(volume: int) -> bool:
    """Set macOS output volume (0-100). Returns True on success."""
    try:
        volume = max(0, min(100, volume))
        result = subprocess.run(
            ["osascript", "-e", f"set volume output volume {volume}"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def get_sink_inputs() -> list[dict]:
    """
    Get all current sink inputs (audio streams from applications).
    
    Returns:
        List of dicts with keys: id, volume_percent, app_name
    """
    try:
        result = subprocess.run(
            ["pactl", "list", "sink-inputs"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode != 0:
            return []
        
        return _parse_sink_inputs(result.stdout)
    except Exception as e:
        print(f"⚠ Error getting sink inputs: {e}")
        return []


def _parse_sink_inputs(output: str) -> list[dict]:
    """
    Parse pactl list sink-inputs output.
    
    Example output format:
        Sink Input #17961
            ...
            Volume: front-left: 55706 /  85% / -4.23 dB,   front-right: 55706 /  85% / -4.23 dB
            ...
            Properties:
                application.name = "Chromium"
    """
    sink_inputs = []
    current_input = None
    
    for line in output.split('\n'):
        # New sink input
        match = re.match(r'^Sink Input #(\d+)', line)
        if match:
            if current_input:
                sink_inputs.append(current_input)
            current_input = {
                'id': int(match.group(1)),
                'volume_percent': 100,
                'app_name': 'Unknown'
            }
            continue
        
        if current_input is None:
            continue
        
        # Volume line - extract percentage from first channel
        # Format: "Volume: front-left: 55706 /  85% / -4.23 dB, ..."
        if line.strip().startswith('Volume:'):
            vol_match = re.search(r'/\s*(\d+)%', line)
            if vol_match:
                current_input['volume_percent'] = int(vol_match.group(1))
        
        # Application name
        if 'application.name' in line:
            name_match = re.search(r'application\.name\s*=\s*"([^"]*)"', line)
            if name_match:
                current_input['app_name'] = name_match.group(1)
    
    # Don't forget the last one
    if current_input:
        sink_inputs.append(current_input)
    
    return sink_inputs


def set_sink_input_volume(sink_input_id: int, volume_percent: int) -> bool:
    """
    Set the volume of a specific sink input.
    
    Args:
        sink_input_id: The sink input ID from pactl
        volume_percent: Target volume percentage (0-100+)
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # Clamp volume to reasonable range (allow over 100% but cap at 150%)
        volume_percent = max(0, min(150, volume_percent))
        
        result = subprocess.run(
            ["pactl", "set-sink-input-volume", str(sink_input_id), f"{volume_percent}%"],
            capture_output=True,
            timeout=5
        )
        return result.returncode == 0
    except Exception as e:
        print(f"⚠ Error setting sink input {sink_input_id} volume: {e}")
        return False


class AudioDucker:
    """
    Manages audio ducking - reducing other audio when recording.
    
    Usage:
        ducker = AudioDucker(duck_percent=20.0)
        ducker.duck()    # Reduce all audio by 20%
        # ... recording ...
        ducker.restore() # Restore original volumes
    """
    
    def __init__(self, duck_percent: float = 20.0, exclude_apps: Optional[list[str]] = None):
        """
        Initialize the audio ducker.

        Args:
            duck_percent: Percentage to reduce audio by (0-100).
                         20 means reduce to 80% of original.
            exclude_apps: List of application names to exclude from ducking
        """
        self._duck_percent = duck_percent
        self._exclude_apps = exclude_apps or []
        self._original_volumes: dict[int, int] = {}  # {sink_input_id: original_volume_percent}
        self._macos_original_volume: Optional[int] = None  # macOS system volume before ducking
        self._is_ducked = False
        self._use_macos = is_macos() and not is_pactl_available()
        self._available = is_pactl_available() or self._use_macos

        if not self._available:
            print("⚠ pactl not available - audio ducking disabled")
        elif self._use_macos:
            print("ℹ Using macOS osascript for audio ducking")
    
    @property
    def is_available(self) -> bool:
        """Check if audio ducking is available on this system."""
        return self._available
    
    @property
    def is_ducked(self) -> bool:
        """Check if audio is currently ducked."""
        return self._is_ducked
    
    def set_duck_percent(self, percent: float) -> None:
        """Update the duck percentage."""
        self._duck_percent = max(0, min(100, percent))
    
    def duck(self) -> bool:
        """
        Reduce all audio sources by the configured duck percentage.

        Returns:
            True if ducking was applied, False if unavailable or already ducked
        """
        if not self._available:
            return False

        if self._is_ducked:
            # Already ducked - don't double-duck
            return False

        # macOS branch — duck system output volume via osascript
        if self._use_macos:
            current_vol = _get_macos_volume()
            if current_vol is None:
                return False
            self._macos_original_volume = current_vol
            reduction_factor = (100 - self._duck_percent) / 100
            ducked_vol = int(current_vol * reduction_factor)
            if _set_macos_volume(ducked_vol):
                self._is_ducked = True
                print(f"🔉 Ducked macOS volume {current_vol}% → {ducked_vol}%")
                return True
            return False

        # Linux / PulseAudio / PipeWire branch
        # Get all current sink inputs
        sink_inputs = get_sink_inputs()

        if not sink_inputs:
            return False

        self._original_volumes.clear()
        ducked_count = 0

        for sink in sink_inputs:
            sink_id = sink['id']
            original_vol = sink['volume_percent']
            app_name = sink['app_name']

            # Skip excluded apps (case-insensitive)
            if any(exc.lower() in app_name.lower() for exc in self._exclude_apps):
                continue

            # Store original volume
            self._original_volumes[sink_id] = original_vol

            # Calculate ducked volume
            # If duck_percent is 20, we reduce to 80% of original
            reduction_factor = (100 - self._duck_percent) / 100
            ducked_vol = int(original_vol * reduction_factor)

            # Apply ducked volume
            if set_sink_input_volume(sink_id, ducked_vol):
                ducked_count += 1

        self._is_ducked = True

        if ducked_count > 0:
            print(f"🔉 Ducked {ducked_count} audio source(s) by {self._duck_percent}%")

        return True
    
    def restore(self) -> bool:
        """
        Restore all audio sources to their original volumes.

        Returns:
            True if restoration was applied, False if unavailable or not ducked
        """
        if not self._available:
            return False

        if not self._is_ducked:
            # Not ducked - nothing to restore
            return False

        # macOS branch — restore system output volume via osascript
        if self._use_macos:
            if self._macos_original_volume is not None:
                if _set_macos_volume(self._macos_original_volume):
                    print(f"🔊 Restored macOS volume to {self._macos_original_volume}%")
                    self._macos_original_volume = None
                    self._is_ducked = False
                    return True
            self._is_ducked = False
            return False

        # Linux / PulseAudio / PipeWire branch
        restored_count = 0

        for sink_id, original_vol in self._original_volumes.items():
            if set_sink_input_volume(sink_id, original_vol):
                restored_count += 1

        self._original_volumes.clear()
        self._is_ducked = False

        if restored_count > 0:
            print(f"🔊 Restored {restored_count} audio source(s)")

        return True
    
    def __del__(self):
        """Ensure audio is restored if object is destroyed while ducked."""
        if self._is_ducked:
            self.restore()
