import pytest
import os


@pytest.fixture(scope="session")
def api_base_url():
    return os.getenv("E2E_API_URL", "http://localhost:8080")


@pytest.fixture(scope="session")
def frontend_base_url():
    return os.getenv("E2E_FRONTEND_URL", "http://localhost:3000")