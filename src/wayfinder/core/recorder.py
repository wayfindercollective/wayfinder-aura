"""
Audio recording module for Wayfinder Aura.
Captures audio using sounddevice with callback-based streaming.
Automatically resamples to 16kHz for Whisper compatibility.
Includes audio preprocessing for improved transcription accuracy.
Supports chunked recording for indefinite duration sessions.
"""

import queue
import subprocess
import sys
import tempfile
import threading
import wave
from math import gcd
from pathlib import Path
from typing import Callable

import numpy as np

try:
    import sounddevice as sd
except (ImportError, OSError) as _sounddevice_error:
    _SOUNDDEVICE_IMPORT_ERROR = str(_sounddevice_error)

    class _MissingInputStream:
        def __init__(self, *args, **kwargs):
            raise OSError(f"sounddevice unavailable: {_SOUNDDEVICE_IMPORT_ERROR}")

    class _MissingDefault:
        device = [None, None]

    class _MissingSoundDevice:
        PortAudioError = OSError
        InputStream = _MissingInputStream
        default = _MissingDefault()

        def query_devices(self, *args, **kwargs):
            raise OSError(f"sounddevice unavailable: {_SOUNDDEVICE_IMPORT_ERROR}")

        def query_hostapis(self, *args, **kwargs):
            raise OSError(f"sounddevice unavailable: {_SOUNDDEVICE_IMPORT_ERROR}")

        def check_input_settings(self, *args, **kwargs):
            raise OSError(f"sounddevice unavailable: {_SOUNDDEVICE_IMPORT_ERROR}")

        def _terminate(self):
            raise OSError(f"sounddevice unavailable: {_SOUNDDEVICE_IMPORT_ERROR}")

        def _initialize(self):
            raise OSError(f"sounddevice unavailable: {_SOUNDDEVICE_IMPORT_ERROR}")

    sd = _MissingSoundDevice()
    sys.modules.setdefault("sounddevice", sd)

# SciPy's signal package pulls in a large statistics/interpolation stack. Importing it at
# module scope added roughly a second to every launch even though it is only needed after a
# recording is complete. Resolve and cache the three functions on first audio-processing use.
_SCIPY_SIGNAL_FUNCTIONS = None
_SCIPY_IMPORT_ATTEMPTED = False
_SCIPY_IMPORT_LOCK = threading.Lock()


def _get_scipy_signal_functions():
    """Return ``(butter, filtfilt, resample_poly)`` when SciPy is available.

    The import is deliberately lazy so microphone discovery and recorder construction stay
    fast. A failed optional import is cached too; repeated dictations then use the existing
    NumPy fallback without repeatedly probing SciPy.
    """
    global _SCIPY_SIGNAL_FUNCTIONS, _SCIPY_IMPORT_ATTEMPTED
    if _SCIPY_IMPORT_ATTEMPTED:
        return _SCIPY_SIGNAL_FUNCTIONS
    with _SCIPY_IMPORT_LOCK:
        if _SCIPY_IMPORT_ATTEMPTED:
            return _SCIPY_SIGNAL_FUNCTIONS
        try:
            from scipy.signal import butter, filtfilt, resample_poly
        except ImportError:
            _SCIPY_SIGNAL_FUNCTIONS = None
        else:
            _SCIPY_SIGNAL_FUNCTIONS = (butter, filtfilt, resample_poly)
        _SCIPY_IMPORT_ATTEMPTED = True
    return _SCIPY_SIGNAL_FUNCTIONS


def preload_audio_processing() -> None:
    """Warm optional audio processing after first paint.

    This is a one-shot import, not a resident worker. It keeps SciPy off the click-to-visible
    path without making the first completed dictation absorb the import cost.
    """
    _get_scipy_signal_functions()


# Target sample rate for Whisper
WHISPER_SAMPLE_RATE = 16000

# Known microphone brands/keywords to prioritize
PREFERRED_MIC_KEYWORDS = [
    # Dedicated USB microphones (high priority — these are what users want for dictation)
    "shure", "mv7", "sm7", "blue", "yeti", "snowball", "rode", "nt-usb", "ntusb",
    "audio-technica", "at2020", "at2035", "hyperx", "quadcast", "elgato", "wave",
    "focusrite", "scarlett", "behringer", "samson", "fifine", "maono", "tonor",
    "microphone", "mic input", "headset",
]

# Dedicated USB mic brands get a higher score than generic "microphone" keyword
_USB_MIC_BRANDS = {
    "shure", "mv7", "sm7", "blue", "yeti", "snowball", "rode", "nt-usb", "ntusb",
    "audio-technica", "at2020", "at2035", "hyperx", "quadcast", "elgato", "wave",
    "focusrite", "scarlett", "behringer", "samson", "fifine", "maono", "tonor",
    "obsbot",
}

# Keywords indicating the device is NOT a microphone input
EXCLUDED_DEVICE_KEYWORDS = [
    # Output devices
    "monitor", "hdmi", "hdmi/dp", "loopback", "output", "speaker", "playback",
    "digital stereo (hdmi", "s/pdif", "headphone", "front headphone",
    "rear headphone",
    # GPU audio controllers (these are HDMI/DP audio outputs, not microphones)
    "navi ", "radeon", "nvidia", "geforce", "intel hda",
    "dp audio controller", "hdmi audio controller",
    "display audio", "displayport",
    # Virtual/system devices (when they appear as standalone device names)
    "pipewire", "pulse", "default",
    # ALSA output nodes
    "alsa_output",
]


# Peak amplitude below this means the recording is effectively silence — a muted,
# disconnected, or wrong-device capture (true silence is exact zeros; floor noise on a
# live mic is well above this). Whisper hallucinates text on silence, so callers should
# check get_peak_amplitude() against this before transcribing and tell the user instead.
SILENCE_PEAK_THRESHOLD = 0.001


def get_wav_peak_amplitude(audio_path: str | Path) -> float | None:
    """Return a mono/stereo PCM WAV's normalized peak, or ``None`` if unreadable.

    Chunked recordings are written as signed 16-bit PCM before transcription.
    Measuring the saved file lets the orchestration layer reject an individually
    silent trailing chunk even when earlier chunks contained real speech.  Fail
    open for an unexpected WAV format so a format mismatch never drops words.
    """
    try:
        with wave.open(str(audio_path), "rb") as wav_file:
            if wav_file.getsampwidth() != 2:
                return None
            frames = wav_file.readframes(wav_file.getnframes())
        if not frames:
            return 0.0
        samples = np.frombuffer(frames, dtype="<i2")
        if samples.size == 0:
            return 0.0
        # Promote before abs: abs(int16(-32768)) overflows in int16.
        return float(np.max(np.abs(samples.astype(np.int32)))) / 32768.0
    except (OSError, EOFError, wave.Error):
        return None


def is_output_device(device_name: str) -> bool:
    """Check if a device name matches known output device patterns."""
    name_lower = device_name.lower()
    return any(kw in name_lower for kw in EXCLUDED_DEVICE_KEYWORDS)


_IS_STEAM_DECK: bool | None = None


def _is_steam_deck() -> bool:
    """True on Steam Deck hardware (DMI product 'Jupiter'/'Galileo'), cached.

    Decides whether to hide JACK-backed devices: on the Deck the libjack shim fails to open
    streams (PaErrorCode -9999 in a user-service context — Issue 20), but off-Deck the JACK
    host API is PipeWire and exposes the user's real, friendly-named mics. Local DMI check to
    avoid importing wayfinder.core.setup from the recorder.
    """
    global _IS_STEAM_DECK
    if _IS_STEAM_DECK is None:
        try:
            with open("/sys/class/dmi/id/product_name") as f:
                _IS_STEAM_DECK = f.read().strip() in ("Jupiter", "Galileo")
        except Exception:
            _IS_STEAM_DECK = False
    return _IS_STEAM_DECK


def _pa_rescan() -> bool:
    """Force PortAudio to rebuild its device table. Returns True if the re-init worked.

    PortAudio snapshots the host's devices at initialization and NEVER updates the
    table, so a device that appears later (a mic on a USB hub powered on after the
    app booted) is invisible to this process — by index OR by name — until the
    library is re-initialized. sounddevice exposes that as _terminate/_initialize.
    Only call this with no streams open: re-init invalidates existing streams.
    """
    try:
        sd._terminate()
        sd._initialize()
        return True
    except Exception as e:
        print(f"PortAudio rescan failed: {e}")
        return False


def _system_default_input_index() -> int | None:
    """Index of the ALSA ``pulse``/``default`` PCM that routes to the user's system-default source.

    In a Flatpak/sandbox PortAudio is built ALSA-only: the raw ``hw:`` devices expose no capture
    and the only working mic is the ``pulse`` PCM -- the PipeWire/Pulse proxy, which follows
    whatever KDE/PipeWire has chosen as the default source. Prefer ``pulse``; on SteamOS the bare
    ``default`` PCM can route to a silent sink while ``pulse`` carries the live signal. Returns
    None when neither is capturable (a normal desktop with real hardware mics, where this
    fallback is never reached).
    """
    try:
        devices = sd.query_devices()
    except Exception:
        return None
    for target in ("pulse", "default"):
        for i, dev in enumerate(devices):
            if dev.get("name", "").lower() == target and dev.get("max_input_channels", 0) > 0:
                return i
    return None


def find_best_input_device(preferred_name: str | None = None) -> int | None:
    """
    Intelligently find the best audio input device for voice recording.
    
    Strategy:
    1. If preferred_name is given, try to find a device matching that name
    2. Prefer PipeWire/PulseAudio/JACK virtual devices over raw ALSA
    3. Prioritize devices with microphone-related keywords
    4. Filter out monitor, HDMI, and loopback devices
    5. Ensure the device has input channels
    
    Args:
        preferred_name: Optional device name to look for (partial match)
        
    Returns:
        Device index, or None to use system default
    """
    try:
        devices = sd.query_devices()
    except Exception:
        return None
    
    candidates = []
    
    for i, dev in enumerate(devices):
        name = dev.get('name', '').lower()
        max_inputs = dev.get('max_input_channels', 0)
        
        # Skip devices with no input channels
        if max_inputs < 1:
            continue
        
        # Skip excluded device types
        if any(excl in name for excl in EXCLUDED_DEVICE_KEYWORDS):
            continue
        
        # Calculate a priority score
        score = 0
        
        # Preferred name match (highest priority)
        if preferred_name and preferred_name.lower() in name:
            score += 1000
        
        # Host-API preference. JACK-backed devices are NOT preferred: on PipeWire/SteamOS
        # the libjack shim won't autostart from a user-service context and the stream open
        # fails with PaErrorCode -9999 on every recording (STEAMDECK-INSTALL-LOG Issue 20).
        # Skip them on Linux so auto-selection never lands on an unusable clone.
        hostapi = dev.get('hostapi', -1)
        try:
            hostapi_info = sd.query_hostapis(hostapi)
            hostapi_name = hostapi_info.get('name', '').lower()
            if 'jack' in hostapi_name:
                if sys.platform.startswith('linux'):
                    continue  # unusable on PipeWire/SteamOS
            elif 'pulse' in hostapi_name or 'pipewire' in hostapi_name:
                score += 80
        except Exception:
            pass
        
        # Prefer known USB microphone brands (high confidence these are real mics)
        is_usb_brand = any(kw in name for kw in _USB_MIC_BRANDS)
        if is_usb_brand:
            score += 200  # Strong preference for dedicated USB mics
        elif any(kw in name for kw in PREFERRED_MIC_KEYWORDS):
            score += 50  # Generic "microphone" keyword match

        # Penalize phone/mobile/bluetooth mics (unreliable for dictation)
        if any(kw in name for kw in ("iphone", "ipad", "android", "phone", "airpod", "bluetooth")):
            score -= 100
        
        # Prefer "mono" for voice (cleaner for speech recognition)
        if 'mono' in name:
            score += 20
        
        # Slight preference for devices with fewer channels (more likely dedicated mics)
        if max_inputs == 1:
            score += 10
        elif max_inputs == 2:
            score += 5
        
        # Penalize raw ALSA hw: devices (IDs can shift)
        if 'hw:' in name:
            score -= 30
        
        candidates.append((score, i, dev['name']))
    
    if not candidates:
        # No dedicated hardware mic was found. In a Flatpak/sandbox the raw hw: devices expose
        # no capture, and the only usable input is the 'pulse' PCM (PipeWire proxy -> the user's
        # system-default source) -- fall back to it. Off-sandbox this returns None (unchanged).
        return _system_default_input_index()
    
    # Sort by score (highest first)
    candidates.sort(key=lambda x: x[0], reverse=True)
    
    # Return the best candidate
    best_score, best_idx, best_name = candidates[0]
    
    # Only use auto-detected device if it has a reasonable score
    if best_score > 0:
        print(f"Auto-selected audio device: {best_name} (score: {best_score})")
        return best_idx
    
    # Fall back to system default if no good candidates
    return None


def get_input_device_by_name(name: str) -> int | None:
    """
    Find a device by name (partial match, case-insensitive).

    Args:
        name: Device name to search for

    Returns:
        Device index or None if not found
    """
    if not name:
        return None

    jack_fallback = None  # a JACK-only name match — used only if nothing better exists
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        name_lower = name.lower()

        for i, dev in enumerate(devices):
            if name_lower in dev.get('name', '').lower() and dev.get('max_input_channels', 0) > 0:
                # Avoid the JACK host API on Linux: on PipeWire a mic is often exposed
                # ONLY as a pipewire-jack clone, whose tiny ~3ms buffer xruns ("input
                # overflow") every time audio ducking reconfigures the graph — and that
                # occasionally cascades into a wedged whisper-server request or a
                # PortAudio teardown abort. The ALSA/Pulse PCM has a real ~35ms buffer.
                # (Mirrors the JACK avoidance in find_best_input_device.)
                hostapi_name = ""
                try:
                    hostapi_name = hostapis[dev['hostapi']].get('name', '').lower()
                except Exception:
                    pass
                if 'jack' in hostapi_name and sys.platform.startswith('linux'):
                    if jack_fallback is None:
                        jack_fallback = i
                    continue
                return i

        # Only a JACK clone matched the name (common on PipeWire). Route through the
        # system-default Pulse/PipeWire PCM, which carries the same mic when it's the
        # default source but with a buffer big enough to survive ducking.
        if jack_fallback is not None:
            sysdef = _system_default_input_index()
            if sysdef is not None:
                return sysdef
    except Exception:
        pass

    # The picker may have stored a pactl friendly description (e.g. "Webcam C920")
    # that has no PortAudio name equivalent on Pulse-only systems — resolve it via
    # the same source→device matching the curated picker uses.
    try:
        name_lower = name.lower()
        for s in _pactl_input_sources():
            if name_lower == s['description'].lower():
                return _match_source_to_device(s, sd.query_devices(), sd.query_hostapis())
    except Exception:
        pass

    # Last resort: if the only match was a JACK clone and nothing better resolved,
    # use it rather than failing to find the mic at all (never worse than before).
    return jack_fallback


def _pactl_input_sources() -> list[dict]:
    """Real capture sources from pactl — the same list KDE/GNOME sound settings show.

    Returns [{'name', 'description', 'alsa_card', 'alsa_device', 'is_default'}, ...] or
    [] when pactl is unavailable or returns nothing (non-PipeWire/Pulse systems,
    sandboxes without the socket). Monitor sources (sink loopbacks) are skipped — they
    are playback taps, not microphones.
    """
    try:
        out = subprocess.run(["pactl", "list", "sources"],
                             capture_output=True, text=True, timeout=3)
        if out.returncode != 0 or not out.stdout:
            return []
    except Exception:
        return []

    default_name = ""
    try:
        d = subprocess.run(["pactl", "get-default-source"],
                           capture_output=True, text=True, timeout=3)
        if d.returncode == 0:
            default_name = d.stdout.strip()
    except Exception:
        pass

    sources: list[dict] = []
    cur: dict | None = None
    for raw in out.stdout.splitlines():
        line = raw.strip()
        if line.startswith("Source #"):
            cur = {"name": "", "description": "", "alsa_card": "", "alsa_device": "",
                   "is_default": False}
            sources.append(cur)
        elif cur is None:
            continue
        elif line.startswith("Name:"):
            cur["name"] = line.split(":", 1)[1].strip()
        elif line.startswith("Description:"):
            cur["description"] = line.split(":", 1)[1].strip()
        elif line.startswith("alsa.card = "):
            cur["alsa_card"] = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("alsa.device = "):
            cur["alsa_device"] = line.split("=", 1)[1].strip().strip('"')

    result = []
    for s in sources:
        if not s["name"] or s["name"].endswith(".monitor") or not s["description"]:
            continue
        s["is_default"] = bool(default_name) and s["name"] == default_name
        result.append(s)
    return result


def _match_source_to_device(source: dict, devices, hostapis) -> int | None:
    """Map a pactl source to its PortAudio device index.

    Preference order: exact JACK/PipeWire name match (PipeWire names JACK capture
    nodes after the source description) → exact name match on any host API → ALSA
    "(hw:card,device)" match via the source's alsa.* properties (Pulse-only systems,
    where descriptions never appear in PortAudio names) → substring match.
    """
    desc = source["description"].lower()
    hw_tag = None
    if source["alsa_card"]:
        hw_tag = f"(hw:{source['alsa_card']},{source['alsa_device'] or '0'}"
    jack_eq = name_eq = alsa_hw = fuzzy = None

    for i, dev in enumerate(devices):
        if dev.get("max_input_channels", 0) < 1:
            continue
        name = dev.get("name", "").lower()
        try:
            api = hostapis[dev.get("hostapi", -1)].get("name", "")
        except (IndexError, KeyError, TypeError):
            api = ""
        if name == desc:
            if "JACK" in api:
                if jack_eq is None:
                    jack_eq = i
            elif name_eq is None:
                name_eq = i
        elif hw_tag and hw_tag in name:
            if alsa_hw is None:
                alsa_hw = i
        elif desc in name or name in desc:
            if fuzzy is None:
                fuzzy = i

    for cand in (jack_eq, name_eq, alsa_hw, fuzzy):
        if cand is not None:
            return cand
    return None


def list_input_devices(exclude_outputs: bool = False) -> list[dict]:
    """
    List all available input devices with relevant info.
    
    Args:
        exclude_outputs: If True, omit devices that look like outputs/monitors.
    
    Returns:
        List of dicts with device info (index, name, channels, recommended, excluded)
    """
    result = []

    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        is_linux = sys.platform.startswith('linux')

        # Curated path (desktop Linux): pactl's source list is exactly what KDE/GNOME
        # sound settings show. PortAudio's raw list both over-shows (sink monitors
        # exposed as JACK capture nodes, raw ALSA hw: duplicates, app streams) and
        # under-shows (keyword filters can hide odd-named real mics) — the two picker
        # complaints. Show the real sources by friendly name, mapped to their PortAudio
        # index. Fall back to legacy enumeration when pactl is absent (non-PipeWire
        # distros) or nothing maps (e.g. ALSA-only sandbox, where the pulse-PCM
        # fallback below takes over). Deck keeps its validated legacy behavior.
        if is_linux and not _is_steam_deck():
            pw_sources = _pactl_input_sources()
            if pw_sources:
                curated = []
                for s in pw_sources:
                    idx = _match_source_to_device(s, devices, hostapis)
                    channels = 1
                    if idx is not None:
                        channels = devices[idx].get('max_input_channels', 1)
                    curated.append({
                        # idx None = source visible in the OS but unmapped in PortAudio;
                        # keep it listed (it may be the user's mic) — selection falls
                        # back to the system default stream until it maps.
                        'index': idx,
                        'name': s['description'],
                        'channels': channels,
                        'recommended': s['is_default'] or any(
                            kw in s['description'].lower() for kw in PREFERRED_MIC_KEYWORDS),
                        'excluded': False,
                    })
                if any(c['index'] is not None for c in curated):
                    return curated

        for i, dev in enumerate(devices):
            max_inputs = dev.get('max_input_channels', 0)
            if max_inputs < 1:
                continue

            name = dev.get('name', f'Device {i}')

            # On the Steam Deck, hide JACK-backed clones — the libjack shim can't open a
            # stream there (PaErrorCode -9999 in a user-service context, Issue 20). But OFF
            # the Deck the JACK host API IS PipeWire, and these entries are the user's real,
            # friendly-named mics (Shure/OBSBOT/Wireless …). Filtering them everywhere left
            # only cryptic ALSA "hw:" names and broke mic selection on desktops — so only
            # filter on the Deck.
            if is_linux and _is_steam_deck():
                try:
                    if 'JACK' in hostapis[dev.get('hostapi', -1)].get('name', ''):
                        continue
                except (IndexError, KeyError, TypeError):
                    pass

            is_excluded = is_output_device(name)

            if exclude_outputs and is_excluded:
                continue
            
            is_recommended = (
                not is_excluded and
                any(kw in name.lower() for kw in PREFERRED_MIC_KEYWORDS)
            )
            
            result.append({
                'index': i,
                'name': name,
                'channels': max_inputs,
                'recommended': is_recommended,
                'excluded': is_excluded,
            })
    except Exception:
        pass
    
    # Sandbox/Flatpak: PortAudio is ALSA-only and every entry above is an output/monitor or a
    # 0-input hw: device -- the only capture path is the 'pulse' PCM (PipeWire proxy). If nothing
    # usable surfaced, present that single PCM as one friendly "System Default" mic so the picker
    # isn't empty and dictation works out of the box. (Desktops with real mics never hit this.)
    if not any(not r['excluded'] for r in result):
        sysidx = _system_default_input_index()
        if sysidx is not None:
            return [{
                'index': sysidx,
                'name': 'System Default (PipeWire)',
                'channels': 1,
                'recommended': True,
                'excluded': False,
            }]

    return result


# A healthy mic opens in well under a second; anything past this is a wedged
# PipeWire/PortAudio node (e.g. a source stuck in a bad state — a default source
# left in a '(null)' state hangs PortAudio's `pulse`/`default` probe forever).
# Because the open runs on the Tk main thread, a hang freezes the ENTIRE app
# (UI, tray, hotkeys). This watchdog caps every attempt so a wedged device falls
# through the fallback chain instead of taking the app down. (2026-07-03 freeze.)
_MIC_OPEN_TIMEOUT = 4.0


def _run_with_timeout(fn: Callable, timeout_s: float):
    """Run ``fn()`` on a daemon thread; return its result, or raise
    ``TimeoutError`` if it doesn't finish within ``timeout_s``.

    A hung PortAudio C call can't be interrupted, so on timeout the worker thread
    is abandoned (it leaks until/unless the call ever returns) — but the CALLER is
    freed, which is the whole point: the Tk main thread must never block forever on
    a wedged audio device. Exceptions raised by ``fn`` propagate to the caller.
    """
    box: dict = {}

    def run():
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 — relay the real failure
            box["error"] = exc

    t = threading.Thread(target=run, daemon=True, name="mic-open-watchdog")
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise TimeoutError(f"audio device open exceeded {timeout_s:.0f}s (wedged node?)")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def get_supported_sample_rate(device: int | None = None, target_rate: int = 16000) -> int:
    """
    Find a supported sample rate for the given device.
    
    Args:
        device: Audio device index (None for default)
        target_rate: Preferred sample rate
        
    Returns:
        Supported sample rate (may be different from target)
    """
    # Common sample rates to try, in order of preference
    sample_rates_to_try = [target_rate, 44100, 48000, 22050, 32000, 96000]
    
    # Get device info
    try:
        if device is not None:
            device_info = sd.query_devices(device)
        else:
            device_info = sd.query_devices(kind='input')
        
        # Try the device's default rate first
        default_rate = int(device_info.get('default_samplerate', 44100))
        if default_rate not in sample_rates_to_try:
            sample_rates_to_try.insert(0, default_rate)
    except Exception:
        pass
    
    # Test each sample rate
    for rate in sample_rates_to_try:
        try:
            # Test if this rate works by checking device compatibility
            sd.check_input_settings(device=device, samplerate=rate, channels=1)
            return rate
        except sd.PortAudioError:
            continue
    
    # Fallback to 44100 which is most universally supported
    return 44100


def resample_audio(audio: np.ndarray, orig_rate: int, target_rate: int) -> np.ndarray:
    """
    Resample audio to target sample rate.
    
    Args:
        audio: Audio data as numpy array
        orig_rate: Original sample rate
        target_rate: Target sample rate
        
    Returns:
        Resampled audio
    """
    if orig_rate == target_rate:
        return audio
    
    scipy_signal = _get_scipy_signal_functions()
    if scipy_signal is not None:
        # Use scipy's polyphase resampling for high quality
        _, _, resample_poly = scipy_signal
        g = gcd(orig_rate, target_rate)
        up = target_rate // g
        down = orig_rate // g
        return resample_poly(audio, up, down).astype(np.float32)
    else:
        # Simple linear interpolation fallback
        duration = len(audio) / orig_rate
        target_length = int(duration * target_rate)
        indices = np.linspace(0, len(audio) - 1, target_length)
        return np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)


def preprocess_audio(
    audio_data: np.ndarray, 
    sample_rate: int, 
    level: str = "light"
) -> np.ndarray:
    """
    Preprocess audio for transcription.
    
    Levels:
    - "off": No processing at all (raw audio)
    - "light": Only gain normalization (recommended for most cases)
    - "medium": Gain normalization + gentle high-pass filter
    - "heavy": Full processing including noise gate (may cut off words)
    
    Args:
        audio_data: Audio samples as float32 array
        sample_rate: Sample rate in Hz
        level: Processing level - "off", "light", "medium", "heavy"
        
    Returns:
        Preprocessed audio data
    """
    if level == "off" or not level:
        return audio_data
    
    # Work with a copy
    audio = audio_data.copy()
    
    # Flatten if needed (mono)
    if audio.ndim > 1:
        audio = audio.flatten()
    
    # Gain normalization - applied for all levels except "off"
    # Normalize to -3dB peak (0.707) to ensure good signal level
    peak = np.max(np.abs(audio))
    if peak > 0.001:  # Avoid division by near-zero
        target_peak = 0.707  # -3dB
        audio = audio * (target_peak / peak)
    
    # Medium and Heavy: High-pass filter at 80Hz (remove rumble/noise)
    if level in ("medium", "heavy"):
        scipy_signal = _get_scipy_signal_functions()
        if scipy_signal is not None:
            butter, filtfilt, _ = scipy_signal
            try:
                nyquist = sample_rate / 2
                cutoff = 80 / nyquist
                if cutoff < 1:
                    b, a = butter(2, cutoff, btype='high')
                    audio = filtfilt(b, a, audio).astype(np.float32)
            except Exception:
                pass
    
    # Heavy only: Noise gate (can cut off soft consonants - use with caution!)
    if level == "heavy":
        noise_floor = 0.002  # -54dB - very gentle threshold
        gate_reduction = 0.5  # Only reduce to 50%, don't kill the signal
        audio = np.where(np.abs(audio) < noise_floor, audio * gate_reduction, audio)
    
    # Ensure output shape matches input
    if audio_data.ndim > 1:
        audio = audio.reshape(audio_data.shape)
    
    return audio.astype(np.float32)


class WarmMic:
    """A single persistent capture stream, kept 'warm' between recordings.

    Opening the capture stream on SteamOS/PipeWire (the 'pulse' PCM in the Flatpak sandbox)
    costs ~0.4-0.5s, and a stale cached device index adds a ~0.15s dead-probe on top — so a
    fresh open per recording clipped the first words of short or rapid-fire dictations into
    silence ("No speech detected"). WarmMic opens the stream once and keeps it open, routing
    audio to whichever recorder is currently attached (its ``_audio_callback``) and dropping it
    otherwise, so the next recording starts capturing instantly. The stream auto-closes after
    ``idle_secs`` with no recorder attached, releasing the mic.

    It also heals the dead/renumbered-index problem once: ``acquire()`` walks the same fallback
    chain the recorders used (configured device -> system-default PCM -> None) and remembers the
    index that actually opened, so a dead index is probed at most once instead of on every
    recording. Shared by both AudioRecorder and ChunkedRecorder; only one is ever attached at a
    time (start/stop are serialized on the Tk thread).
    """

    def __init__(self, device: int | None = None, sample_rate: int = 16000,
                 channels: int = 1, idle_secs: float = 30.0,
                 resolve_device: Callable[[], int | None] | None = None):
        self.device = device
        self.target_sample_rate = sample_rate
        self.channels = channels
        self.idle_secs = idle_secs
        # Optional re-resolver (e.g. the app's name-based resolve_audio_device). Called
        # after a PortAudio rescan when the whole fallback chain fails — the one case a
        # rescan can fix is a device that APPEARED after this process initialized
        # PortAudio (its device table is a snapshot from init and never updates), e.g.
        # a mic on a USB hub powered on after the app booted.
        self._resolve_device = resolve_device
        self._stream: sd.InputStream | None = None
        self._recording_sample_rate: int | None = None
        self._sink: Callable | None = None
        # RLock so acquire() can cancel the idle timer and open the stream while holding it,
        # and the idle-timer thread can take the same lock without self-deadlock.
        self._lock = threading.RLock()
        self._idle_timer: threading.Timer | None = None
        # True when an open was abandoned by the watchdog — a late PortAudio worker may
        # still be inside InputStream()/start(). Never _pa_rescan() while set: terminate
        # racing an in-flight open has SEGV'd ALSA (2026-07-13 Detect session).
        self._abandoned_open = False

    @property
    def sample_rate(self) -> int:
        """The open stream's rate, or the target rate until opened."""
        return self._recording_sample_rate or self.target_sample_rate

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    def _callback(self, indata, frames, time_info, status) -> None:
        # Runs on PortAudio's thread. Read the sink once (atomic under the GIL) and forward to
        # the attached recorder's callback, which makes its own copy. No lock on this hot path —
        # a swap at the boundary at worst mis-routes a single frame, which is harmless.
        sink = self._sink
        if sink is not None:
            sink(indata, frames, time_info, status)

    def _open(self) -> None:
        """Open the stream, healing a stale device index via the fallback chain.

        If the ENTIRE chain fails, force a PortAudio rescan and retry once: PortAudio's
        device table is a snapshot from process init, so a mic that appeared after the
        app booted (USB hub powered on in the morning) is invisible to every rung of
        the chain until the tables are rebuilt (2026-07-02 morning failure mode).

        Never rescan while an open was abandoned mid-flight — ``sd._terminate()`` racing
        a still-running ``InputStream()`` is the ALSA SEGV class of crash.
        """
        # Do NOT clear _abandoned_open here. Once a watchdog abandoned an in-flight
        # PortAudio open, a late C worker may still be inside InputStream()/start()
        # for the rest of the process lifetime of this WarmMic. Clearing the flag on
        # the next acquire would re-enable _pa_rescan() under that worker (Sol R2).
        if self._abandoned_open:
            raise RuntimeError(
                "Microphone backend is still recovering from a timed-out open; "
                "restart Wayfinder Aura before recording again"
            )
        try:
            self._open_chain()
        except Exception as first_err:
            if self._abandoned_open:
                # A watchdog-abandoned open may still be inside PortAudio; terminating
                # the library here is worse than leaving a stale device table.
                raise first_err
            if not _pa_rescan():
                raise first_err
            # Re-resolve the user's saved mic by name against the FRESH device table.
            if self._resolve_device is not None:
                try:
                    fresh = self._resolve_device()
                    if fresh is not None:
                        self.device = fresh
                except Exception:
                    pass
            print("WarmMic: all inputs failed — rescanned PortAudio devices, retrying")
            self._open_chain()  # raises to the caller if the rescan didn't help

    def _open_chain(self) -> None:
        """Walk the fallback chain (configured -> system default -> None) once."""
        fallbacks: list[int | None] = []
        if self.device is not None:
            fallbacks.append(self.device)
        sys_default = _system_default_input_index()
        if sys_default is not None and sys_default not in fallbacks:
            fallbacks.append(sys_default)
        fallbacks.append(None)
        last_err = None
        for dev in fallbacks:
            def _probe_and_open(dev=dev):
                rate = get_supported_sample_rate(device=dev, target_rate=self.target_sample_rate)
                stream = sd.InputStream(
                    samplerate=rate,
                    channels=self.channels,
                    device=dev,
                    dtype=np.float32,
                    callback=self._callback,
                    # 'high' latency = a larger input ring buffer, so a momentary stall
                    # (audio ducking reconfiguring the PipeWire graph at record start, or
                    # general system jitter) can't overrun it. Without this, PortAudio's
                    # default small buffer overflowed on EVERY recording ("input overflow"),
                    # which occasionally cascaded into a wedged whisper-server request or a
                    # PortAudio teardown abort. Capture latency is irrelevant for dictation.
                    latency="high",
                )
                stream.start()
                return stream, rate
            try:
                # Cap the probe+open with a watchdog: a wedged device (e.g. a
                # '(null)'-state default source) hangs PortAudio here, and this runs
                # on the Tk main thread — uncapped, that hang freezes the whole app.
                # On timeout we abandon it and fall through to the next fallback.
                stream, rate = _run_with_timeout(_probe_and_open, _MIC_OPEN_TIMEOUT)
                with self._lock:
                    self._stream = stream
                    self._recording_sample_rate = rate
                    self.device = dev  # remember the working index — heals a stale/dead cached one
                return
            except TimeoutError as e:
                last_err = e
                self._abandoned_open = True
                print(f"WarmMic: device {dev} open timed out (>{_MIC_OPEN_TIMEOUT:.0f}s) — "
                      "likely a wedged PipeWire node; blocking further opens until restart")
                # PortAudio is process-global.  The timed-out C call is still alive
                # on the watchdog thread, so trying the system-default/None fallbacks
                # concurrently only creates more uncancellable opens and eventually
                # wedges PipeWire itself.  Fail this recording immediately and let a
                # process restart clear the one abandoned call.
                raise
            except Exception as e:
                last_err = e
                if dev is not None:
                    print(f"WarmMic: device {dev} failed to open ({e}); trying next input")
        raise last_err if last_err else RuntimeError("WarmMic: no audio input device could be opened")

    def acquire(self, sink: Callable) -> int | None:
        """Attach ``sink`` (a recorder's ``_audio_callback``) and ensure the stream is warm.

        Returns the device index that actually opened so the caller can heal its cached index.
        """
        with self._lock:
            self._cancel_idle_timer()
            self._sink = sink
            if self._stream is None:
                self._open()
            return self.device

    def release(self) -> None:
        """Detach the current sink and arm the idle-close timer. Leaves the stream warm."""
        with self._lock:
            self._sink = None
            self._arm_idle_timer()

    def _arm_idle_timer(self) -> None:
        self._cancel_idle_timer()
        if self.idle_secs and self.idle_secs > 0:
            self._idle_timer = threading.Timer(self.idle_secs, self._on_idle)
            self._idle_timer.daemon = True
            self._idle_timer.start()

    def _cancel_idle_timer(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _on_idle(self) -> None:
        # Timer target. Only close if still idle — a recording may have re-acquired the mic
        # after the timer fired but before this ran.
        with self._lock:
            if self._sink is None:
                self._close_stream()
                self._idle_timer = None

    def _close_stream(self) -> None:
        """Stop/close the PortAudio stream without double-close races.

        Atomically detach under ``_lock`` so concurrent callers cannot stop/close the
        same handle twice. ALSA's ``snd_async_del_handler`` / ``snd_pcm_close`` has
        SEGV'd when PortAudio tore down a stream that was already half-closed
        (live: 2026-07-13 Detect-key session after a mic rescan). Python
        try/except cannot catch SIGSEGV, so the only defense is never double-enter.
        Teardown runs outside the lock so abort/close cannot deadlock other paths.
        """
        with self._lock:
            stream = self._stream
            self._stream = None
            self._recording_sample_rate = None
        if stream is None:
            return
        try:
            # abort() is preferred when available: stop() can block waiting for the
            # PortAudio callback thread while a rescan/terminate is racing.
            abort = getattr(stream, "abort", None)
            if callable(abort):
                abort()
            else:
                stream.stop()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    def close(self) -> None:
        """Stop and close the stream entirely (app shutdown, device change, force reset)."""
        with self._lock:
            self._cancel_idle_timer()
            self._sink = None
        self._close_stream()

    def set_device(self, device: int | None) -> None:
        """Point at a new device and drop the warm stream so the next acquire reopens on it."""
        with self._lock:
            self.device = device
        self._close_stream()

    @property
    def in_use(self) -> bool:
        """True while a recorder currently holds the mic (a sink is attached)."""
        return self._sink is not None

    def rescan(self) -> bool:
        """Rebuild PortAudio's device table so a hotplugged mic becomes visible.

        PortAudio snapshots devices at init and never updates the table, so a mic plugged in
        after the app started is invisible until a re-init. A rescan invalidates open streams,
        so the warm stream is closed first; the next acquire() re-opens against the fresh table
        (via resolve_device). Serialized on the same lock as open/close. Refuses (returns False)
        while a recording holds the mic — call it between recordings. Returns True on success.
        Also refuses while an open was abandoned mid-flight (watchdog timeout) so we never
        ``_terminate()`` PortAudio under a still-running InputStream constructor.
        """
        with self._lock:
            if self._sink is not None:
                return False  # a recording is active — don't yank its stream
            if self._abandoned_open:
                return False  # late open worker may still be inside PortAudio
            self._cancel_idle_timer()
        self._close_stream()
        with self._lock:
            if self._sink is not None or self._abandoned_open:
                return False
            return _pa_rescan()


class AudioRecorder:
    """Records audio to a temporary WAV file for transcription."""

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        device: int | None = None,
        preprocessing: bool | str = "light",
        warm_mic: "WarmMic | None" = None,
    ):
        """
        Initialize the audio recorder.

        Args:
            sample_rate: Target audio sample rate in Hz (default 16000 for whisper)
            channels: Number of audio channels (default 1 for mono)
            device: Audio input device index (None for default)
            preprocessing: Audio preprocessing level:
                - False or "off": No processing
                - True or "light": Only gain normalization (recommended)
                - "medium": Normalization + high-pass filter
                - "heavy": Full processing with noise gate
            warm_mic: Optional shared WarmMic. When given, this recorder does not own its own
                stream — it attaches to the warm stream on start() (instant capture) and detaches
                on stop(), instead of opening/closing a stream each time.
        """
        self.target_sample_rate = sample_rate  # What we want (for Whisper)
        self.channels = channels
        self.device = device
        self.recording_sample_rate = None  # Will be set when starting
        # Convert bool to level string for backwards compatibility
        if preprocessing is True:
            self.preprocessing = "light"
        elif preprocessing is False:
            self.preprocessing = "off"
        else:
            self.preprocessing = preprocessing
        self.frames: list[np.ndarray] = []
        self.stream: sd.InputStream | None = None
        self.warm_mic = warm_mic
        self._active = False
        self._temp_file: tempfile.NamedTemporaryFile | None = None
        self.last_peak: float = 0.0

    @property
    def sample_rate(self) -> int:
        """Return the current/target sample rate."""
        return self.recording_sample_rate or self.target_sample_rate

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        """Callback function for sounddevice stream."""
        if status:
            print(f"Audio status: {status}")
        peak = float(np.max(np.abs(indata)))
        if peak > self.last_peak:
            self.last_peak = peak
        # Make a copy since indata is reused
        self.frames.append(indata.copy())

    def get_peak_amplitude(self) -> float:
        """Highest absolute sample seen since start() — compare to SILENCE_PEAK_THRESHOLD."""
        return self.last_peak

    def start(self) -> None:
        """Start recording audio.

        If the configured device fails to open (a renumbered/disconnected index, or a JACK
        clone that errors with PaErrorCode -9999), fall back to the system default device so
        dictation still works instead of failing.
        """
        self.frames = []
        self.last_peak = 0.0

        # Warm path: attach to the shared, already-open stream — capture is instant.
        if self.warm_mic is not None:
            self.device = self.warm_mic.acquire(self._audio_callback)
            self.recording_sample_rate = self.warm_mic.sample_rate
            self._active = True
            return

        fallbacks = []
        if self.device is not None:
            fallbacks.append(self.device)
        sys_default = _system_default_input_index()
        if sys_default is not None and sys_default not in fallbacks:
            fallbacks.append(sys_default)
        fallbacks.append(None)
        last_err = None
        for dev in fallbacks:
            try:
                self._open_stream(dev)
                self.device = dev
                self._active = True
                return
            except Exception as e:
                last_err = e
                if dev is not None:
                    print(f"Audio device {dev} failed to open ({e}); trying next input")
        raise last_err if last_err else RuntimeError("No audio input device could be opened")

    def _open_stream(self, device: int | None) -> None:
        # Find a supported sample rate for this device
        self.recording_sample_rate = get_supported_sample_rate(
            device=device,
            target_rate=self.target_sample_rate
        )

        if self.recording_sample_rate != self.target_sample_rate:
            print(f"Note: Recording at {self.recording_sample_rate}Hz, will resample to {self.target_sample_rate}Hz")

        self.stream = sd.InputStream(
            samplerate=self.recording_sample_rate,
            channels=self.channels,
            device=device,
            dtype=np.float32,
            callback=self._audio_callback,
            latency="high",  # larger input buffer — tolerate duck/jitter stalls (see WarmMic)
        )
        self.stream.start()

    def stop(self, apply_preprocessing: bool | str | None = None) -> str:
        """
        Stop recording and save to a temporary WAV file.

        Args:
            apply_preprocessing: Override instance preprocessing setting
                Can be bool (True="light", False="off") or level string
            
        Returns:
            Path to the temporary WAV file containing the recording.
        """
        self._active = False
        if self.warm_mic is not None:
            self.warm_mic.release()  # detach, leave the stream warm for the next recording
        elif self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None

        if not self.frames:
            raise ValueError("No audio recorded")

        # Concatenate all frames
        audio_data = np.concatenate(self.frames, axis=0)
        
        # Flatten if needed (mono)
        if audio_data.ndim > 1:
            audio_data = audio_data.flatten()
        
        # Resample to target rate if needed (16kHz for Whisper)
        if self.recording_sample_rate and self.recording_sample_rate != self.target_sample_rate:
            audio_data = resample_audio(
                audio_data, 
                self.recording_sample_rate, 
                self.target_sample_rate
            )
        
        # Determine preprocessing level
        level = apply_preprocessing if apply_preprocessing is not None else self.preprocessing
        if level is True:
            level = "light"
        elif level is False:
            level = "off"
        
        # Apply preprocessing (using target sample rate)
        if level and level != "off":
            audio_data = preprocess_audio(audio_data, self.target_sample_rate, level=level)

        # Convert float32 [-1.0, 1.0] to int16
        audio_int16 = (audio_data * 32767).astype(np.int16)

        # Create temporary WAV file under the app-owned private temp dir (0700).
        from wayfinder.utils.fs_security import get_app_temp_dir
        self._temp_file = tempfile.NamedTemporaryFile(
            suffix=".wav", delete=False, dir=str(get_app_temp_dir())
        )
        temp_path = self._temp_file.name

        # Write WAV file at target sample rate (16kHz for Whisper)
        with wave.open(temp_path, "wb") as wav_file:
            wav_file.setnchannels(self.channels)
            wav_file.setsampwidth(2)  # 16-bit = 2 bytes
            wav_file.setframerate(self.target_sample_rate)
            wav_file.writeframes(audio_int16.tobytes())

        return temp_path

    def get_duration(self) -> float:
        """
        Get the duration of recorded audio in seconds.

        Returns:
            Duration in seconds, or 0 if no frames recorded.
        """
        if not self.frames:
            return 0.0
        total_frames = sum(f.shape[0] for f in self.frames)
        return total_frames / self.sample_rate

    def get_audio_level(self) -> float:
        """
        Get current audio level (RMS) for visual feedback.
        
        Returns:
            RMS level between 0.0 and 1.0
        """
        if not self.frames:
            return 0.0
        # Use last few frames for responsiveness
        recent_frames = self.frames[-5:] if len(self.frames) >= 5 else self.frames
        audio = np.concatenate(recent_frames, axis=0)
        rms = np.sqrt(np.mean(audio ** 2))
        return min(1.0, rms * 3)  # Scale up for visibility

    def cleanup(self) -> None:
        """Clean up temporary files."""
        if self._temp_file is not None:
            try:
                Path(self._temp_file.name).unlink(missing_ok=True)
            except Exception:
                pass
            self._temp_file = None

    def is_recording(self) -> bool:
        """Check if currently recording."""
        if self.warm_mic is not None:
            return self._active
        return self.stream is not None and self.stream.active


class ChunkedRecorder:
    """
    Records audio in chunks for indefinite duration sessions.
    
    Features:
    - Automatic chunk extraction at configurable intervals
    - Overlap between chunks to avoid cutting words
    - Background processing via callback
    - No maximum recording duration
    - Automatic resampling to 16kHz for Whisper compatibility
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        device: int | None = None,
        preprocessing: bool | str = "light",
        chunk_duration: float = 30.0,
        chunk_overlap: float = 2.0,
        on_chunk_ready: Callable[[str, int], None] | None = None,
        warm_mic: "WarmMic | None" = None,
    ):
        """
        Initialize the chunked audio recorder.

        Args:
            sample_rate: Target audio sample rate in Hz (default 16000 for whisper)
            channels: Number of audio channels (default 1 for mono)
            device: Audio input device index (None for default)
            preprocessing: Audio preprocessing level:
                - False or "off": No processing
                - True or "light": Only gain normalization (recommended)
                - "medium": Normalization + high-pass filter
                - "heavy": Full processing with noise gate
            chunk_duration: Duration of each chunk in seconds
            chunk_overlap: Overlap between chunks in seconds
            on_chunk_ready: Callback when a chunk is ready (path, chunk_index)
            warm_mic: Optional shared WarmMic. When given, this recorder attaches to the warm
                stream on start() (instant capture) instead of opening its own each time.
        """
        self.target_sample_rate = sample_rate  # What we want (for Whisper)
        self.recording_sample_rate = None  # Will be set when starting
        self.channels = channels
        self.device = device
        # Convert bool to level string for backwards compatibility
        if preprocessing is True:
            self.preprocessing = "light"
        elif preprocessing is False:
            self.preprocessing = "off"
        else:
            self.preprocessing = preprocessing
        self.chunk_duration = chunk_duration
        self.chunk_overlap = chunk_overlap
        self.on_chunk_ready = on_chunk_ready
        
        # Audio buffer (ring buffer style)
        self._buffer: list[np.ndarray] = []
        self._buffer_lock = threading.Lock()
        
        # Chunk management
        self._chunk_queue: queue.Queue = queue.Queue()
        self._chunk_index = 0
        self._last_chunk_end = 0  # Sample position of last chunk end
        
        # Recording state
        self._stream: sd.InputStream | None = None
        self.warm_mic = warm_mic
        self._active = False
        self._stop_event = threading.Event()
        self._chunk_thread: threading.Thread | None = None
        self._temp_files: list[str] = []
        self.last_peak: float = 0.0
        # Count of chunks that failed to save (resample/preprocess/WAV-write error).
        # A nonzero count means a section of audio was dropped; the app layer reads
        # this after stop() and surfaces it to the activity log so a mid-dictation
        # hole is traceable instead of silently vanishing.
        self.dropped_chunk_count: int = 0

        # Calculate samples (will be updated based on actual recording rate)
        self._chunk_samples = int(chunk_duration * sample_rate)
        self._overlap_samples = int(chunk_overlap * sample_rate)

    @property
    def sample_rate(self) -> int:
        """Return the current/target sample rate."""
        return self.recording_sample_rate or self.target_sample_rate

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        """Callback function for sounddevice stream."""
        if status:
            print(f"Audio status: {status}")
        peak = float(np.max(np.abs(indata)))
        if peak > self.last_peak:
            self.last_peak = peak
        with self._buffer_lock:
            self._buffer.append(indata.copy())

    def get_peak_amplitude(self) -> float:
        """Highest absolute sample seen since start() — compare to SILENCE_PEAK_THRESHOLD."""
        return self.last_peak

    def _get_total_samples(self) -> int:
        """Get total number of samples in buffer."""
        with self._buffer_lock:
            return sum(f.shape[0] for f in self._buffer)

    def _extract_chunk(self, start_sample: int, end_sample: int) -> np.ndarray | None:
        """Extract a chunk of audio from the buffer."""
        with self._buffer_lock:
            if not self._buffer:
                return None
            
            # Concatenate buffer
            all_audio = np.concatenate(self._buffer, axis=0)
            
            if end_sample > len(all_audio):
                return None
            
            return all_audio[start_sample:end_sample].copy()

    def _chunk_monitor_thread(self) -> None:
        """Background thread that monitors for ready chunks."""
        while not self._stop_event.is_set():
            total_samples = self._get_total_samples()
            
            # Calculate if we have enough for a new chunk
            next_chunk_end = self._last_chunk_end + self._chunk_samples
            
            if total_samples >= next_chunk_end:
                # We have enough audio for a chunk
                # Start with overlap from previous chunk (except first)
                if self._chunk_index == 0:
                    start = 0
                else:
                    start = self._last_chunk_end - self._overlap_samples
                    start = max(0, start)
                
                end = next_chunk_end
                
                # Extract and save chunk
                chunk_audio = self._extract_chunk(start, end)
                if chunk_audio is not None:
                    chunk_path = self._save_chunk(chunk_audio)
                    if chunk_path:
                        self._chunk_queue.put((chunk_path, self._chunk_index))
                        
                        # Call callback if provided
                        if self.on_chunk_ready:
                            try:
                                self.on_chunk_ready(chunk_path, self._chunk_index)
                            except Exception as e:
                                print(f"Chunk callback error: {e}")
                        
                        self._chunk_index += 1
                        self._last_chunk_end = end
            
            # Small sleep to avoid busy waiting
            self._stop_event.wait(0.5)

    def _save_chunk(self, audio_data: np.ndarray) -> str | None:
        """Save a chunk to a temporary WAV file."""
        try:
            # Flatten if needed (mono)
            if audio_data.ndim > 1:
                audio_data = audio_data.flatten()
            
            # Resample to target rate if needed (16kHz for Whisper)
            if self.recording_sample_rate and self.recording_sample_rate != self.target_sample_rate:
                audio_data = resample_audio(
                    audio_data, 
                    self.recording_sample_rate, 
                    self.target_sample_rate
                )
            
            # Apply preprocessing based on level (using target sample rate)
            if self.preprocessing and self.preprocessing != "off":
                audio_data = preprocess_audio(audio_data, self.target_sample_rate, level=self.preprocessing)
            
            # Convert float32 to int16
            audio_int16 = (audio_data * 32767).astype(np.int16)
            
            # Create temp file under app-owned private temp dir (0700).
            from wayfinder.utils.fs_security import get_app_temp_dir
            temp_file = tempfile.NamedTemporaryFile(
                suffix=f"_chunk{self._chunk_index}.wav",
                delete=False,
                dir=str(get_app_temp_dir()),
            )
            temp_path = temp_file.name
            self._temp_files.append(temp_path)
            
            # Write WAV at target sample rate (16kHz for Whisper)
            with wave.open(temp_path, "wb") as wav_file:
                wav_file.setnchannels(self.channels)
                wav_file.setsampwidth(2)
                wav_file.setframerate(self.target_sample_rate)
                wav_file.writeframes(audio_int16.tobytes())
            
            return temp_path
        except Exception as e:
            # A dropped chunk is a hole in the transcript. Count it so the app layer
            # can surface the loss to the activity log (the recorder stays UI-agnostic
            # — it does not know about the log file, only that a chunk was lost).
            self.dropped_chunk_count += 1
            print(f"Error saving chunk: {e}")
            return None

    def start(self) -> None:
        """Start chunked recording."""
        self._buffer = []
        self._chunk_index = 0
        self._last_chunk_end = 0
        self._temp_files = []
        self.last_peak = 0.0
        self.dropped_chunk_count = 0
        self._stop_event.clear()
        
        # Clear queue
        while not self._chunk_queue.empty():
            try:
                self._chunk_queue.get_nowait()
            except queue.Empty:
                break
        
        if self.warm_mic is not None:
            # Warm path: attach to the shared open stream — capture is instant. Chunk sizes
            # come from the warm stream's actual rate, not the 16k target.
            self.device = self.warm_mic.acquire(self._audio_callback)
            self.recording_sample_rate = self.warm_mic.sample_rate
            self._chunk_samples = int(self.chunk_duration * self.recording_sample_rate)
            self._overlap_samples = int(self.chunk_overlap * self.recording_sample_rate)
        else:
            # Open the audio stream. If the configured device fails (renumbered index, or a JACK
            # clone with PaErrorCode -9999), fall back to the system default so recording still
            # works instead of failing.
            _sys_default = _system_default_input_index()
            _devices = [self.device] if self.device is not None else []
            if _sys_default is not None and _sys_default not in _devices:
                _devices.append(_sys_default)
            _devices.append(None)
            _last_err = None
            for _dev in _devices:
                try:
                    self.recording_sample_rate = get_supported_sample_rate(
                        device=_dev,
                        target_rate=self.target_sample_rate
                    )
                    if self.recording_sample_rate != self.target_sample_rate:
                        print(f"Note: Recording at {self.recording_sample_rate}Hz, will resample to {self.target_sample_rate}Hz")
                    # Recalculate chunk samples based on actual recording rate
                    self._chunk_samples = int(self.chunk_duration * self.recording_sample_rate)
                    self._overlap_samples = int(self.chunk_overlap * self.recording_sample_rate)
                    self._stream = sd.InputStream(
                        samplerate=self.recording_sample_rate,
                        channels=self.channels,
                        device=_dev,
                        dtype=np.float32,
                        callback=self._audio_callback,
                        latency="high",  # larger input buffer — tolerate duck/jitter stalls (see WarmMic)
                    )
                    self._stream.start()
                    self.device = _dev
                    break
                except Exception as e:
                    _last_err = e
                    if _dev is not None:
                        print(f"Audio device {_dev} failed to open ({e}); falling back to system default")
            else:
                raise _last_err if _last_err else RuntimeError("Failed to open any audio input device")

        # Start chunk monitoring thread
        self._chunk_thread = threading.Thread(target=self._chunk_monitor_thread, daemon=True)
        self._chunk_thread.start()
        self._active = True

    def stop(self) -> tuple[str | None, list[str]]:
        """
        Stop recording and return final chunk and all chunk paths.
        
        Returns:
            Tuple of (final_chunk_path, list_of_all_chunk_paths)
        """
        self._active = False
        self._stop_event.set()

        if self.warm_mic is not None:
            self.warm_mic.release()  # detach, leave the stream warm for the next recording
        elif self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        if self._chunk_thread is not None:
            self._chunk_thread.join(timeout=2.0)
            self._chunk_thread = None
        
        # Save any remaining audio as final chunk
        final_path = None
        with self._buffer_lock:
            if self._buffer:
                all_audio = np.concatenate(self._buffer, axis=0)
                
                # Get remaining audio after last chunk
                if self._chunk_index == 0:
                    # No chunks extracted yet, save everything
                    remaining = all_audio
                else:
                    # Save from overlap point before last chunk end
                    start = max(0, self._last_chunk_end - self._overlap_samples)
                    remaining = all_audio[start:]
                
                if len(remaining) > self.sample_rate * 0.5:  # At least 0.5 seconds
                    final_path = self._save_chunk(remaining)
        
        return final_path, self._temp_files.copy()

    def get_duration(self) -> float:
        """Get total duration of recorded audio in seconds."""
        return self._get_total_samples() / self.sample_rate

    def get_chunk_count(self) -> int:
        """Get number of chunks extracted so far."""
        return self._chunk_index

    def get_pending_chunks(self) -> list[tuple[str, int]]:
        """Get all pending chunks from the queue."""
        chunks = []
        while not self._chunk_queue.empty():
            try:
                chunks.append(self._chunk_queue.get_nowait())
            except queue.Empty:
                break
        return chunks

    def get_audio_level(self) -> float:
        """Get current audio level (RMS) for visual feedback."""
        with self._buffer_lock:
            if not self._buffer:
                return 0.0
            # Use last few frames for responsiveness
            recent_frames = self._buffer[-5:] if len(self._buffer) >= 5 else self._buffer
            audio = np.concatenate(recent_frames, axis=0)
            rms = np.sqrt(np.mean(audio ** 2))
            return min(1.0, rms * 3)

    def is_recording(self) -> bool:
        """Check if currently recording."""
        if self.warm_mic is not None:
            return self._active
        return self._stream is not None and self._stream.active

    def cleanup(self) -> None:
        """Clean up all temporary files."""
        for path in self._temp_files:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass
        self._temp_files = []


# =============================================================================
# Audio Calibration
# =============================================================================

class AudioCalibrationResult:
    """Results from audio calibration analysis."""
    
    def __init__(
        self,
        peak_level: float,
        rms_level: float,
        noise_floor: float,
        clipping_detected: bool,
        clipping_percentage: float,
        signal_to_noise: float,
        recommended_preprocessing: str,
        issues: list[str],
        recommendations: list[str],
    ):
        self.peak_level = peak_level
        self.rms_level = rms_level
        self.noise_floor = noise_floor
        self.clipping_detected = clipping_detected
        self.clipping_percentage = clipping_percentage
        self.signal_to_noise = signal_to_noise
        self.recommended_preprocessing = recommended_preprocessing
        self.issues = issues
        self.recommendations = recommendations
    
    @property
    def overall_quality(self) -> str:
        """Get overall audio quality assessment."""
        if self.clipping_detected and self.clipping_percentage > 1.0:
            return "poor"
        elif self.clipping_detected or self.signal_to_noise < 20:
            return "fair"
        elif self.signal_to_noise > 40 and not self.clipping_detected:
            return "excellent"
        else:
            return "good"
    
    @property
    def quality_icon(self) -> str:
        """Get icon for quality level."""
        icons = {
            "excellent": "✅",
            "good": "✅",
            "fair": "⚠️",
            "poor": "❌",
        }
        return icons.get(self.overall_quality, "❓")


def analyze_audio_calibration(
    audio_data: np.ndarray,
    sample_rate: int = 16000,
) -> AudioCalibrationResult:
    """
    Analyze recorded audio to detect issues and recommend settings.
    
    Args:
        audio_data: Audio samples as float32 array (-1.0 to 1.0)
        sample_rate: Sample rate in Hz
        
    Returns:
        AudioCalibrationResult with analysis and recommendations
    """
    # Flatten if multi-channel
    if audio_data.ndim > 1:
        audio_data = audio_data.flatten()
    
    issues = []
    recommendations = []
    
    # === Basic level analysis ===
    peak_level = float(np.max(np.abs(audio_data)))
    rms_level = float(np.sqrt(np.mean(audio_data ** 2)))
    
    # === Clipping detection ===
    # Samples very close to or at max level indicate clipping
    clipping_threshold = 0.99
    clipping_samples = np.sum(np.abs(audio_data) >= clipping_threshold)
    total_samples = len(audio_data)
    clipping_percentage = (clipping_samples / total_samples) * 100
    clipping_detected = clipping_percentage > 0.1  # More than 0.1% clipping
    
    # === Noise floor estimation ===
    # Sort samples by absolute value and take the 10th percentile as noise floor
    sorted_abs = np.sort(np.abs(audio_data))
    noise_floor_idx = int(len(sorted_abs) * 0.1)
    noise_floor = float(sorted_abs[noise_floor_idx]) if noise_floor_idx < len(sorted_abs) else 0.001
    
    # Ensure noise floor isn't zero for SNR calculation
    noise_floor = max(noise_floor, 0.0001)
    
    # === Signal-to-noise ratio (rough estimate) ===
    # Use RMS as signal, noise floor as noise
    if noise_floor > 0:
        signal_to_noise = 20 * np.log10(rms_level / noise_floor) if rms_level > noise_floor else 0
    else:
        signal_to_noise = 60  # Assume good if noise floor is 0
    
    # === Issue detection and recommendations ===
    
    # Clipping issues
    if clipping_percentage > 5.0:
        issues.append(f"Severe clipping detected ({clipping_percentage:.1f}% of samples)")
        recommendations.append("Turn down your microphone gain significantly")
        recommendations.append("Move the microphone further from your mouth")
    elif clipping_percentage > 1.0:
        issues.append(f"Clipping detected ({clipping_percentage:.1f}% of samples)")
        recommendations.append("Turn down your microphone gain slightly")
    elif clipping_detected:
        issues.append(f"Minor clipping detected ({clipping_percentage:.2f}% of samples)")
        recommendations.append("Consider reducing microphone gain")
    
    # Signal level issues
    if peak_level < 0.1:
        issues.append("Signal is very quiet")
        recommendations.append("Turn up your microphone gain")
        recommendations.append("Move closer to the microphone")
    elif peak_level < 0.3:
        issues.append("Signal is somewhat quiet")
        recommendations.append("Consider turning up microphone gain slightly")
    
    # Noise floor issues
    if noise_floor > 0.05:
        issues.append("High background noise detected")
        recommendations.append("Try to reduce background noise in your environment")
        recommendations.append("Use a noise gate or 'Heavy' audio preprocessing")
    elif noise_floor > 0.02:
        issues.append("Moderate background noise")
        recommendations.append("Consider using 'Medium' audio preprocessing")
    
    # === Determine recommended preprocessing ===
    if clipping_detected and clipping_percentage > 1.0:
        # Hot signal - don't normalize, it will make clipping worse
        recommended_preprocessing = "off"
        recommendations.insert(0, "Disable audio preprocessing (normalization makes clipping worse)")
    elif peak_level < 0.2:
        # Quiet signal - normalization will help
        recommended_preprocessing = "light"
        if "Consider turning up microphone gain" not in recommendations:
            recommendations.append("Audio normalization will boost your signal")
    elif noise_floor > 0.03:
        # Noisy environment - use filtering
        recommended_preprocessing = "medium"
    elif noise_floor > 0.05:
        # Very noisy - use noise gate
        recommended_preprocessing = "heavy"
    else:
        # Normal signal - light preprocessing is fine
        recommended_preprocessing = "light"
    
    # Add positive feedback if everything looks good
    if not issues:
        if signal_to_noise > 40:
            issues.append("Excellent audio quality!")
        elif signal_to_noise > 30:
            issues.append("Good audio quality")
        else:
            issues.append("Audio quality is acceptable")
    
    return AudioCalibrationResult(
        peak_level=peak_level,
        rms_level=rms_level,
        noise_floor=noise_floor,
        clipping_detected=clipping_detected,
        clipping_percentage=clipping_percentage,
        signal_to_noise=signal_to_noise,
        recommended_preprocessing=recommended_preprocessing,
        issues=issues,
        recommendations=recommendations,
    )


class AudioCalibrator:
    """
    Records and analyzes audio for calibration purposes.
    
    Usage:
        calibrator = AudioCalibrator(device=device_id)
        calibrator.start()
        # ... wait for user to speak ...
        result = calibrator.stop_and_analyze()
        print(result.recommendations)
    """
    
    def __init__(
        self,
        sample_rate: int = 16000,
        device: int | None = None,
    ):
        self.target_sample_rate = sample_rate
        self.device = device
        self.recording_sample_rate = None
        self._frames: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
    
    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        """Callback for audio stream."""
        if status:
            print(f"Audio calibration status: {status}")
        self._frames.append(indata.copy())
    
    def start(self) -> None:
        """Start recording for calibration."""
        self._frames = []
        
        # Find supported sample rate
        self.recording_sample_rate = get_supported_sample_rate(
            device=self.device,
            target_rate=self.target_sample_rate,
        )
        
        self._stream = sd.InputStream(
            samplerate=self.recording_sample_rate,
            channels=1,
            device=self.device,
            dtype=np.float32,
            callback=self._audio_callback,
            latency="high",  # larger input buffer — tolerate duck/jitter stalls (see WarmMic)
        )
        self._stream.start()
    
    def stop_and_analyze(self) -> AudioCalibrationResult:
        """Stop recording and analyze the audio."""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        
        if not self._frames:
            # Return empty result
            return AudioCalibrationResult(
                peak_level=0,
                rms_level=0,
                noise_floor=0,
                clipping_detected=False,
                clipping_percentage=0,
                signal_to_noise=0,
                recommended_preprocessing="light",
                issues=["No audio recorded"],
                recommendations=["Try recording again"],
            )
        
        # Concatenate frames
        audio_data = np.concatenate(self._frames, axis=0)
        
        # Flatten if multi-channel
        if audio_data.ndim > 1:
            audio_data = audio_data.flatten()
        
        # Resample if needed
        if self.recording_sample_rate != self.target_sample_rate:
            audio_data = resample_audio(
                audio_data,
                self.recording_sample_rate,
                self.target_sample_rate,
            )
        
        return analyze_audio_calibration(audio_data, self.target_sample_rate)
    
    def get_current_level(self) -> float:
        """Get current audio level for live feedback."""
        if not self._frames:
            return 0.0
        recent_frames = self._frames[-5:] if len(self._frames) >= 5 else self._frames
        audio = np.concatenate(recent_frames, axis=0)
        rms = np.sqrt(np.mean(audio ** 2))
        return min(1.0, rms * 3)
    
    def get_duration(self) -> float:
        """Get duration of recorded audio in seconds."""
        if not self._frames:
            return 0.0
        total_samples = sum(f.shape[0] for f in self._frames)
        rate = self.recording_sample_rate or self.target_sample_rate
        return total_samples / rate
    
    def is_recording(self) -> bool:
        """Check if currently recording."""
        return self._stream is not None and self._stream.active
