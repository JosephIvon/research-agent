"""
E2E tests for the research agent - tests the full stack from API to workflow completion.

These tests require the backend server to be running. Set E2E_API_URL and E2E_FRONTEND_URL
environment variables if using non-default addresses.

Two test intents, kept apart on purpose (see tests/e2e/conftest.py):

* ``contract`` - health / task creation / SSE contract. Needs only a running
  backend, no credentials. CI runs these on every push and pull request.
* ``real_services`` - the full search + LLM pipeline. Needs provider secrets, so
  CI runs it in a separate, credential-gated job.

An unreachable backend or missing credentials produces a *tagged* skip
(``E2E-NO-BACKEND`` / ``E2E-NO-CREDENTIALS``), never a quiet pass.
"""
import json
import pytest
import time
import requests
import uuid


def _auth_headers(api_base_url):
    username = f"e2e_{uuid.uuid4().hex[:12]}"
    password = f"pw_{uuid.uuid4().hex}"
    response = requests.post(
        f"{api_base_url}/auth/register",
        json={"username": username, "email": f"{username}@example.com", "password": password},
        timeout=10,
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.contract
def test_health_check(live_backend):
    """Verify the /health endpoint returns 200."""
    response = requests.get(f"{live_backend}/health", timeout=10)
    assert response.status_code == 200


@pytest.mark.contract
def test_create_research_task(live_backend):
    """POST to /research/tasks with minimal payload, verify task_id is returned."""
    headers = _auth_headers(live_backend)
    payload = {
        "query": "test competitor analysis",
        "urls": [],
        "enable_search": False,
    }
    response = requests.post(f"{live_backend}/research/tasks", json=payload, headers=headers, timeout=10)
    assert response.status_code == 200
    data = response.json()
    assert "task_id" in data
    assert data["task_id"] is not None


@pytest.mark.contract
def test_sse_connection(live_backend):
    """Verify SSE endpoint returns an event stream with at least a task_created event."""
    headers = _auth_headers(live_backend)
    # First create a task
    payload = {
        "query": "test competitor analysis",
        "urls": [],
        "enable_search": False,
    }
    create_response = requests.post(f"{live_backend}/research/tasks", json=payload, headers=headers, timeout=10)
    assert create_response.status_code == 200
    task_id = create_response.json()["task_id"]

    # Connect to SSE stream
    sse_url = f"{live_backend}/research/tasks/{task_id}/events"
    events_received = {}

    sse_headers = {**headers, "Accept": "text/event-stream"}
    with requests.get(sse_url, headers=sse_headers, stream=True, timeout=30) as resp:
        assert resp.status_code == 200
        buffer = b""
        for chunk in resp.iter_content(chunk_size=256):
            if chunk:
                buffer += chunk
                # 字节层切块再解码（同 test_full_research_flow 的理由：chunk 边界
                # 可能切断 UTF-8 字符，逐块 decode 会随机 UnicodeDecodeError）
                while b"\n\n" in buffer:
                    block, buffer = buffer.split(b"\n\n", 1)
                    event_type = None
                    data_lines = []
                    for line in block.decode("utf-8", errors="replace").split("\n"):
                        if line.startswith("event: "):
                            event_type = line[7:].strip()
                        elif line.startswith("data: "):
                            data_lines.append(line[6:].strip())
                    if data_lines and event_type:
                        events_received[event_type] = "\n".join(data_lines)
                        if event_type == "task_created":
                            break
                if "task_created" in events_received:
                    break

    assert "task_created" in events_received, f"Expected task_created event, got: {events_received}"


@pytest.mark.real_services
def test_full_research_flow(real_service_credentials, live_backend):
    """
    Create a research task and wait for completion.

    用**固定稳定目标 URL**而不是自动搜索：真实搜索命中什么完全不可控
    （营销页/反爬页/大页面超时），把任意公网站点抓取失败当成 E2E 失败
    会让真服务测试 flaky——抓取失败是产品层有降级设计的正常现象。
    目标是极稳定的官网首页，验证"LLM 真调用 → 抓取 → 抽取 → 引用校验
    → 报告产出"整条真链路能跑通。

    Credential check comes first so a credential-less run reports the credential
    reason instead of a misleading backend reason.
    """
    headers = _auth_headers(live_backend)
    # 稳定目标：维基百科页——内容厚、无登录墙、无广告反爬，2026 实测可从
    # CI 容器稳定抓取。用固定 URL 而非自动搜索（搜索命中什么不可控）。
    #
    # 验收语义（与产品"诚实降级"对齐）：真链路跑通 = 任务走到终点并给出
    # 交付物——要么 completed（报告产出），要么 verify 阶段"数据质量不足"
    # 的 failed（产品特性：明确说明缺什么、给建议，不是静默失败）。
    # 真实网络+真实模型下强求"一定产出高分报告"不可控，那不是产品承诺。
    payload = {
        # query 与维基页内容贴合，提高抽取命中 features/target_users 等 key 的概率；
        # 即便最终"数据质量不足"，verify 阶段诚实降级也算真链路跑通（见上）。
        "query": "什么是竞品分析，其步骤、方法和适用场景",
        "urls": ["https://en.wikipedia.org/wiki/Competitor_analysis"],
        "enable_search": False,
    }
    create_response = requests.post(f"{live_backend}/research/tasks", json=payload, headers=headers, timeout=30)
    assert create_response.status_code == 200
    task_id = create_response.json()["task_id"]

    # Poll SSE until completion
    sse_url = f"{live_backend}/research/tasks/{task_id}/events"
    # 真模型全链路比"快速失败"路径慢一个量级：decompose + extract 各一次 LLM
    # 调用，report_generate 是 16384 max_tokens 的推理模型生成（单次实测可达
    # 1-3 分钟），引用校验不过还要再重生成一次。300s 对快速失败够用、对"真出
    # 报告"不够——CI 实测走到 report_generate 后 5 分钟没等到终态。
    # 放宽到 900s（GitHub Actions 默认 job 超时 360 分钟，无冲突），并把收到的
    # stage 顺序记成时间线，万一再超时 annotation 能直接指出卡在哪一步。
    max_wait = 900  # 15 minutes
    start = time.time()
    final_data = None
    completed = False
    terminal_error = None
    last_error_seen = None
    graceful_degraded = False
    citation_payload = None  # 引用校验通过事件的指标，通过时一并打印
    timeline: list = []  # "elapsed_s:event/stage" 序列，诊断用
    last_event_id = -1  # SSE 续传游标；-1 = 从起点回放全部
    reconnect_attempts = 0
    max_reconnects = 3  # 重连上限：后端真挂了不无限空转

    # 服务端支持 Last-Event-ID 续传（产品自身的重连设计）。真服务全链路最长
    # 15 分钟，单条 SSE 连接可能因网络抖动/容器重启在任务跑到一半时断开——
    # 那是最亏的断法：后台任务还在继续，客户端却以为失败了。这里按产品语义
    # 重连：断流且未到终态就带 Last-Event-ID 重新订阅，服务端从上次位置续传
    # （task.events 持久化在 Redis，重连时按 id > last_event_id 回放）。
    while not (completed or graceful_degraded or terminal_error):
        if time.time() - start > max_wait or reconnect_attempts > max_reconnects:
            break
        sse_headers = {**headers, "Accept": "text/event-stream"}
        if last_event_id >= 0:
            sse_headers["Last-Event-ID"] = str(last_event_id)
        try:
            with requests.get(sse_url, headers=sse_headers, stream=True, timeout=max_wait) as resp:
                if resp.status_code != 200:
                    last_error_seen = f"SSE 连接返回 HTTP {resp.status_code}"
                else:
                    # 字节层缓冲 + 在 \n\n 处切块：chunk 边界可能切断 UTF-8 多字节字符，
                    # 逐块 decode 会随机抛 UnicodeDecodeError（实测 chunk_size=256 下
                    # 真实触发过）。切完的整块再解码，字符必然完整。
                    buffer = b""
                    for chunk in resp.iter_content(chunk_size=256):
                        if time.time() - start > max_wait:
                            break
                        if chunk:
                            buffer += chunk
                            # SSE events are separated by blank lines (\n\n)
                            while b"\n\n" in buffer:
                                block, buffer = buffer.split(b"\n\n", 1)
                                event_type = None
                                event_id = None
                                data_lines = []
                                for line in block.decode("utf-8", errors="replace").split("\n"):
                                    if line.startswith("event: "):
                                        event_type = line[7:].strip()
                                    elif line.startswith("id: "):
                                        event_id = line[4:].strip()
                                    elif line.startswith("data: "):
                                        data_lines.append(line[6:].strip())
                                if data_lines and event_type:
                                    data_str = "\n".join(data_lines)
                                    try:
                                        event_obj = json.loads(data_str)
                                    except ValueError:
                                        event_obj = {}
                                    stage = event_obj.get("stage", "?")
                                    timeline.append(f"{time.time() - start:.0f}s:{event_type}/{stage}")
                                    if event_id is not None:
                                        last_event_id = max(last_event_id, int(event_id))
                                    if event_type == "citation_check" and event_obj.get("status") == "completed":
                                        citation_payload = event_obj.get("payload") or {}
                                    # 任务级终止事件才算结果。
                                    # 可接受的终点（产品"诚实降级"设计）：
                                    #   completed                                       → 报告产出
                                    #   error + stage=verify + 质量/数据不足            → 明确说明缺什么，非静默失败
                                    # 不可接受的终点：
                                    #   error 且 payload 未标 recoverable               → 流程中途崩溃，立即停，不再傻等
                                    #   error 但 payload.recoverable=True（如抓取失败）  → 产品设计会继续，记录后继续等终态
                                    if event_type == "completed":
                                        final_data = data_str
                                        completed = True
                                        break
                                    elif event_type == "error":
                                        is_degradation = (
                                            stage == "verify"
                                            and ("数据质量不足" in data_str or "抽取数据不足" in data_str)
                                        )
                                        recoverable = bool((event_obj.get("payload") or {}).get("recoverable"))
                                        if is_degradation:
                                            graceful_degraded = True
                                            break
                                        last_error_seen = data_str
                                        if not recoverable:
                                            terminal_error = data_str
                                            break
                        if completed or graceful_degraded or terminal_error:
                            break
                    # 走到这 = 流正常结束且未到终态（连接被服务端关闭/网络抖动），
                    # 交由外层 while 带 Last-Event-ID 重连
        except requests.exceptions.RequestException as exc:
            last_error_seen = f"SSE 连接异常: {type(exc).__name__}: {exc}"
        if completed or graceful_degraded or terminal_error:
            break
        reconnect_attempts += 1
        if reconnect_attempts <= max_reconnects:
            time.sleep(1)  # 重连前留一口气，避免紧贴断流瞬间反复撞墙

    if not completed and not graceful_degraded:
        elapsed = time.time() - start
        timeline_str = " -> ".join(timeline) if timeline else "(未收到任何事件)"
        # 最后错误放最前：annotation 有截断风险，错误必须第一个被看到
        pytest.fail(
            f"真链路未到终态（已等 {elapsed:.0f}s / 上限 {max_wait}s，重连 {reconnect_attempts} 次）"
            f"；最后错误: {terminal_error or last_error_seen}"
            f"；事件时间线: {timeline_str}"
        )
    if completed:
        assert final_data is not None

    # 结果形态直接打进日志（CI 用 -s，stdout 实时可见）：completed 还是
    # verify 降级——测试通过本身不区分这两种形态，M7 存档需要它。
    outcome = "completed(报告产出)" if completed else "graceful_degraded(verify 数据不足)"
    detail = f"引用校验 {citation_payload}" if citation_payload else ""
    print(f"[e2e-result] 终态={outcome} 耗时 {time.time() - start:.0f}s "
          f"重连 {reconnect_attempts} 次 {detail}", flush=True)
