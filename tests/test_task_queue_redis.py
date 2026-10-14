"""Redis 任务存储的事件分发回归测试。

背景：`RedisTaskStore` 用 pub/sub 把事件转发给持有 SSE 订阅的进程。
修复前存在一条闭环：

    emitter.emit() -> _on_event -> store.add_event() -> client.publish()
      -> 本进程 psubscribe 监听器 -> 同一个 emitter.emit() -> 回到起点

当 `TASK_STORE_BACKEND=redis`（Docker 默认值）时，一次事件会被无限自我放大，
Redis 事件列表无界增长、SSE 客户端收到无限重复事件、单 worker 下事件循环被占满。

只有 Redis 后端受影响：内存后端没有 pub/sub 监听器。
这些用例此前完全没有覆盖，因此该缺陷能一路走到 main。
"""
import asyncio

import pytest

from src.workflow.task_store import (
    RedisTaskStore,
    TaskEvent,
    TaskEventType,
)

fakeredis = pytest.importorskip(
    "fakeredis", reason="需要 fakeredis 才能覆盖 Redis 任务存储后端"
)
import fakeredis.aioredis  # noqa: E402

# 回环失控时用来兜底，避免测试挂死而不是失败
AMPLIFICATION_GUARD = 50
SETTLE_SECONDS = 0.5


def _make_store(server) -> RedisTaskStore:
    """构造一个连接同一 fakeredis server 的 store（模拟同一 Redis 上的两个进程）。

    注意 decode_responses=True 必须与 RedisTaskStore._get_client() 里的生产配置一致，
    否则测试跑在 bytes 上、生产跑在 str 上，会掩盖真实行为差异。
    """
    store = RedisTaskStore("redis://unused")
    # 直接注入 client，绕过 from_url，避免 monkeypatch 全局 redis 模块
    store._client = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    return store


def _event(task_id: str, index: int = 0) -> TaskEvent:
    return TaskEvent(
        id=index,
        event=TaskEventType.DECOMPOSE.value,
        task_id=task_id,
        stage="decompose",
        status="running",
        message="正在理解需求",
    )


@pytest.mark.asyncio
async def test_emit_is_not_amplified_by_local_pubsub_echo():
    """单次 emit 只能触发一次 on_event，Redis 里也只能留下一条事件。"""
    server = fakeredis.FakeServer()
    store = _make_store(server)
    await store.create("task-loop", "query", {"report": True, "prd": False})

    emitter = store.get_or_create_emitter("task-loop")
    received = []

    async def on_event(event):
        received.append(event)
        if len(received) >= AMPLIFICATION_GUARD:
            # 回归时兜底：解除回环，让断言失败而不是让测试永久挂起
            emitter._on_event = None
            return
        await store.add_event("task-loop", event)

    emitter._on_event = on_event

    # 本进程同时持有任务执行方和监听器（即当前 Docker 的单进程部署形态）
    await store._start_listener()
    await asyncio.sleep(0.05)

    await emitter.emit(_event("task-loop"))
    await asyncio.sleep(SETTLE_SECONDS)

    assert len(received) == 1, (
        f"单次 emit 被放大成 {len(received)} 次 on_event 调用，pub/sub 回环未修复"
    )

    stored = await store._client.lrange("task:task-loop:events", 0, -1)
    assert len(stored) == 1, f"Redis 事件列表残留 {len(stored)} 条，应为 1 条"

    task = await store.get("task-loop")
    assert len(task.events) == 1, f"任务状态里残留 {len(task.events)} 条事件，应为 1 条"

    await store.close()


@pytest.mark.asyncio
async def test_event_from_another_instance_is_still_delivered():
    """跨进程分发必须保留：别的进程 publish 的事件仍要送到本地 SSE 订阅者。"""
    server = fakeredis.FakeServer()
    runner = _make_store(server)   # 进程 A：执行任务并 publish
    observer = _make_store(server)  # 进程 B：只承接 SSE 订阅

    await runner.create("task-fanout", "query", {"report": True, "prd": False})

    queue = observer.subscribe_task("task-fanout")
    await observer._start_listener()
    await asyncio.sleep(0.05)

    published = []

    async def on_event(event):
        published.append(event)
        if len(published) >= AMPLIFICATION_GUARD:
            runner_emitter = runner.get_or_create_emitter("task-fanout")
            runner_emitter._on_event = None
            return
        await runner.add_event("task-fanout", event)

    runner_emitter = runner.get_or_create_emitter("task-fanout")
    runner_emitter._on_event = on_event

    await runner_emitter.emit(_event("task-fanout"))
    await asyncio.sleep(SETTLE_SECONDS)

    assert len(published) == 1, f"发布方自身发生放大：{len(published)} 次"

    delivered = []
    while not queue.empty():
        delivered.append(queue.get_nowait())

    assert len(delivered) == 1, (
        f"另一进程只收到 {len(delivered)} 条事件，跨进程分发被误伤"
    )
    assert delivered[0].message == "正在理解需求"

    await runner.close()
    await observer.close()


@pytest.mark.asyncio
async def test_listener_survives_unknown_payload_keys():
    """注入的来源标记不得导致消息解析失败（否则事件会被静默吞掉）。"""
    server = fakeredis.FakeServer()
    store = _make_store(server)
    await store.create("task-shape", "query", {"report": True, "prd": False})

    emitter = store.get_or_create_emitter("task-shape")
    delivered = []

    async def on_event(event):
        delivered.append(event)

    emitter._on_event = on_event
    await store._start_listener()
    await asyncio.sleep(0.05)

    # 模拟另一进程按同样的协议发布消息
    import json

    other = fakeredis.aioredis.FakeRedis(server=server)
    payload = json.dumps({**_event("task-shape", 7).to_dict(), "_origin": "deadbeef"})
    await other.publish(store._channel("task-shape"), payload)
    await asyncio.sleep(SETTLE_SECONDS)

    assert len(delivered) == 1, (
        f"带来源标记的跨进程消息未被正确解析/投递，实际收到 {len(delivered)} 条"
    )
    assert delivered[0].id == 7

    await store.close()