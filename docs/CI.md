# CI and release builds

## Routine validation

Pull requests plus pushes to `main` and `develop` run one GitHub-hosted
`Quality` job. It installs the application once, then runs:

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

# Emergency hosted Flatpak fallback
gh workflow run Release --ref main -f artifacts=hosted-flatpak

# Rerun Quality without artifacts
gh workflow run CI --ref main
```

Version tags always build the AppImage and Flatpak on GitHub-hosted runners
before creating the GitHub release, so publishing does not depend on mini-inf
being online. The standalone raw PyInstaller artifact is intentionally absent:
the AppImage performs the same PyInstaller build and adds the portable runtime,
native inference binaries, and release-grade package smokes.

## Mini-inf Flatpak builder

Wayfinder Aura is public and owned by a personal GitHub account, which cannot
create an organization runner group restricted to one trusted workflow. A
permanently registered repository runner would therefore expose mini-inf to
fork-authored workflow code. The server deliberately is **not** registered as
a runner.

`scripts/ci/build-flatpak-on-mini-inf.sh` is the safe replacement. It:

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
