"""Fixtures and gating rules for the end-to-end suite.

The suite carries two explicit intents, selected by marker:

``contract``
    HTTP contract of a running backend. No LLM/search credentials required.
    This is what every push / pull request runs (``-m "not real_services"``).

``real_services``
    Drives the whole research pipeline through real search and LLM providers,
    so it needs credentials. CI keeps it in a separate job and never folds it
    into a green pull-request build.

Every skip carries a greppable tag so a skipped test can never be read as a
passing one::

    E2E-NO-BACKEND      backend unreachable (expected locally, a FAILURE in CI)
    E2E-NO-CREDENTIALS  real LLM/search credentials were not supplied

Setting ``E2E_REQUIRE_BACKEND=1`` / ``E2E_REQUIRE_REAL_SERVICES=1`` turns those
skips into failures *and* requires the matching marker to have collected at
least one test, so a renamed marker fails the run instead of quietly collecting
nothing and reporting success.
"""

import os

import pytest
import requests

# Machine-greppable tags: every skip/failure below starts with one of these.
BACKEND_TAG = "E2E-NO-BACKEND"
CREDENTIALS_TAG = "E2E-NO-CREDENTIALS"
GUARD_TAG = "E2E-GUARD"

# Documented opt-in for the credentialed pipeline suite (see README).
CREDENTIAL_ENV = "E2E_RUN_FULL_FLOW"

TRUTHY = {"1", "true", "yes", "on"}

DEFAULT_PROBE_TIMEOUT = 3.0


def _flag(name):
    """True when env var ``name`` is set to an affirmative value."""
    return os.getenv(name, "").strip().lower() in TRUTHY


def _probe_timeout():
    try:
        return float(os.getenv("E2E_PROBE_TIMEOUT", DEFAULT_PROBE_TIMEOUT))
    except ValueError:
        return DEFAULT_PROBE_TIMEOUT


def _gate(tag, detail, strict):
    """Skip loudly, or fail loudly when the caller declared the suite required."""
    reason = f"{tag}: {detail}"
    if strict:
        pytest.fail(reason)
    pytest.skip(reason)


def pytest_configure(config):
    # pyproject.toml has no [tool.pytest.ini_options], so register the markers
    # here; CI additionally runs with --strict-markers.
    config.addinivalue_line(
        "markers",
        "contract: HTTP-contract E2E test; needs a running backend, no external credentials.",
    )
    config.addinivalue_line(
        "markers",
        "real_services: drives real search/LLM providers; needs credentials, excluded from PR builds.",
    )


def pytest_report_header(config):
    modes = []
    if _flag("E2E_REQUIRE_BACKEND"):
        modes.append("E2E_REQUIRE_BACKEND=on (unreachable backend = FAILURE, not skip)")
    if _flag("E2E_REQUIRE_REAL_SERVICES"):
        modes.append("E2E_REQUIRE_REAL_SERVICES=on (missing credentials = FAILURE, not skip)")
    if not modes:
        modes.append("local mode: skips allowed (CI sets E2E_REQUIRE_BACKEND=1)")
    return [f"e2e gating: {'; '.join(modes)}"]


def pytest_collection_finish(session):
    """Fail when a *required* suite matched zero tests (typo'd marker / all deselected)."""
    for env_name, marker in (
        ("E2E_REQUIRE_BACKEND", "contract"),
        ("E2E_REQUIRE_REAL_SERVICES", "real_services"),
    ):
        if not _flag(env_name):
            continue
        if not any(item.get_closest_marker(marker) is not None for item in session.items):
            pytest.exit(
                f"{GUARD_TAG}: {env_name}=1 but no '{marker}' test was collected. "
                f"The run would have been green while testing nothing.",
                returncode=1,
            )


@pytest.fixture(scope="session")
def api_base_url():
    return os.getenv("E2E_API_URL", "http://localhost:8080")


@pytest.fixture(scope="session")
def frontend_base_url():
    return os.getenv("E2E_FRONTEND_URL", "http://localhost:3000")


@pytest.fixture(scope="session")
def live_backend(api_base_url):
    """Return the API base URL once ``/health`` answers.

    Unreachable backend -> explicit ``E2E-NO-BACKEND`` skip locally, hard failure
    when ``E2E_REQUIRE_BACKEND=1``. Probing before the tests run also keeps the
    SSE tests from blocking on a connection that can never be established.
    """
    try:
        response = requests.get(f"{api_base_url}/health", timeout=_probe_timeout())
        response.raise_for_status()
    except Exception as exc:  # any transport error means "not reachable"
        _gate(
            BACKEND_TAG,
            f"backend at {api_base_url} is not reachable ({type(exc).__name__}: {exc})",
            _flag("E2E_REQUIRE_BACKEND"),
        )
    return api_base_url


@pytest.fixture(scope="session")
def real_service_credentials():
    """Gate for tests that need real search/LLM providers.

    Checked before the backend so that a credential-less environment reports the
    credential reason rather than a misleading backend reason.
    """
    if not _flag(CREDENTIAL_ENV):
        _gate(
            CREDENTIALS_TAG,
            f"real search/LLM pipeline needs credentials; set {CREDENTIAL_ENV}=1 and "
            f"provide the provider secrets (see the e2e-real-services CI job)",
            _flag("E2E_REQUIRE_REAL_SERVICES"),
        )
    return True