"""Meta-tests: prove that a skipped test can never be reported as a passing one.

Each test drives pytest in a subprocess with a controlled environment and then
asserts on the *observable* pytest result: exit code, summary line, skip reasons.
Nothing here trusts internal pytest state.

These are the guard rail for the intent split described in tests/e2e/conftest.py:

* a tagged skip really surfaces as ``skipped`` with its reason, not as passed;
* the contract selection *deselects* the credentialed suite instead of passing it;
* in "required" mode (what CI uses) an unreachable backend or missing credentials
  become failures, and a marker matching zero tests fails loudly.

If somebody later converts one of those skips into a silent ``return`` or hides it
behind a bare ``except: pass``, the matching test below goes red.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

E2E_DIR = Path(__file__).resolve().parent
REPO_ROOT = E2E_DIR.parent.parent
THIS_FILE = Path(__file__).resolve()

# Port 1 is closed, so connections are refused immediately: neither the probe nor
# the SSE tests can hang, and the "backend missing" path is deterministic in CI.
UNREACHABLE_API_URL = "http://127.0.0.1:1"

BACKEND_TAG = "E2E-NO-BACKEND"
CREDENTIALS_TAG = "E2E-NO-CREDENTIALS"
GUARD_TAG = "E2E-GUARD"

# Stripped from the subprocess environment so an outer CI mode cannot leak in and
# silently change what the assertions below mean.
INHERITED_GATE_VARS = (
    "E2E_API_URL",
    "E2E_FRONTEND_URL",
    "E2E_REQUIRE_BACKEND",
    "E2E_REQUIRE_REAL_SERVICES",
    "E2E_RUN_FULL_FLOW",
    "E2E_PROBE_TIMEOUT",
    "PYTEST_ADDOPTS",
    "PYTEST_CURRENT_TEST",
)

OUTCOME_WORDS = ("passed", "failed", "skipped", "deselected", "error", "xfailed", "xpassed")
TIMEOUT_SECONDS = 300


def _run_pytest(args, overrides=None):
    """Run pytest in a subprocess with the e2e gating variables pinned."""
    env = {key: value for key, value in os.environ.items() if key not in INHERITED_GATE_VARS}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(overrides or {})
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args, "-p", "no:cacheprovider"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
    )


def _summary(output):
    """Return pytest's final outcome line, e.g. ``=== 3 skipped, 1 deselected in 0.1s ===``."""
    for line in reversed(output.splitlines()):
        candidate = line.strip()
        if candidate.startswith("=") and any(word in candidate for word in OUTCOME_WORDS):
            return candidate
    return ""


@pytest.mark.contract
def test_credentialed_test_is_reported_as_skipped_with_a_reason():
    """A missing credential shows up as ``skipped`` + tag, never as ``passed``."""
    proc = _run_pytest(
        ["tests/e2e/test_research_flow.py::test_full_research_flow", "-v", "-rs"],
        {"E2E_API_URL": UNREACHABLE_API_URL},
    )
    output = proc.stdout + proc.stderr

    assert proc.returncode == 0, output
    assert CREDENTIALS_TAG in output, f"skip reason must carry its tag:\n{output}"
    summary = _summary(output)
    assert "skipped" in summary, f"expected a skip, got {summary!r}:\n{output}"
    assert "passed" not in summary, f"a skip must not be counted as passed ({summary!r}):\n{output}"
    assert "PASSED" not in output, f"test must not report PASSED:\n{output}"


@pytest.mark.contract
def test_contract_selection_deselects_the_credentialed_suite():
    """The PR-facing selection marks real-service tests *deselected*, not passed."""
    proc = _run_pytest(
        ["tests/e2e", "-m", "not real_services", "-v", "-rs", "--ignore", str(THIS_FILE)],
        {"E2E_API_URL": UNREACHABLE_API_URL},
    )
    output = proc.stdout + proc.stderr

    assert proc.returncode == 0, output
    summary = _summary(output)
    assert "deselected" in summary, f"credentialed suite must be deselected, got {summary!r}:\n{output}"
    assert "skipped" in summary, f"contract tests must report their skip, got {summary!r}:\n{output}"
    assert "passed" not in summary, f"nothing may pass without a backend ({summary!r}):\n{output}"
    assert "PASSED" not in output, f"no test may report PASSED without a backend:\n{output}"
    assert BACKEND_TAG in output, f"contract skips must carry the backend tag:\n{output}"
    assert CREDENTIALS_TAG not in output, (
        "the credentialed test was deselected, so it must not also appear as skipped:\n" + output
    )


@pytest.mark.contract
def test_required_backend_turns_the_skip_into_a_failure():
    """``E2E_REQUIRE_BACKEND=1`` (CI's mode) must fail, not skip, when /health is down."""
    proc = _run_pytest(
        ["tests/e2e", "-m", "not real_services", "-q", "--ignore", str(THIS_FILE)],
        {"E2E_API_URL": UNREACHABLE_API_URL, "E2E_REQUIRE_BACKEND": "1"},
    )
    output = proc.stdout + proc.stderr

    assert proc.returncode != 0, f"a missing backend must fail the CI job:\n{output}"
    assert BACKEND_TAG in output, f"the failure must name the backend tag:\n{output}"


@pytest.mark.contract
def test_required_real_services_turn_the_skip_into_a_failure():
    """``E2E_REQUIRE_REAL_SERVICES=1`` must fail rather than skip when keys are absent."""
    proc = _run_pytest(
        ["tests/e2e", "-m", "real_services", "-q", "--ignore", str(THIS_FILE)],
        {"E2E_API_URL": UNREACHABLE_API_URL, "E2E_REQUIRE_REAL_SERVICES": "1"},
    )
    output = proc.stdout + proc.stderr

    assert proc.returncode != 0, f"missing credentials must fail the credentialed job:\n{output}"
    assert CREDENTIALS_TAG in output, f"the failure must name the credentials tag:\n{output}"


@pytest.mark.contract
def test_required_suite_matching_zero_tests_fails_loudly():
    """A mistyped/mismatched marker fails instead of running nothing and reporting green."""
    proc = _run_pytest(
        ["tests/e2e", "-m", "contract", "-q", "--ignore", str(THIS_FILE)],
        {"E2E_API_URL": UNREACHABLE_API_URL, "E2E_REQUIRE_REAL_SERVICES": "1"},
    )
    output = proc.stdout + proc.stderr

    assert proc.returncode != 0, f"a required suite that collected nothing must fail:\n{output}"
    assert GUARD_TAG in output, f"the guard must explain itself:\n{output}"