"""任务状态管理和事件发布模块"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4
import asyncio
import logging

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
            "artifacts": {k: v.to_dict() for k, v in self.artifacts.items()},
            "events": [e.to_dict() for e in self.events],
        }
        return result


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


class TaskStore:
    """
    进程内任务存储。
    代码结构预留替换 Redis/数据库的接口。
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
    ) -> TaskState:
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


_task_store: Optional[TaskStore] = None


def get_task_store() -> TaskStore:
    global _task_store
    if _task_store is None:
        _task_store = TaskStore()
    return _task_store


def _sanitize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """移除可能导致密码泄露的字段"""
    sanitized = dict(payload)
    for key in ["password", "auth_credentials", "secret", "token"]:
        if key in sanitized:
            del sanitized[key]
    return sanitized