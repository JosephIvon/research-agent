"""状态Schema定义 - 定义各阶段数据结构"""
from typing import TypedDict, List, Optional, Union, Literal
from datetime import datetime


class Subtask(TypedDict):
    """子任务结构"""
    id: str
    description: str
    status: Literal["pending", "in_progress", "completed", "failed"]
    priority: int
    retry_count: int = 0


class SearchResult(TypedDict):
    """搜索结果结构"""
    url: str
    title: str
    content: str
    timestamp: datetime
    source_type: str


class ExtractedInfo(TypedDict):
    """提取信息结构"""
    key: str
    value: Union[str, dict, list]
    source_url: str
    confidence: float


class VerificationResult(TypedDict):
    """核查结果结构"""
    fact: str
    sources: List[str]
    is_verified: bool
    confidence: float  # 0-10
    conflicting_sources: List[str]


class ErrorRecord(TypedDict):
    """错误记录结构"""
    timestamp: datetime
    stage: str
    error_type: str
    message: str
    retry_attempts: int


class CompetitorData(TypedDict):
    """单个竞品数据结构"""
    name: str
    url: str
    extracted_data: List[ExtractedInfo]
    status: Literal["success", "failed", "partial"]
    error_message: Optional[str]


class ResearchState(TypedDict):
    """核心状态结构 - 定义整个调研流程的共享状态"""

    # 元数据
    task_id: str
    status: Literal["pending", "decomposing", "researching",
                   "verifying", "writing", "completed", "failed"]
    created_at: datetime
    updated_at: datetime
    termination_reason: Optional[str]  # 状态转换原因

    # 输入数据
    user_query: str
    target_urls: List[str]
    auth_credentials: Optional[dict]

    # 分解结果
    subtasks: Optional[List[Subtask]]
    current_subtask_index: int

    # 调研数据
    raw_search_results: List[SearchResult]
    extracted_data: List[ExtractedInfo]

    # 事实核查
    verification_results: List[VerificationResult]
    verification_score: Optional[float]
    verification_passed: Optional[bool]
    verification_warnings: List[str]

    # 数据质量评估
    quality_score: Optional[float]
    quality_grade: Optional[str]
    missing_dimensions: List[str]

    # 多竞品数据
    competitors: List[CompetitorData]
    comparison_matrix: Optional[dict]
    comparison_passed: Optional[bool]

    # 报告生成
    report_draft: Optional[str]
    report_final: Optional[str]

    # 错误处理
    errors: List[ErrorRecord]
    retry_count: int
    max_retries: int

    # 文档同步
    sync_targets: List[str]
    sync_results: dict