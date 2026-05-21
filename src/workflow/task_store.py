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

from src.config.settings import REDIS_URL, TASK_STORE_BACKEND

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskEventType(str, Enum):
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

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue()
        self._subscribers.append(q)
        return q

    async def emit(self, event: TaskEvent):
        if self._on_event:
            try:
                result = self._on_event(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning(f"Event callback error: {e}")
        async with self._lock:
            for q in self._subscribers:
                await q.put(event)

    def emit_sync(self, event: TaskEvent):
        if self._on_event:
            try:
                result = self._on_event(event)
                if asyncio.iscoroutine(result):
                    raise RuntimeError("emit_sync cannot await coroutine")
            except Exception as e:
                logger.warning(f"Event callback error: {e}")
        for q in self._subscribers:
            q.put_nowait(event)


class PersistenceBackend(ABC):
    """抽象持久化后端接口"""

    @abstractmethod
    async def create(self, task_id: str, user_query: str, deliverables: Optional[Dict[str, bool]] = None) -> Optional[TaskState]:
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
    ) -> Optional[TaskState]:
        async with self._lock:
            ts = TaskState(
                task_id=task_id,
                status=TaskStatus.PENDING,
                created_at=datetime.now().isoformat(timespec="seconds"),
                updated_at=datetime.now().isoformat(timespec="seconds"),
                user_query=user_query,
                deliverables=deliverables or {"report": True, "prd": False},
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

    async def update(self, task_id: str, **kwargs) -> Optional[TaskState]:
        ts = await self.get(task_id)
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
        client = await self._get_client()
        await client.hset(self._state_key(task_id), "data", json.dumps(ts.to_dict()))
        return ts

    async def add_event(self, task_id: str, event: TaskEvent) -> Optional[TaskState]:
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
        await client.publish(self._channel(task_id), json.dumps(event.to_dict()))
        return ts

    async def set_artifact(self, task_id: str, key: str, artifact: TaskArtifact) -> Optional[TaskState]:
        ts = await self.get(task_id)
        if not ts:
            return None
        ts.artifacts[key] = artifact
        ts.updated_at = datetime.now().isoformat(timespec="seconds")
        client = await self._get_client()
        await client.hset(self._state_key(task_id), "data", json.dumps(ts.to_dict()))
        return ts

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
                            event = TaskEvent.from_dict(data)
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
