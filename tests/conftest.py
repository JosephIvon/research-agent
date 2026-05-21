import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def auth_headers():
    from src.auth.jwt_auth import create_access_token

    token = create_access_token({"sub": "test-user", "username": "test-user"})
    return {"Authorization": f"Bearer {token}"}
