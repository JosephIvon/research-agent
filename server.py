"""Research Agent MCP Server - 让项目可被其他Agent调用"""
from datetime import datetime
from pathlib import Path
import asyncio
import os
import hmac
import json
import logging
import re
from typing import Optional, List, Dict, Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import hashlib

from src.config.settings import (
    ALLOW_PRIVATE_NETWORK_URLS,
    ALLOWED_HOSTS,
    API_AUTH_TOKEN,
    API_MAX_BODY_BYTES,
    APP_ENV,
    APP_HOST,
    APP_PORT,
    DEEPSEEK_API_KEY,
    DOUBAO_API_KEY,
    FEISHU_APP_ID,
    FEISHU_APP_SECRET,
    GLM_API_KEY,
    JWT_EXPIRE_MINUTES,
    JWT_SECRET_KEY,
    LLM_PROVIDER,
    MAX_RETRIES,
    MAX_FETCH_URLS,
    MINIMAX_API_KEY,
    OPENAI_API_KEY,
    OUTPUT_DIR,
    RATE_LIMIT_ENABLED,
    RATE_LIMIT_PER_IP,
    RATE_LIMIT_PER_TOKEN,
    SEARCH_MAX_RESULTS,
    SEARCH_PROVIDER,
    SEARCH_TIMEOUT,
    SEARXNG_URL,
    SSE_KEEPALIVE_SECONDS,
    TASK_STORE_BACKEND,
    TENCENT_APP_ID,
    TENCENT_APP_SECRET,
    TIMEOUT_SECONDS,
)
from src.auth.jwt_auth import create_access_token, decode_access_token
from src.auth.user_db import get_user_db
from src.security.url_validation import validate_fetch_url, validate_fetch_urls
from src.config.config_manager import get_config_manager
from src.workflow.task_store import (
    get_task_store,
    PersistenceBackend,
    InMemoryTaskStore,
    RedisTaskStore,
    TaskState,
    TaskEvent,
    TaskArtifact,
    TaskEventType,
    TaskStatus,
    EventEmitter,
    _sanitize_payload,
)
from src.storage import get_report_db, REPORT_NOT_IN_DB
from src.logging import get_audit_logger, AuditEvent
# 可观测层统一出口。未启用 Langfuse 时全部为 no-op，不产生网络调用与额外开销。
from src.observability import clear_task_context, end_trace, set_task_context, start_trace, trace_url

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Add audit logger handler
audit_logger = logging.getLogger("audit")
audit_handler = logging.StreamHandler()
audit_handler.setFormatter(logging.Formatter("%(message)s"))
audit_logger.addHandler(audit_handler)
audit_logger.setLevel(logging.INFO)
audit_logger.propagate = False

IS_PRODUCTION = APP_ENV == "production"
# 兼容两种报告 ID：历史格式 research_report_YYYYmmdd_HHMMSS，
# 以及加入 task_id 后缀的新格式 research_report_YYYYmmdd_HHMMSS_<8位hex>。
REPORT_ID_RE = re.compile(r"^research_report_\d{8}_\d{6}(?:_[0-9a-f]{8})?$")
app = FastAPI(
    title="Research Agent MCP Server",
    version="1.0.0",
    docs_url=None if IS_PRODUCTION else "/docs",
    redoc_url=None if IS_PRODUCTION else "/redoc",
    openapi_url=None if IS_PRODUCTION else "/openapi.json",
)

if ALLOWED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            origin.strip()
            for origin in os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:3000").split(",")
            if origin.strip()
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


PUBLIC_PATHS = {"/", "/health", "/health/ready", "/settings", "/sync/status", "/auth/register", "/auth/login"}
if not IS_PRODUCTION:
    PUBLIC_PATHS.update({"/docs", "/redoc", "/openapi.json"})

# Token-based rate limit key function: uses Bearer token if present, otherwise falls back to IP
def get_token_key(request: Request) -> str:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        return hashlib.sha256(token.encode()).hexdigest()[:16]
    return get_remote_address(request)

# Initialize rate limiter
limiter = Limiter(
    key_func=get_token_key,
    default_limits=[RATE_LIMIT_PER_IP] if RATE_LIMIT_ENABLED else [],
)

# Add rate limit exceeded handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# JWT Authentication middleware - supports both legacy API_AUTH_TOKEN and new JWT
@app.middleware("http")
async def enforce_auth(request: Request, call_next):
    # Skip auth for public paths
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    # Try JWT authentication first
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        payload = decode_access_token(token)
        if payload and "sub" in payload:
            request.state.user_id = payload["sub"]
            request.state.username = payload.get("username", "")
            request.state.role = payload.get("role", "user")
            return await call_next(request)

    # Fall back to legacy API_AUTH_TOKEN if configured
    if API_AUTH_TOKEN:
        expected = f"Bearer {API_AUTH_TOKEN}"
        actual = request.headers.get("Authorization", "")
        if hmac.compare_digest(expected, actual):
            # Legacy token 是一个共享的管理凭证：按 admin 主体处理，
            # 既保持它原先"能看到全部数据"的语义，也避免它创建的报告变成谁都读不到的孤儿。
            request.state.user_id = "api-token"
            request.state.username = "api-token"
            request.state.role = "admin"
            return await call_next(request)
        # Token provided but doesn't match either JWT or legacy → reject
        return JSONResponse(status_code=401, content={"detail": "认证失败"})

    # No valid auth provided
    return JSONResponse(status_code=401, content={"detail": "未授权，请先登录"})


@app.middleware("http")
async def enforce_body_size_limit(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > API_MAX_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"detail": f"请求体过大，最大允许 {API_MAX_BODY_BYTES} 字节"},
                )
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Content-Length 无效"})
    return await call_next(request)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuthCredentials(StrictModel):
    username: str = Field(..., min_length=1, max_length=512)
    password: SecretStr = Field(..., min_length=1, max_length=2048)

    def to_plain_dict(self) -> Dict[str, str]:
        return {
            "username": self.username,
            "password": self.password.get_secret_value(),
        }


class CompetitorSite(StrictModel):
    url: str = Field(..., min_length=1, max_length=2048)
    login_url: Optional[str] = Field(default=None, min_length=1, max_length=2048)
    auth_credentials: Optional[AuthCredentials] = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, url: str) -> str:
        return validate_fetch_url(url)

    @field_validator("login_url")
    @classmethod
    def validate_login_url(cls, login_url: Optional[str]) -> Optional[str]:
        return validate_fetch_url(login_url) if login_url else login_url

    def to_workflow_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "login_url": self.login_url,
            "auth_credentials": _auth_dict(self.auth_credentials),
        }


class ResearchRequest(StrictModel):
    query: str = Field(..., min_length=1, max_length=1000)
    urls: List[str] = Field(default_factory=list, max_length=MAX_FETCH_URLS)
    target_sites: List[CompetitorSite] = Field(default_factory=list, max_length=MAX_FETCH_URLS)
    auth_credentials: Optional[AuthCredentials] = None
    login_url: Optional[str] = None
    enable_search: bool = True
    custom_selectors: Optional[Dict[str, List[str]]] = None

    @field_validator("urls")
    @classmethod
    def validate_urls(cls, urls: List[str]) -> List[str]:
        return validate_fetch_urls(urls)

    @field_validator("login_url")
    @classmethod
    def validate_login_url(cls, login_url: Optional[str]) -> Optional[str]:
        return validate_fetch_url(login_url) if login_url else login_url

    @model_validator(mode="after")
    def validate_target_count(self):
        unique_urls = {site.url for site in self.target_sites}
        unique_urls.update(self.urls)
        if len(unique_urls) > MAX_FETCH_URLS:
            raise ValueError(f"目标URL数量不能超过 {MAX_FETCH_URLS} 个")
        return self

    @field_validator("custom_selectors")
    @classmethod
    def validate_custom_selectors(
        cls, selectors: Optional[Dict[str, List[str]]]
    ) -> Optional[Dict[str, List[str]]]:
        if selectors is None:
            return None
        allowed_keys = {"username", "password", "submit"}
        unknown_keys = set(selectors) - allowed_keys
        if unknown_keys:
            raise ValueError(f"不支持的选择器字段: {sorted(unknown_keys)}")
        for key, values in selectors.items():
            if not values or len(values) > 10:
                raise ValueError(f"{key} 选择器数量必须在 1-10 之间")
            for value in values:
                if not value or len(value) > 300:
                    raise ValueError(f"{key} 选择器长度必须在 1-300 字符之间")
        return selectors


class FollowUpRequest(StrictModel):
    original_query: str = Field(..., min_length=1, max_length=1000)
    report_content: str = Field(..., min_length=1, max_length=200_000)
    question: str = Field(..., min_length=1, max_length=1000)


class ReviewRequest(StrictModel):
    report_content: str = Field(..., min_length=1, max_length=200_000)
    role: Optional[str] = None

    @field_validator("role")
    @classmethod
    def validate_role(cls, role: Optional[str]) -> Optional[str]:
        if role is not None and role not in {"developer", "tester", "operations"}:
            raise ValueError("role 仅支持 developer、tester、operations")
        return role


class PRDRequest(StrictModel):
    report_content: str = Field(..., min_length=1, max_length=200_000)
    query: str = Field(..., min_length=1, max_length=1000)


class SyncRequest(StrictModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=120)
    report_id: Optional[str] = Field(default=None, pattern=REPORT_ID_RE.pattern)
    report_content: Optional[str] = Field(default=None, min_length=1, max_length=200_000)
    content: Optional[str] = Field(default=None, min_length=1, max_length=200_000)
    full_report: bool = True


class ExportRequest(BaseModel):
    report_id: str = Field(..., pattern=REPORT_ID_RE.pattern)
    format: str = Field(..., pattern="^(pdf|word|html)$")
    content_type: Optional[str] = Field(default="report", pattern="^(report|prd)$")


class ReportFeedbackRequest(StrictModel):
    """报告反馈请求体（M4 反馈闭环）：rating 仅允许 up/down，comment 上限 500 字。"""
    rating: str
    comment: str = Field(default="", max_length=500)

    @field_validator("rating")
    @classmethod
    def validate_rating(cls, rating: str) -> str:
        if rating not in {"up", "down"}:
            raise ValueError("rating 仅支持 up 或 down")
        return rating


class UserRegister(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    email: str = Field(..., min_length=5, max_length=256)
    password: str = Field(..., min_length=8, max_length=128)

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        if not v.isalnum() and "_" not in v:
            raise ValueError("username 只能包含字母、数字和下划线")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if "@" not in v:
            raise ValueError("email 格式无效")
        return v.lower()


class UserLogin(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class UserResponse(BaseModel):
    id: str
    username: str
    email: str
    role: str
    created_at: str
    last_login: Optional[str] = None


def _auth_dict(credentials: Optional[AuthCredentials]) -> Optional[Dict[str, str]]:
    return credentials.to_plain_dict() if credentials else None


def _target_sites(req: ResearchRequest) -> List[Dict[str, Any]]:
    return [site.to_workflow_dict() for site in req.target_sites]


def _internal_error(operation: str, exc: Exception) -> HTTPException:
    logger.exception("%s失败", operation)
    return HTTPException(status_code=500, detail=f"{operation}失败，请查看服务端日志")


def _output_dir() -> Path:
    path = Path(OUTPUT_DIR).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _report_path(report_id: str) -> Path:
    if not REPORT_ID_RE.fullmatch(report_id):
        raise HTTPException(status_code=404, detail="报告不存在")
    path = (_output_dir() / f"{report_id}.md").resolve()
    if path.parent != _output_dir():
        raise HTTPException(status_code=404, detail="报告不存在")
    return path


def _report_files() -> List[Path]:
    return sorted(
        _output_dir().glob("research_report_*.md"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )


def _read_report(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="报告不存在") from exc


def _extract_title(markdown: str, fallback: str) -> str:
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()[:120] or fallback
        if stripped:
            return stripped[:120]
    return fallback


def _report_payload(path: Path, include_markdown: bool = False) -> Dict[str, Any]:
    markdown = _read_report(path)
    created_at = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    payload = {
        "id": path.stem,
        "title": _extract_title(markdown, path.stem),
        "date": created_at,
        "created_at": created_at,
        "size_bytes": path.stat().st_size,
        "preview": markdown[:240],
        "grade": None,
        "score": None,
        "competitors": None,
        "missing_dimensions": [],
    }
    if include_markdown:
        payload["markdown"] = markdown
    return payload


def _load_report_payload(report_id: str) -> Dict[str, Any]:
    path = _report_path(report_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="报告不存在")
    return _report_payload(path, include_markdown=True)


def _latest_report_payload() -> Dict[str, Any]:
    files = _report_files()
    if not files:
        raise HTTPException(status_code=404, detail="暂无可同步的报告")
    return _report_payload(files[0], include_markdown=True)


def _latest_accessible_report(request: Request):
    """返回当前调用者**有权访问**的最新报告文件；没有则返回 None。

    此前直接取磁盘上最新的文件，不区分归属——任意用户不传 report_id 就能把
    他人的报告同步到飞书/腾讯。
    """
    user_id, is_admin = _principal(request)
    for path in _report_files():
        if is_admin or _can_access_report(path.stem, user_id, is_admin):
            return path
    return None


# ---------- 租户访问控制 ----------

def _principal(request: Request):
    """返回 (user_id, is_admin)。"""
    user_id = getattr(request.state, "user_id", None)
    is_admin = getattr(request.state, "role", None) == "admin"
    return user_id, is_admin


def _can_access_report(report_id: str, user_id: Optional[str], is_admin: bool) -> bool:
    """报告访问权判定。

    无主报告（user_id 为 NULL）和库里查不到归属记录的文件，**一律仅 admin 可见**。
    """
    if is_admin:
        return True
    try:
        owner = get_report_db().get_report_owner(report_id)
    except Exception as exc:
        logger.warning("读取报告归属失败 %s: %s", report_id, exc)
        return False
    if owner is None or owner is REPORT_NOT_IN_DB:
        return False
    return owner == user_id


def _require_report_access(request: Request, report_id: str) -> None:
    """无权限时抛 404 而不是 403 —— 403 等于告诉对方"该报告存在"。"""
    user_id, is_admin = _principal(request)
    if not _can_access_report(report_id, user_id, is_admin):
        raise HTTPException(status_code=404, detail="报告不存在")


def _require_admin(request: Request) -> None:
    """特权端点守卫：以数据库为准，不信任 JWT 里的 role claim。

    token 的 role 只反映签发时刻的库状态；将来一旦加入「改角色/降权」入口，
    旧 token 在有效期内仍会带着 admin。这里每次查库，让降权即时生效；
    查库异常按无权限处理（fail-closed）。读取路径（_principal 的 is_admin）
    仍用 token claim，避免每个请求多一次查询——过期的管理员读权限，
    风险远小于写权限。
    """
    user_id, _ = _principal(request)
    try:
        is_admin = bool(user_id) and get_user_db().is_admin(user_id)
    except Exception as exc:
        logger.warning("管理员校验查库失败，按无权限处理: %s", exc)
        is_admin = False
    if not is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")


def _safe_settings_payload() -> Dict[str, Any]:
    llm_keys = {
        "minimax": MINIMAX_API_KEY,
        "deepseek": DEEPSEEK_API_KEY,
        "doubao": DOUBAO_API_KEY,
        "glm": GLM_API_KEY,
        "openai": OPENAI_API_KEY,
    }
    return {
        "app_env": APP_ENV,
        "max_retries": MAX_RETRIES,
        "timeout_seconds": TIMEOUT_SECONDS,
        "max_fetch_urls": MAX_FETCH_URLS,
        "allow_private_network_urls": ALLOW_PRIVATE_NETWORK_URLS,
        "api_auth_enabled": bool(API_AUTH_TOKEN),
        "llm_provider": LLM_PROVIDER,
        "llm_configured": bool(llm_keys.get(LLM_PROVIDER)),
        "minimax_configured": bool(MINIMAX_API_KEY),
        "search_provider": SEARCH_PROVIDER,
        "searxng_url": SEARXNG_URL,
        "search_timeout": SEARCH_TIMEOUT,
        "search_max_results": SEARCH_MAX_RESULTS,
        "feishu_configured": bool(FEISHU_APP_ID and FEISHU_APP_SECRET),
        "tencent_configured": bool(TENCENT_APP_ID and TENCENT_APP_SECRET),
    }


def _sync_content(req: SyncRequest, request: Request) -> tuple[str, str]:
    content = req.report_content or req.content
    title = req.title or "智能调研报告"
    if content:
        return content, title

    if req.report_id:
        _require_report_access(request, req.report_id)
        report = _load_report_payload(req.report_id)
        return report["markdown"], req.title or report["title"]

    # 未指定报告：只允许取调用者自己有权访问的那一份
    path = _latest_accessible_report(request)
    if path is None:
        raise HTTPException(status_code=404, detail="暂无可同步的报告")
    report = _report_payload(path, include_markdown=True)
    return report["markdown"], req.title or report["title"]


@limiter.limit(RATE_LIMIT_PER_TOKEN)
@app.post("/research/competitive", response_model=Dict[str, Any])
async def competitive_research(request: Request, req: ResearchRequest):
    """竞品分析接口"""
    try:
        from src.workflow.mvp_workflow import MVPWorkflow

        user_id, _ = _principal(request)
        workflow = MVPWorkflow()
        result = await workflow.run(
            user_query=req.query,
            target_urls=req.urls,
            target_sites=_target_sites(req),
            auth_credentials=_auth_dict(req.auth_credentials),
            login_url=req.login_url,
            enable_search=req.enable_search,
            custom_selectors=req.custom_selectors,
            user_id=user_id,
        )

        return {
            # partial 不折成 success 也不折成 failed：报告已经产出了但被预算截断，
            # 说成功是撒谎，说失败又会让调用方白白丢掉这份报告。
            # 注意 completed 仍对外报 "success"（既有契约，不在本次改动范围）。
            "status": ("success" if result["status"] == "completed"
                       else ("partial" if result["status"] == "partial" else "failed")),
            "task_id": result.get("task_id"),
            "query": req.query,
            "report": result.get("report_final"),
            "quality_score": result.get("quality_score"),
            "quality_grade": result.get("quality_grade"),
            "missing_dimensions": result.get("missing_dimensions", []),
            "verification_score": result.get("verification_score"),
            "termination_reason": result.get("termination_reason"),
            "competitors": result.get("competitors", []),
            "errors": result.get("errors", []),
            "output_files": result.get("sync_results", {}),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
    except Exception as e:
        raise _internal_error("竞品分析", e)


@limiter.limit(RATE_LIMIT_PER_TOKEN)
@app.post("/research/followup", response_model=Dict[str, Any])
def followup(request: Request, req: FollowUpRequest):
    """追问接口"""
    try:
        from src.tools.followup_optimizer import get_follow_up_optimizer

        optimizer = get_follow_up_optimizer()
        answer = optimizer.answer_follow_up(
            req.original_query,
            req.report_content,
            req.question
        )

        return {"status": "success", "answer": answer}
    except Exception as e:
        raise _internal_error("追问优化", e)


@limiter.limit(RATE_LIMIT_PER_TOKEN)
@app.post("/research/suggest-followup", response_model=Dict[str, Any])
def suggest_followup(request: Request, req: FollowUpRequest):
    """推荐追问方向接口"""
    try:
        from src.tools.followup_optimizer import get_follow_up_optimizer

        optimizer = get_follow_up_optimizer()
        questions = optimizer.suggest_follow_up_questions(
            req.original_query,
            req.report_content
        )

        return {"status": "success", "suggested_questions": questions}
    except Exception as e:
        raise _internal_error("推荐追问方向", e)


@limiter.limit(RATE_LIMIT_PER_TOKEN)
@app.post("/research/multi-role-review", response_model=Dict[str, Any])
def review(request: Request, req: ReviewRequest):
    """多角色审查接口"""
    try:
        from src.tools.multi_role_reviewer import get_multi_role_reviewer

        reviewer = get_multi_role_reviewer()
        if req.role:
            result = reviewer.review_from_role(req.report_content, req.role)
            return {"status": "success", "role": req.role, "review": result}
        else:
            results = reviewer.review_all_roles(req.report_content)
            return {"status": "success", "reviews": results}
    except Exception as e:
        raise _internal_error("多角色审查", e)


@limiter.limit(RATE_LIMIT_PER_TOKEN)
@app.post("/research/prd", response_model=Dict[str, Any])
def generate_prd(request: Request, req: PRDRequest):
    """从竞品分析生成PRD文档"""
    try:
        from src.tools.prd_generator import PRDGenerator

        generator = PRDGenerator()
        prd_content = generator.generate_from_competitive_report(req.report_content, req.query)

        return {"status": "success", "prd": prd_content}
    except Exception as e:
        raise _internal_error("PRD生成", e)


@limiter.limit(RATE_LIMIT_PER_TOKEN)
@app.post("/research/prd-from-query", response_model=Dict[str, Any])
async def prd_from_query(request: Request, req: ResearchRequest):
    """PRD一体化接口：输入查询，自动执行竞品分析+PRD生成

    工作流：
    1. 调用竞品分析
    2. 获取报告
    3. 调用PRD生成
    4. 返回PRD文档
    """
    try:
        from src.workflow.mvp_workflow import MVPWorkflow
        from src.tools.prd_generator import PRDGenerator

        workflow = MVPWorkflow()
        research_result = await workflow.run(
            user_query=req.query,
            target_urls=req.urls,
            target_sites=_target_sites(req),
            auth_credentials=_auth_dict(req.auth_credentials),
            login_url=req.login_url,
            enable_search=req.enable_search,
            custom_selectors=req.custom_selectors,
            user_id=_principal(request)[0],
        )

        if research_result["status"] != "completed":
            return {
                "status": "failed",
                "stage": "competitive_analysis",
                "error": research_result.get("termination_reason"),
                "quality_score": research_result.get("quality_score")
            }

        generator = PRDGenerator()
        prd_content = await asyncio.to_thread(
            generator.generate_from_competitive_report,
            research_result["report_final"],
            req.query
        )

        return {
            "status": "success",
            "task_id": research_result.get("task_id"),
            "query": req.query,
            "competitors": [
                {"name": c.get("name"), "url": c.get("url"), "status": c.get("status")}
                for c in research_result.get("competitors", [])
            ],
            "competitive_report": research_result.get("report_final"),
            "quality_score": research_result.get("quality_score"),
            "prd": prd_content,
            "output_files": research_result.get("sync_results", {})
        }
    except Exception as e:
        raise _internal_error("PRD一体化流程", e)


class TaskRequest(BaseModel):
    """创建任务的请求体"""
    query: str = Field(..., min_length=1, max_length=1000)
    urls: List[str] = Field(default_factory=list, max_length=MAX_FETCH_URLS)
    target_sites: List[CompetitorSite] = Field(default_factory=list, max_length=MAX_FETCH_URLS)
    auth_credentials: Optional[AuthCredentials] = None
    login_url: Optional[str] = Field(default=None, min_length=1, max_length=2048)
    enable_search: bool = True
    custom_selectors: Optional[Dict[str, List[str]]] = None
    deliverables: Dict[str, bool] = Field(
        default_factory=lambda: {"report": True, "prd": True}
    )

    @field_validator("urls")
    @classmethod
    def validate_urls(cls, urls: List[str]) -> List[str]:
        return validate_fetch_urls(urls)

    @field_validator("login_url")
    @classmethod
    def validate_login_url(cls, login_url: Optional[str]) -> Optional[str]:
        return validate_fetch_url(login_url) if login_url else login_url

    @model_validator(mode="after")
    def validate_target_count(self):
        unique_urls = {site.url for site in self.target_sites}
        unique_urls.update(self.urls)
        if len(unique_urls) > MAX_FETCH_URLS:
            raise ValueError(f"目标URL数量不能超过 {MAX_FETCH_URLS} 个")
        return self

    @field_validator("custom_selectors")
    @classmethod
    def validate_custom_selectors(
        cls, selectors: Optional[Dict[str, List[str]]]
    ) -> Optional[Dict[str, List[str]]]:
        if selectors is None:
            return None
        allowed_keys = {"username", "password", "submit"}
        unknown_keys = set(selectors) - allowed_keys
        if unknown_keys:
            raise ValueError(f"不支持的选择器字段: {sorted(unknown_keys)}")
        for key, values in selectors.items():
            if not values or len(values) > 10:
                raise ValueError(f"{key} 选择器数量必须在 1-10 之间")
            for value in values:
                if not value or len(value) > 300:
                    raise ValueError(f"{key} 选择器长度必须在 1-300 字符之间")
        return selectors


_task_queue: List[asyncio.Task] = []

# 工作流**状态** → 任务终态。抽成显式映射而不是行内三元：预算超额（partial）与
# 正常完成（completed）必须能被一眼看出是两回事，行内表达式会把它们糊在一起。
#
# 注意这里的键是 TaskEvent.status，**不是** TaskEventType 的事件名——那是另一套词表：
# 工作流失败时发的事件名叫 `error`，状态却是 `failed`。两套词表混用过一次，
# 会把 `failed` 键悄悄换成 `error`，让所有失败任务查表落空。
_TASK_STATUS_BY_EVENT_STATUS = {
    "completed": TaskStatus.COMPLETED,
    "failed": TaskStatus.FAILED,
    "partial": TaskStatus.PARTIAL,
}


async def _run_research_task(task_id: str, req: TaskRequest, user_id: Optional[str] = None):
    """Background task that runs the research workflow and emits events."""
    from src.workflow.mvp_workflow import MVPWorkflow

    # Langfuse 链路接线：整个任务跑在一个 trace 下，LLM 调用各自成 span。
    # 两个入口都必须开——未启用时这两行是 no-op（_init 早退），零开销。
    set_task_context(task_id, user_id=user_id)
    start_trace("research.run", input={"query": req.query}, task_id=task_id)

    store = get_task_store()
    emitter = store.get_or_create_emitter(task_id)

    event_counter = 0

    async def on_event(event: TaskEvent):
        nonlocal event_counter
        event_counter += 1
        event.id = event_counter
        await store.add_event(task_id, event)
        if event.status == "running":
            await store.update(task_id, current_stage=event.stage, current_message=event.message, status=TaskStatus.RUNNING)
        elif event.status in ("completed", "failed", "partial"):
            # partial = 预算超额收束：报告已产出但内容不完整。它既不是成功也不是失败，
            # 单列一个终态才能让前端如实提示"这是被截断的结果"，而不是谎称完成。
            await store.update(
                task_id,
                current_stage=event.stage,
                current_message=event.message,
                status=_TASK_STATUS_BY_EVENT_STATUS[event.status],
            )

    emitter._on_event = on_event

    workflow = MVPWorkflow(event_emitter=emitter)

    target_sites_dict = _target_sites(req)
    auth_creds = _auth_dict(req.auth_credentials)

    result = await workflow.run(
        user_query=req.query,
        target_urls=req.urls,
        target_sites=target_sites_dict,
        auth_credentials=auth_creds,
        login_url=req.login_url,
        enable_search=req.enable_search,
        custom_selectors=req.custom_selectors,
        deliverables=req.deliverables,
        task_id=task_id,
        user_id=user_id,
    )

    research_metadata = {
        "crawl_results": {"competitors": result.get("competitors", [])},
        "quality_score": result.get("quality_score"),
        "quality_grade": result.get("quality_grade"),
        "missing_dimensions": result.get("missing_dimensions", []),
        # 提示注入检测命中数与成本报告：工作流产出的两个新指标，随终态一并落库
        "injection_findings_count": result.get("injection_findings_count", 0),
        "cost_report": result.get("cost_report"),
    }

    if result["status"] in ("completed", "partial"):
        # partial 与 completed 走同一条产出路径（报告照常落库），只是终态不同：
        # 预算超额收束出来的报告是不完整的，标成 completed 就是在骗用户。
        await store.update(
            task_id,
            status=_TASK_STATUS_BY_EVENT_STATUS[result["status"]],
            report_id=result.get("report_id"),
            **research_metadata,
        )
        if req.deliverables.get("report") and result.get("report_final"):
            await store.set_artifact(
                task_id, "report",
                TaskArtifact(artifact_type="report", content=result["report_final"], report_id=result.get("report_id"))
            )
        if req.deliverables.get("prd") and result.get("prd_content"):
            await store.set_artifact(
                task_id, "prd",
                TaskArtifact(artifact_type="prd", content=result.get("prd_content"))
            )
    else:
        await store.update(
            task_id,
            status=TaskStatus.FAILED,
            report_id=result.get("report_id"),
            **research_metadata,
        )

    # Always set report artifact if present (even partial/warning results)
    if result.get("report_final"):
        await store.set_artifact(
            task_id, "report",
            TaskArtifact(artifact_type="report", content=result["report_final"], report_id=result.get("report_id"))
        )

    # Audit log research completion/failure
    audit = get_audit_logger()
    if result["status"] in ("completed", "partial"):
        audit.log(AuditEvent.RESEARCH_COMPLETE, None, {
            "task_id": task_id,
            "quality_score": result.get("quality_score"),
            "quality_grade": result.get("quality_grade"),
            "competitors_count": len(result.get("competitors", [])),
            # partial 也记 COMPLETE 是因为报告确实产出了；超收束这一事实记在这里，
            # 否则审计日志里只剩"成功"，事后无从判断当时是不是被预算截断的。
            "termination_reason": result.get("termination_reason") if result["status"] == "partial" else None,
        })
    else:
        audit.log(AuditEvent.RESEARCH_FAIL, None, {
            "task_id": task_id,
            "termination_reason": result.get("termination_reason"),
            "quality_score": result.get("quality_score"),
        }, "warning")

    # 关闭 trace：必须放在所有落库之后，且无论成功失败都要走。未启用时是 no-op。
    end_trace({
        "status": result["status"],
        "termination_reason": result.get("termination_reason"),
        "quality_score": result.get("quality_score"),
    })
    # 清掉 contextvars，避免 trace/task 关联残留到复用同一上下文的后续动作。
    clear_task_context()


@limiter.limit(RATE_LIMIT_PER_TOKEN)
@app.post("/research/tasks", response_model=Dict[str, Any])
async def create_task(request: Request, req: TaskRequest):
    """创建异步调研任务，立即返回 task_id"""
    task_id = str(uuid4())
    store = get_task_store()

    user_id, _ = _principal(request)
    await store.create(task_id, req.query, req.deliverables, user_id=user_id)

    task_state = await store.get(task_id)
    created_event = TaskEvent(
        id=0,
        event=TaskEventType.TASK_CREATED.value,
        task_id=task_id,
        stage="created",
        status="created",
        message="任务已创建，正在排队执行",
    )
    await store.add_event(task_id, created_event)

    # Audit log research start
    get_audit_logger().log(AuditEvent.RESEARCH_START, user_id, {
        "task_id": task_id,
        "query": req.query[:200],
        "target_urls_count": len(req.urls),
        "target_sites_count": len(req.target_sites),
    })

    background = asyncio.create_task(_run_research_task(task_id, req, user_id))
    # 兜底收尾：工作流抛异常时 _run_research_task 的函数体会被跳过，
    # end_trace / clear_task_context 都执行不到，trace 会一直挂着不关。
    # 这里用 done callback 补上，并把异常显式取走记日志——否则 asyncio 会一直
    # 挂着一个 "Task exception was never retrieved" 的告警。
    def _finalize_trace(fut: "asyncio.Task") -> None:
        try:
            if not fut.cancelled():
                exc = fut.exception()
                if exc is not None:
                    logger.error("research task %s 异常终止", task_id, exc_info=exc)
                    end_trace({"status": "error", "error": str(exc)})
        finally:
            clear_task_context()

    background.add_done_callback(_finalize_trace)

    return {
        "task_id": task_id,
        "status": "pending",
        "created_at": task_state.created_at,
    }


def _require_task_access(task, request: Request) -> None:
    """任务访问权。无主任务（user_id 为 None）仅 admin 可读。"""
    user_id, is_admin = _principal(request)
    if is_admin:
        return
    if task.user_id and task.user_id == user_id:
        return
    # 与报告一致：无权时返 404 而非 403，避免泄露"该任务存在"
    raise HTTPException(status_code=404, detail="任务不存在")


@app.get("/research/tasks/{task_id}", response_model=Dict[str, Any])
async def get_task(task_id: str, request: Request):
    """获取任务状态和历史事件"""
    store = get_task_store()
    task = await store.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    _require_task_access(task, request)
    payload = task.to_dict()
    # 任务详情增强：Langfuse trace 跳转链接（未启用时为 None）、
    # 提示注入检测命中数、成本报告（后两者由 task_store 持有，缺省 0/None）
    payload["trace_url"] = trace_url(task_id)
    payload["injection_findings_count"] = getattr(task, "injection_findings_count", 0)
    payload["cost_report"] = getattr(task, "cost_report", None)
    return payload


@app.get("/research/tasks/{task_id}/events")
async def stream_task_events(task_id: str, request: Request):
    """SSE流，支持重连"""
    store = get_task_store()
    task = await store.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # SSE 同样必须校验归属，否则可订阅他人任务的实时事件流
    _require_task_access(task, request)

    q = store.subscribe_task(task_id)

    last_event_id = 0
    if request.headers.get("Last-Event-ID"):
        try:
            last_event_id = int(request.headers["Last-Event-ID"])
        except ValueError:
            pass

    async def event_stream():
        try:
            for event in task.events:
                if event.id > last_event_id:
                    yield f"id: {event.id}\nevent: {event.event}\ndata: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=SSE_KEEPALIVE_SECONDS)
                    yield f"id: {event.id}\nevent: {event.event}\ndata: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield f": keepalive\n\n"
        finally:
            # 客户端断开时释放订阅，否则队列会永久滞留在 emitter 上
            store.unsubscribe_task(task_id, q)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@limiter.limit(RATE_LIMIT_PER_TOKEN)
@app.get("/research/history", response_model=List[Dict[str, Any]])
def list_history(request: Request):
    items: List[Dict[str, Any]] = []
    seen: set[str] = set()
    user_id, is_admin = _principal(request)

    try:
        db = get_report_db()
        # 非 admin 只能看到自己的报告
        for r in db.list_reports(limit=100, user_id=None if is_admin else user_id):
            seen.add(r["id"])
            items.append({
                "id": r["id"],
                "title": r["user_query"][:120] if r["user_query"] else r["id"],
                "date": r["created_at"],
                "created_at": r["created_at"],
                "grade": r["quality_grade"],
                "score": r["quality_score"],
                "task_id": r["task_id"],
            })
    except Exception as exc:
        logger.warning("Failed to list reports from SQLite, falling back to files: %s", exc)

    # 文件兜底分支只给 admin：这类报告在库里没有归属记录，归属未知。
    # 非 admin 若在此看到，等于把别人的历史报告交出去。
    if is_admin:
        for path in _report_files():
            if path.stem not in seen:
                items.append(_report_payload(path, include_markdown=False))

    return sorted(items, key=lambda item: item.get("created_at", ""), reverse=True)[:100]


async def _task_meta_for_report(report_id: str) -> Dict[str, Any]:
    """按 report_id 反查任务元数据（成本报告 / 注入命中数 / trace 链接）。

    这三个字段此前只在"任务实时轮询"那一路的载荷里出现；从历史报告重新打开时
    载荷里没有它们，成本卡与 Trace 按钮整个消失——用户刷新一下页面，就再也看不到
    这次调研花了多少钱、点了跳转过去还会 404。

    这里按 report_id 线性扫任务列表把元数据找回来，且**只在单份报告读取时调用**。
    列表接口刻意不做 enrichment，否则 N 份报告就是 N 次全量扫描。任务量级涨到
    需要索引时（Redis store 尤其明显），应改为维护 report_id → task_id 的反查索引。
    """
    store = get_task_store()
    for task in await store.list_tasks():
        if task.report_id == report_id:
            return {
                "cost_report": getattr(task, "cost_report", None),
                "injection_findings_count": getattr(task, "injection_findings_count", 0),
                "trace_url": trace_url(task.task_id),
            }
    # 找不到归属任务（比如任务存储已过期、或报告来自文件兜底）：如实给空值。
    # 用 report_id 拼一个假的 trace 链接只会让用户点进一个必然 404 的页面。
    return {"cost_report": None, "injection_findings_count": 0, "trace_url": None}


@limiter.limit(RATE_LIMIT_PER_TOKEN)
@app.get("/research/history/{report_id}", response_model=Dict[str, Any])
async def get_history_report(request: Request, report_id: str):
    """读取单份历史报告。"""
    _require_report_access(request, report_id)
    payload = _load_report_payload(report_id)
    payload.update(await _task_meta_for_report(report_id))
    return payload


@app.get("/research/compare/{report_id1}/{report_id2}", response_model=Dict[str, Any])
def compare_reports(request: Request, report_id1: str, report_id2: str):
    """对比两个报告版本的差异"""
    _require_report_access(request, report_id1)
    _require_report_access(request, report_id2)

    db = get_report_db()
    report1 = db.get_report(report_id1)
    report2 = db.get_report(report_id2)

    if not report1 or not report2:
        raise HTTPException(status_code=404, detail="Report not found")

    from src.tools.report_comparator import compare_reports as compare
    content1 = report1.get("report_content", "")
    content2 = report2.get("report_content", "")

    diff = compare(content1, content2)
    diff["report1"] = {"id": report_id1, "created_at": report1["created_at"]}
    diff["report2"] = {"id": report_id2, "created_at": report2["created_at"]}
    return diff


@limiter.limit(RATE_LIMIT_PER_TOKEN)
@app.post("/research/export")
async def export_report(request: Request, req: ExportRequest):
    """将报告导出为 PDF/Word/HTML 格式"""
    if req.content_type == "prd":
        raise HTTPException(
            status_code=501,
            detail="PRD 尚未支持后端导出，请在报告页使用「复制当前」功能获取 PRD 内容"
        )

    _require_report_access(request, req.report_id)

    # Report content is stored on the file system (authoritative source)
    # Fall back to DB only if the file does not exist
    report_path = _report_path(req.report_id)
    if report_path.exists():
        content = report_path.read_text(encoding="utf-8")
    else:
        db = get_report_db()
        report = db.get_report(req.report_id)
        if not report:
            raise HTTPException(status_code=404, detail="报告不存在")
        content = report.get("report_content", "")
        if not content:
            raise HTTPException(status_code=404, detail="报告内容不存在")

    # Note: PRD is stored in the frontend task store cache and is not
    # persisted on the backend. For PRD export, use the frontend's
    # native markdown download (copy) functionality.

    # Determine suffix and mime type
    suffix_map = {"pdf": ".pdf", "word": ".docx", "html": ".html"}
    mime_map = {"pdf": "application/pdf", "word": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "html": "text/html"}
    suffix = suffix_map[req.format]
    mime = mime_map[req.format]

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = tmp.name

    if req.format == "pdf":
        from src.tools.export import export_to_pdf
        export_to_pdf(content, tmp_path)
    elif req.format == "word":
        from src.tools.export import export_to_word
        export_to_word(content, tmp_path)
    elif req.format == "html":
        from src.tools.export import export_to_html
        export_to_html(content, tmp_path)

    filename = f"report_{req.report_id}{suffix}"
    response = FileResponse(tmp_path, media_type=mime, filename=filename)

    # Schedule temp file cleanup after response is fully sent
    async def cleanup_later():
        await asyncio.sleep(2)
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    asyncio.create_task(cleanup_later())

    return response


@app.get("/research/versions/{report_id}", response_model=List[Dict[str, Any]])
def get_report_versions(request: Request, report_id: str):
    """获取报告的所有版本"""
    _require_report_access(request, report_id)
    db = get_report_db()
    return db.get_report_versions(report_id)


@limiter.limit(RATE_LIMIT_PER_TOKEN)
@app.post("/research/reports/{report_id}/feedback", response_model=Dict[str, Any])
def submit_report_feedback(request: Request, report_id: str, req: ReportFeedbackRequest):
    """提交报告反馈（M4 反馈闭环）：写入一条 up/down 评价并返回最新统计。"""
    _require_report_access(request, report_id)
    user_id, _ = _principal(request)
    db = get_report_db()
    db.save_report_feedback(report_id, user_id, req.rating, req.comment)
    return {"ok": True, "summary": db.report_feedback_summary(report_id)}


@app.get("/research/reports/{report_id}/feedback", response_model=Dict[str, Any])
def get_report_feedback(request: Request, report_id: str):
    """获取报告反馈统计。"""
    _require_report_access(request, report_id)
    return get_report_db().report_feedback_summary(report_id)


@app.delete("/research/history/{report_id}", response_model=Dict[str, Any])
def delete_history_report(request: Request, report_id: str):
    """删除本地历史报告。"""
    _require_report_access(request, report_id)

    path = _report_path(report_id)
    deleted_file = False
    if path.exists():
        path.unlink()
        deleted_file = True

    # 同时清理数据库记录：此前只删文件，DB 行仍在，报告还会继续出现在历史列表里
    db = get_report_db()
    db.delete_report(report_id)

    if not deleted_file and not db.get_report(report_id):
        raise HTTPException(status_code=404, detail="报告不存在")

    return {"status": "success", "deleted": report_id}


@app.get("/settings", response_model=Dict[str, Any])
def get_settings():
    """返回安全的运行时配置状态，不回传密钥。"""
    return _safe_settings_payload()


@app.get("/settings/all", response_model=Dict[str, Any])
def get_all_settings(request: Request):
    """获取所有运行时配置（通过配置管理器，支持热更新）"""
    cm = get_config_manager()
    return cm.get_public_config()


@app.post("/settings/{key}", response_model=Dict[str, Any])
def update_setting(request: Request, key: str, value: str):
    """更新单个配置项（仅允许热更新配置项）"""
    # 此前任何自注册用户都能改限流/爬虫/阈值，且不写审计。role 字段此前全代码无消费点。
    _require_admin(request)

    cm = get_config_manager()

    if not cm.is_hot_reloadable(key):
        raise HTTPException(status_code=403, detail=f"Configuration key '{key}' does not support hot reload. Requires server restart.")

    old_value = cm.get(key)

    # Type conversion based on the value
    try:
        if value.isdigit():
            new_value = int(value)
        elif value.replace(".", "", 1).isdigit():
            new_value = float(value)
        elif value.lower() in ("true", "false"):
            new_value = value.lower() == "true"
        else:
            new_value = value
    except ValueError:
        new_value = value

    cm.set(key, new_value)
    return {"key": key, "old_value": old_value, "new_value": new_value, "hot_reloadable": True}


@app.post("/settings/reload", response_model=Dict[str, Any])
def reload_settings(request: Request):
    """重新从环境变量加载所有配置"""
    cm = get_config_manager()
    cm.reload()
    return {"status": "ok", "message": "Configuration reloaded from environment"}


@app.get("/settings/hot-reloadable", response_model=Dict[str, Any])
def get_hot_reloadable_keys(request: Request):
    """获取支持热更新的配置项列表"""
    cm = get_config_manager()
    return {"hot_reloadable_keys": list(cm.HOT_RELOADABLE_KEYS)}


@app.get("/sync/status", response_model=Dict[str, Any])
def sync_status():
    """文档同步平台连接状态。"""
    return {
        "status": "success",
        "platforms": {
            "feishu": {"configured": bool(FEISHU_APP_ID and FEISHU_APP_SECRET)},
            "tencent": {"configured": bool(TENCENT_APP_ID and TENCENT_APP_SECRET), "experimental": True},
        },
    }


@app.get("/llm/status", response_model=Dict[str, Any])
def llm_status():
    """LLM 提供商状态和熔断器状态"""
    from src.llm import get_provider_status

    get_audit_logger().log(AuditEvent.LLM_CALL, None, {
        "endpoint": "llm.status",
        "provider": LLM_PROVIDER,
    })
    return {
        "status": "success",
        "providers": get_provider_status(),
        "current_provider": LLM_PROVIDER,
        "fallback_provider": os.getenv("LLM_FALLBACK_PROVIDER", ""),
    }


@app.post("/llm/circuit/reset", response_model=Dict[str, Any])
def llm_circuit_reset(provider: Optional[str] = None):
    """重置熔断器"""
    from src.llm.circuit_breaker import reset_all_circuit_breakers, get_circuit_breaker

    if provider:
        breaker = get_circuit_breaker(provider.lower())
        breaker.reset()
        return {"status": "success", "message": f"Circuit breaker for {provider} has been reset"}
    else:
        reset_all_circuit_breakers()
        return {"status": "success", "message": "All circuit breakers have been reset"}


@limiter.limit(RATE_LIMIT_PER_TOKEN)
@app.post("/sync/feishu", response_model=Dict[str, Any])
def sync_feishu(request: Request, req: SyncRequest):
    """同步报告到飞书文档。"""
    if not (FEISHU_APP_ID and FEISHU_APP_SECRET):
        raise HTTPException(status_code=400, detail="飞书文档未配置")
    content, title = _sync_content(req, request)
    from src.sync import doc_sync_manager

    result = doc_sync_manager.sync_to_feishu(content, title)
    if "error" in result:
        raise HTTPException(status_code=502, detail="飞书同步失败，请查看服务端日志")
    user_id = getattr(request.state, "user_id", None)
    get_audit_logger().log(AuditEvent.DOC_SYNC, user_id, {
        "platform": "feishu",
        "title": title[:100],
        "report_id": req.report_id,
    })
    return {"status": "success", "result": result}


@limiter.limit(RATE_LIMIT_PER_TOKEN)
@app.post("/sync/tencent", response_model=Dict[str, Any])
def sync_tencent(request: Request, req: SyncRequest):
    """同步报告到腾讯文档。"""
    if not (TENCENT_APP_ID and TENCENT_APP_SECRET):
        raise HTTPException(status_code=400, detail="腾讯文档未配置")
    content, title = _sync_content(req, request)
    from src.sync import doc_sync_manager

    result = doc_sync_manager.sync_to_tencent(content, title)
    if "error" in result:
        raise HTTPException(status_code=502, detail="腾讯文档同步失败，请查看服务端日志")
    user_id = getattr(request.state, "user_id", None)
    get_audit_logger().log(AuditEvent.DOC_SYNC, user_id, {
        "platform": "tencent",
        "title": title[:100],
        "report_id": req.report_id,
    })
    return {"status": "success", "result": result}


# ============ Authentication Endpoints ============

@app.post("/auth/register", response_model=Dict[str, Any])
def register(req: UserRegister):
    """Register a new user"""
    db = get_user_db()
    user_id = db.create_user(req.username, req.email, req.password)
    if not user_id:
        raise HTTPException(status_code=400, detail="Username or email already exists")
    created = db.get_user(user_id) or {}
    access_token = create_access_token({
        "sub": user_id,
        "username": req.username,
        "role": created.get("role", "user"),
    })
    get_audit_logger().log(AuditEvent.USER_REGISTER, user_id, {"username": req.username})
    return {"user_id": user_id, "access_token": access_token, "token_type": "bearer"}


@app.post("/auth/login", response_model=Dict[str, Any])
def login(req: UserLogin):
    """User login"""
    db = get_user_db()
    principal = db.authenticate(req.username, req.password)
    if not principal:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    access_token = create_access_token({
        "sub": principal["id"],
        "username": req.username,
        "role": principal.get("role", "user"),
    })
    get_audit_logger().log(AuditEvent.USER_LOGIN, user_id, {"username": req.username})
    return {"access_token": access_token, "token_type": "bearer"}


@app.get("/auth/me", response_model=UserResponse)
def get_me(request: Request):
    """Get current user info"""
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    db = get_user_db()
    user = db.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return UserResponse(**user)


@app.get("/health")
def health():
    """健康检查（存活探针）。

    注意与 `/health/ready` 的分工：这里只回答"进程还活着吗"，**绝不因为
    LLM 密钥缺失就改判 unhealthy**——CI 与 docker healthcheck 用的正是这个端点，
    而它们本来就不持有真实密钥。用它判失败等于让探针天天假红。

    配置就绪信息放在 `llm` 段里**如实呈现**，需要严格判定的人用 `/health/ready`。
    """
    from src.config.settings import (
        LLM_FALLBACK_PROVIDER,
        LLM_PROVIDER,
        SEARCH_PROVIDER,
        assess_llm_credential,
    )

    credential = assess_llm_credential()
    return {
        "status": "healthy",
        "service": "research-agent",
        "version": "1.0.0",
        "features": {
            "competitive_analysis": True,
            "prd_generation": True,
            "followup_optimization": True,
            "multi_role_review": True
        },
        "llm": {
            "provider": credential.get("provider"),
            # 只报判定结论与建议动作，绝不回显密钥本身
            "credential": credential.get("credential"),
            "credential_usable": credential.get("credential") in ("ok", "not_required"),
            "remedy": credential.get("message") or None,
            "fallback_provider": LLM_FALLBACK_PROVIDER or None,
        },
        "search_provider": SEARCH_PROVIDER,
    }


@app.get("/health/ready")
def health_ready():
    """就绪探针——回答"现在能不能真的干活"。

    与 `/health` 分开是因为两者语义不同：存活看进程，就绪看依赖。
    LLM 密钥不可用时返回 503，让编排系统能据此摘流量，而不是让用户点一次
    "开始调研"才发现 401。
    """
    from src.config.settings import LLM_PROVIDER, assess_llm_credential

    credential = assess_llm_credential()
    usable = credential.get("credential") in ("ok", "not_required")

    # MCP 是**可选的对外能力**：SDK 缺席只让 /mcp 回 503，主链路不受影响
    # （见 src/mcpserver/server.py 的 _unavailable_asgi_app）。所以就绪判定
    # 仍只看 LLM，这里额外如实报告 MCP 状态——不报告才是隐瞒。
    from src.mcpserver import mcp_sdk_available

    mcp_available = mcp_sdk_available()
    payload = {
        "status": "ready" if usable else "not_ready",
        "service": "research-agent",
        "llm": {
            "provider": credential.get("provider") or LLM_PROVIDER,
            "credential": credential.get("credential"),
            "remedy": credential.get("message") or None,
        },
        "mcp": {
            "available": mcp_available,
            "remedy": None if mcp_available else "uv add 'mcp>=2.3' 后重启服务",
        },
    }
    if not usable:
        return JSONResponse(status_code=503, content=payload)
    return payload


@app.get("/")
def index():
    """服务信息"""
    return {
        "name": "Research Agent MCP Server",
        "version": "1.0.0",
        "description": "基于AI的竞品调研自动化工具，可被其他Agent调用",
        "endpoints": [
            "POST /research/competitive - 竞品分析",
            "POST /research/followup - 追问优化",
            "POST /research/suggest-followup - 推荐追问方向",
            "POST /research/multi-role-review - 多角色审查",
            "POST /research/prd - 生成PRD文档",
            "POST /research/prd-from-query - PRD一体化（竞品分析+PRD生成）",
            "GET /research/history - 历史报告",
            "GET /settings - 运行配置状态",
            "GET /sync/status - 文档同步状态",
            "GET /health - 健康检查"
        ]
    }


def main():
    """Console script entrypoint."""
    uvicorn.run(app, host=APP_HOST, port=APP_PORT)




# ---------- MCP 子应用挂载（M5 接线）----------
# 必须放在全部路由定义之后；鉴权由上方 enforce_auth 中间件统一把关——
# /mcp 不在 PUBLIC_PATHS 白名单内，未认证请求在进入子应用前即被拦下。
from src.mcpserver import create_mcp_asgi_app

app.mount("/mcp", create_mcp_asgi_app())


if __name__ == "__main__":
    main()
