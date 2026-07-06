#!/usr/bin/env python3
"""Generate the golden-audio corpus deterministically from the manifest.

Synthetic speech only — license-clean and safe to commit to a public repo (no real
recorded voice). Prefers piper (natural, if installed); otherwise uses espeak-ng
(robotic but intelligible and always reproducible). Silence clips are written
directly. Output: 16 kHz mono WAVs next to this file, one per manifest entry.

Usage:
    PYTHONPATH=src python tests/golden_audio/generate.py [--voice espeak|piper]

Requires: espeak-ng + ffmpeg (default path), OR piper + its voice model + ffmpeg.
Re-running is idempotent: identical inputs → identical WAVs.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from manifest import CLIPS, clip_path  # noqa: E402

SAMPLE_RATE = 16000


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _write_silence(path: Path, seconds: float = 2.0) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(b"\x00\x00" * int(SAMPLE_RATE * seconds))


def _resample_to_16k_mono(src: Path, dst: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         "-ar", str(SAMPLE_RATE), "-ac", "1", str(dst)],
        check=True,
    )


def _synth_espeak(text: str, dst: Path, tmp: Path) -> None:
    # Fixed voice + rate → deterministic output. en-us, 150 wpm.
    subprocess.run(
        ["espeak-ng", "-v", "en-us", "-s", "150", "-w", str(tmp), text],
        check=True,
    )
    _resample_to_16k_mono(tmp, dst)


def _synth_piper(text: str, dst: Path, tmp: Path, model: str) -> None:
    # piper reads text on stdin, writes a WAV. Model path via --model.
    with open(tmp, "wb") as f:
        subprocess.run(["piper", "--model", model, "--output_file", str(tmp)],
                       input=text.encode(), check=True)
    _resample_to_16k_mono(tmp, dst)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--voice", choices=["auto", "espeak", "piper"], default="auto")
    ap.add_argument("--piper-model", default="",
                    help="piper .onnx voice model path (required for --voice piper)")
    args = ap.parse_args()

    if not _have("ffmpeg"):
        print("ERROR: ffmpeg is required (resamples synth output to 16kHz mono).",
              file=sys.stderr)
        return 2

    voice = args.voice
    if voice == "auto":
        voice = "piper" if (_have("piper") and args.piper_model) else "espeak"
    if voice == "espeak" and not _have("espeak-ng"):
        print("ERROR: espeak-ng not found. Install it, or use --voice piper.",
              file=sys.stderr)
        return 2
    if voice == "piper" and not (_have("piper") and args.piper_model):
        print("ERROR: --voice piper needs both the piper binary and --piper-model.",
              file=sys.stderr)
        return 2

    print(f"[golden] synthesizing {len(CLIPS)} clips with '{voice}'")
    tmp = clip_path("_tmp_raw")
    for clip in CLIPS:
        dst = clip_path(clip["id"])
        if clip["category"] == "silence":
            _write_silence(dst)
            print(f"  {clip['id']:14s} silence -> {dst.name}")
            continue
        text = clip["reference_text"]
        if voice == "piper":
            _synth_piper(text, dst, tmp, args.piper_model)
        else:
            _synth_espeak(text, dst, tmp)
        print(f"  {clip['id']:14s} {len(text):3d} chars -> {dst.name}")
    if tmp.exists():
        tmp.unlink()
    print("[golden] done. Commit the .wav files (they are synthetic + license-clean).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
