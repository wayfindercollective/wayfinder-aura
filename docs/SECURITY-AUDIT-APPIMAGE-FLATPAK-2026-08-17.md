# Security audit: Wayfinder Aura AppImage + Flatpak (v1.1.8-beta.9)

**Date:** 2026-08-17  
**Auditor:** Grok 4.6 (first pass + self-adversarial disproof)  
**Scope:** Public `v1.1.8-beta.9` AppImage and Flatpak, the GitHub `releases/latest` install path, and the in-app model/catalog/TLS/socket code those packages run.  
**Not in scope:** Flathub review (not listed), dependency CVE bulk scan, license DRM, writing exploit PoCs.  
**Prior art:** `docs/SECURITY-REVIEW.md` (2026-07-09). Several items below are that review’s still-open F2/F3/F11, re-checked against the shipping packages.

**How to read this:** Surviving findings first. Withdrawn or downgraded claims are in §4 so a reviewer does not re-litigate them from the earlier verbal summary.

---

## 0. Implementation status (2026-08-17, Claude)

All five surviving findings were implemented in the order §7 recommended. Suite
after the change: **1937 passed / 17 skipped** (was 1878/17; +59 tests).
`ruff check --select F821,F823,F722,E9 .` clean.

| Finding | State | What landed |
|---|---|---|
| F-B | **Fixed** | `safe_model_filename` / `safe_download_url` / `safe_cdn_object` in `sanitize_entry`; `resolve_model_dest()` containment at both download sites; `merge_section` refuses an unsafe filename overlay. Traversal fixture now rejected at validation. |
| F-C | **Fixed for catalog downloads** | `sha256` is a catalog field, verified before rename in all three catalog downloaders; all 19 built-in entries pinned; HF URLs moved off mutable `main` to fixed revisions; a shipped digest is authoritative — a remote catalog cannot replace it, nor reuse a shipped *filename* under a different digest. Does **not** cover Faster-Whisper (F-F). |
| F-A | **Partly fixed — needs an owner decision** | Installer verifies the bundle before `flatpak install` (GitHub asset digest, or `--expect-sha256` out-of-band) and refuses on mismatch. Signing and immutable releases remain open (§0.1). |
| F-D | **Fixed** | Deck units log to `%t/wayfinder-aura-logs/` with `RuntimeDirectoryMode=0700` + `UMask=0077`, not world-readable `/tmp`. |
| F-E | **Fixed** | `appimagetool` pinned by **sha256** (1.9.1, `ed4ce84f…`), build aborts on mismatch. |

**A caution the auditor should note:** §6's repro block passes a whole catalog
*section* to `resolve_download_url`, which takes a single *entry* — so steps 2
and 3 print empty for the wrong reason. Extract `["base.en"]` first. Re-run that
way, the F-B claim reproduced exactly as written on pre-fix `main`, including the
load-bearing step 3.

### 0.0 Review round — seven defects found in the fixes themselves

The remediation went through Codex **Sol xhigh** (the standing pre-commit gate).
It found seven issues in my own patch; all seven were fixed before commit except
F-F, which is a genuinely new finding recorded below rather than a defect in the
patch:

1. **The digest pin followed the model id, not the filename.** A compromised
   catalog could publish `base.en: {disabled: true}` plus a fresh id reusing
   `ggml-base.en.bin` with its own URL and its own digest — the shipped pin was
   never consulted, defeating the whole property. `merge_section` now rejects
   any entry that reuses a shipped filename under a different digest, whether or
   not the built-in was disabled first. Test:
   `test_shipped_filename_cannot_be_reused_under_another_digest`.
2. **Faster-Whisper is not covered** → new finding **F-F** below.
3. **`--flatpak-bundle` was compared against `releases/latest`,** so a rollback
   build or an unpublished candidate would be rejected. The auto-fetched digest
   now applies only to a bundle the script itself downloaded from `latest`.
4. **The verification gate could exit 0 with all 7 paid objects unchecked,** and
   could not see a license activated inside the Flatpak. It now also probes the
   sandbox config home (`--config-home` to override) and exits **2** on skips
   unless `--allow-skips` is passed. *(Round 2 caught a bug in that fix: purging
   `wayfinder.*` from `sys.modules` left `config` cached as an attribute of the
   parent package, so the second candidate silently reused the first config dir.
   The root package is purged too now — verified directly: pre-fix the second
   probe returned candidate A's path, post-fix it returns B's.)*
5. **The cheap mode failed `qwen3.5-2b` spuriously** by comparing against
   `size_bytes`, which was then a rounded *display* value (1.39 GB stated vs
   1,280,835,840 actual). *(Superseded by §0.2: `size_bytes` is now the exact
   byte count and the app enforces it before transferring, so a size
   disagreement is once again a **FAIL** — it means every client would abort
   that download.)*
6. **Cancel was ignored while hashing.** A multi-GB verify is not instant; the
   UI said "Cancelling…" and installed anyway. `sha256_file` now polls a
   `should_cancel` callback per chunk.
7. **A preinstalled `appimagetool` on PATH bypassed the pin entirely.** The
   build now always uses the hash-verified download; `APPIMAGETOOL=<path>` is an
   explicit, loudly-announced opt-out.

Sol independently re-derived all 19 pinned digests from Hugging Face metadata
(match on every one), fully hashed `tiny.en`, and confirmed the appimagetool
pin. It agreed the 7 gated CDN pins should ship **fail-closed** with byte
verification as a hard release gate, rather than shipping unpinned.

**Round 2 verdict:** findings 1, 3, 5, 6, 7 confirmed closed — filename pins
block both disable/re-add and direct collision while same-digest aliases still
work; a user-supplied bundle is no longer compared against `latest`; skips exit
2; cancellation removes temp files and a real mismatch is still an error, not a
cancellation; `APPIMAGETOOL` is assigned on both branches. Finding 2 (F-F)
stands as documented, with the reasoning for not shipping an untested pin
accepted — "the next step is testing in a Faster-Whisper environment, not
shipping a guessed pin."

**Round 3 verdict:** the remaining Low is closed — 20/20 experimental checks,
no new findings. The gate is therefore clean with one open item carried
forward: F-F.

### F-F. Faster-Whisper models are downloaded unpinned and unverified (new)

| | |
|--|--|
| **Severity** | Medium |
| **Category** | Supply chain |
| **Status** | **Open** — deliberately not fixed in this pass |

`src/wayfinder/core/transcriber.py:1518` (and the CPU fallback at :1536) call
`WhisperModel(self.model_size, …)` with no `revision`, no `local_files_only`,
and no digest check; the default is a symbolic name
(`faster_whisper_model: "large-v3-turbo"`, `src/wayfinder/config.py:280`).
`faster_whisper` resolves that through `snapshot_download` at whatever the Hub
repo's HEAD is. An Ultra customer selecting Faster-Whisper therefore fetches an
unpinned snapshot and loads it in-process — outside every check F-C added.

**Why it was not fixed here.** The fix is to pass a pinned `revision=` per
model, which requires faster-whisper's internal model→repo mapping (version
dependent) to be correct. `faster_whisper` is an optional Ultra extra and is
**not installed in this environment**, so a pin could not be exercised at all.
Shipping an untested pin on a paid code path risks breaking Faster-Whisper for
exactly the customers who paid for it. It needs a machine with the extra
installed and one real load per offered model.

### 0.2 Balance pass — reducing customer friction without weakening the check

Fail-closed verification is only acceptable if it never fires on a *correct*
install. A second Sol design review (APPROVE WITH CHANGES) shaped this pass; the
guiding split is that a digest check can only block a customer for three
reasons — our pin is wrong, we changed the bytes after shipping, or the transfer
corrupted — so eliminate the first, detect the second, and absorb the third.

| Change | Effect |
|---|---|
| **One retry, transport failures only** | A dropped connection, premature EOF or short body retries once and the customer never sees it — in the whisper panel and in first-run Setup. (The inline GGUF downloader already turns its button into an explicit one-click **Retry** on failure, so it was left as-is rather than restructured late.) A *correctly sized* body that fails its digest is **not** retried anywhere — that means a wrong pin or a changed object, and refetching 3 GB only delays the real error. |
| **Free models may fall back to the shipped URL** | An ungated model whose filename we pin retries against the revision-pinned Hugging Face URL *we shipped* (never a URL the catalog supplied), so a CDN hiccup does not block a free user. |
| **Paid models never leave the CDN** | Gated weights stay on the authenticated origin even when it fails. Falling back to a public mirror would route them around the Bearer check — a licensing boundary, not just a security one. Test: `test_paid_model_never_falls_back_off_the_cdn`. |
| **Prose errors, hex in the log** | The label now says the file doesn't match what this version expects, that it's our problem, and to check for an update. Digests go to the log for support. |
| **Exact-size bounds** | `size_bytes` is now the true byte count for all 19 models and is enforced: a disagreeing `Content-Length` aborts before any body transfers, and a stream that exceeds the pinned size is cut off. A digest only proves what arrived *after* it all arrived — this bounds what a bad origin can spend of a customer's bandwidth and disk first. |
| **https-only transport** | Both clients follow redirects by hand and refuse any non-https hop; a same-host downgrade would otherwise keep `requests`' Authorization header. `src/wayfinder/core/download_guard.py`, 18 tests. |
| **Shipped tuple widened** | The filename pin now locks `(sha256, size_bytes, requires_feature)` together — an alias keeping the digest but dropping `requires_feature` would have handed a paid model to a free user. |
| **Unshipped models are origin-trusted** | A brand-new remote entry now requires *both* a `cdn_object` and a digest. Its own digest cannot vouch for its own URL, so the authenticated origin is the only trust available. |
| **Metadata corrected** | Every whisper entry under-declared its size by 1.5–4.7% and `qwen3.5-2b` over-declared by 8.5%; labels mixed decimal and binary units. All 19 now carry exact bytes with labels derived from them, in both catalog copies. |
| **Drift detection** | `.github/workflows/model-pin-drift.yml` runs weekly: it compares every pin against Hugging Face's LFS oids over the API — seconds, zero bytes transferred — plus the catalog ratchet. Deliberately **no license token in CI**; gated objects stay on the manual gate. |
| **Rule placed where the mistake happens** | `infra/models-cdn/README.md` now carries the overwrite warning next to the upload instructions, with the new-filename escape hatch. |

**Rejected, deliberately:** an "install anyway" override (it converts a
cryptographic control into a social one), a warn-only digest mode, and automatic
fallback of paid weights to public mirrors.

**A second implementation review found nine defects in the above; all were
fixed.** The four that mattered:

1. **The Ultra bearer leaked across redirects** on the GGUF path. `Authorization`
   sat on the `requests.Session`, so the hand-rolled redirect follower
   reattached it to every hop; stripping it from an already-sent request was too
   late. Headers are now computed **per hop** by the same helper that decides
   the token belongs only on the CDN origin — reproduced leaking to
   `https://other.example`, now provably absent.
2. **"Gated models never leave the CDN" was not enforced.**
   `resolve_download_url` fell back to the catalog URL whenever the CDN base was
   unset — and that base lives in user-writable config, so Ultra weights could
   be pulled from a public mirror. A gated entry now resolves to the CDN or to
   nothing.
3. **Filename trust was section-local.** whisper and llm merge separately, so an
   llm entry could claim a shipped *whisper* filename (`ggml-base.bin`) with its
   own digest and origin, and the llm downloader — finding no llm trust for that
   name — verified the attacker's digest. One combined trust map now spans both
   catalogs, with a ratchet forbidding a filename in both.
4. **My "no upload tooling exists" premise was simply wrong.**
   `scripts/upload-models-r2.sh` exists, pulled from mutable `/resolve/main`,
   and overwrote live keys unconditionally — the exact way a pin gets broken in
   practice. It now fetches revision-pinned URLs, **verifies each file against
   the app's pin before uploading**, and refuses to replace an existing object
   without `--overwrite`. Prevention, not a README note.

Also fixed: an overlay could rename a shipped entry out of the trust map or drop
its size bound; brand-new entries now require an exact size and have any `url`
stripped; an unknown-length stream is capped at 8 GiB instead of unbounded; 4xx
responses are no longer retried; a cancel landing between the last hash poll and
the rename no longer installs; and the verifier now **fails** on a size
disagreement instead of warning (a one-byte `size_bytes` typo would abort every
client download while the old check called it PASS).

**A third review round found six more, all fixed.** Two mattered:

- **The new verifier leaked the bearer itself.** It ran on default `urllib`
  redirect handling, which copies `Authorization` to whatever origin a redirect
  names — and this tool runs with a real license token. It now uses the same
  redirect handler the app does: https-only, bearer dropped off the CDN origin.
- **The uploader's existence probe was unsound.** The Worker authenticates gated
  keys *before* checking existence, so a missing Ultra object also answers 401 —
  the probe would have skipped exactly the objects an operator was trying to
  seed, while a curl failure or 5xx fell through to upload anyway. The probe is
  gone. The digest gate is the real guard: bytes that disagree with the shipped
  pin never reach the bucket, so re-running the uploader can only ever write
  what clients already expect. A key with **no** pin is now refused outright
  unless `--allow-unpinned` (it used to upload unverified).

Plus: a brand-new entry's declared `size_bytes` now bounds its download (it had
been ignored, since only shipped entries were consulted); `Content-Length: 0`
against a positive pin now FAILs instead of reporting OK; and two docs that
still described the pre-fix behaviour were corrected.

**A fourth round found four more, all fixed.** The bearer could still go out in
clear text on an *initial* request if `WAYFINDER_MODELS_CDN_BASE` were set to
`http://…` — that origin matches itself, so the origin check passed. The token
is now refused on any non-https URL in `download_auth_headers` itself, which
covers every caller rather than each call site. A hostile catalog could also
declare a multi-terabyte `size_bytes` and thereby *raise* the transfer ceiling
instead of lowering it; a declared size above 8 GiB is now dropped at
sanitization and the ceiling is a hard cap regardless. The verifier's
`--hf-metadata` path bypassed the https-only opener, and urllib was quietly
rewriting a redirected HEAD into a GET — turning the cheap reachability check
into a full model download whenever an origin redirects.

**Final verdict (Sol xhigh):** *"Clean. All four findings are closed; no
remaining or newly introduced regressions found."* Across the whole pass the
gate ran six times — one design review of the plan and five on the code — and
found 26 defects in my own work, every one fixed here. Suite **1937 passed / 17
skipped**, CI ruff selection clean. Two items are carried forward deliberately:
**F-F** (Faster-Whisper, needs an environment with the extra installed) and the
six unhashed Ultra CDN objects in §0.1.

**A design idea that did not survive review:** allowing any https origin for any
entry carrying a digest. It is circular — a remote catalog supplies its own
`sha256`, so a digest cannot authorize that catalog's own URL — and its
justification collapsed anyway, because the catalog is fetched from the same
Worker, so during a CDN outage the "emergency redirect" cannot be delivered. The
real answer to paid-download availability is server-side: have the Worker proxy
a pinned upstream object *after* validating the Bearer, so the token never
leaves the trusted origin. Recorded as a follow-up, not built.

### 0.1 Open decisions for the owner

1. **Release authenticity (F-A).** The installer now checks a digest, but a
   digest GitHub itself publishes is integrity, not authorship — an attacker who
   can replace the asset can replace the digest. `immutable` is still `false` on
   every release.

   **Enabling immutable releases is worth doing and is not a substitute for
   signing.** What it gives: future releases get their tag locked to a commit
   and their assets locked against modification or deletion, plus an automatic
   release attestation (tag + commit SHA + assets). What it does *not* give:
   it cannot stop a compromised account from publishing a *new* malicious
   release and pointing `latest` at it, the attestation lives in GitHub's own
   trust domain rather than an independent publisher identity, and **nothing in
   our installer verifies an attestation today** (that needs
   `gh release verify-asset` or equivalent on the client). Offline-key signing
   remains the only control that survives a GitHub-side compromise; deferring it
   is reasonable, but not because attestations replace it.

   Costs to accept before enabling: applies to future releases only; a wrong
   asset needs a new version rather than a re-upload; a deleted release frees
   its tag but the tag **name can never be reused**; and post-publish editing is
   limited to title and notes — GitHub's docs state *"you can only edit the
   title and release notes after a release is published"* — so **the
   latest/prerelease designation is fixed at publication**. The exact
   `gh release edit --prerelease=false --latest` operation used on
   `v1.1.8-beta.9` would no longer be available. Workflow becomes draft →
   attach all assets → set designation → publish, and the rollback plan in
   `docs/PROMOTION-READINESS-CHECKLIST.md` has been updated to match.

   **DECIDED 2026-08-17: deferred until the 1.1.8 stable line. Do not enable
   during the beta cycle.** The reasoning, so this is not re-argued:

   - The benefit is **narrower than it first appears**. It stops an attacker
     with repo write access from swapping bytes under an *existing* tag — but
     that same attacker can publish a *new* release and repoint `latest`, which
     immutability does not prevent. Every Aura install path (the Deck
     installer's default, the README button, AppImageUpdate's zsync) follows
     `releases/latest`, so the protection only reaches someone who pinned a
     specific version and re-downloads it.
   - The attestation is real but **unverified by anything we ship** — the
     installer checks a digest, not an attestation.
   - The cost lands hardest exactly now: five prereleases in three days, each
     published → tested → sometimes promoted. Immutability's constraints
     multiply by publish frequency, and prerelease→latest promotion is the one
     operation it forbids.

   **Trigger to revisit:** the 1.1.8 stable cut, when releases become
   infrequent, customers pin versions, and prereleases are no longer promoted.
   At that point the workflow cost is near zero and the value is at its peak.
   Until then the equivalent protection is already shipped: the installer
   verifies before installing, and `--expect-sha256` gives a pinned install.
2. **RESOLVED 2026-08-17 — every CDN object is byte-verified.**
   `python3 scripts/verify-model-digests.py --full --source cdn` streamed and
   hashed all 19 objects: **0 failed, 0 skipped**, exit 0. That includes the
   seven license-gated ones (`medium.en`, `large-v3-turbo`,
   `large-v3-turbo-q5_0`, `medium`, `large-v3`, `qwen3-4b-2507`, `phi-3-mini`)
   which no earlier pass could reach — the verifier finds the activation inside
   the Flatpak sandbox, so the owner's workstation sufficed. All 19 pins also
   match Hugging Face's LFS oids independently.

   The R2 mirror is therefore confirmed byte-identical to what the app expects,
   and the fail-closed digest check cannot fire on a correct install today.
   Re-run this whenever an R2 object is touched.

3. **Re-uploading an R2 object is now a coupled change.** Digest pins are
   in-tree, and a remote catalog can neither override a shipped digest nor
   reuse a shipped *filename* under a different one — that pair is what
   survives a catalog compromise. Two consequences for ops:
   - Replacing the bytes behind an existing filename requires an app update
     that ships the new pin. Re-uploading alone will break that model's
     download for everyone on the current build.
   - The escape hatch that needs no app update is a **new filename**: publish
     the new weights as `ggml-<model>-v2.bin` under a new catalog id. A new id
     alone is not enough — the rule follows the filename.

---

## 1. Verdict for the reviewing agent

Treat this as **moderate residual risk**, not a clean ship and not a critical remote-unauth RCE.

What is actually wrong in *code the packages run*:

1. Catalog `filename` is not sandboxed; the download dest is `models_dir / filename` with no `resolve()` containment.
2. No client-side model digest (known open F3). The *primary* extra-model path is the Models CDN, not Hugging Face `main`.
3. The Deck installer and AppImageUpdate do not pin or check authenticity. GitHub *does* expose per-asset SHA-256; nothing in-tree consumes it.

What is **not** a new defect (already documented product tradeoffs): Flatpak X11, same-user control socket, plaintext `0600` secrets.

A first-pass summary claimed “no checksums on the release” and “all live downloads are HF `main`.” Both are **false**. See §4.

---

## 2. Method

- Read Flatpak finish-args, AppImage builder/AppRun, `install-steamdeck.sh`, socket, catalog, CDN resolver, both downloaders, TLS, license, injector, hostexec.
- Live GitHub API for `v1.1.8-beta.9` (assets, `digest`, `immutable`, `prerelease`).
- Live catalog `GET https://wayfinder-models-cdn.peter-7b5.workers.dev/v1/catalog`.
- Executed merge/resolver/path/`file://` experiments with `PYTHONPATH=src` (not a network exploit).
- Then re-read the same code intending to **disprove** each claim.

Reviewer should re-run the commands in §6. If any command disagrees, the corresponding finding is wrong.

---

## 3. Surviving findings

### F-A. Installers do not pin or verify release authenticity

| | |
|--|--|
| **Severity** | **Medium** (was overstated as High) |
| **Category** | Supply chain / missing verification |
| **Packages** | AppImage (README `releases/latest`), Flatpak (Deck `install-steamdeck.sh --refresh-flatpak`) |
| **Status** | Partly fixed 2026-08-17 — client-side verification landed; signing/immutability still open (§0.1) |

**What is true**

- `scripts/steamdeck/install-steamdeck.sh:40,123-140` curls  
  `https://github.com/wayfindercollective/wayfinder-aura/releases/latest/download/io.wayfindercollective.WayfinderAura.flatpak`  
  and `flatpak install`s the bytes. No digest compare.
- AppImage self-update metadata is unsigned zsync:  
  `scripts/build-appimage.sh:477-478`  
  `gh-releases-zsync|wayfindercollective|wayfinder-aura|latest|Wayfinder_Aura-*${ARCH}.AppImage.zsync`
- Release `immutable` is **false**. Assets can still be replaced on the tag.
- No minisign/cosign/GPG asset. No checksums in the release body.

**What is also true (first pass missed this)**

GitHub’s API already publishes SHA-256 for every asset:

| Asset | `digest` (API, 2026-08-17) |
|---|---|
| `io.wayfindercollective.WayfinderAura.flatpak` | `sha256:b20f0105ee6238e50cd54d73e5c3701aa0a06a2e150dacf3c28dd2bf9321df40` |
| `Wayfinder_Aura-1.1.8-beta.9-x86_64.AppImage` | `sha256:fd9a63798360bef3d7aac3a75851e2eb2a95babb8b54ea928b759f1663a942c5` |
| `wayfinder-aura` (PyInstaller) | `sha256:8d3f29ad95f154f5cfe70e08e0ba85b79137294aee717f8b328cc3acc9195dfb` |
| `.AppImage.zsync` | `sha256:851a5af25595cfb6343cb41b5da436d343d06a1f9d4408a594163cd7cdcf8649` |

Those digests are “what GitHub currently has,” not a pin. A replaced asset gets a new digest. HTTPS already guarantees the download matches GitHub. The missing piece is **the client comparing against a separately pinned hash** (in the installer, or a signed SUMS file).

**Impact.** Compromised GitHub credentials or a swapped `latest` pointer become whatever the next Deck `--refresh-flatpak` or AppImageUpdate pull installs. Not a network MITM given working TLS.

**Remediation.** Pin expected SHA-256 in `install-steamdeck.sh` (or verify `gh api … .assets[].digest` against a committed pin). Enable GitHub immutable releases. Sign zsync/AppImage or stop advertising AppImageUpdate as authenticated.

---

### F-B. Remote catalog `filename` is joined unsanitized onto the models dir

| | |
|--|--|
| **Severity** | **Medium** |
| **Category** | Path traversal / arbitrary file write (download body) |
| **Packages** | Both (same Python). AppImage impact is host `$HOME`. Flatpak impact is the sandbox home only. |
| **Status** | **Fixed 2026-08-17** — see §0 |

**Code**

- `src/wayfinder/model_catalog.py:102-143` `sanitize_entry()` — requires `name` + `filename` + (`url` or `cdn_object`). No basename check, no scheme allowlist.
- `merge_section()` overlays remote fields onto builtins, including `filename`, `url`, `cdn_object`.
- `wayfinder_main.py:5002-5005` applies the remote catalog to `WHISPER_CPP_MODELS` / LLM tables at startup.
- Whisper dest: `wayfinder_main.py:2520-2522`  
  `dest_path = self.models_dir / filename`  
  `temp_path = self.models_dir / f"{filename}.downloading"`
- LLM dest: `wayfinder_main.py:12006-12008`  
  `model_file = models_dir / filename`  
  `temp_path = model_file.with_suffix('.tmp')`
- Default dirs: AppImage whisper `~/whisper.cpp/models`; Flatpak whisper sandbox `~/.local/share/wayfinder-aura/whisper-models` (`wayfinder_main.py:150-161`).

**Executed experiment (must still hold)**

```
sanitize_entry(filename="../../.config/wayfinder-aura/config.json",
               url="http://127.0.0.1:9/payload.bin")  → accepted
merge + empty cdn_object + evil url → resolve_download_url returns the evil url
~/whisper.cpp/models / ../../.config/wayfinder-aura/config.json
    resolves to ~/.config/wayfinder-aura/config.json
```

**Narrowing that survived disproof (do not drop these)**

1. **CDN wins if `cdn_object` remains.** Overlaying only `url` does **not** redirect. `resolve_download_url` (`models_cdn.py:88-102`) prefers `cdn_base + cdn_object`. Default CDN is set. Live catalog entries have `cdn_object`.
2. **Redirect requires** the remote entry to clear or replace `cdn_object` (`cdn_object: ""` is accepted and makes `catalog_cdn_object()` return `None`).
3. **User must start a download.** Nothing writes the file on catalog fetch alone.
4. **Whisper `ModelDownloader` has no `min_bytes`.** A small attacker body can land at the dest. (The older `setup.py` `_download_model_file` *does* have `min_bytes=10_000_000`.)
5. **`file://` works on the whisper urllib path, not the LLM `requests` path.** Local-file read is same-user-adjacent; the remote case is `http(s)://` after `cdn_object` is cleared.
6. **Flatpak cannot use this to write host `~/.ssh`.** `Path.home()` is the app dir under `~/.var/app/io.wayfindercollective.WayfinderAura/`. Traversal stays in that namespace.
7. **This is write-of-HTTP-body, not a shell.** Overwriting `authorized_keys` with a GGML blob is not SSH access. Overwriting `config.json` with a *small* attacker document *is* a useful same-uid config plant if they also cleared `cdn_object` and the user clicked Download.

**Realistic attacker.** Someone who can publish `catalog/v1.json` on the Models CDN (R2/Worker admin, leaked `ADMIN_UPLOAD_SECRET`, or Worker compromise). That is the same trust boundary as F-C. Traversal is extra: they can write the body *outside* the models directory, not only replace a `.bin`.

**Remediation.** In `sanitize_entry` (and again at download time):

- `filename` must match `^[A-Za-z0-9._-]+$` (no `/`, no `..`).
- `url` if present must be `https://`.
- After join, `dest.resolve()` must be inside `models_dir.resolve()`.
- Reject empty `cdn_object` unless you intentionally want HF fallback, and even then keep the dest check.

Add a test next to `tests/test_model_catalog.py` that the `../../` fixture is **rejected**. Current tests only cover happy-path sanitize.

---

### F-C. No client-side model content hash (July F3, still open)

| | |
|--|--|
| **Severity** | **Medium** |
| **Category** | Supply chain |
| **Packages** | Both |
| **Status** | **Fixed 2026-08-17** — see §0; Ultra objects still need one verification run (§0.1) |

**What is true**

- Live catalog (2026-08-17): 11 whisper + 8 llm entries. **Zero** `sha256` / checksum keys. All `url` fields are Hugging Face `/resolve/main/…`.
- `ModelDownloader.download_model` does not consult `size_bytes` or a digest. It writes whatever the HTTP body is, then `replace`s.
- Worker `infra/models-cdn/src/index.ts` authenticates Ultra objects (Ed25519 Bearer). It does not attach or require a content hash.

**What the first pass got wrong**

Those HF `url` fields are **fallbacks**. With the default CDN base and a present `cdn_object`, in-app Settings downloads go to  
`https://wayfinder-models-cdn.peter-7b5.workers.dev/v1/objects/…`  
not Hugging Face. “19/19 users pull mutable `main`” is false for that path.

**What still hits Hugging Face `main` with no hash**

`src/wayfinder/core/setup.py:66,855-862` `download_whisper_model()`:

```python
MODEL_DOWNLOAD_BASE = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
url = f"{MODEL_DOWNLOAD_BASE}/ggml-{model_name}.bin"
target = model_dir / f"ggml-{model_name}.bin"
```

Called from `src/wayfinder/ui/setup_pane.py:899-905` when Setup’s `whisper_model` step runs. `model_name` comes from the builtin picker keys (no catalog `filename` traversal here).

- **AppImage** (`--full`, `BUNDLE_MODELS` unset): no model in the image. First-run Setup downloads Base from HF `main`, size/`min_bytes` only.
- **Flatpak:** `ggml-base.en.bin` is sha256-pinned in the manifest (`flatpak/io.wayfindercollective.WayfinderAura.yml:218-221`). First-run should skip Setup download if that bundled file is visible and license-usable. Extra models use `ModelDownloader` → CDN.

**Impact.** A Worker/R2 object swap, or (AppImage first-run) a swap of `ggerganov/whisper.cpp` `main` blobs, is loaded by bundled `whisper-cli` / `llama-simple`. Client cannot tell. Native GGML loaders are the historical memory-unsafety class; this audit did **not** prove a live CVE in the pinned `v1.9.1` / `b9608` tags. Do not write that up as a confirmed RCE.

**Remediation.** Catalog field `sha256` + verify before rename. Prefer HF revision/blob URLs over `main` in the Setup fallback. Same as July B1.

---

### F-D. Deck systemd units log to `/tmp` as 0644

| | |
|--|--|
| **Severity** | **Low** |
| **Category** | Local data exposure |
| **Packages** | Flatpak-on-Deck host units (not the Flatpak itself) |
| **Status** | **Fixed 2026-08-17** — logs moved to `%t/wayfinder-aura-logs/`, 0700 + UMask 0077 |

`scripts/steamdeck/systemd/wayfinder-aura.service:16-17`  
`wayfinder-trigger.service:14-15`  
`wayfinder-mode-supervisor.service:13`

`StandardOutput=append:/tmp/….log` with no `UMask=` / `PrivateTmp=`. First create follows user umask `022` → **0644**.

**Disproof notes.** This audit did **not** show those files contain transcripts (app activity log is separately `0600` under cache). Trigger logs are device names and socket commands. Symlink-follow into another uid’s file was **not** demonstrated; modern systemd `append:` is often `O_NOFOLLOW`. Do not claim a symlink primitive without checking the Deck’s systemd.

**Residual.** Other local uids can read whatever the app prints to stdout/stderr. Move logs to `%t/wayfinder-aura/` (`XDG_RUNTIME_DIR`).

---

### F-E. AppImage builder fetches unpinned `appimagetool` “continuous”

| | |
|--|--|
| **Severity** | **Low** (build machine / CI, not end-user runtime) |
| **Category** | Build supply chain |
| **Packages** | AppImage only |
| **Status** | **Fixed 2026-08-17** — pinned to 1.9.1 + sha256, build aborts on mismatch |

`scripts/build-appimage.sh:159-163` — if `appimagetool` is missing, wget  
`https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage`  
with no hash. CI `.github/workflows/ci.yml` `build-appimage` job does not preinstall a pinned tool, so this path is live on `ubuntu-22.04`.

Compromising that `continuous` asset compromises **new** AppImages, not already-shipped ones.

**Remediation.** Pin a release tag + sha256, or vendor the tool in-repo.

---

## 4. Disproof log — do not re-open these as stated

| First-pass claim | Result | Why |
|---|---|---|
| “Release has no checksums” | **Withdrawn** | GitHub asset `digest` sha256 is present (see F-A table). Clients still do not pin it. |
| “19/19 live downloads are HF `main`” | **Withdrawn as the primary path** | `resolve_download_url` prefers CDN. HF URLs are fallbacks. Setup.py is the remaining HF `main` path (AppImage first-run). |
| “Any catalog `url` overlay redirects the download” | **False** | Overlaying only `url` keeps `cdn_object`; CDN still wins. Must clear/replace `cdn_object`. |
| “`file://` works in both downloaders” | **False for LLM** | `requests` raises `InvalidSchema`. urllib (whisper `ModelDownloader`) does follow `file://`. |
| “Traversal is host RCE / plant `authorized_keys`” | **Overstated** | Write of HTTP body. Flatpak boxed. Useful plant needs a small body + user click + `cdn_object` cleared. |
| Flatpak X11 + IPC is an *undocumented* hole | **Not a new defect** | Intentional (`yml:33-42`). July F11-adjacent. Files-only sandbox. Do not file as a surprise bug. |
| Unauthenticated Unix socket | **Not a new defect** | July F2 / deferred B4. Modes `0600` + parent `0700` hold. Same-user is required for Deck/KDE triggers. |
| “TLS was disabled to fix Fedora” | **False** | Opposite: 1.1.7 bundled certifi; `tls.py` never sets verify=False. |
| License `{valid:true}` JSON grants Ultra | **False** | `license.py` fail-closed on Ed25519. |
| Bearer leaks to Hugging Face on redirect | **False** | Both downloaders strip `Authorization` off CDN origin. |
| `open_url` will launch `file:` / `javascript:` | **False** | http(s) only. |
| Overlay `pkill -f` / `shell=True` desktop probe | **Fixed** in July (F7/F8). |

Accepted tradeoffs (do not re-litigate without a product decision): plaintext API keys at `0600` (F9), client-side license (F10), Flatpak `--share=network` (F11), config-controlled binary paths (F12).

---

## 5. Positive controls (packages)

- Flatpak Python wheels, Tcl/Tk, pygobject, bundled `base.en` are sha256- or commit-pinned.
- No `--filesystem=home`. Socket lives on `xdg-run/wayfinder-aura` only.
- AppImage extracts PyInstaller payload under `~/.cache/wayfinder-aura/runtime` mode `0700` (not tmpfs `/tmp`).
- `hostexec.host_env()` strips bundle `LD_LIBRARY_PATH` before host binaries.
- ydotool is **not** bundled (host client only).
- Portal GlobalShortcuts treats user-cancel as success (avoids 10s re-prompt spam).
- `open_url` waits for portal `Response`; schemes allowlisted.
- License activation does not trust unsigned JSON.

---

## 6. Reviewer reproduction

```bash
cd wayfinder-aura
export PYTHONPATH=src

# 1) GitHub digests exist; release is not immutable
curl -sL https://api.github.com/repos/wayfindercollective/wayfinder-aura/releases/tags/v1.1.8-beta.9 \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("immutable", d["immutable"]);
[print(a["name"], a.get("digest")) for a in d["assets"]]'

# 2) Installer still does not mention digest
grep -n "sha256\|digest\|minisign" scripts/steamdeck/install-steamdeck.sh || echo "no verify"

# 3) Catalog sanitize + CDN preference + dest escape
python3 - <<'PY'
from pathlib import Path
from wayfinder.model_catalog import sanitize_entry, merge_section
from wayfinder.models_cdn import resolve_download_url

print("traversal accepted", sanitize_entry({
    "name":"E","filename":"../../.config/wayfinder-aura/config.json",
    "url":"http://127.0.0.1:9/x","cdn_object":""}))

builtin = {"base.en": {
    "name":"Base","filename":"ggml-base.en.bin",
    "url":"https://huggingface.co/example/b.bin",
    "cdn_object":"whisper/ggml-base.en.bin"}}
print("url-only still CDN", resolve_download_url(merge_section(builtin, {"base.en":{"url":"http://evil/"}})))
print("cleared cdn -> evil", resolve_download_url(merge_section(builtin, {"base.en":{"url":"http://evil/","cdn_object":""}})))
print("appimage dest", (Path.home()/"whisper.cpp/models"/"../../.config/wayfinder-aura/config.json").resolve())
PY

# 4) Live catalog has no sha256; urls are HF main (fallbacks)
curl -sL https://wayfinder-models-cdn.peter-7b5.workers.dev/v1/catalog \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); e=next(iter(d["whisper"].values())); print(sorted(e)); print(e.get("url")); print("sha256" in json.dumps(d).lower())'

# 5) Setup fallback is still HF main
grep -n "MODEL_DOWNLOAD_BASE\|download_whisper_model" src/wayfinder/core/setup.py src/wayfinder/ui/setup_pane.py
```

Expected: (1) four asset digests, `immutable False`; (2) no verify; (3) first print is a dict, second URL is `workers.dev`, third is `http://evil/`, fourth is `$HOME/.config/wayfinder-aura/config.json`; (4) `False`; (5) setup.py still interpolates `resolve/main`.

---

## 7. Suggested patch order for an implementer

1. **F-B** — sanitize + dest containment + tests. Small, no product decision.
2. **F-C** — `sha256` on catalog + verify in `ModelDownloader` and `setup.py`. Already planned as B1.
3. **F-A** — pin GitHub `digest` in `install-steamdeck.sh`; consider immutable releases.
4. **F-D / F-E** — hygiene.

Do not “fix” X11 or the control socket in the same change.

---

## 8. Sign-off from this auditor

I attempted to kill every finding. Three code/process issues remain (F-A, F-B, F-C) plus two lows. The earlier verbal High on “unsigned latest + catalog write-anywhere RCE” does **not** survive contact with the GitHub digest field, the CDN resolver, or Flatpak’s filesystem namespace.

If the reviewer disagrees, the first thing to re-check is F-B step 3 (`cdn_object: ""` still redirects). If that experiment fails on current `main`, F-B’s remote-write path is dead and only the dest-containment hardening remains as defense-in-depth.
