#!/usr/bin/env python3
"""End-to-end Windows verification for Wayfinder Aura.

Runs the real record -> transcribe -> type pipeline on this machine and prints a
PASS / FAIL / SKIP report, so "does the Windows build actually work?" has an
automated answer instead of only manual testing. No microphone is needed:
speech is synthesized with the Windows built-in TTS and fed through the app's
own transcription path.

    python scripts/verify_windows.py            # full run
    python scripts/verify_windows.py --no-ui    # skip the focus-stealing injection checks

Exit code 0 when nothing FAILED (SKIP is not a failure), 1 otherwise.

Some checks (injection round-trips) briefly take keyboard focus to type into a
throwaway window they own. Close nothing; it cleans up after itself.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PHRASE = "The quick brown fox jumps over the lazy dog."
KEY_WORDS = ("quick", "brown", "fox", "lazy", "dog")

results: list[tuple[str, str, str]] = []  # (name, status, detail)


def record(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    icon = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP"}[status]
    line = f"  [{icon}] {name}"
    if detail:
        line += f" — {detail}"
    print(line, flush=True)


def check(name: str):
    """Decorator-ish runner: call fn, translate return/raises into a result row.

    fn returns (status, detail) or raises -> FAIL.
    """
    def run(fn):
        try:
            status, detail = fn()
        except Exception as e:  # noqa: BLE001 - report any failure, keep going
            record(name, "FAIL", f"{type(e).__name__}: {e}")
            return
        record(name, status, detail)
    return run


# ---------------------------------------------------------------------------
# Speech fixture (Windows SAPI -> 16 kHz mono WAV)
# ---------------------------------------------------------------------------

def synthesize_speech(text: str, out_path: Path) -> bool:
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo("
        "16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
        "[System.Speech.AudioFormat.AudioChannel]::Mono); "
        f"$s.SetOutputToWaveFile('{out_path}', $fmt); "
        f"$s.Speak('{text}'); $s.Dispose()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True, timeout=60,
    )
    return out_path.exists() and out_path.stat().st_size > 1000


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_platform():
    from wayfinder.utils.platform import (
        get_cache_dir,
        get_config_dir,
        get_data_dir,
        get_platform,
    )
    if get_platform() != "windows":
        return "FAIL", f"get_platform()={get_platform()!r}, expected 'windows'"
    appdata = Path(os.environ["APPDATA"])
    localappdata = Path(os.environ["LOCALAPPDATA"])
    assert get_config_dir() == appdata / "wayfinder-aura", get_config_dir()
    assert get_data_dir() == localappdata / "wayfinder-aura", get_data_dir()
    assert get_cache_dir() == localappdata / "wayfinder-aura" / "cache", get_cache_dir()
    return "PASS", "platform=windows; config/data/cache dirs correct"


def check_injector_identifier():
    from wayfinder.utils.platform import get_text_injector
    val = get_text_injector()
    if val != "windows":
        return "FAIL", f"get_text_injector()={val!r}, expected 'windows'"
    return "PASS", "native SendInput/paste adapter advertised"


def check_hotkey_backend():
    from wayfinder_main import resolve_hotkey_backend
    val = resolve_hotkey_backend("win32", False, False, "")
    if val != "pynput":
        return "FAIL", f"resolve_hotkey_backend(win32)={val!r}, expected 'pynput'"
    return "PASS", "Windows resolves to the pynput global listener"


def check_pynput_capture():
    from pynput import keyboard

    from wayfinder.core import injector_windows as w
    seen: set = set()

    def on_press(k):
        # Regular keys arrive as KeyCode (has .vk); special keys (F9) as Key.
        if isinstance(k, keyboard.KeyCode):
            seen.add(k.vk)
        else:
            seen.add(str(k))

    listener = keyboard.Listener(on_press=on_press)
    listener.start()
    time.sleep(0.5)
    w._send(w._vkey_inputs(0x4A))  # 'J' (a char key)
    time.sleep(0.15)
    w._send(w._vkey_inputs(0x78))  # F9 (a special key -> Key.f9)
    time.sleep(0.15)
    time.sleep(0.4)
    listener.stop()
    if 0x4A in seen and "Key.f9" in seen:
        return "PASS", "pynput captured a char key and F9 globally (hotkey path works)"
    return "FAIL", f"pynput captured {sorted(map(str, seen))} (wanted 'J' + Key.f9)"


def check_whisper_transcription(wav: Path):
    from wayfinder.config import load_config
    from wayfinder.core.transcriber import transcribe_with_config
    from wayfinder.utils.platform import get_default_whisper_binary
    binary = Path(os.path.expanduser(get_default_whisper_binary()))
    if not binary.exists():
        return "SKIP", f"whisper-cli not installed at {binary}"
    config = load_config()
    model = Path(os.path.expanduser(str(config.get("model_path", ""))))
    if not model.exists():
        return "SKIP", f"model not found at {model}"
    text = transcribe_with_config(str(wav), config, skip_post_processing=True) or ""
    lower = text.lower()
    hits = [w for w in KEY_WORDS if w in lower]
    if len(hits) >= 4:
        return "PASS", f"transcribed via app pipeline: {text.strip()!r}"
    return "FAIL", f"weak transcript {text.strip()!r} (matched {hits})"


def check_recorder_no_lock(wav: Path):
    """The recorder must release the WAV so whisper-cli (a separate process)
    can read it — the Windows file-lock regression."""
    import numpy as np

    from wayfinder.config import load_config
    from wayfinder.core.recorder import AudioRecorder
    from wayfinder.utils.platform import get_default_whisper_binary

    model_path = os.path.expanduser(str(load_config().get("model_path", "")))

    with wave.open(str(wav), "rb") as wf:
        n = wf.getnframes()
        rate = wf.getframerate()
        pcm = np.frombuffer(wf.readframes(n), dtype=np.int16)
    audio = (pcm.astype(np.float32) / 32768.0)

    rec = AudioRecorder(sample_rate=16000, channels=1)
    rec.warm_mic = None
    rec.stream = None
    rec.recording_sample_rate = rate
    rec.preprocessing = "off"
    rec.frames = [audio]
    out_path = rec.stop()  # writes WAV and (post-fix) closes the reservation handle

    if not Path(out_path).exists():
        return "FAIL", "recorder.stop() produced no file"
    binary = Path(os.path.expanduser(get_default_whisper_binary()))
    try:
        if binary.exists():
            # A separate process reading the file proves it is not locked.
            proc = subprocess.run(
                [str(binary), "-m", model_path, "-f", out_path, "-nt"],
                capture_output=True, text=True, timeout=120,
            )
            if "failed to read audio file" in (proc.stdout + proc.stderr).lower():
                return "FAIL", "whisper-cli could not read the recorder's WAV (still locked)"
            got = proc.stdout.strip().splitlines()[-1].strip() if proc.stdout.strip() else ""
            return "PASS", f"recorder WAV readable by whisper-cli: {got!r}"
        # No whisper binary: at least prove another handle can open it read-write.
        with open(out_path, "rb+"):
            pass
        return "PASS", "recorder WAV is not locked (opened by a second handle)"
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _tk_roundtrip(method: str):
    """Type/paste into a throwaway Tk Text we own; return the exact text read back."""
    import ctypes
    import tkinter as tk

    from wayfinder.core import injector_windows as w

    user32 = ctypes.windll.user32
    title = "AURA_VERIFY_TARGET_7731"
    text = "verify cafe 123 - the quick brown fox."
    box = {}

    root = tk.Tk()
    root.title(title)
    root.geometry("640x180+120+120")
    root.attributes("-topmost", True)
    widget = tk.Text(root, font=("Consolas", 12))
    widget.pack(fill="both", expand=True)

    def focus_self():
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            user32.ShowWindow(hwnd, 9)
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 0x0002, 0)
            user32.SetForegroundWindow(hwnd)
        widget.focus_force()

    def run():
        focus_self()
        time.sleep(0.35)
        try:
            if method == "type":
                w.inject_text_windows(text, typing_speed="instant")
            else:
                w.inject_text_paste_windows(text)
        except Exception as e:  # noqa: BLE001
            box["err"] = repr(e)
        root.after(500, capture)

    def capture():
        box["got"] = widget.get("1.0", "end").strip()
        root.destroy()

    root.after(500, run)
    root.mainloop()
    return text, box.get("got", ""), box.get("err")


def check_injection_typing():
    expected, got, err = _tk_roundtrip("type")
    if err:
        return "FAIL", f"raised {err}"
    if got == expected:
        return "PASS", "SendInput typed the exact text into a live control"
    return "FAIL", f"got {got!r}, expected {expected!r}"


def check_injection_paste():
    """The paste path's Windows-specific mechanic is the clipboard write/read/
    restore; Ctrl+V itself is ordinary synthetic input already covered by the
    typing round-trip. Verify an exact Unicode clipboard round-trip and that the
    prior clipboard is put back afterward."""
    from wayfinder.core import injector_windows as w
    prior = w._clipboard_get_windows()
    marker = "AURA cafe 123 - roundtrip ✓ \U0001f600"
    if not w._clipboard_set_windows(marker):
        return "FAIL", "could not write the Windows clipboard"
    got = w._clipboard_get_windows()
    if prior is not None:  # paste path restores the previous clipboard too
        w._clipboard_set_windows(prior)
        if w._clipboard_get_windows() != prior:
            return "FAIL", "clipboard not restored to prior contents"
    if got == marker:
        return "PASS", "exact Unicode clipboard round-trip + restore (paste mechanics)"
    return "FAIL", f"clipboard read back {got!r}, expected {marker!r}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-ui", action="store_true",
                        help="skip the injection round-trips that briefly take focus")
    args = parser.parse_args()

    if sys.platform != "win32":
        print("verify_windows.py only runs on Windows.")
        return 0

    print("=" * 64)
    print("  WAYFINDER AURA — WINDOWS VERIFICATION")
    print("=" * 64)

    print("\nStatic wiring:")
    check("Platform detection & directories")(check_platform)
    check("Text-injection identifier")(check_injector_identifier)
    check("Hotkey backend selection")(check_hotkey_backend)

    print("\nInput capture:")
    check("pynput global key capture")(check_pynput_capture)

    print("\nSpeech pipeline (synthesized audio, no mic):")
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "verify.wav"
        if synthesize_speech(PHRASE, wav):
            check("Whisper transcription (app pipeline)")(lambda: check_whisper_transcription(wav))
            check("Recorder releases WAV (no file lock)")(lambda: check_recorder_no_lock(wav))
        else:
            record("Whisper transcription (app pipeline)", "SKIP", "TTS synthesis unavailable")
            record("Recorder releases WAV (no file lock)", "SKIP", "TTS synthesis unavailable")

    print("\nText injection:")
    check("Clipboard round-trip (paste mechanics)")(check_injection_paste)
    if args.no_ui:
        record("SendInput typing round-trip (live control)", "SKIP", "--no-ui")
    else:
        check("SendInput typing round-trip (live control)")(check_injection_typing)

    passed = sum(1 for _, s, _ in results if s == "PASS")
    failed = sum(1 for _, s, _ in results if s == "FAIL")
    skipped = sum(1 for _, s, _ in results if s == "SKIP")
    print("\n" + "=" * 64)
    print(f"  {passed} PASSED   {failed} FAILED   {skipped} SKIPPED")
    print("=" * 64)
    if failed:
        print("  FAILURES:")
        for name, status, detail in results:
            if status == "FAIL":
                print(f"    - {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
