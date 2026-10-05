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
`~/.config/wayfinder-aura/config.json` on Linux (in the Flatpak:
`~/.var/app/io.wayfindercollective.WayfinderAura/config/wayfinder-aura/config.json`),
`~/Library/Application Support/wayfinder-aura/config.json` on macOS, or
`%APPDATA%\wayfinder-aura\config.json` on Windows. Your license token (if you
buy Ultra) lives beside it as `license.json`. On Wayland desktops, the one-time
approval for typing into every app is remembered as a restore token in
`portal-keyboard.json`, also beside it.

These files are stored **in plaintext**, but all are written with file
permissions `0600` (`os.chmod(..., 0o600)`), meaning only your user account can
read them. The same owner-only mode is applied to `voice_profile.json`
(transcription history used for Personal tone learning), structured app logs
under the cache directory, and config backups matching `config.json*`. They are
not encrypted; anyone with access to your logged-in account can read them, so
treat them like any other local credential file.

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
involved. There is no switch for it in Settings; to turn it off, set
`check_for_model_updates` to `false` in `config.json`.

## Local diagnostic log

For troubleshooting, the app keeps a local activity log at
`~/.cache/wayfinder-aura/activity.log` on Linux (in the Flatpak:
`~/.var/app/io.wayfindercollective.WayfinderAura/cache/wayfinder-aura/activity.log`) or
`~/Library/Caches/wayfinder-aura/activity.log` on macOS. Be aware:

- It **may contain transcribed text**, so treat it as sensitive.
- It is **local only** — it is never uploaded anywhere.
- It is written with permissions `0600` (owner-only) and is **size-capped**
  (truncated once it grows past ~5 MB) so it can't grow without bound.

You can delete it at any time; the app recreates it as needed.

## Clipboard (Linux)

Aura normally types your dictation as keystrokes. When it types through the
desktop portal and your keyboard layout has no key for a character (for example
é on a US layout, or an emoji), it pastes the dictation through the clipboard
instead, then puts the text that was on your clipboard back afterwards. Gamer
mode pastes your dictation into a game's chat through the clipboard too, but
does not restore it, so the dictation stays on the clipboard. Aura also pastes
as a fallback when typing fails in SteamOS Game Mode, and in an off-by-default
advanced option (`desktop_paste_on_focus_drift`); both restore the clipboard on
a best-effort basis. Clipboard managers (Klipper, CopyQ, and similar) may record
what Aura puts there.

## Window detection (Linux)

Gamer mode (on by default) reads the active X11 window's class, title, and
`STEAM_GAME` property at each dictation to tell whether a game is in front.
This stays on your machine: nothing is sent, and the only trace is an
activity-log line naming the game when one is recognized.

## No analytics, no telemetry

Wayfinder Aura contains no analytics or usage telemetry. It does not track how
you use the app. The network activity is exactly the following; none of it
carries your audio or transcripts unless you enable a cloud backend (item 6):

1. **Model catalog, at every launch.** Aura fetches the list of downloadable
   models from the Wayfinder Models CDN and keeps a copy for 6 hours. The
   request carries no license data, audio, or text. There is no Settings
   switch for it.
2. **Model-update check, weekly** (Hugging Face API). Off with
   `check_for_model_updates: false` in `config.json`.
3. **App-release check, daily** (GitHub Releases API), which shows an "Update
   available" banner. Off with `check_for_app_updates: false` in `config.json`.
   **Get Update** opens the release's download link in your browser; Aura does
   not download the file itself.
4. **Model downloads, when you ask for them,** from Hugging Face or the
   Wayfinder Models CDN, depending on the model. Ultra models are served only
   by the CDN, and those requests carry your signed license token as a
   `Bearer` header. The token is never sent to Hugging Face.
5. **The feedback form, only when you press Send feedback.** It sends your
   message, your email address if you typed one, the app version, your plan
   (free or ultra), and your operating system description to the Wayfinder
   feedback service. Delivery is not guaranteed; if the form cannot send, it
   says so.
6. **Cloud transcription/cleanup backends** (off by default; your own keys).
7. **License activation** (only if you activate an Ultra license), as
   described above.

## Questions

If anything here is unclear, or you believe the app's behavior differs from
this notice, please open an issue at
<https://github.com/wayfindercollective/wayfinder-aura/issues>.
