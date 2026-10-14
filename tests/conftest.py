import sys
from pathlib import Path
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TEST_USER_ID = "test-user"
OTHER_USER_ID = "other-user"


@pytest.fixture(autouse=True)
def _rate_limits_off():
    """测试期间关掉限流。

    全局限流按 token 计数，而整套用例共用少数几个测试 token——不关的话几百个
    请求会挤在同一个分钟窗口里互相踩，用例结果取决于执行顺序与机器快慢。
    限流本身由 tests/test_rate_limit.py 单独验（那里会显式打开），不在这里生效。
    """
    import server

    previous = server.limiter.enabled
    server.limiter.enabled = False
    try:
        yield
    finally:
        server.limiter.enabled = previous


def _headers(user_id: str, role: str = "user"):
    from src.auth.jwt_auth import create_access_token

    token = create_access_token({"sub": user_id, "username": user_id, "role": role})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def auth_headers():
    return _headers(TEST_USER_ID)


@pytest.fixture
def other_user_headers():
    """另一个普通用户，用于跨用户隔离测试。"""
    return _headers(OTHER_USER_ID)


@pytest.fixture
def admin_headers():
    """token 层管理员主体（role 直接取自 JWT claim）。

    仅适用于读取路径的 admin 放行判定（_principal）。特权写端点走
    _require_admin，已改为查库比对（防 token role 与库脱节），其测试
    用 test_tenant_isolation.py 里的 real_admin_headers。
    """
    return _headers("admin-user", role="admin")