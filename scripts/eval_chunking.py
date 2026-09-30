#!/usr/bin/env python3
"""Compare whole-file ASR with Aura's overlapping chunk strategy.

This is an accuracy evaluator, not a hardware benchmark. It slices a known WAV
exactly like ``ChunkedRecorder``, carries prior-chunk prompt context, joins text
with the production boundary code, and reports WER against a reference. The
default audio is the repository's 21-second ``long_clean`` golden clip.

Examples:
    PYTHONPATH=src:scripts python scripts/eval_chunking.py \
        --whisper-binary /path/to/whisper-cli --model /path/to/ggml-base.en.bin
    PYTHONPATH=src:scripts python scripts/eval_chunking.py \
        --profiles 10:1,15:2,30:2 --device cpu --json /tmp/chunking.json
    # Auto mode (the Ultra default): one request up to 30 s, then 15 s/2 s,
    # through the app's resident whisper-server (docs/EVAL-2026-09-30-chunking.md):
    PYTHONPATH=src:scripts python scripts/eval_chunking.py --server \
        --model ggml-large-v3-turbo-q5_0.bin --audio long.wav \
        --reference "$(cat long.txt)" --profiles 15:2:30

--server goes through transcribe_with_config() and the resident whisper-server,
as the app does, with scripts/eval_matrix.py's binaries, eval model cache and
EVAL-ONLY licence stub (see that module's docstring; never used by the app).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for path in (REPO / "src", REPO / "scripts", REPO / "tests" / "golden_audio"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from manifest import CLIPS, clip_path  # noqa: E402
from tone_eval.metrics import wer  # noqa: E402
from wayfinder.core.chunking import (  # noqa: E402
    chunk_prompt_context,
    chunk_text_looks_truncated,
    deduplicate_overlap_text,
    prefer_fuller_chunk_text,
)
from wayfinder.core.recorder import get_wav_duration_seconds  # noqa: E402
from wayfinder.core.transcriber import (  # noqa: E402
    WhisperCppBackend,
    clean_whisper_artifacts,
    normalize_whisper_caps,
    whisper_decoding,
)
from wayfinder.utils.runtime_assets import find_whisper_binary  # noqa: E402


def _find_base_model(explicit: str) -> str | None:
    if explicit and Path(explicit).expanduser().is_file():
        return str(Path(explicit).expanduser())
    names = ("ggml-base.en.bin", "ggml-base.bin")
    dirs = (
        Path.home() / ".local/share/wayfinder-aura/whisper-models",
        Path.home() / "whisper.cpp/models",
        Path("/app/share/whisper-models"),
    )
    for directory in dirs:
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)
    return None


def _parse_profiles(value: str) -> list[tuple[float, float, float]]:
    """duration:overlap[:first] - first is Auto's longer first boundary."""
    profiles: list[tuple[float, float, float]] = []
    for raw in value.split(","):
        try:
            parts = [float(part) for part in raw.strip().split(":")]
            if len(parts) not in (2, 3):
                raise ValueError
            duration, overlap = parts[0], parts[1]
            first = parts[2] if len(parts) == 3 else duration
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(
                f"invalid profile {raw!r}; expected duration:overlap[:first]"
            ) from None
        if duration <= 0 or overlap < 0 or overlap >= duration or first < duration:
            raise argparse.ArgumentTypeError(
                f"invalid profile {raw!r}; require first >= duration > overlap >= 0"
            )
        profiles.append((duration, overlap, first))
    if not profiles:
        raise argparse.ArgumentTypeError("at least one chunk profile is required")
    return profiles


def _golden_reference(audio: Path) -> str | None:
    for clip in CLIPS:
        if clip_path(clip["id"]).resolve() == audio.resolve():
            return str(clip["reference_text"])
    return None


def _write_slice(
    source: wave.Wave_read,
    params,
    start_frame: int,
    end_frame: int,
    destination: Path,
) -> None:
    source.setpos(start_frame)
    frames = source.readframes(max(0, end_frame - start_frame))
    with wave.open(str(destination), "wb") as output:
        output.setparams(params)
        output.writeframes(frames)


def _slice_like_recorder(
    audio: Path,
    directory: Path,
    chunk_duration: float,
    chunk_overlap: float,
    first_chunk: float | None = None,
) -> list[Path]:
    """Create the same full chunks + final tail used by ``ChunkedRecorder``."""
    paths: list[Path] = []
    with wave.open(str(audio), "rb") as source:
        params = source.getparams()
        rate = source.getframerate()
        total = source.getnframes()
        chunk_frames = int(chunk_duration * rate)
        first_frames = int((first_chunk or chunk_duration) * rate)
        overlap_frames = int(chunk_overlap * rate)
        last_end = 0
        index = 0

        while total >= (first_frames if index == 0 else last_end + chunk_frames):
            end = first_frames if index == 0 else last_end + chunk_frames
            start = 0 if index == 0 else max(0, last_end - overlap_frames)
            destination = directory / f"chunk-{index:03d}.wav"
            _write_slice(source, params, start, end, destination)
            paths.append(destination)
            last_end = end
            index += 1

        # Match the fixed runtime: overlap is included for context, but it cannot
        # by itself make an empty tail eligible for another transcription.
        new_frames = total if index == 0 else max(0, total - last_end)
        if new_frames > rate * 0.5:
            start = 0 if index == 0 else max(0, last_end - overlap_frames)
            destination = directory / f"chunk-{index:03d}.wav"
            _write_slice(source, params, start, total, destination)
            paths.append(destination)
    return paths


class _ServerBackend:
    """transcribe_with_config() on the resident whisper-server, like the app."""

    def __init__(self, config: dict):
        self.config = config

    def transcribe(self, audio_path: str, context: str = "") -> str:
        import contextlib
        import io

        from wayfinder.core.transcriber import transcribe_with_config

        with contextlib.redirect_stdout(io.StringIO()):
            return transcribe_with_config(
                audio_path, self.config, context=context, skip_post_processing=True
            )


def _server_backend(model_arg: str, use_gpu: bool, parser):
    import atexit

    import eval_matrix
    from wayfinder.core.transcriber import WhisperServerBackend

    eval_matrix._install_eval_gate()  # EVAL ONLY, see eval_matrix's docstring
    model = Path(model_arg).expanduser()
    if not model.is_file():
        model = eval_matrix.MODELS_DIR / "whisper" / model_arg
    if not model.is_file():
        parser.error(f"model not found: {model_arg}")
    config = eval_matrix.asr_config(model, use_gpu)
    atexit.register(WhisperServerBackend.shutdown)
    return _ServerBackend(config), config["whisper_binary"], str(model)


def _transcribe(backend: WhisperCppBackend, audio: Path, context: str = "") -> tuple[str, float]:
    started = time.monotonic()
    raw = backend.transcribe(str(audio), context=context)
    elapsed = time.monotonic() - started
    text = normalize_whisper_caps(clean_whisper_artifacts(raw))
    return text, elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--audio",
        default=str(REPO / "tests/golden_audio/long_clean.wav"),
        help="16-bit PCM WAV with known speech",
    )
    parser.add_argument("--reference", default="", help="ground-truth text")
    parser.add_argument("--model", default="", help="base Whisper GGML model")
    parser.add_argument("--whisper-binary", default="", help="whisper-cli path")
    parser.add_argument("--device", choices=("gpu", "cpu"), default="gpu")
    parser.add_argument(
        "--profiles",
        type=_parse_profiles,
        default=_parse_profiles("10:1,15:2,30:2"),
        help="comma-separated duration:overlap pairs",
    )
    parser.add_argument("--json", default="", help="write machine-readable results")
    parser.add_argument(
        "--server", action="store_true",
        help="use the app's whisper-server path (eval_matrix binaries and model cache)",
    )
    args = parser.parse_args()

    audio = Path(args.audio).expanduser()
    if not audio.is_file():
        parser.error(f"audio not found: {audio}")
    reference = args.reference.strip() or _golden_reference(audio)
    if reference is None:
        parser.error("--reference is required for audio outside the golden corpus")

    use_gpu = args.device == "gpu"
    if args.server:
        backend, binary, model = _server_backend(args.model, use_gpu, parser)
    else:
        binary = args.whisper_binary.strip() or find_whisper_binary({}, cpu=not use_gpu)
        model = _find_base_model(args.model)
        if not binary or not Path(binary).is_file():
            parser.error("whisper-cli not found; pass --whisper-binary")
        if not model:
            parser.error("base model not found; pass --model")

        # The app's own decoding (greedy since docs/EVAL-2026-09-24.md).
        beam_size, best_of = whisper_decoding({})
        backend = WhisperCppBackend(
            whisper_binary=binary,
            model_path=model,
            language="en",
            use_gpu=use_gpu,
            beam_size=beam_size,
            best_of=best_of,
        )
    whole_text, whole_seconds = _transcribe(backend, audio)
    report = {
        "audio": str(audio),
        "binary": binary,
        "model": model,
        "device": args.device,
        "whole": {
            "wer": wer(reference, whole_text),
            "seconds": round(whole_seconds, 3),
            "text": whole_text,
        },
        "profiles": [],
    }

    for duration, overlap, first in args.profiles:
        with tempfile.TemporaryDirectory(prefix="wayfinder-chunk-eval-") as temp:
            chunk_paths = _slice_like_recorder(
                audio, Path(temp), duration, overlap, first
            )
            texts: list[str] = []
            timings: list[float] = []
            context = ""
            for chunk in chunk_paths:
                text, seconds = _transcribe(backend, chunk, context=context)
                # Same guard as WayfinderApp._transcribe_chunk.
                if context and chunk_text_looks_truncated(
                    text, get_wav_duration_seconds(chunk)
                ):
                    retry, retry_seconds = _transcribe(backend, chunk)
                    text = prefer_fuller_chunk_text(text, retry)
                    seconds += retry_seconds
                texts.append(text)
                timings.append(seconds)
                context = chunk_prompt_context(text)
        combined = deduplicate_overlap_text(texts)
        report["profiles"].append(
            {
                "chunk_duration": duration,
                "chunk_overlap": overlap,
                "first_chunk": first,
                "chunk_count": len(texts),
                "wer": wer(reference, combined),
                "chunk_seconds": [round(value, 3) for value in timings],
                "text": combined,
                "chunks": texts,
            }
        )

    print(
        f"whole-file  WER={report['whole']['wer']:.3f}  "
        f"time={report['whole']['seconds']:.3f}s"
    )
    for profile in report["profiles"]:
        print(
            f"{profile['chunk_duration']:g}s/{profile['chunk_overlap']:g}s"
            f" first {profile['first_chunk']:g}s  WER={profile['wer']:.3f}  chunks={profile['chunk_count']}  "
            f"times={profile['chunk_seconds']}"
        )
    print("\nWhole:", report["whole"]["text"])
    for profile in report["profiles"]:
        print(
            f"\n{profile['chunk_duration']:g}s/{profile['chunk_overlap']:g}s:",
            profile["text"],
        )

    if args.json:
        destination = Path(args.json).expanduser()
        destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
