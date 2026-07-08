# Privacy

Wayfinder Aura is built to keep your voice on your own machine. This notice
describes exactly what the app does and does not do with your data. It reflects
the actual behavior of the code — not aspirations.

## Local by default

Out of the box, Wayfinder Aura runs the entire dictation pipeline on your
device:

- **Transcription** runs locally with [whisper.cpp](https://github.com/ggerganov/whisper.cpp).
- **Cleanup** (grammar, punctuation, tone) runs locally with a small
  [llama.cpp](https://github.com/ggerganov/llama.cpp) model.

In this default mode (`processing_mode: "local"`, `post_processing_backend:
"llama_cpp"`), your audio and its transcript never leave your computer.

## Recordings are temporary and are deleted

When you dictate, the recorded audio is written to a temporary WAV file
(`tempfile.NamedTemporaryFile(suffix=".wav")`) only so the transcriber can read
it. As soon as transcription finishes, that temporary file is deleted
(`Recorder.cleanup()` / `ChunkedRecorder.cleanup()` unlink it).

There is **no "save audio" option**. Wayfinder Aura does not keep a library of
your recordings.

## Cloud backends are opt-in and off by default

Wayfinder Aura can optionally use cloud services for transcription or text
cleanup — **Groq** and **OpenAI** for transcription, **OpenAI** and
**Anthropic** for cleanup. These are **turned off by default** and are only
used if you explicitly enable them and supply your **own API key**:

- The default processing mode is `local`, and every cloud API key
  (`groq_api_key`, `openai_api_key`, `anthropic_api_key`) is empty until you
  set it.
- When you enable a cloud backend, the audio or transcript for that dictation
  is sent to the provider you chose, using your key, subject to that provider's
  own privacy policy.

If you never turn these on, no audio or text is ever sent to a cloud
transcription or cleanup service.

## Secrets stored on your machine

Your settings — including any cloud API keys — live in
`~/.config/wayfinder-aura/config.json`. Your license token (if you buy Ultra)
lives in `~/.config/wayfinder-aura/license.json`.

These files are stored **in plaintext**, but both are written with file
permissions `0600` (`os.chmod(..., 0o600)`), meaning only your user account can
read them. They are not encrypted; anyone with access to your logged-in account
can read them, so treat them like any other local credential file.

## License activation

Activating an Ultra license contacts the licensing server. When it does, the
app sends **only two fields**: your license `key` and a `machineId`.

The `machineId` is **not** your raw machine identity. It is a SHA-256 hash of a
combination of local identifiers (your `/etc/machine-id`, the DMI product UUID,
your hostname, and your CPU architecture), truncated to the first 16
characters. The raw `/etc/machine-id` is never transmitted.

After activation, the app stores a signed token and can validate it **offline**
for a grace window, so an activated install keeps working without contacting
the server on every launch. It only re-contacts the server to refresh the token
or catch a refund/revocation.

## Weekly model-update check

By default the app checks online (the Hugging Face API) about **once a week**
to see whether newer transcription/cleanup models are available, and offers them
for download. This is a simple version check — no audio or transcript is
involved. You can turn it off with the **Check for model updates** setting
(`check_for_model_updates: false`).

## Local diagnostic log

For troubleshooting, the app keeps a local activity log at
`~/.cache/wayfinder-aura/activity.log`. Be aware:

- It **may contain transcribed text**, so treat it as sensitive.
- It is **local only** — it is never uploaded anywhere.
- It is written with permissions `0600` (owner-only) and is **size-capped**
  (truncated once it grows past ~5 MB) so it can't grow without bound.

You can delete it at any time; the app recreates it as needed.

## No analytics, no telemetry

Wayfinder Aura contains no analytics or usage telemetry. It does not track how
you use the app. The only network activity is the three things named above,
each of which you control:

1. The weekly model-update check (toggleable).
2. Cloud transcription/cleanup backends (off by default; your own keys).
3. License activation (only if you activate an Ultra license).

## Questions

If anything here is unclear, or you believe the app's behavior differs from
this notice, please open an issue at
<https://github.com/wayfindercollective/wayfinder-aura/issues>.
