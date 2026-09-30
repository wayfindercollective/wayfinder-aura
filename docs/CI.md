# CI and release builds

## Routine validation

Trusted repository pull requests plus pushes to `main` and `develop` run a
self-hosted `Quality` job. Linux jobs select any available machine with the
`aura-linux` label; no workflow falls back to a GitHub-hosted runner. It installs the application once, then runs:

- the runtime-breaking Ruff rules;
- `scripts/verify_structure.py`;
- the non-UI/non-network pytest suite and coverage upload.

New pushes cancel older in-progress runs on the same ref. Model-pin drift is a
separate weekly/manual metadata check that also runs only when its four pin
surfaces or workflow change; it does not run on ordinary source edits.

## Artifact builds

Artifact builds are deliberately tag/manual only:

```bash
# Flatpak candidate on mini-inf (exact origin/main commit, copied back locally)
scripts/ci/build-flatpak-on-mini-inf.sh

# Ubuntu 22.04 AppImage candidate
gh workflow run Release --ref main -f artifacts=appimage

# Self-hosted Flatpak candidate
gh workflow run Release --ref main -f artifacts=flatpak

# Rerun Quality without artifacts
gh workflow run CI --ref main
```

Version tags build the AppImage and Flatpak on the self-hosted `aura-build`
runner before creating the GitHub release. Offline or busy hardware leaves
jobs queued. AppImages retain Ubuntu 22.04 in a rootless Docker container. The standalone raw PyInstaller artifact is intentionally absent:
the AppImage performs the same PyInstaller build and adds the portable runtime,
native inference binaries, and release-grade package smokes.

## User update contract

A push to `main` validates source but is not itself a public release. Shipping
every merged commit would make an accidental merge an immediate customer
rollout and would provide no stable version for Flatpak/AppStream metadata.

The release boundary is a version tag:

1. Push the release commit to `main` and wait for `Quality`.
2. Build and test the exact commit with
   `scripts/ci/build-flatpak-on-mini-inf.sh`.
3. After hands-on signoff, push the matching `vX.Y.Z` tag.
4. The tag workflow builds both packages and publishes a GitHub Release.
5. Direct-install users are notified in-app. Stable installs follow stable
   releases; prerelease installs also follow newer prereleases.
6. Once accepted on Flathub, its external-data checker notices new stable tags
   and opens an update PR. Merging that green PR publishes the Flatpak update;
   Discover/GNOME Software can then install it automatically.

Single-file `.flatpak` bundles are candidates/downloads, not update channels.
Until Flathub is live, users must install a newer GitHub Release bundle after
the in-app notification.

## Mini-inf Flatpak builder

Aura is public. All external contributor workflows require approval, and the
host-installed `scripts/ci/trusted-runner.py` hook refuses fork-authored jobs
before checkout even if a maintainer approves them. Review outside contributions
before bringing them into a trusted repository branch. The hook lives outside
the job workspace; a workflow must never install or update it.

| Runner | Eligible jobs | Initial limits |
| --- | --- | --- |
| mini-inf-aura | `aura-linux` | 2 CPU cores, 6 GiB RAM |
| mini-infinity-aura-linux (WSL) | `aura-linux`, `aura-build` | 2 CPU cores, 16 GiB RAM |
| mini-infinity-aura-windows | `aura-windows` | one native Windows job; 2 build threads |
| mac-studio-aura | `aura-macos` | one native Mac job; 2 build threads; low priority |

Linux runners use a dedicated `aurarunner` account and systemd service caps.
The WSL builder uses a private **rootless** Docker daemon, not membership in the
privileged Docker group. AppImage containers additionally cap CPU at 2 and RAM
at 12 GiB. Host package installation is an administrator task; workflows do not
change host swap or overcommit. Python environments are recreated for every job.

The Mac runner requires 40 GiB headroom before starting. Its supervisor stops
only its own CI process group if sampled RSS exceeds 20 GiB or host headroom
falls below 20 GiB. This is a watchdog, **not a hard memory reservation**. It uses
the existing memory monitor's anonymous/wired/compressed page accounting.
Browser CI stays on Linux: the Mac's existing guardian kills Chromium while the
local model is loaded. Neither the model server nor its guardian is reconfigured.

Fox Grid already has Linux memory reservations, CPU admission and durable job
tracking. Its bounded fleet worker currently depends on systemd/cgroups; native
macOS worker enrollment and shared CI reservation accounting are not yet wired.
A capped Linux VM is the compatible path for Fox Grid agents on Mac Studio;
native Mac packaging still requires the host's macOS runner. Ordinary GitHub
runner matching handles availability, not Fox Grid resource reservations.

`scripts/ci/check-runner-policy.py` fails CI if any workflow reintroduces hosted
or dynamic runner targets. Candidate artifacts expire after 3–7 days. GitHub
still hosts source, check results and release assets; only execution is local.

The optional manual helper `scripts/ci/build-flatpak-on-mini-inf.sh` is the safe replacement. It:

- resolves and fetches the requested ref locally;
- refuses any commit not contained in `origin/main`;
- makes a fresh detached worktree from the public repository on mini-inf;
- runs the shared build/smoke script in a transient systemd scope;
- copies the finished bundle back over SSH.

The persistent Flatpak Builder state remains on mini-inf for cache reuse. The
transient scope is resource-capped and lower priority so the server's live
Whisper fallback keeps precedence:

- `CPUQuota=200%`
- `MemoryHigh=16G`, `MemoryMax=24G`
- `Nice=10`, idle I/O scheduling
- `TasksMax=2048`

`--force-clean` resets the actual build directory while `--state-dir`,
`--ccache`, and `--jobs=2` preserve and bound the expensive native compilation.
Candidate workflow artifacts expire after three days; tagged release assets
remain attached to the GitHub release.

Measured on 2026-08-26, mini-inf's first cold SDK/native build took about
29m35s. An immediate warm run of the complete build, five smokes, bundle, and
copy-back took 4m52s. The consolidated hosted Quality job completed in 1m47s.

Build a tag after it is pushed with:

```bash
scripts/ci/build-flatpak-on-mini-inf.sh --tag vX.Y.Z
```
