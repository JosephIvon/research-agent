"""EventEmitter 订阅生命周期与背压测试。

覆盖两个此前存在的问题：

1. `subscribe()` 只增不减 —— SSE 连接断开后队列永久滞留在 emitter 上，
   长连接服务下会随连接次数无界增长。
2. 订阅队列无上界 —— 失联或极慢的客户端会让队列无限堆积。

注意第 2 点的修法本身有风险：队列加上 maxsize 之后，如果投递仍用
`await queue.put()`，队列满时会**阻塞事件发布方**，进而卡住整个工作流。
因此这里同时锁死"非阻塞投递 + 丢弃最旧"的行为。
"""
import asyncio

import pytest

from src.workflow.task_store import (
    SUBSCRIBER_QUEUE_MAXSIZE,
    EventEmitter,
    InMemoryTaskStore,
    RedisTaskStore,
    TaskEvent,
    TaskEventType,
)


def _event(index: int = 0) -> TaskEvent:
    return TaskEvent(
        id=index,
        event=TaskEventType.DECOMPOSE.value,
        task_id="t",
        stage="decompose",
        status="running",
        message=f"step-{index}",
    )


def _drain(queue: asyncio.Queue):
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


class TestSubscriptionLifecycle:
    def test_unsubscribe_stops_delivery(self):
        emitter = EventEmitter()
        queue = emitter.subscribe()
        emitter.unsubscribe(queue)
        assert emitter._subscribers == []

    @pytest.mark.asyncio
    async def test_unsubscribed_queue_receives_nothing(self):
        emitter = EventEmitter()
        queue = emitter.subscribe()
        emitter.unsubscribe(queue)

        await emitter.emit(_event(1))

        assert queue.empty(), "解除订阅后仍收到事件"

    def test_unsubscribe_is_idempotent(self):
        emitter = EventEmitter()
        queue = emitter.subscribe()
        emitter.unsubscribe(queue)
        emitter.unsubscribe(queue)  # 重复调用不应抛异常
        emitter.unsubscribe(asyncio.Queue())  # 陌生队列也不应抛异常
        assert emitter._subscribers == []

    @pytest.mark.asyncio
    async def test_remaining_subscribers_keep_receiving(self):
        emitter = EventEmitter()
        kept = emitter.subscribe()
        dropped = emitter.subscribe()
        emitter.unsubscribe(dropped)

        await emitter.emit(_event(2))

        assert len(_drain(kept)) == 1
        assert dropped.empty()


class TestBackpressure:
    def test_subscribe_queue_is_bounded_by_default(self):
        emitter = EventEmitter()
        queue = emitter.subscribe()
        assert queue.maxsize == SUBSCRIBER_QUEUE_MAXSIZE

    @pytest.mark.asyncio
    async def test_slow_consumer_does_not_block_emit(self):
        """队列满时投递必须立即返回，绝不能把发布方卡住。"""
        emitter = EventEmitter()
        emitter.subscribe(maxsize=2)

        for i in range(5):
            # 若投递实现是 await queue.put()，这里会在第 3 个事件后永久挂起
            await asyncio.wait_for(emitter.emit(_event(i)), timeout=1.0)

    @pytest.mark.asyncio
    async def test_overflow_drops_oldest_keeps_newest(self):
        emitter = EventEmitter()
        queue = emitter.subscribe(maxsize=2)

        for i in range(5):
            await emitter.emit(_event(i))

        delivered = _drain(queue)
        assert len(delivered) == 2, "队列应保持有界"
        assert [e.id for e in delivered] == [3, 4], "应保留最新事件、丢弃最旧事件"

    @pytest.mark.asyncio
    async def test_emit_sync_also_respects_bound(self):
        emitter = EventEmitter()
        queue = emitter.subscribe(maxsize=2)

        for i in range(4):
            emitter.emit_sync(_event(i))

        assert [e.id for e in _drain(queue)] == [2, 3]


class TestOrdering:
    @pytest.mark.asyncio
    async def test_concurrent_emit_delivery_follows_call_order(self):
        """并发 emit 时，投递顺序必须等于 emit 调用顺序。

        这是 emit 整体串行化的回归测试。修复前是两段式（回调完成后再拿锁
        投递）：第一个 emit 的回调在 await 处挂起时，第二个 emit 的回调
        可以抢先完成并先投递——订阅者看到乱序帧，且回调里分配的 id 顺序
        与投递顺序脱钩。测试刻意让先调用的事件回调更慢（模拟 Redis 写
        延迟抖动），无串行化时第二个事件必然先入队。
        """
        emitter = EventEmitter()
        queue = emitter.subscribe()

        # 复刻 server.py _run_research_task.on_event 的闭包计数器模式：
        # id 在回调内、await 之后才分配——这是最容易被交错打断的位置
        event_counter = 0
        delays = {"first": 0.05, "second": 0.0}

        async def on_event(event: TaskEvent):
            nonlocal event_counter
            await asyncio.sleep(delays[event.message])
            event_counter += 1
            event.id = event_counter

        emitter._on_event = on_event

        first = _event(0)
        first.message = "first"
        second = _event(0)
        second.message = "second"

        t1 = asyncio.create_task(emitter.emit(first))
        await asyncio.sleep(0)  # 确保 t1 先起步，固定"调用顺序"
        t2 = asyncio.create_task(emitter.emit(second))
        await asyncio.gather(t1, t2)

        delivered = _drain(queue)
        assert [e.message for e in delivered] == ["first", "second"], (
            "并发 emit 下投递乱序：先调用的事件被后投递"
        )
        assert [e.id for e in delivered] == [1, 2], (
            "id 分配顺序与投递顺序脱钩，SSE 游标去重会丢帧"
        )


class TestStoreUnsubscribe:
    @pytest.mark.parametrize(
        "backend",
        ["memory", "redis"],
    )
    def test_store_implements_unsubscribe(self, backend):
        store = InMemoryTaskStore() if backend == "memory" else RedisTaskStore("redis://unused")
        task_id = f"task-{backend}"

        queue = store.subscribe_task(task_id)
        emitter = store.get_or_create_emitter(task_id)
        assert len(emitter._subscribers) == 1

        store.unsubscribe_task(task_id, queue)
        assert emitter._subscribers == []

    def test_unsubscribe_unknown_task_is_safe(self):
        store = InMemoryTaskStore()
        store.unsubscribe_task("never-created", asyncio.Queue())  # 不应抛异常