"""任务状态管理和事件发布模块"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4
import asyncio
import json
import logging

import redis.asyncio as redis
from redis.exceptions import WatchError

# CAS（WATCH 乐观锁）最大重试次数：正常并发下 1-2 次即可成功
CAS_MAX_RETRIES = 5

from src.config.settings import REDIS_URL, TASK_STORE_BACKEND

logger = logging.getLogger(__name__)

# 单个订阅者（SSE 连接）的待消费事件上限。防止慢消费者或失联连接导致内存无界增长。
SUBSCRIBER_QUEUE_MAXSIZE = 1000


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    # 预算耗尽收束：已有结论但未跑完预算，按部分结果交付（README 对外口径）
    PARTIAL = "partial"
    FAILED = "failed"


class TaskEventType(str, Enum):
    """SSE 事件名的**唯一登记处**。

    这个枚举此前是装饰性的：工作流全程用裸字符串发事件，从不查它，于是
    `citation_check` / `injection_detected` / `partial` 三个真实事件压根不在册，
    打错一个名字也没人会发现——前端只会安静地少收一个事件。
    现在 `_emit` 会拿事件名来这里对表（MVPWorkflow._emit），未登记即抛错。
    """
    TASK_CREATED = "task_created"
    DECOMPOSE = "decompose"
    SEARCH = "search"
    CRAWL_START = "crawl_start"
    CRAWL_PROGRESS = "crawl_progress"
    CRAWL_COMPLETE = "crawl_complete"
    EXTRACT = "extract"
    VERIFY = "verify"
    REPORT_GENERATE = "report_generate"
    PRD_GENERATE = "prd_generate"
    ARTIFACT_READY = "artifact_ready"
    COMPLETED = "completed"
    ERROR = "error"
    # ReAct 模式（AGENT_MODE=react）每轮工具调用的进度事件
    AGENT_STEP = "agent_step"
    # 引用校验闭环（M2）：开始 / 未通过待重生成 / 通过 / 终判未过
    CITATION_CHECK = "citation_check"
    # 提示注入检测（M5 L5）：边爬边发，供前端实时提示"来源 X 检测到 N 处"
    INJECTION_DETECTED = "injection_detected"
    # 预算耗尽收束（M6）：报告已交付但被截断，与 completed 互斥
    PARTIAL = "partial"
    # 缺口定向补抓（gap refetch）：判定层决定补抓 / 补抓进行中 / 补抓完成 / 判定不触发
    GAP_REFETCH = "gap_refetch"


#: 事件名白名单。`_emit` 对表用，避免每个调用点各自遍历枚举。
KNOWN_EVENT_NAMES = frozenset(item.value for item in TaskEventType)


@dataclass
class TaskEvent:
    id: int
    event: str
    task_id: str
    stage: str
    status: str
    message: str
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat(timespec="seconds")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TaskEvent":
        return cls(**d)


@dataclass
class TaskArtifact:
    artifact_type: str  # "report" | "prd"
    content: Optional[str] = None
    content_id: Optional[str] = None
    report_id: Optional[str] = None
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat(timespec="seconds")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TaskArtifact":
        return cls(**d)


@dataclass
class TaskState:
    task_id: str
    status: TaskStatus
    created_at: str
    updated_at: str
    user_query: str
    current_stage: str = ""
    current_message: str = ""
    events: List[TaskEvent] = field(default_factory=list)
    artifacts: Dict[str, TaskArtifact] = field(default_factory=dict)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    report_id: Optional[str] = None
    deliverables: Dict[str, bool] = field(default_factory=lambda: {"report": True, "prd": False})
    crawl_results: Dict[str, Any] = field(default_factory=dict)
    quality_score: Optional[float] = None
    quality_grade: Optional[str] = None
    missing_dimensions: List[str] = field(default_factory=list)
    # 任务归属用户。默认值保证旧任务（Redis/内存里已存在的状态）反序列化时不报错。
    # 为 None 表示无主，按策略仅 admin 可读。
    user_id: Optional[str] = None
    # 注入审计发现的条数（冗余计数，供列表页免加载 findings 明细直接展示）
    injection_findings_count: int = 0
    # ReAct 模式的成本报告（TaskBudget.snapshot() 的口径）；legacy 路径为 None
    cost_report: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if isinstance(self.status, str):
            self.status = TaskStatus(self.status)
        if isinstance(self.created_at, datetime):
            self.created_at = self.created_at.isoformat(timespec="seconds")
        if isinstance(self.updated_at, datetime):
            self.updated_at = self.updated_at.isoformat(timespec="seconds")

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "task_id": self.task_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "user_query": self.user_query,
            "current_stage": self.current_stage,
            "current_message": self.current_message,
            "errors": self.errors,
            "report_id": self.report_id,
            "deliverables": self.deliverables,
            "crawl_results": self.crawl_results,
            "quality_score": self.quality_score,
            "quality_grade": self.quality_grade,
            "missing_dimensions": self.missing_dimensions,
            "user_id": self.user_id,
            "injection_findings_count": self.injection_findings_count,
            "cost_report": self.cost_report,
            "artifacts": {k: v.to_dict() for k, v in self.artifacts.items()},
            "events": [e.to_dict() for e in self.events],
        }
        return result

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TaskState":
        artifacts = {k: TaskArtifact.from_dict(v) for k, v in d.get("artifacts", {}).items()}
        events = [TaskEvent.from_dict(e) for e in d.get("events", [])]
        d = dict(d)
        d["artifacts"] = artifacts
        d["events"] = events
        return cls(**d)


class EventEmitter:
    """事件发布器，可选callback，线程安全"""

    def __init__(self, on_event: Optional[Callable[[TaskEvent], None]] = None):
        self._on_event = on_event
        self._subscribers: List[asyncio.Queue] = []
        self._lock = asyncio.Lock()

    def subscribe(self, maxsize: int = SUBSCRIBER_QUEUE_MAXSIZE) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=maxsize)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """移除订阅者。SSE 连接断开后必须调用，否则队列永久滞留在 emitter 上。"""
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    @staticmethod
    def _deliver(queue: asyncio.Queue, event: TaskEvent) -> None:
        """非阻塞投递。队列满时丢弃最旧事件。

        这里必须用 put_nowait 而不是 await queue.put()：加了 maxsize 之后，
        put() 会在慢消费者上阻塞，进而阻塞事件发布方（乃至整个工作流）。
        实时进度场景下丢失中间帧是可接受的，调用方另有重连补发兜底。
        """
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Subscriber queue still full after dropping oldest event")

    async def emit(self, event: TaskEvent):
        """串行化整个发布过程：回调（id 分配 + 持久化）→ 投递。

        曾是两段式（回调完成后再拿锁投递）：两个并发 emit 的回调在 await 点
        交错时，先调用的可能后投递——订阅者看到乱序帧，id 分配顺序（回调内）
        与投递顺序脱钩，SSE 的 id 游标过滤还会把迟到的低 id 帧静默丢弃。
        整体串行后，调用顺序 == id 顺序 == 投递顺序 == 本实例内的存储顺序。
        慢回调（如 Redis 写）持锁挂起是预期语义：它只推迟后续事件——
        `_emit` 是 fire-and-forget，工作流本身不会被阻塞。
        """
        async with self._lock:
            if self._on_event:
                try:
                    result = self._on_event(event)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    logger.warning(f"Event callback error: {e}")
            # 迭代快照：unsubscribe 不持锁，遍历快照可避免遍历期间列表被改
            for q in list(self._subscribers):
                self._deliver(q, event)

    def emit_sync(self, event: TaskEvent):
        if self._on_event:
            try:
                result = self._on_event(event)
                if asyncio.iscoroutine(result):
                    raise RuntimeError("emit_sync cannot await coroutine")
            except Exception as e:
                logger.warning(f"Event callback error: {e}")
        for q in list(self._subscribers):
            self._deliver(q, event)


class PersistenceBackend(ABC):
    """抽象持久化后端接口"""

    @abstractmethod
    async def create(self, task_id: str, user_query: str, deliverables: Optional[Dict[str, bool]] = None, user_id: Optional[str] = None) -> Optional[TaskState]:
        pass

    @abstractmethod
    async def get(self, task_id: str) -> Optional[TaskState]:
        pass

    @abstractmethod
    async def update(self, task_id: str, **kwargs) -> Optional[TaskState]:
        pass

    @abstractmethod
    async def add_event(self, task_id: str, event: TaskEvent) -> Optional[TaskState]:
        pass

    @abstractmethod
    async def set_artifact(self, task_id: str, key: str, artifact: TaskArtifact) -> Optional[TaskState]:
        pass

    @abstractmethod
    async def list_tasks(self) -> List[TaskState]:
        pass

    @abstractmethod
    def subscribe_task(self, task_id: str) -> asyncio.Queue:
        pass

    @abstractmethod
    def unsubscribe_task(self, task_id: str, queue: asyncio.Queue) -> None:
        """解除 SSE 订阅，释放队列引用。"""
        pass

    @abstractmethod
    async def close(self):
        pass


class InMemoryTaskStore(PersistenceBackend):
    """
    进程内任务存储（基于内存字典）。
    """

    def __init__(self):
        self._tasks: Dict[str, TaskState] = {}
        self._lock = asyncio.Lock()
        self._emitters: Dict[str, EventEmitter] = {}

    async def create(
        self,
        task_id: str,
        user_query: str,
        deliverables: Optional[Dict[str, bool]] = None,
        user_id: Optional[str] = None,
    ) -> Optional[TaskState]:
        async with self._lock:
            ts = TaskState(
                task_id=task_id,
                status=TaskStatus.PENDING,
                created_at=datetime.now().isoformat(timespec="seconds"),
                updated_at=datetime.now().isoformat(timespec="seconds"),
                user_query=user_query,
                deliverables=deliverables or {"report": True, "prd": False},
                user_id=user_id,
            )
            self._tasks[task_id] = ts
            return ts

    async def get(self, task_id: str) -> Optional[TaskState]:
        async with self._lock:
            return self._tasks.get(task_id)

    async def update(self, task_id: str, **kwargs) -> Optional[TaskState]:
        async with self._lock:
            ts = self._tasks.get(task_id)
            if not ts:
                return None
            if "status" in kwargs and kwargs["status"] is not None:
                ts.status = TaskStatus(kwargs["status"])
            if "current_stage" in kwargs:
                ts.current_stage = kwargs["current_stage"]
            if "current_message" in kwargs:
                ts.current_message = kwargs["current_message"]
            if "updated_at" not in kwargs:
                ts.updated_at = datetime.now().isoformat(timespec="seconds")
            if "report_id" in kwargs:
                ts.report_id = kwargs["report_id"]
            if "errors" in kwargs:
                ts.errors = kwargs["errors"]
            if "crawl_results" in kwargs:
                ts.crawl_results = kwargs["crawl_results"]
            if "quality_score" in kwargs:
                ts.quality_score = kwargs["quality_score"]
            if "quality_grade" in kwargs:
                ts.quality_grade = kwargs["quality_grade"]
            if "missing_dimensions" in kwargs:
                ts.missing_dimensions = kwargs["missing_dimensions"] or []
            if "injection_findings_count" in kwargs:
                ts.injection_findings_count = kwargs["injection_findings_count"]
            if "cost_report" in kwargs:
                ts.cost_report = kwargs["cost_report"]
            self._tasks[task_id] = ts
            return ts

    async def add_event(self, task_id: str, event: TaskEvent) -> Optional[TaskState]:
        async with self._lock:
            ts = self._tasks.get(task_id)
            if not ts:
                return None
            ts.events.append(event)
            ts.updated_at = datetime.now().isoformat(timespec="seconds")
            self._tasks[task_id] = ts
            return ts

    async def set_artifact(self, task_id: str, key: str, artifact: TaskArtifact) -> Optional[TaskState]:
        async with self._lock:
            ts = self._tasks.get(task_id)
            if not ts:
                return None
            ts.artifacts[key] = artifact
            ts.updated_at = datetime.now().isoformat(timespec="seconds")
            self._tasks[task_id] = ts
            return ts

    async def list_tasks(self) -> List[TaskState]:
        async with self._lock:
            return list(self._tasks.values())

    def get_or_create_emitter(self, task_id: str) -> EventEmitter:
        if task_id not in self._emitters:
            self._emitters[task_id] = EventEmitter()
        return self._emitters[task_id]

    def subscribe_task(self, task_id: str) -> asyncio.Queue:
        emitter = self.get_or_create_emitter(task_id)
        return emitter.subscribe()

    def unsubscribe_task(self, task_id: str, queue: asyncio.Queue) -> None:
        emitter = self._emitters.get(task_id)
        if emitter:
            emitter.unsubscribe(queue)

    async def close(self):
        pass


class RedisTaskStore(PersistenceBackend):
    """
    Redis-backed 任务存储。
    使用 Redis hash 存储 TaskState，list 存储 events，pub/sub 事件分发。
    """

    def __init__(self, redis_url: str = REDIS_URL):
        self._redis_url = redis_url
        self._client: Optional[redis.Redis] = None
        self._pubsub: Optional[redis.client.PubSub] = None
        self._emitters: Dict[str, EventEmitter] = {}
        self._lock = asyncio.Lock()
        self._listener_task: Optional[asyncio.Task] = None
        self._subscribers: Dict[str, asyncio.Queue] = {}
        # 标识本进程实例。publish 时带上它，监听器据此丢弃本进程自己的回声，
        # 否则 emit -> on_event -> add_event -> publish -> 监听器 -> 同一个 emitter
        # 会构成闭环，一次事件被无限自我放大（仅 Redis 后端受影响）。
        self._instance_id = uuid4().hex[:8]

    async def _get_client(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.from_url(self._redis_url, decode_responses=True)
        return self._client

    def _state_key(self, task_id: str) -> str:
        return f"task:{task_id}:state"

    def _events_key(self, task_id: str) -> str:
        return f"task:{task_id}:events"

    def _artifacts_key(self, task_id: str) -> str:
        return f"task:{task_id}:artifacts"

    def _channel(self, task_id: str) -> str:
        return f"task_events:{task_id}"

    def _task_index_key(self) -> str:
        return "task:index"

    async def create(
        self,
        task_id: str,
        user_query: str,
        deliverables: Optional[Dict[str, bool]] = None,
        user_id: Optional[str] = None,
    ) -> Optional[TaskState]:
        client = await self._get_client()
        async with self._lock:
            ts = TaskState(
                task_id=task_id,
                status=TaskStatus.PENDING,
                created_at=datetime.now().isoformat(timespec="seconds"),
                updated_at=datetime.now().isoformat(timespec="seconds"),
                user_query=user_query,
                deliverables=deliverables or {"report": True, "prd": False},
                user_id=user_id,
            )
            data = json.dumps(ts.to_dict())
            pipe = client.pipeline()
            pipe.hset(self._state_key(task_id), mapping={"data": data})
            pipe.sadd(self._task_index_key(), task_id)
            await pipe.execute()
            return ts

    async def get(self, task_id: str) -> Optional[TaskState]:
        client = await self._get_client()
        data = await client.hget(self._state_key(task_id), "data")
        if not data:
            return None
        return TaskState.from_dict(json.loads(data))

    async def _cas_write(self, task_id: str, mutator) -> Optional[TaskState]:
        """读-改-写带 WATCH 乐观锁：并发写者以数据版本互斥，输家重读重放。

        旧实现 get→改→hset 是裸读改写：两个并发 add_event 都基于同一快照各写
        一份，后写覆盖先写——事件与状态静默丢失（多 worker 部署必现）。
        mutator 每次重试都作用于**最新**已提交快照。任务不存在返回 None；
        重试耗尽仍冲突则放弃本次写并告警——比静默覆盖丢数据诚实。
        """
        client = await self._get_client()
        key = self._state_key(task_id)
        for _ in range(CAS_MAX_RETRIES):
            async with client.pipeline() as pipe:
                try:
                    await pipe.watch(key)
                    raw = await pipe.hget(key, "data")
                    if raw is None:
                        await pipe.unwatch()
                        return None
                    ts = TaskState.from_dict(json.loads(raw))
                    mutator(ts)
                    pipe.multi()
                    pipe.hset(key, "data", json.dumps(ts.to_dict()))
                    await pipe.execute()
                    return ts
                except WatchError:
                    continue
        logger.warning(
            "task %s CAS 重试 %d 次仍冲突，放弃本次写（读侧仍看到最新已提交状态）",
            task_id, CAS_MAX_RETRIES,
        )
        return None

    @staticmethod
    def _apply_update_fields(ts: TaskState, kwargs: Dict[str, Any]) -> None:
        if "status" in kwargs and kwargs["status"] is not None:
            ts.status = TaskStatus(kwargs["status"])
        if "current_stage" in kwargs:
            ts.current_stage = kwargs["current_stage"]
        if "current_message" in kwargs:
            ts.current_message = kwargs["current_message"]
        if "updated_at" not in kwargs:
            ts.updated_at = datetime.now().isoformat(timespec="seconds")
        if "report_id" in kwargs:
            ts.report_id = kwargs["report_id"]
        if "errors" in kwargs:
            ts.errors = kwargs["errors"]
        if "crawl_results" in kwargs:
            ts.crawl_results = kwargs["crawl_results"]
        if "quality_score" in kwargs:
            ts.quality_score = kwargs["quality_score"]
        if "quality_grade" in kwargs:
            ts.quality_grade = kwargs["quality_grade"]
        if "missing_dimensions" in kwargs:
            ts.missing_dimensions = kwargs["missing_dimensions"] or []
        if "injection_findings_count" in kwargs:
            ts.injection_findings_count = kwargs["injection_findings_count"]
        if "cost_report" in kwargs:
            ts.cost_report = kwargs["cost_report"]

    async def update(self, task_id: str, **kwargs) -> Optional[TaskState]:
        def _mutate(ts: TaskState) -> None:
            self._apply_update_fields(ts, kwargs)
        return await self._cas_write(task_id, _mutate)

    async def add_event(self, task_id: str, event: TaskEvent) -> Optional[TaskState]:
        def _mutate(ts: TaskState) -> None:
            ts.events.append(event)
            ts.updated_at = datetime.now().isoformat(timespec="seconds")

        ts = await self._cas_write(task_id, _mutate)
        if ts is None:
            return None
        client = await self._get_client()
        # events list 本身 rpush 原子追加；状态 JSON 里的 events 由 CAS 保证一致
        await client.rpush(self._events_key(task_id), json.dumps(event.to_dict()))
        # 只在 publish 的载荷上标记来源，不写入存储：存储侧的结构保持向后兼容
        published = json.dumps({**event.to_dict(), "_origin": self._instance_id})
        await client.publish(self._channel(task_id), published)
        return ts

    async def set_artifact(self, task_id: str, key: str, artifact: TaskArtifact) -> Optional[TaskState]:
        def _mutate(ts: TaskState) -> None:
            ts.artifacts[key] = artifact
            ts.updated_at = datetime.now().isoformat(timespec="seconds")

        return await self._cas_write(task_id, _mutate)

    async def list_tasks(self) -> List[TaskState]:
        client = await self._get_client()
        task_ids = await client.smembers(self._task_index_key())
        tasks = []
        for task_id in task_ids:
            ts = await self.get(task_id)
            if ts:
                tasks.append(ts)
        return tasks

    def get_or_create_emitter(self, task_id: str) -> EventEmitter:
        if task_id not in self._emitters:
            self._emitters[task_id] = EventEmitter()
        return self._emitters[task_id]

    def subscribe_task(self, task_id: str) -> asyncio.Queue:
        emitter = self.get_or_create_emitter(task_id)
        return emitter.subscribe()

    def unsubscribe_task(self, task_id: str, queue: asyncio.Queue) -> None:
        emitter = self._emitters.get(task_id)
        if emitter:
            emitter.unsubscribe(queue)

    async def _start_listener(self):
        """Start background listener for pubsub messages."""
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
                            # 必须在构造 TaskEvent 之前取出，否则 dataclass 会因未知
                            # 字段抛 TypeError，异常被下面的 except 吞掉后表现为事件静默丢失
                            origin = data.pop("_origin", None)
                            event = TaskEvent.from_dict(data)
                            if origin == self._instance_id:
                                # 本进程发布的事件，已由 emit() 直接投递给本地订阅者
                                continue
                            task_id = event.task_id
                            if task_id in self._emitters:
                                await self._emitters[task_id].emit(event)
                        except Exception as e:
                            logger.warning(f"Failed to process pubsub message: {e}")
            except asyncio.CancelledError:
                pass
            finally:
                await pubsub.punsubscribe("task_events:*")
                await pubsub.close()

        self._listener_task = asyncio.create_task(listener())

    async def close(self):
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.close()
            self._client = None


_task_store: Optional[PersistenceBackend] = None


def get_task_store() -> PersistenceBackend:
    global _task_store
    if _task_store is None:
        if TASK_STORE_BACKEND == "redis":
            _task_store = RedisTaskStore()
        else:
            _task_store = InMemoryTaskStore()
    if isinstance(_task_store, RedisTaskStore):
        try:
            if not _task_store._listener_task or _task_store._listener_task.done():
                asyncio.create_task(_task_store._start_listener())
        except RuntimeError:
            logger.warning("Redis task listener will start when get_task_store is called inside an event loop")
    return _task_store


def _sanitize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """移除可能导致密码泄露的字段"""
    sanitized = dict(payload)
    for key in ["password", "auth_credentials", "secret", "token"]:
        if key in sanitized:
            del sanitized[key]
    return sanitized
