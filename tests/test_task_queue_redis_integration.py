"""针对**真实 Redis** 的集成验证（默认跳过）。

为什么需要这个文件：fakeredis 复刻的是协议语义，足以验证本项目代码里的回环，
但它不是真实中间件。本文件用真实 redis-server 复验一遍，并且**同时跑
「修复前」与「修复后」两种行为**：

  - 修复前在真实 Redis 上同样放大 → 证明 fakeredis 没有凭空造出这个缺陷
  - 修复后在真实 Redis 上不放大 → 证明修复对真实中间件同样成立

用法（需要一个可用的 Redis，例如 docker run -d -p 6379:6379 redis:7-alpine）：

    REDIS_INTEGRATION=1 REDIS_URL=redis://127.0.0.1:6379/0 \
        pytest tests/test_task_queue_redis_integration.py -v

本文件只使用独立命名空间的 task_id，不会清库、不影响其他数据。
"""
import asyncio
import json
import os
import uuid
from datetime import datetime

import pytest

from src.workflow.task_store import (
    RedisTaskStore,
    TaskEvent,
    TaskEventType,
)

REDIS_INTEGRATION = os.getenv("REDIS_INTEGRATION", "").strip() not in ("", "0", "false")
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")

AMOUNT = 30          # 两种行为下测量的调用次数
SETTLE_SECONDS = 1.0

pytestmark = pytest.mark.skipif(
    not REDIS_INTEGRATION,
    reason="需要真实 Redis：设置 REDIS_INTEGRATION=1 与 REDIS_URL",
)


class PreFixRedisTaskStore(RedisTaskStore):
    """修复前的行为：publish 不带来源标记，监听器也不做本进程回声过滤。

    这是从 2026-10-14 修复前的实现逐行还原，只保留 pub/sub 所需的最小逻辑。
    用它证明：回环不是 fakeredis 的产物，真实 Redis 上同样成立。
    """

    async def add_event(self, task_id: str, event: TaskEvent):
        ts = await self.get(task_id)
        if not ts:
            return None
        ts.events.append(event)
        ts.updated_at = datetime.now().isoformat(timespec="seconds")
        client = await self._get_client()
        pipe = client.pipeline()
        pipe.hset(self._state_key(task_id), "data", json.dumps(ts.to_dict()))
        pipe.rpush(self._events_key(task_id), json.dumps(event.to_dict()))
        await pipe.execute()
        # 旧行为：没有 _origin 标记
        await client.publish(self._channel(task_id), json.dumps(event.to_dict()))
        return ts

    async def _start_listener(self):
        if self._listener_task and not self._listener_task.done():
            return

        async def listener():
            client = await self._get_client()
            pubsub = client.pubsub()
            await pubsub.psubscribe("task_events:*")
            try:
                async for message in pubsub.listen():
                    if message["type"] in {"message", "pmessage"}:
                        try:
                            data = json.loads(message["data"])
                            event = TaskEvent.from_dict(data)
                            task_id = event.task_id
                            if task_id in self._emitters:
                                # 旧行为：无条件回灌到同一个 emitter
                                await self._emitters[task_id].emit(event)
                        except Exception:
                            pass
            except asyncio.CancelledError:
                pass

        self._listener_task = asyncio.create_task(listener())


def _event(task_id: str) -> TaskEvent:
    return TaskEvent(
        id=0,
        event=TaskEventType.DECOMPOSE.value,
        task_id=task_id,
        stage="decompose",
        status="running",
        message="正在理解需求",
    )


async def _drive(store, task_id: str, amount: int = AMOUNT):
    """挂上 on_event、发一次事件、等待放大稳定，返回 on_event 调用次数。"""
    await store.create(task_id, "integration query", {"report": True, "prd": False})
    emitter = store.get_or_create_emitter(task_id)
    calls = []

    async def on_event(event):
        calls.append(event)
        if len(calls) >= amount:
            emitter._on_event = None  # 护栏：让断言失败而不是挂死
            return
        await store.add_event(task_id, event)

    emitter._on_event = on_event
    await store._start_listener()
    await asyncio.sleep(0.2)

    await emitter.emit(_event(task_id))
    await asyncio.sleep(SETTLE_SECONDS)

    stored = await store._client.llen(store._events_key(task_id))
    return len(calls), stored


async def _cleanup(store, task_id: str):
    client = store._client
    await client.delete(
        store._state_key(task_id),
        store._events_key(task_id),
        store._artifacts_key(task_id),
    )


@pytest.mark.asyncio
async def test_pre_fix_behavior_amplifies_on_real_redis():
    """修复前的代码在真实 Redis 上同样被放大——缺陷不是 fakeredis 的产物。"""
    task_id = f"itx-prefix-{uuid.uuid4().hex[:8]}"
    store = PreFixRedisTaskStore(REDIS_URL)
    try:
        calls, stored = await _drive(store, task_id)
    finally:
        await _cleanup(store, task_id)
        await store.close()

    assert calls > 10, (
        f"修复前行为在真实 Redis 上只触发了 {calls} 次，未能复现回环，"
        "说明 fakeredis 与真实中间件存在语义差异，需要重新评估"
    )
    assert stored > 10, f"真实 Redis 事件列表应有大量残留，实际 {stored} 条"
    print(f"\n[真实 Redis · 修复前] on_event 调用 {calls} 次，事件列表残留 {stored} 条")


@pytest.mark.asyncio
async def test_fixed_behavior_does_not_amplify_on_real_redis():
    """修复后的代码在真实 Redis 上只投递一次。"""
    task_id = f"itx-fixed-{uuid.uuid4().hex[:8]}"
    store = RedisTaskStore(REDIS_URL)
    try:
        calls, stored = await _drive(store, task_id)
    finally:
        await _cleanup(store, task_id)
        await store.close()

    assert calls == 1, f"修复后仍被放大 {calls} 次"
    assert stored == 1, f"事件列表应只剩 1 条，实际 {stored} 条"
    print(f"\n[真实 Redis · 修复后] on_event 调用 {calls} 次，事件列表残留 {stored} 条")


@pytest.mark.asyncio
async def test_cross_process_delivery_still_works_on_real_redis():
    """跨进程分发未被回环修复误伤。"""
    task_id = f"itx-fanout-{uuid.uuid4().hex[:8]}"
    runner = RedisTaskStore(REDIS_URL)
    observer = RedisTaskStore(REDIS_URL)
    try:
        await runner.create(task_id, "integration query", {"report": True, "prd": False})
        queue = observer.subscribe_task(task_id)
        await observer._start_listener()
        await asyncio.sleep(0.2)

        published = []

        async def on_event(event):
            published.append(event)
            if len(published) >= AMOUNT:
                runner.get_or_create_emitter(task_id)._on_event = None
                return
            await runner.add_event(task_id, event)

        runner_emitter = runner.get_or_create_emitter(task_id)
        runner_emitter._on_event = on_event

        await runner_emitter.emit(_event(task_id))
        await asyncio.sleep(SETTLE_SECONDS)

        delivered = []
        while not queue.empty():
            delivered.append(queue.get_nowait())

        assert len(published) == 1, f"发布方自身被放大 {len(published)} 次"
        assert len(delivered) == 1, f"另一进程收到 {len(delivered)} 条，应为 1 条"
    finally:
        await _cleanup(runner, task_id)
        await runner.close()
        await observer.close()