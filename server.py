"""Research Agent MCP Server - 让项目可被其他Agent调用"""
from datetime import datetime
from pathlib import Path
import hmac
import logging
import re
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware
import uvicorn

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
    LLM_PROVIDER,
    MAX_RETRIES,
    MAX_FETCH_URLS,
    MINIMAX_API_KEY,
    OPENAI_API_KEY,
    OUTPUT_DIR,
    SEARCH_MAX_RESULTS,
    SEARCH_PROVIDER,
    SEARCH_TIMEOUT,
    SEARXNG_URL,
    TENCENT_APP_ID,
    TENCENT_APP_SECRET,
    TIMEOUT_SECONDS,
)
from src.security.url_validation import validate_fetch_url, validate_fetch_urls

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

IS_PRODUCTION = APP_ENV == "production"
REPORT_ID_RE = re.compile(r"^research_report_\d{8}_\d{6}$")
app = FastAPI(
    title="Research Agent MCP Server",
    version="1.0.0",
    docs_url=None if IS_PRODUCTION else "/docs",
    redoc_url=None if IS_PRODUCTION else "/redoc",
    openapi_url=None if IS_PRODUCTION else "/openapi.json",
)

if ALLOWED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)


PUBLIC_PATHS = {"/", "/health", "/settings", "/sync/status"}
if not IS_PRODUCTION:
    PUBLIC_PATHS.update({"/docs", "/redoc", "/openapi.json"})


@app.middleware("http")
async def enforce_optional_bearer_auth(request: Request, call_next):
    if API_AUTH_TOKEN and request.url.path not in PUBLIC_PATHS:
        expected = f"Bearer {API_AUTH_TOKEN}"
        actual = request.headers.get("Authorization", "")
        if not hmac.compare_digest(expected, actual):
            return JSONResponse(status_code=401, content={"detail": "认证失败"})
    return await call_next(request)


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
        "grade": "B",
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


def _sync_content(req: SyncRequest) -> tuple[str, str]:
    content = req.report_content or req.content
    title = req.title or "智能调研报告"
    if content:
        return content, title
    report = _load_report_payload(req.report_id) if req.report_id else _latest_report_payload()
    return report["markdown"], req.title or report["title"]


@app.post("/research/competitive", response_model=Dict[str, Any])
async def competitive_research(req: ResearchRequest):
    """竞品分析接口"""
    try:
        from src.workflow.mvp_workflow import MVPWorkflow

        workflow = MVPWorkflow()
        result = await workflow.run(
            user_query=req.query,
            target_urls=req.urls,
            target_sites=_target_sites(req),
            auth_credentials=_auth_dict(req.auth_credentials),
            login_url=req.login_url,
            enable_search=req.enable_search,
            custom_selectors=req.custom_selectors
        )

        return {
            "status": "success" if result["status"] == "completed" else "failed",
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


@app.post("/research/followup", response_model=Dict[str, Any])
def followup(req: FollowUpRequest):
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


@app.post("/research/suggest-followup", response_model=Dict[str, Any])
def suggest_followup(req: FollowUpRequest):
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


@app.post("/research/multi-role-review", response_model=Dict[str, Any])
def review(req: ReviewRequest):
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


@app.post("/research/prd", response_model=Dict[str, Any])
def generate_prd(req: PRDRequest):
    """从竞品分析生成PRD文档"""
    try:
        from src.tools.prd_generator import PRDGenerator

        generator = PRDGenerator()
        prd_content = generator.generate_from_competitive_report(req.report_content, req.query)

        return {"status": "success", "prd": prd_content}
    except Exception as e:
        raise _internal_error("PRD生成", e)


@app.post("/research/prd-from-query", response_model=Dict[str, Any])
async def prd_from_query(req: ResearchRequest):
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
            custom_selectors=req.custom_selectors
        )

        if research_result["status"] != "completed":
            return {
                "status": "failed",
                "stage": "competitive_analysis",
                "error": research_result.get("termination_reason"),
                "quality_score": research_result.get("quality_score")
            }

        generator = PRDGenerator()
        prd_content = generator.generate_from_competitive_report(
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


@app.get("/research/history", response_model=List[Dict[str, Any]])
def list_history():
    """列出本地历史报告。"""
    return [_report_payload(path) for path in _report_files()[:100]]


@app.get("/research/history/{report_id}", response_model=Dict[str, Any])
def get_history_report(report_id: str):
    """读取单份历史报告。"""
    return _load_report_payload(report_id)


@app.delete("/research/history/{report_id}", response_model=Dict[str, Any])
def delete_history_report(report_id: str):
    """删除本地历史报告。"""
    path = _report_path(report_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="报告不存在")
    path.unlink()
    return {"status": "success", "deleted": report_id}


@app.get("/settings", response_model=Dict[str, Any])
def get_settings():
    """返回安全的运行时配置状态，不回传密钥。"""
    return _safe_settings_payload()


@app.get("/sync/status", response_model=Dict[str, Any])
def sync_status():
    """文档同步平台连接状态。"""
    return {
        "status": "success",
        "platforms": {
            "feishu": {"configured": bool(FEISHU_APP_ID and FEISHU_APP_SECRET)},
            "tencent": {"configured": bool(TENCENT_APP_ID and TENCENT_APP_SECRET)},
        },
    }


@app.post("/sync/feishu", response_model=Dict[str, Any])
def sync_feishu(req: SyncRequest):
    """同步报告到飞书文档。"""
    if not (FEISHU_APP_ID and FEISHU_APP_SECRET):
        raise HTTPException(status_code=400, detail="飞书文档未配置")
    content, title = _sync_content(req)
    from src.sync import doc_sync_manager

    result = doc_sync_manager.sync_to_feishu(content, title)
    if "error" in result:
        raise HTTPException(status_code=502, detail="飞书同步失败，请查看服务端日志")
    return {"status": "success", "result": result}


@app.post("/sync/tencent", response_model=Dict[str, Any])
def sync_tencent(req: SyncRequest):
    """同步报告到腾讯文档。"""
    if not (TENCENT_APP_ID and TENCENT_APP_SECRET):
        raise HTTPException(status_code=400, detail="腾讯文档未配置")
    content, title = _sync_content(req)
    from src.sync import doc_sync_manager

    result = doc_sync_manager.sync_to_tencent(content, title)
    if "error" in result:
        raise HTTPException(status_code=502, detail="腾讯文档同步失败，请查看服务端日志")
    return {"status": "success", "result": result}


@app.get("/health")
def health():
    """健康检查"""
    return {
        "status": "healthy",
        "service": "research-agent",
        "version": "1.0.0",
        "features": {
            "competitive_analysis": True,
            "prd_generation": True,
            "followup_optimization": True,
            "multi_role_review": True
        }
    }


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


if __name__ == "__main__":
    main()
