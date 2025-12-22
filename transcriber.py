"""
Transcription module for Wayfinder Voice.
Interfaces with whisper.cpp for CPU-based speech-to-text.
"""

import os
import subprocess
from pathlib import Path


class TranscriptionError(Exception):
    """Raised when transcription fails."""

    pass


def transcribe(
    audio_path: str,
    whisper_binary: str = "~/whisper.cpp/build/bin/whisper-cli",
    model_path: str = "~/whisper.cpp/models/ggml-small.bin",
    prompt: str = "Hello, this is a dictation with proper punctuation and grammar.",
    threads: int = 6,
    timeout: int = 120,
) -> str:
    """
    Transcribe an audio file using whisper.cpp.

    Args:
        audio_path: Path to the WAV file to transcribe
        whisper_binary: Path to the whisper.cpp main binary
        model_path: Path to the GGML model file
        prompt: Initial prompt for better punctuation/grammar
        threads: Number of CPU threads to use
        timeout: Maximum seconds to wait for transcription

    Returns:
        Transcribed text string

    Raises:
        TranscriptionError: If transcription fails
    """
    # Expand ~ to home directory
    whisper_binary = os.path.expanduser(whisper_binary)
    model_path = os.path.expanduser(model_path)

    # Validate paths
    if not Path(whisper_binary).exists():
        raise TranscriptionError(f"whisper.cpp binary not found: {whisper_binary}")
    if not Path(model_path).exists():
        raise TranscriptionError(f"Model file not found: {model_path}")
    if not Path(audio_path).exists():
        raise TranscriptionError(f"Audio file not found: {audio_path}")

    cmd = [
        whisper_binary,
        "-m",
        model_path,
        "-f",
        audio_path,
        "--no-timestamps",
        "--prompt",
        prompt,
        "-t",
        str(threads),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        if result.returncode != 0:
            raise TranscriptionError(f"whisper.cpp failed: {result.stderr}")

        # Parse output - whisper.cpp outputs text to stdout
        # Filter out any metadata lines and get the actual transcription
        output_lines = result.stdout.strip().split("\n")
        
        # whisper.cpp outputs the transcription after some processing info
        # The actual text is typically in lines that don't start with special markers
        text_lines = []
        for line in output_lines:
            line = line.strip()
            # Skip empty lines and whisper.cpp info lines
            if not line:
                continue
            # Skip common whisper.cpp output prefixes
            if any(
                line.startswith(prefix)
                for prefix in [
                    "whisper_",
                    "main:",
                    "system_info:",
                    "operator():",
                    "log_mel_spectrogram",
                ]
            ):
                continue
            text_lines.append(line)

        transcription = " ".join(text_lines).strip()
        return transcription

    except subprocess.TimeoutExpired:
        raise TranscriptionError(f"Transcription timed out after {timeout} seconds")
    except FileNotFoundError:
        raise TranscriptionError(f"Could not execute whisper.cpp: {whisper_binary}")


def transcribe_with_config(audio_path: str, config: dict) -> str:
    """
    Transcribe using settings from config dictionary.

    Args:
        audio_path: Path to the WAV file to transcribe
        config: Configuration dictionary with whisper settings

    Returns:
        Transcribed text string
    """
    return transcribe(
        audio_path=audio_path,
        whisper_binary=config.get("whisper_binary", "~/whisper.cpp/build/bin/whisper-cli"),
        model_path=config.get("model_path", "~/whisper.cpp/models/ggml-small.bin"),
        prompt=config.get(
            "prompt", "Hello, this is a dictation with proper punctuation and grammar."
        ),
        threads=config.get("threads", 6),
        timeout=config.get("timeout", 120),
    )



