"""Execution boundaries for persistent runners on a public repository."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts/ci" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("value", ["ubuntu-latest", "windows-latest", "macos-latest", "${{ matrix.os }}", "[self-hosted, unknown]", ""])
def test_runner_policy_rejects_hosted_unknown_and_dynamic_targets(value):
    assert load("check-runner-policy").check_workflow(f"    runs-on: {value}\n")


def test_every_checked_in_workflow_uses_own_hardware():
    assert load("check-runner-policy").main() == 0


def test_external_reusable_job_cannot_hide_hosted_execution():
    assert load("check-runner-policy").check_workflow("    uses: somebody/repo/.github/workflows/ci.yml@main")


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "schedule"])
def test_trusted_repository_events_are_accepted(event):
    guard = load("trusted-runner")
    assert guard.trusted_event(event, {"repository": {"full_name": guard.REPOSITORY}})


@pytest.mark.parametrize("head", ["stranger/wayfinder-aura", "", None])
def test_public_fork_pull_requests_are_refused_even_after_workflow_approval(head):
    guard = load("trusted-runner")
    assert not guard.trusted_event("pull_request", {
        "repository": {"full_name": guard.REPOSITORY},
        "pull_request": {"head": {"repo": {"full_name": head}}},
    })


def test_own_branch_pull_request_is_accepted():
    guard = load("trusted-runner")
    assert guard.trusted_event("pull_request", {
        "repository": {"full_name": guard.REPOSITORY},
        "pull_request": {"head": {"repo": {"full_name": guard.REPOSITORY}}},
    })


@pytest.mark.parametrize("event", ["pull_request_target", "issue_comment", "repository_dispatch", "", "workflow_run"])
def test_privileged_or_incomplete_events_are_refused(event):
    guard = load("trusted-runner")
    assert not guard.trusted_event(event, {"repository": {"full_name": guard.REPOSITORY}})


def test_workflow_run_from_a_fork_is_refused():
    guard = load("trusted-runner")
    assert not guard.trusted_event("workflow_run", {
        "repository": {"full_name": guard.REPOSITORY},
        "workflow_run": {"head_repository": {"full_name": "stranger/fork"}, "event": "pull_request"},
    })


def test_mac_admission_reserves_future_model_growth_before_ci():
    capacity = load("mac-runner-supervisor")
    gb = capacity.GIB
    # The observed machine looks idle enough if only current usage is counted,
    # but the model is entitled to grow to its configured 218 GiB ceiling.
    assert capacity.spare_after_reserves(133 * gb, 88 * gb, 218 * gb) < capacity.CI_BUDGET
    # A genuinely available machine still permits work after the same reserves.
    assert capacity.spare_after_reserves(64 * gb, 218 * gb, 218 * gb) >= capacity.CI_BUDGET


@pytest.mark.parametrize("used,maximum", [(219, 218), (-1, 218), (0, float("nan")), (None, 218)])
def test_mac_admission_refuses_invalid_model_telemetry(used, maximum):
    with pytest.raises(ValueError):
        load("mac-runner-supervisor").spare_after_reserves(133, used, maximum)


@pytest.mark.parametrize("head,expected", [("wayfindercollective/wayfinder-aura", 0), ("stranger/fork", 1)])
def test_installed_hook_entrypoint_accepts_owned_pr_and_blocks_fork(tmp_path, head, expected):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"repository": {"full_name": "wayfindercollective/wayfinder-aura"},
        "pull_request": {"head": {"repo": {"full_name": head}}}}))
    result = subprocess.run([sys.executable, str(ROOT / "scripts/ci/trusted-runner.py")],
        env={**os.environ, "GITHUB_REPOSITORY": "wayfindercollective/wayfinder-aura",
             "GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": str(event),
             "RUNNER_TEMP": str(tmp_path), "AURA_MIN_FREE_GB": "0"},
        capture_output=True, text=True)
    assert result.returncode == expected, result.stdout + result.stderr
    assert ("AURA_TRUSTED_RUNNER_OK" in result.stdout) == (expected == 0)
