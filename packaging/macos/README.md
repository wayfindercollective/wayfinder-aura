# macOS packaging

For what the port changes beyond packaging, see
[docs/MACOS-PORT.md](../../docs/MACOS-PORT.md).

The current macOS artifact supports **Apple Silicon on macOS 14 or newer**. It
is an architecture-specific PyInstaller `.app` containing
the Python UI and pinned Metal-enabled whisper.cpp and llama.cpp executables.
Whisper uses its resident server; local cleanup uses the deterministic
`llama-simple` executable (the optional llama.cpp web server and general CLI
are not packaged).
Speech and cleanup models remain user-downloadable so the installer does not
carry hundreds of megabytes of weights.

## Local candidate

Use the Python.org Python 3.12 framework build. The macOS UI is intentionally
pinned to the 3.12 line: 3.11 ships an older Aqua Tk, while 3.13+ has produced
visibly different/flat CustomTkinter rendering in project testing. The build
helper refuses any other Python minor so a local package cannot silently ship a
different UI stack.

```bash
python3.12 -m venv venv-mac
venv-mac/bin/python -m pip install --upgrade pip
venv-mac/bin/python -m pip install -c packaging/macos/constraints.txt -e '.[dev]'
venv-mac/bin/python packaging/macos/build.py
```

The current release stack requires Tk 8.6.16 or newer within the 8.6 line.
The official Python 3.12.10 universal2 installer satisfies that requirement.

The floating status waveform and in-app hero are rendered by the bundled native
Metal helper; Qt/Tk retain the approved chrome, typography, and controls.

The native runtime build also requires Xcode Command Line Tools and CMake
(`xcode-select --install` and `brew install cmake`).

The build helper pins and compiles whisper.cpp, creates
`dist/Wayfinder Aura.app`, validates its code signature and packaged runtime,
runs the bundle self-tests, and creates an architecture-labelled DMG. Local
builds use an ad-hoc signature and are suitable for development/QA only.

Use `--skip-native-build` to reuse the pinned native build, or `--no-dmg` when
iterating on the app bundle.

## Automatic Turbo Core ML encoder

Mac builds include a Core ML twin by default (`--no-coreml` omits it).
It can run Turbo's full 30-second encoder on the Apple Neural Engine while
retaining the existing Whisper decoder, Raw Transcript behavior, and GPU
entitlement check. It lives under `bin/whisper-coreml`, never replacing the
standard Metal/CPU executables. Missing/unverified assets select Metal; the
native twin also permits Metal fallback when Core ML fails to load.

**Download once; convert locally.** The ordinary Turbo GGML download (F16,
Q5_0, or Q8_0) is immediately usable. A background worker extracts/dequantizes
its encoder weights into a bundled, **weight-free ~1 MiB architecture graph**.
The native `coreml-compile` helper compiles it using macOS's Core ML framework.
No second model/encoder download, Torch, coremltools, Xcode or network connection
is needed by the installed app. Every learned tensor comes from the downloaded
model; the decoder still uses that same original GGML file. Derived weights
take about 1.2 GiB in a per-revision/precision cache. Conversion requires
temporary free disk space and otherwise keeps Metal. Other model architectures
currently stay on Metal; this first automatic path targets Turbo.

`export_coreml_encoder.py` and `export_coreml_template.py` are **maintainer-only
graph regeneration tools**, not customer setup steps. They verify the pinned
Whisper converter and official checkpoint, and verify all 487 local tensor
bindings against its export before extracting only architecture/layout. The
normalization parameters are loaded as float32 before the ANE bias/weight
division (one Turbo coefficient exceeds float16's range). Core ML retains
overflow-sensitive operations at full precision and uses float16 elsewhere;
input/output remain float32 for the native bridge. The graph ships no weights.

`whisper_coreml_encoder` defaults to true only on Mac. A background startup
worker compares Metal and Core ML using bundled public synthetic speech, never
the microphone. It requires identical normalized words and at least a 10% /
25 ms improvement, caches timings only for the current model/binaries/chip/OS,
and hands off the already-warm winner only while idle. Synthetic tests run on
separate, supervised children, never on the user's dictation server. Nothing
starts until nobody has dictated for 5 minutes, and starting a dictation stops
every preparation child at once (package compiler and probe servers); the next
try waits for another quiet spell. Metal remains usable throughout.

The first Core ML load hands the encoder to Apple's `ANECompilerService`, a
system daemon that keeps compiling after the probe exits and then discards the
orphaned result. That step is therefore counted in `coreml-attempts.json` (app
cache directory) *before* it starts, keyed by model, binaries, chip and OS. A
failed or abandoned attempt waits a day before the next one, then a week; after
three, Metal stays selected for that combination. Changing models/settings
supersedes old preparation. Hotkeys never convert, compile, benchmark, or wait
for device preparation.
Setting it to false and restarting is the advanced Metal-only escape hatch.
Local weight population/package compilation took 6.6 seconds on a 24 GiB M4
and ~9 seconds on a 16 GiB M1. First device specialization took another ~59
seconds on the M4, ~125 seconds on an idle M1 and ~250 seconds on a busy one;
after one completes, macOS caches the result and later loads take under a
second. Core ML probe startup is bounded to 600 seconds (Metal probes: 120).
Inference/recovery deadlines are unchanged. Public-clip tests of locally
converted Q5 weights measured ~0.51–0.64 seconds versus
~0.97–1.19 on Metal Q5, with identical normalized words across five clips.
This is experimental, not a claim of instant completion or broad accuracy
validation. Selection remains conditional on that Mac's local accuracy/speed
check and the existing Ultra GPU entitlement.

## Developer ID signing and notarization

Import the Developer ID Application certificate into the login keychain, then
set the identity by its full `security find-identity -v -p codesigning` name:

```bash
export MACOS_CODESIGN_IDENTITY='Developer ID Application: Example Company (TEAMID)'
xcrun notarytool store-credentials wayfinder-aura-notary \
  --apple-id you@example.com --team-id TEAMID --password APP_SPECIFIC_PASSWORD
export MACOS_NOTARY_PROFILE=wayfinder-aura-notary
venv-mac/bin/python packaging/macos/build.py
```

The helper passes the hardened-runtime entitlements to PyInstaller, then
follows Apple's order: it notarizes and staples the **app** first, builds the
DMG around that stapled app, signs the DMG, then notarizes and staples the
**DMG**, running Gatekeeper assessment on both. Instead of a keychain profile
it also accepts an App Store Connect API key through
`MACOS_NOTARY_API_KEY_PATH` (the `.p8` file), `MACOS_NOTARY_API_KEY_ID` and
`MACOS_NOTARY_API_ISSUER`, which is what CI uses. With no credentials at all
the build stays ad-hoc signed exactly as before. Never commit certificate
files, Apple credentials, or keychain passwords.

## Releasing a signed DMG

Pushing a `v*` tag runs `.github/workflows/release.yml`; its **Build (macOS)**
job builds the Apple Silicon DMG next to the Linux artifacts. To build just the
Mac DMG, run the Release workflow manually (Actions → Release → Run workflow)
with `artifacts: macos`; a manual run on a branch signs and notarizes but never
publishes.

Create these six repository secrets (Settings → Secrets and variables →
Actions). They must be set together: all six sign and publish, none builds an
unsigned DMG, and a partial set fails the Mac job with the missing names.

| Secret | What it is |
| --- | --- |
| `MACOS_CERTIFICATE_P12_BASE64` | Developer ID Application certificate **and its private key**, exported as `.p12`, base64-encoded |
| `MACOS_CERTIFICATE_PASSWORD` | The password you gave the `.p12` on export |
| `MACOS_KEYCHAIN_PASSWORD` | Any long random string; locks the throwaway CI keychain |
| `APPLE_API_KEY_P8_BASE64` | App Store Connect API key (`AuthKey_XXXXXXXXXX.p8`), base64-encoded |
| `APPLE_API_KEY_ID` | That key's Key ID (10 characters) |
| `APPLE_API_ISSUER_ID` | The Issuer ID (UUID) shown above the key list |

**Developer ID certificate (.p12).** The Account Holder creates a *Developer
ID Application* certificate at developer.apple.com → Certificates (or Xcode →
Settings → Accounts → Manage Certificates). On a Mac that has it, open
**Keychain Access** → *login* → *My Certificates*, expand
`Developer ID Application: … (TEAMID)` so the private key shows beneath it,
select the certificate, *File → Export Items…*, save as
`DeveloperID.p12` (Personal Information Exchange) and set a strong password.
Then:

```bash
base64 -i DeveloperID.p12 | pbcopy   # paste into MACOS_CERTIFICATE_P12_BASE64
openssl rand -base64 32 | pbcopy     # paste into MACOS_KEYCHAIN_PASSWORD
```

**App Store Connect API key.** In App Store Connect → Users and Access →
Integrations → App Store Connect API → Team Keys, generate a key with the
**Developer** role. Note the Issuer ID and Key ID, and download the `.p8`
(Apple lets you download it only once). Then:

```bash
base64 -i AuthKey_XXXXXXXXXX.p8 | pbcopy   # paste into APPLE_API_KEY_P8_BASE64
```

Delete the local `.p12`/`.p8` copies (or keep them only in a password
manager) once the secrets are saved.

What the job does:

- **Secrets present:** imports the certificate into a temporary keychain,
  builds, signs, notarizes and staples the app and then the DMG with
  `xcrun notarytool submit --key/--key-id/--issuer --wait`, checks
  `spctl --assess --type open --context context:primary-signature` and
  `xcrun stapler validate` (for the DMG and the app inside it), and confirms the
  DMG name carries the tag's version. After the Linux **Create Release** job has
  published, **Publish (macOS)** attaches the DMG with `gh release upload`.
  The temporary keychain and key file are deleted even when a step fails.
- **Secrets missing:** builds an unsigned, ad-hoc DMG, uploads it only as the
  `wayfinder-aura-macos-unsigned` workflow artifact, and prints a
  *macOS release skipped* notice. Nothing is attached to the GitHub Release.

The Mac jobs never gate the Linux release: a failed or unsigned Mac build
leaves the AppImage/Flatpak release exactly as it would be without macOS.
The in-app update check on macOS only offers releases that carry this DMG, so
a Linux-only release does not prompt Mac users to update.

## Manual release QA

The automated checks prove bundle structure and imports, not desktop behavior.
Before distributing, follow the macOS checklist in
`docs/PLATFORM-DEVELOPMENT.md` on a clean Mac, including microphone denial and
re-enable, Accessibility/Input Monitoring, global hotkeys, Unicode paste and
clipboard restoration, overlay/menu-bar behavior, sleep, and multiple apps.

For a clean install, drag **Wayfinder Aura.app** into **Applications** before
launching it. macOS privacy approvals should be granted only to that stable copy.
