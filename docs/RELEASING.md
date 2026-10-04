# Releasing Wayfinder Aura

Keep merging to `main`. Users choose how new they want Aura to be; releases
follow from that with one deliberate step for customers.

## Channels

| Channel | Who | What they get | How often |
|---|---|---|---|
| **Stable** (default) | customers | a beta that held up, promoted by hand | every 2–3 weeks, when there is something worth shipping |
| **Beta** | testers, friends, anyone who opts in | `main` as of last night | at most once a day, only when `main` changed |

Users pick in **Settings → System → Updates** (config `update_channel`). An
install that never picked follows its own build: a beta build stays on Beta.
Moving from Beta back to Stable keeps the installed beta until the next stable
release outranks it. The website's download page offers Stable, with a Beta
link (`/aura/download/mac?channel=beta`).

The app checks GitHub once a day (and from the menu bar's *Check for
Updates…*) and offers the newest release on its channel that carries a
download for that platform (`src/wayfinder/core/app_updates.py`).

## What happens by itself

The **Beta** workflow (`.github/workflows/beta.yml`) runs every night at 09:00
UTC, and on demand from Actions → Beta → Run workflow. It cuts
`vX.Y.Z-beta.N` when `main` moved since the last beta and `main`'s required
checks (`Quality`, `Platform smoke (macOS)`, `Windows tests`) passed;
otherwise it does nothing and tries again the next night
(`scripts/release/cut_beta.py`).

- The tag points at a one-commit `release: X.Y.Z-beta.N` on top of `main`
  that stamps the version into every file (`scripts/release/versioning.py`).
  `main` is never written.
- The Release workflow then builds the AppImage, the Flatpak and the Windows
  installer and publishes a GitHub **prerelease**. The Windows installer is
  attached by its own job (`publish-windows`), so Linux never waits on it.
- The Mac DMG is built, notarized and attached by the **Mac release watcher**
  on the Mac Studio (below), within about an hour of the release.
- A beta is finished when its release carries the AppImage, its zsync file,
  the Flatpak and the Windows installer. If the dispatch, the build or an upload failed, the next
  night waits on that beta's build, or starts it once more while `main` is
  unchanged. Once `main` has moved (say, with the fix: a tag always rebuilds
  with the workflow it was cut with), the next beta replaces it instead.
  After two builds that left it unfinished it is left for a person
  (Actions → Release). Promotion never picks an unfinished beta.
- Automated betas older than 14 days that are not among the newest 5 are
  deleted (release and tag). Stable releases and older hand-made tags are
  never touched.
- The release is announced in Slack like any other.

To pause betas, disable the Beta workflow in Actions (re-enable to resume).

## Versions

Semantic versioning. `main`'s `pyproject.toml` names the **next stable
release**: today `1.2.0`, so nightly betas are `1.2.0-beta.N`. After `v1.2.0`
ships, betas move to `1.2.1-beta.N` by themselves. Raise `main` to the next
minor (`1.3.0`) in the first pull request that adds a feature; patch releases
are fixes only.

## CHANGELOG

Every user-visible pull request adds one bullet under `## [Unreleased]` in
`CHANGELOG.md`, starting with a bold sentence:

```markdown
- **Aura uses about a sixth of the CPU while it waits.** Details…
```

The bold leads become the AppStream release notes (Flatpak, software
centres) and the list shown with each beta, so write them for users.

## Promoting a beta to Stable

When to ship: the beta is **complete** (AppImage, zsync, Flatpak, the
Windows installer and the signed Mac DMG all attached) and has been for at least **5 days**, nobody has
reported a blocker against it (feedback, crash reports), and
`## [Unreleased]` has something a user would notice. Otherwise wait for the
next beta. The script never promotes an incomplete beta; the soak counts from
its last required download.

```bash
GH_TOKEN=$(gh auth token --user wayfindercollective) \
  python scripts/release/promote.py            # newest beta, version X.Y.Z
# or: --beta v1.2.0-beta.4 --version 1.2.0     # --dry-run to preview
```

It ships **exactly the code the beta was built from**, not today's `main`:

1. commits `release: X.Y.Z` on the beta's `main` commit, moving that beta's
   Unreleased notes under `## [X.Y.Z] — date` and adding the stable AppStream
   `<release>`;
2. opens a pull request into `main` with the same notes (bullets merged after
   the beta stay Unreleased) — merge it once its checks pass;
3. pushes `vX.Y.Z`, which builds and publishes the stable release.

Re-running after a failure picks up where it stopped, for the same beta only:
a pushed `vX.Y.Z` is kept if it was made from that beta's `main` commit, and
an open or merged notes pull request or an existing notes branch if it
carries that beta's notes (commits added on top are kept; nothing is
force-pushed). Anything else stops the script for a person: a tag, branch or
open pull request from a different beta, or a notes pull request closed
without merging (reopen it, or re-run with `--new-notes-pr`).

The Mac release watcher attaches its DMG within the hour. The website's
download links and every installed app pick the new release up by themselves.

Before the first stable release on a platform, also run its manual checklist
in [PLATFORM-DEVELOPMENT.md](PLATFORM-DEVELOPMENT.md). Linux keeps its hands-on
Flatpak signoff ([CI.md](CI.md#user-update-contract)).

## Hotfixes

Ship a patch outside the cycle only when it really hurts:

- dictation doesn't work, or the app crashes;
- text lands in the wrong place, or wrong text is pasted;
- paying for or activating Ultra is blocked;
- a security or privacy problem.

Fix it on `main` as usual. If `main` is safe to ship, run the Beta workflow by
hand, check the beta (wait for its Mac DMG), and promote it with `--yes`
(skips the 5-day soak, not the completeness check). If
`main` holds unfinished work, cut `release/X.Y` from the stable tag, fix it
there, and tag `vX.Y.(Z+1)` from that branch.

## The Mac DMG: the Mac release watcher

The Developer ID certificate and the `wayfinder-aura` notary profile live in
the Mac Studio's login Keychain and never leave it. A LaunchAgent
(`io.wayfindercollective.aura-mac-release`, installed with
`scripts/release/install_mac_release_watcher.sh`, log
`~/Library/Logs/aura-mac-release.log`) checks GitHub hourly. When a release
from the last 7 days has no Mac DMG, it runs
`scripts/release/attach_mac_dmg.sh` for it: build from the tag, notarize,
staple, attach. It builds one release per run and gives a release 3 tries.

It shares the Mac with the local model and the Fox Grid VM, so it is admitted
the way Fox Grid admits Mac CI jobs ([CI.md](CI.md)): available memory, minus
the model's unused allowance (its 160 GiB guard, or the smaller reserve in
Fox Grid's grant for the model loaded right now), minus the VM's unused
commitment and the 24 GiB protected headroom, must still cover the 2 GiB
build; memory pressure must be normal, no Mac CI job running, and the Mac CI
queue not held by someone else. It holds the queue while it builds. Every
15 seconds it re-checks the build (4 GiB cap, 2-hour wall clock) and the host,
re-reading the grant each time, and stops the build if any check fails. The
hold is released only once every process of the build is gone.
The build runs as a background task (efficiency cores, throttled disk I/O) and
reuses the checkout's native whisper/llama binaries, so it is a single
PyInstaller pass plus Apple's notarization wait. Its peak memory is in the log.

Run `attach_mac_dmg.sh vX.Y.Z` by hand to (re)attach a DMG at any time.

## Mac signing secrets (optional alternative)

With these six repository secrets the Release workflow signs, notarizes and
attaches the Mac DMG itself, with no Mac-side step.
Export the Developer ID certificate as `.p12` and create an App Store Connect
API key ([packaging/macos/README.md](../packaging/macos/README.md#releasing-a-signed-dmg)),
then run, yourself, in a terminal:

```bash
scripts/release/set_macos_signing_secrets.sh DeveloperID.p12 AuthKey_XXXXXXXXXX.p8
```

It prompts for the `.p12` password and the key IDs and stores all six
secrets without printing them. Until they exist, tag builds skip the Mac job
(a DMG built without them could never be published) and the watcher attaches
it; with both in place, whichever finishes first attaches it and the watcher
skips releases that already have their DMG.
