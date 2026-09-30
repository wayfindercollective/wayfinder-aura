# CI and release builds

## For developers and agents

The shared branch workflows use the owner's hardware. GitHub still coordinates
Actions and stores artifacts; it does not supply the execution machines.
Update your current branch without discarding local work:

```bash
git fetch origin
git pull --ff-only
python scripts/ci/check-runner-policy.py
```

If your local branch has diverged, integrate its upstream CI changes explicitly.
Do not restore hosted workflow files while resolving a merge. Older commits and
tags retain their historical workflows: do not rerun them if they target hosted
machines; validate an updated branch instead.

| Workload | Required `runs-on` labels |
| --- | --- |
| Linux tests and lightweight checks | `[self-hosted, Linux, X64, aura-linux]` |
| Linux packaging | `[self-hosted, Linux, X64, aura-build]` |
| Native Windows tests and installers | `[self-hosted, Windows, X64, aura-windows]` |
| Native macOS tests and DMGs | `[self-hosted, macOS, ARM64, aura-macos]` |

Inspect available runners and request a Linux-only check with:

```bash
gh api repos/wayfindercollective/wayfinder-aura/actions/runners \
  --jq '.runners[] | {name,status,busy,labels:[.labels[].name]}'
gh workflow run CI --ref main -f platforms=linux
```

Runner inventory requires repository access. Pull requests and release checks
retain their native-platform gates. A Linux-only manual run does not prove
Windows/macOS compatibility. `develop` and the older polish branch retain their
branch-specific CI jobs and do not expose the `platforms` input.

As of 2026-09-30, Mini-inf's Linux Quality job is verified (2,533 tests passed
on the initial migration candidate). Native Windows is not registered, the Mac
runner is offline pending capacity acceptance, and no `aura-build` runner is
enabled. Those jobs queue; they never fall back to hosted execution. Publishing
this routing change is not installer or application-release signoff.

## Routine validation

Trusted repository pull requests plus pushes to `main` and `develop` run a
self-hosted `Quality` job. Linux jobs select any available machine with the
`aura-linux` label; no workflow falls back to a GitHub-hosted runner. It installs the application once, then runs:

- the runtime-breaking Ruff rules;
- `scripts/verify_structure.py`;
- the non-UI/non-network pytest suite and coverage upload.

The native macOS check runs the same automated test selection, in addition to
the platform contract smoke, using the candidate's pinned Python 3.12.10 and
macOS constraints. Windows also runs the suite and contract smoke, followed by
the separate installer candidate workflow on pull requests. Passing Linux
alone is insufficient to merge shared application changes to Main.

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
gh workflow run CI --ref main -f platforms=linux
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
| mini-infinity-aura-linux (old WSL; disabled) | pending shared-capacity integration | stopped |
| Native Windows | `aura-windows` | not registered yet |
| mac-studio-aura (offline) | `aura-macos` | peak-model admission required |

Linux runners use a dedicated `aurarunner` account and systemd service caps.
The prepared WSL builder's private **rootless** Docker daemon is also disabled;
it must join the shared admission policy before use. It does not require
membership in the privileged Docker group. AppImage containers cap CPU at 2 and RAM
at 12 GiB. Host package installation is an administrator task; workflows do not
change host swap or overcommit. Python environments are recreated for every job.

The Mac runner is currently **offline**, pending shared-capacity acceptance. The
owner requires the local model at full context, existing server processes and
at least one cloud coding agent to take precedence. On 2026-09-30, the owner
approved testing a smaller, model-specific budget on the 256 GiB Mac Studio.
The exact Qwen 3.8 Flash Next oQ5e build now has a **160 GiB** oMLX guard ceiling;
its **131,072-token context**, MTP and PLE SSD offload remain enabled.

The acceptance trial used cold and repeated requests with approximately 130,489
input tokens and checked retrieval from the beginning, middle and end. The kernel
recorded a peak of approximately **115.2 GiB** for the serving process, normal host
memory pressure and **no additional swap**. These are measurements of the tested
profile with existing services running, not a guarantee for every workload or
acceptance of simultaneous CI. Evidence is retained on the Mac at
`~/litellm-proxy/aura-memory-trial-20260930/` (including the confirmation run).
The near-limit repeat reported zero cached tokens; full-context cache reuse is
not established. A separate 16k repeat did reuse 14,336 cached tokens. Keep this
performance observation separate from the successful memory-capacity test.

The host's `~/litellm-proxy/model-swap.sh` selects 160 GiB only for the tested
5-bit snapshot/configuration with one concurrent request. Switching to the 8-bit,
4-bit or a different snapshot/context/MTP/offload/concurrency configuration
restores the previous **218 GiB** budget.
The switcher serializes against memory trials, requires Aura CI to be disabled,
and refuses a switch when a CI worker or inference request is busy. Other model
management paths must restore the larger allowance before loading an unverified
model; this host setting is not an oMLX per-model API feature. Fleet admission
must re-evaluate reservations after every model/configuration change.

The native supervisor reserves unused model growth, plus 24 GiB for one cloud
agent, server growth and OS safety (8 GiB each), beyond current process usage.
CI's sampled process-group stop limit remains **4 GiB**, not the proposed 20 GiB.
This is a watchdog, **not a hard memory reservation**. Missing model/OS telemetry
refuses work. The supervisor and workflows must not change model settings.
A 20 GiB CI allocation still needs concurrent-load measurements and a verified
shared admission/cleanup contract. Browser CI stays on Linux: the existing Mac
guardian kills Chromium while the local model is loaded.

The newly added Aura runner and rootless Docker on mini-infinity's old
Ubuntu-24.04 distro are **stopped and disabled**. The existing Arawn CI runner
uses the separate Ubuntu-26.04 `arawn-ci` distro, a Windows task, a fixed 22 GiB
cgroup and a private network. Aura must integrate with that owner's shared
capacity contract before its second Linux slot is re-enabled. A repository-level
runner registered to `aenect/arawn` cannot directly accept Aura jobs merely by
adding a label; sharing hardware requires a separate registration coordinated
under the same resource admission policy. Native Windows packaging likewise
requires Windows execution, not the WSL Linux runner.

Fox Grid's CI adapter source is at
`PersonalOS-harness-release-6134576/docs/ci-resource-admission.md`. It currently
needs a same-user systemd manager; its notes explicitly identify cross-user,
cross-distro admission as an activation gate. Do not start another systemd in
the new CI distro or assume GitHub's idle-runner matching implements these leases.

The active harness owner is the Steam Machine Codex session titled
`Investigate PersonalOS agent hang` (session
`01a0eb16-e7f8-7703-a17c-16d88d32ffd2`). Its shared infrastructure handoff is
`/home/deck/dev/personal/PersonalOS/workspace/projects/fox-grid-ci-runner-awareness-handoff-2026-09-30.md`
on Steam Machine. Consult its newest dated section; early sections describe the
superseded systemd setup. The v217 adapter passed a disposable live completion
and descendant-cleanup check, but that is not whole-fleet activation evidence.
Cross-user reservations, WSL accounting and pre-intake eligibility still need
coordinated deployment. The Arawn registration cannot substitute for Aura's.

Package maintenance must preserve running jobs. Mini-inf has a needrestart
exclusion for `actions.runner.*.service` after an apt transaction restarted an
active runner. Keep that exclusion and schedule host maintenance with the runner
owner. Do not start another service manager in the `arawn-ci` WSL distro.

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
