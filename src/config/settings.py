"""配置模块 - 加载环境变量和全局设置"""
import logging
import os
import re
from typing import Optional
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# 加载环境变量
load_dotenv()

# .env.example 遗留的占位符特征（大小写不敏感）。
# 命中任意一个就说明用户没改过 .env.example，这类值不得进入生产。
SECRET_PLACEHOLDER_PATTERN = re.compile(
    r"CHANGE_ME|YOUR_|REPLACE_WITH|X{5,}",
    re.IGNORECASE,
)

# 长度阈值：密钥类与口令类分开，避免用同一把尺子误伤短口令
SECRET_MIN_LENGTH = 32
MIN_PASSWORD_LENGTH = 12


def is_placeholder_secret(value: Optional[str]) -> bool:
    """值是否命中占位符特征。空值不算占位符，"是否设置"由调用方单独判定。"""
    if not value:
        return False
    return bool(SECRET_PLACEHOLDER_PATTERN.search(value.strip()))


def is_weak_secret(value: Optional[str], min_length: int = SECRET_MIN_LENGTH) -> bool:
    """值是否不可信：命中占位符，或长度低于阈值。"""
    if not value or not value.strip():
        return False
    return is_placeholder_secret(value) or len(value.strip()) < min_length


def describe_weak_secret(key: str, value: str, min_length: int = SECRET_MIN_LENGTH) -> str:
    """生成诊断信息。只描述问题，绝不回显密钥本身。"""
    if is_placeholder_secret(value):
        return f"{key} 仍是 .env.example 里的占位符"
    return f"{key} 长度不足 {min_length} 位（当前 {len(value.strip())} 位）"


def _get_bool(key: str, default: bool = False) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(key: str, default: int, minimum: int = 1) -> int:
    raw_value = os.getenv(key, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{key} must be >= {minimum}")
    return value


def _get_float(key: str, default: float, minimum: float = 0.0) -> float:
    raw_value = os.getenv(key, str(default))
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{key} must be a number") from exc
    if value < minimum:
        raise ValueError(f"{key} must be >= {minimum}")
    return value

# LLM提供商配置
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "minimax").lower()

# Minimax API配置
MINIMAX_API_KEY = os.getenv("MINIMAX_API_KEY")
MINIMAX_BASE_URL = os.getenv("MINIMAX_BASE_URL", "https://api.minimax.chat/v1")
MINIMAX_MODEL = os.getenv("MINIMAX_MODEL", "MiniMax-M2.5")

# DeepSeek API配置
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# 豆包 API配置
DOUBAO_API_KEY = os.getenv("DOUBAO_API_KEY")
DOUBAO_BASE_URL = os.getenv("DOUBAO_BASE_URL", "https://api.doubao.com")
DOUBAO_MODEL = os.getenv("DOUBAO_MODEL", "Doubao-3.5")

# GLM (智谱) API配置
GLM_API_KEY = os.getenv("GLM_API_KEY")
GLM_BASE_URL = os.getenv("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
GLM_MODEL = os.getenv("GLM_MODEL", "glm-4")

# OpenAI API配置
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

# 搜索API配置（三选一）
SERPAPI_KEY = os.getenv("SERPAPI_KEY")

# SearXNG 配置（推荐，免费自托管）
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8888")
SEARCH_PROVIDER = os.getenv("SEARCH_PROVIDER", "searxng").lower()
SEARCH_TIMEOUT = _get_int("SEARCH_TIMEOUT", 15)
SEARCH_MAX_RESULTS = _get_int("SEARCH_MAX_RESULTS", 10)

# 飞书文档配置（可选）
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET")

# 腾讯文档配置（可选）
TENCENT_APP_ID = os.getenv("TENCENT_CLIENT_ID") or os.getenv("TENCENT_APP_ID")
TENCENT_APP_SECRET = os.getenv("TENCENT_APP_SECRET")

# 运行配置
MAX_RETRIES = _get_int("MAX_RETRIES", 3)
TIMEOUT_SECONDS = _get_int("TIMEOUT_SECONDS", 60)
MAX_FETCH_URLS = _get_int("MAX_FETCH_URLS", 10)
ALLOW_PRIVATE_NETWORK_URLS = _get_bool("ALLOW_PRIVATE_NETWORK_URLS", False)

# LLM 超时与熔断配置
LLM_CONNECT_TIMEOUT = _get_int("LLM_CONNECT_TIMEOUT", 10)  # 连接超时（秒）
LLM_READ_TIMEOUT = _get_int("LLM_READ_TIMEOUT", 120)  # 读取超时（秒）
LLM_RETRY_BACKOFF_MAX = _get_int("LLM_RETRY_BACKOFF_MAX", 30)  # 最大退避时间（秒）
LLM_CIRCUIT_BREAKER_THRESHOLD = _get_int("LLM_CIRCUIT_BREAKER_THRESHOLD", 5)  # 熔断阈值（连续失败次数）
LLM_CIRCUIT_BREAKER_TIMEOUT = _get_int("LLM_CIRCUIT_BREAKER_TIMEOUT", 60)  # 熔断恢复超时（秒）
LLM_FALLBACK_PROVIDER = os.getenv("LLM_FALLBACK_PROVIDER", "")  # 备用 LLM 提供商

LLM_RETRY_DELAY = _get_float("LLM_RETRY_DELAY", 1.0)  # 重试初始延迟（秒）
LLM_RETRY_BACKOFF_FACTOR = _get_int("LLM_RETRY_BACKOFF_FACTOR", 2)  # 重试退避因子
LLM_MAX_CONTENT_CHARS = _get_int("LLM_MAX_CONTENT_CHARS", 8000)  # LLM 输入内容截断字符数

CRAWL_MAX_CONCURRENCY = _get_int("CRAWL_MAX_CONCURRENCY", 5)  # 最大并发
CRAWL_MIN_CONCURRENCY = _get_int("CRAWL_MIN_CONCURRENCY", 1)  # 最小并发
CRAWL_REQUEST_INTERVAL = _get_float("CRAWL_REQUEST_INTERVAL", 1.0)  # 请求间隔（秒）
CRAWL_TIMEOUT_PER_PAGE = _get_int("CRAWL_TIMEOUT_PER_PAGE", 30)  # 每页超时

MIN_COMPETITORS_FOR_COMPARISON = _get_int("MIN_COMPETITORS_FOR_COMPARISON", 2)  # 横向对比最少竞品数

SSE_KEEPALIVE_SECONDS = _get_int("SSE_KEEPALIVE_SECONDS", 30)  # SSE 心跳间隔（秒）

# 核查阈值配置（可通过环境变量动态调整）
VERIFICATION_PASS_THRESHOLD = _get_float("VERIFICATION_PASS_THRESHOLD", 7.0)
VERIFICATION_WARN_THRESHOLD = _get_float("VERIFICATION_WARN_THRESHOLD", 5.0)

# 服务配置
APP_ENV = os.getenv("APP_ENV", "development").lower()
AUDIT_LOG_ENABLED = _get_bool("AUDIT_LOG_ENABLED", True)
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = _get_int("APP_PORT", 8080)
API_MAX_BODY_BYTES = _get_int("API_MAX_BODY_BYTES", 1_048_576)
API_AUTH_TOKEN = os.getenv("API_AUTH_TOKEN")

# JWT Authentication
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY") or ""
if APP_ENV == "production" and not JWT_SECRET_KEY:
    raise ValueError("JWT_SECRET_KEY must be set in production")
# fail-closed：占位符/弱密钥与"完全没设"同等对待，直接拒绝启动
if APP_ENV == "production" and is_weak_secret(JWT_SECRET_KEY):
    raise ValueError(
        "JWT_SECRET_KEY is not a usable production secret: "
        + describe_weak_secret("JWT_SECRET_KEY", JWT_SECRET_KEY)
    )
if not JWT_SECRET_KEY:
    JWT_SECRET_KEY = "dev-secret-key-change-in-development-32chars"
JWT_EXPIRE_MINUTES = _get_int("JWT_EXPIRE_MINUTES", 1440)

# API速率限制配置
RATE_LIMIT_ENABLED = _get_bool("RATE_LIMIT_ENABLED", True)
RATE_LIMIT_PER_IP = os.getenv("RATE_LIMIT_PER_IP", "60/minute")
RATE_LIMIT_PER_TOKEN = os.getenv("RATE_LIMIT_PER_TOKEN", "120/minute")
ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("ALLOWED_HOSTS", "127.0.0.1,localhost,testserver").split(",")
    if host.strip()
]
# 生产环境只信回环/内部地址，几乎必然是"忘了配 ALLOWED_HOSTS"：
# 域名/外部 IP 访问会被 TrustedHostMiddleware 拒成 400，且这个报错对运维不可读。
# 不 fail（纯本机部署是合法形态），但警告要说得足够响。
if APP_ENV == "production" and set(ALLOWED_HOSTS) <= {
    "localhost", "127.0.0.1", "research-agent", "testserver",
}:
    logger.warning(
        "生产环境 ALLOWED_HOSTS 仅含回环/内部地址（%s）。若通过域名或外部 IP 访问，"
        "TrustedHostMiddleware 将返回 400；请在 .env 显式配置 ALLOWED_HOSTS。",
        ", ".join(ALLOWED_HOSTS),
    )

# 路径配置
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")

# 初始管理员账号（容器首次启动时自动创建）
INITIAL_ADMIN_USERNAME = os.getenv("INITIAL_ADMIN_USERNAME", "admin")
INITIAL_ADMIN_PASSWORD = os.getenv("INITIAL_ADMIN_PASSWORD", "")

# Config center
CONFIG_HOT_RELOAD_ENABLED = _get_bool("CONFIG_HOT_RELOAD_ENABLED", True)

# Redis 配置
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
TASK_STORE_BACKEND = os.getenv("TASK_STORE_BACKEND", "memory").lower()

# 确保输出目录存在
os.makedirs(OUTPUT_DIR, exist_ok=True)
