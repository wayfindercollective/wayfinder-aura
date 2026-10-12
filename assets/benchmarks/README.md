`coreml_probe.wav` is the project's existing deterministic synthetic
`tests/golden_audio/short_clean.wav` (espeak-generated, no recorded person).
The Mac encoder comparison uses it locally; it never records a microphone,
injects text, adds History entries, or uploads audio. Results cache timings
and whether normalized words match, not audio or transcript text.
