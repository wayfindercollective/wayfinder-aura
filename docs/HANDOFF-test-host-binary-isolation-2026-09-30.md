# Handoff: keep host whisper/llama binaries out of the test suite (2026-09-30)

Test-only work. No production code changed. For the next agent picking this up.

## The problem

Binary discovery looks for whisper.cpp / llama.cpp executables in
`~/whisper.cpp/build/bin`, `~/llama.cpp/build/bin`, `/usr/bin`, `/usr/local/bin`,
`/opt/homebrew/bin` and on `PATH`. The code that does this:

- `wayfinder.utils.runtime_assets.find_whisper_binary` / `find_llama_binary`
- `wayfinder.config._repair_config_path`
- `transcriber._resolve_whisper_cli_binary`, plus `get_backend` in the
  transcriber and postprocessor

CI runners have none of these binaries. The owner's Mac Studio has
`~/whisper.cpp/build/bin/whisper-{cli,server}` and Homebrew
`/opt/homebrew/bin/llama-{cli,simple,server}`. So a test can find the host's
binary instead of the fake it created. It then fails locally while passing on
CI, or passes locally for the wrong reason.

## What has landed, and what this pull request adds

| Where | Change |
| --- | --- |
| `801252c`, already on `main` via PR #11 | Four tests that failed on the Mac, fixed with per-test stubs (`find_*_binary → None`; config `_path_exists` / `_first_existing_path` stubbed). |
| this pull request, branch `fix/test-host-binary-isolation` | An opt-in `no_host_binaries` fixture in `tests/conftest.py`, applied to `TestGetBackend` (test_postprocessor.py) and `TestServerModeDefaultAndFallback` (test_transcriber.py). It replaces the stubs in those two classes. `test_config_unification.py::test_fresh_install_creates_file_and_returns_defaults` keeps its `801252c` stubs on purpose: that test also needs host *models* hidden, and the fixture only hides binaries. |

History: this work first landed on the now-retired `macos` branch as
`936b292`, `97eafb9` and `2a3ec2c`. That happened a few minutes after `macos`
was frozen for trunk-based development (docs/PLATFORM-DEVELOPMENT.md,
"Branches and merges"). Those commits are not part of PR #13, which carries
`macos` into `main`. This pull request rebases them onto `main`. The fixture
commit is unchanged, and the two doc commits are folded into this one.

How `no_host_binaries` works: it wraps `Path.exists`, `Path.is_file`,
`os.path.exists` and `shutil.which`. A file is hidden when its name is one of
`whisper-cli[-cpu]`, `whisper-server[-cpu]`, `llama`, `llama-cli`,
`llama-simple` or `llama-server` (optionally `.exe`) and it lives outside
pytest's temp dirs. Everything else is untouched, so discovery code runs for
real against the test's own fakes. Use it with
`@pytest.mark.usefixtures("no_host_binaries")`.

The branch also renames the model in
`test_get_backend_falls_back_to_cli_when_server_missing` to `ggml-base.en.bin`.
With the old name `m.bin`, a Free license gate swapped the model for a missing
path, and the test passed for that reason rather than because the server binary
was missing. `wayfinder.license._feature_gate` is a module singleton that
earlier tests can leave in either state.

## Verification

Run on the Mac Studio (venv-mac, Python 3.12.10) with
`-m "not ui and not slow and not network and not perf"`.

- Full suite on the `macos`-based commit: 3201 passed / 201 skipped /
  10 deselected, the same count as `macos` at `cbd9c01`. Rebased onto `main`,
  the test code is identical.
- Before the change, `test_get_backend_uses_server_when_binary_present`
  resolved `~/whisper.cpp/build/bin/whisper-server`. After it, the test uses
  its tmp fake.
- The two classes pass when run after `test_gpu_premium.py`,
  `test_integration.py` or `test_license.py`. These three files leave a
  premium gate cached.
- The two `linux_only` tests in `TestServerModeDefaultAndFallback` also pass on
  macOS with their skip lifted.
- Before this pull request, Linux CI run 36777188316 (a manual dispatch with
  `platforms=linux`) tested the same test code on `macos`-based `97eafb9`. Its
  Quality job, 110097946905, reported 3320 passed / 117 skipped /
  10 deselected, the same counts as the PR #11 Linux gate. The pull request's
  own required checks, `Quality` and `Platform smoke (macOS)`, run on the PR
  head. The macOS job needs the infra agent's supervised admission
  (docs/CI.md), which is not the Aura agent's to grant.

## Remaining work (next steps, in order)

Run the probe below on the Mac (not on CI). On this branch, 104 tests still
reach a host binary while running. Most of them are incidental: `load_config()`
repairs `llama_cpp_binary` to the Homebrew copy, and the test never asserts on
it. The ones worth doing first choose server mode because the host
`whisper-server` exists:

- `test_transcriber.py::TestTranscriptionBackends::test_get_backend_default`
- `test_transcriber.py::TestTranscriptionBackends::test_get_backend_whisper_cpp`
- `test_transcriber.py::TestTranscriptionBackends::test_large_model_downgrade_fails_closed_when_no_free_alternative`
- `test_transcriber.py::TestTranscriptionBackends::test_large_model_downgrade_never_points_at_missing_path`
- `test_transcriber.py::TestGreedyDecoding::test_server_and_cli_backends_get_greedy[True]`
- `test_transcriber.py::TestWhisperServerWarmup::test_module_warm_up_routes_to_server_backend`
- `test_e2e_flows.py::TestChunkedRecordingPipeline::test_multiple_chunks_transcribed`
- `test_e2e_flows.py::TestErrorRecovery::test_transcription_failure_raises`
- `test_integration.py::TestErrorRecovery::test_invalid_audio_path_handled`
- `test_integration.py::TestErrorRecovery::test_transcription_error_handled`

For each test, decide whether its assertion depends on the host binary (a real
false pass) or merely touches it. Apply `no_host_binaries` at class level where
the class is about backend selection, then check the result on the Mac with the
probe. Other files that reach host binaries, with test counts: test_config 28,
test_transcriber 21 (the list above plus whisper-cli-only cases), test_model_download 9,
test_model_downloader 9, test_e2e_setup 7, test_macos_keychain 6, test_e2e_flows 6,
test_integration 4, test_gpu_premium 3, test_config_unification 3, test_vocabulary 2,
test_cloud_keys 2, test_chat_template 2, test_voice_profile 1,
test_macos_game_chat 1.

Don't make the fixture autouse for the whole suite without checking the tests
that use real binaries on purpose, such as golden ASR and live smoke.

Related pending work: the license-gate reset (autouse `reset_feature_gate`) was
uncommitted in worktree `.claude/worktrees/stoic-tharp-8b5e72`, based on
`ac6a102`. It also edits `tests/conftest.py`. At 21:15Z its diff applied
cleanly on top of this work (a line offset only), and the combined tree passed
the marked suite on the Mac: 3202 passed / 201 skipped / 10 deselected. Once
it lands, the free-tier model name described above is extra safety rather than
required. PR #13 also edits `tests/conftest.py`, prepending the checkout's
`src/` to `PYTHONPATH`.

Do the remaining work per docs/PLATFORM-DEVELOPMENT.md "Branches and merges":
branch from the latest `origin/main`, use one short task branch (for example
`fix/...`), and open a pull request into `main`.

## Probe used to find host-binary hits

Save as `hostleak_probe.py` in a scratch dir, then run:

```bash
HOSTLEAK_OUT=/path/out.txt PYTHONPATH=/path/to/scratch \
  venv-mac/bin/python -m pytest -q -m "not ui and not slow and not network and not perf" \
  -p no:cacheprovider -p hostleak_probe
```

```python
"""Record tests during which a probe *succeeds* on a host whisper/llama binary."""
import os, shutil, tempfile
from pathlib import Path
import pytest

NAMES = {"whisper-cli", "whisper-cli-cpu", "whisper-server", "whisper-server-cpu",
         "llama", "llama-cli", "llama-simple", "llama-server"}
ROOTS = {os.path.realpath(tempfile.gettempdir()), os.path.abspath(tempfile.gettempdir())}
HITS, _cur = {}, [None]

def _host(p):
    try:
        p = os.path.abspath(os.fspath(p))
    except TypeError:
        return False
    return os.path.basename(p) in NAMES and not any(p.startswith(r + os.sep) for r in ROOTS)

def _rec(p):
    if _cur[0]:
        HITS.setdefault(_cur[0], set()).add(os.fspath(p))

_pe, _pf, _oe, _w = Path.exists, Path.is_file, os.path.exists, shutil.which
def pe(self, *a, **k):
    r = _pe(self, *a, **k); r and _host(self) and _rec(self); return r
def pf(self, *a, **k):
    r = _pf(self, *a, **k); r and _host(self) and _rec(self); return r
def oe(p):
    r = _oe(p); r and _host(p) and _rec(p); return r
def w(cmd, mode=os.F_OK | os.X_OK, path=None):
    r = _w(cmd, mode, path); r and _host(r) and _rec(r); return r
Path.exists, Path.is_file, os.path.exists, shutil.which = pe, pf, oe, w

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _cur[0] = item.nodeid
    yield
    _cur[0] = None

def pytest_sessionfinish(session):
    with open(os.environ["HOSTLEAK_OUT"], "w") as f:
        for k in sorted(HITS):
            f.write(f"{k}\n    " + ", ".join(sorted(HITS[k])) + "\n")
```

## Environment notes

- `~/wayfinder-aura/venv-mac` is an editable install pointing at the main
  checkout's `src/`. Until PR #13 lands, a test that spawns `sys.executable`
  from a `.claude/worktrees/*` checkout imports the main checkout's code. On
  this branch, `tests/test_overlay.py::test_overlay_bare_script_import_bootstraps_wayfinder_package`
  fails in a worktree for that reason, and only for that reason.
- Desktop-app worktrees may start from a stale local `main`. Reset the
  worktree branch to `origin/main` before starting. `macos` is retired.
- `gh`: the active account may be `aenect`. For org actions, pass
  `GH_TOKEN=$(gh auth token --user wayfindercollective)` inline.
