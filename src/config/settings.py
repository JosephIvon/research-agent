"""配置模块 - 加载环境变量和全局设置"""
import os
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

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

# 路径配置
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")

# Config center
CONFIG_HOT_RELOAD_ENABLED = _get_bool("CONFIG_HOT_RELOAD_ENABLED", True)

# Redis 配置
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
TASK_STORE_BACKEND = os.getenv("TASK_STORE_BACKEND", "memory").lower()

# 确保输出目录存在
os.makedirs(OUTPUT_DIR, exist_ok=True)
