"""限流接线测试：全局兜底 + /mcp 显式守卫（M5 §4-M5「复用限流」）。

这组用例的存在理由是一个实测结论：

    普通未装饰路由 /plain  -> [200,200,200,200,200,429,429,429]   限住了
    mount 子应用 /mcp/ping  -> [200,200,200,200,200,200,200,200] 完全没限

slowapi 的 _find_route_handler 只在顶层路由里找 endpoint，Mount 没有 endpoint，
于是返回 None；而 _should_exempt 对 None **直接返回 True**——请求被当成"无主路由"
整条跳过限流。所以只挂 SlowAPIMiddleware 会给 /mcp 装一道看着有、实际不设防的门。

conftest 默认把限流关掉（整套用例共用少数几个测试 token，不关会互相踩），
这里显式打开并把阈值调到极小，才测得到东西。
"""
import pytest
from fastapi.testclient import TestClient
from limits import parse

import server

TINY = "3/minute"
# 全局兜底用的是 Limiter 构造时定下的 default_limits（默认 60/minute），
# 那是 slowapi 的内部状态，没法干净地改小——所以这条用例真打 61 次。
GLOBAL_DEFAULT_LIMIT = 60


@pytest.fixture
def limited_client(monkeypatch):
    """/mcp 档压到 3/minute；全局档保持默认 60/minute（那条用例按真值打满）。

    `limiter.reset()` 是 slowapi 的公开清空接口，用它而不是去动 storage——
    计数字段是内部属性，改了就等于让测试依赖实现细节。

    必须以 `with` 进入：/mcp 是挂载的 MCP streamable 子应用，其会话管理器
    在 ASGI lifespan 里初始化——裸 TestClient 不跑 lifespan，请求会在
    "Task group is not initialized" 上炸（本地与 CI 同红，实测踩过）。
    """
    monkeypatch.setattr(server, "_MCP_RATE_LIMIT", parse(TINY))
    server.limiter.reset()
    server.limiter.enabled = True
    try:
        with TestClient(server.app) as client:
            yield client
    finally:
        server.limiter.enabled = False
        server.limiter.reset()


def test_mcp_is_rate_limited(limited_client, auth_headers):
    """核心用例：/mcp 必须在超额后返回 429，而不是无限放行。

    阈值内的具体状态码不写死：本机没装 MCP SDK，/mcp 走降级返回 503；
    装了 SDK 的 CI 上会是 200。要断的是"阈值内放行、超额变 429"这件事。
    """
    codes = [limited_client.get("/mcp/", headers=auth_headers).status_code for _ in range(6)]
    assert codes[:3] == [codes[0]] * 3 and 429 not in codes[:3], \
        f"阈值内必须放行到子应用（实测 {codes[:3]}）"
    assert codes[3:] == [429, 429, 429], f"超额后必须稳定 429，实测 {codes}"


def test_rate_limited_mcp_returns_the_standard_429_shape(limited_client, auth_headers):
    """429 的响应体要与其它路由一致（复用同一个已注册的处理器）。"""
    for _ in range(5):
        r = limited_client.get("/mcp/", headers=auth_headers)
    assert r.status_code == 429
    assert "detail" in r.json() or "error" in r.json()


def test_global_backstop_covers_undecorated_routes(limited_client, auth_headers):
    """没有 @limiter.limit 装饰的普通路由也要被兜住——这正是挂中间件的意义。

    用 /settings/all：它在路由表里但**不在** PUBLIC_PATHS，也没被装饰器覆盖，
    正好是全局兜底该管的典型。默认阈值 60/minute，就真打满。
    """
    codes = [limited_client.get("/settings/all", headers=auth_headers).status_code
             for _ in range(GLOBAL_DEFAULT_LIMIT + 3)]
    assert codes[:GLOBAL_DEFAULT_LIMIT] == [200] * GLOBAL_DEFAULT_LIMIT, \
        f"阈值内必须全部放行，实测尾部 {codes[-3:]}"
    assert codes[GLOBAL_DEFAULT_LIMIT] == 429, \
        f"未装饰路由没有被全局兜底限流，实测尾部 {codes[-3:]}"


def test_public_health_paths_are_never_throttled(limited_client):
    """探活不能被限流：负载均衡每秒轮询就是 60/min，正好卡在默认阈值上。

    必须真打满 60/minute 才有判别力——变异验证实测：只打 30 次时，
    把公开路径豁免整段删掉这条用例**照样是绿的**（够不到阈值，什么都测不出来）。
    """
    over = GLOBAL_DEFAULT_LIMIT + 10
    for i in range(over):
        r = limited_client.get("/health")
        assert r.status_code == 200, f"/health 在第 {i + 1} 次被限流了（{r.status_code}）"


def test_public_settings_path_is_never_throttled(limited_client):
    """/settings 同属 PUBLIC_PATHS，同样必须打满验证。"""
    over = GLOBAL_DEFAULT_LIMIT + 10
    for i in range(over):
        r = limited_client.get("/settings")
        assert r.status_code == 200, f"/settings 在第 {i + 1} 次被限流了（{r.status_code}）"


def test_login_is_never_throttled_by_the_global_backstop(limited_client):
    """登录被误伤等于把服务锁死，PUBLIC_PATHS 一律豁免。"""
    over = GLOBAL_DEFAULT_LIMIT + 10
    for i in range(over):
        r = limited_client.post("/auth/login", json={"username": "nobody", "password": "x"})
        assert r.status_code != 429, f"登录接口在第 {i + 1} 次被限流了"


def test_disabling_the_limiter_stops_all_of_it(monkeypatch, auth_headers):
    """关掉限流后连打也不会 429——这是运维开关的兜底保证。

    必须把阈值压到极小，否则这条用例是**假绿**：用真实 RATE_LIMIT_PER_TOKEN
    （120/min）打 8 次根本碰不到阈值，守卫拦没拦都看不出区别。守卫直连
    `limiter.limiter.hit()` 会绕过 slowapi 的 `enabled` 标志，所以这里要能
    在"真会触发 429"的条件下证明限流确实停住了。
    """
    monkeypatch.setattr(server, "_MCP_RATE_LIMIT", parse(TINY))
    server.limiter.reset()
    server.limiter.enabled = False
    try:
        with TestClient(server.app) as client:  # /mcp 需要 lifespan（见 limited_client 注释）
            codes = [client.get("/mcp/", headers=auth_headers).status_code for _ in range(8)]
            assert 429 not in codes, f"limiter.enabled=False 后期望完全不限流，实测 {codes}"
    finally:
        server.limiter.enabled = False
        server.limiter.reset()


def test_rate_limit_enabled_false_stops_the_mcp_guard(monkeypatch, auth_headers):
    """运维把 RATE_LIMIT_ENABLED 关掉时，/mcp 守卫也必须跟着停。

    守卫此前是无条件注册的模块级中间件，只有上面那层 SlowAPIMiddleware 看开关。
    结果是设 RATE_LIMIT_ENABLED=false 后全站解除限流，唯独 /mcp 还按
    RATE_LIMIT_PER_TOKEN 硬限——调试期无路可关，行为与配置项语义直接矛盾。
    同样把阈值压到极小，保证这条不是碰巧够不着。
    """
    monkeypatch.setattr(server, "_MCP_RATE_LIMIT", parse(TINY))
    monkeypatch.setattr(server, "RATE_LIMIT_ENABLED", False)
    server.limiter.reset()
    server.limiter.enabled = True
    try:
        with TestClient(server.app) as client:
            codes = [client.get("/mcp/", headers=auth_headers).status_code for _ in range(8)]
            assert 429 not in codes, f"RATE_LIMIT_ENABLED=False 后期望完全不限流，实测 {codes}"
    finally:
        server.limiter.enabled = False
        server.limiter.reset()


def test_mcp_still_requires_authentication(limited_client):
    """限流不能变成绕过鉴权的门：未认证请求仍是 401，不是 429。"""
    r = limited_client.get("/mcp/")
    assert r.status_code == 401
