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

# 核查阈值配置（可通过环境变量动态调整）
VERIFICATION_PASS_THRESHOLD = _get_float("VERIFICATION_PASS_THRESHOLD", 7.0)
VERIFICATION_WARN_THRESHOLD = _get_float("VERIFICATION_WARN_THRESHOLD", 5.0)

# 服务配置
APP_ENV = os.getenv("APP_ENV", "development").lower()
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = _get_int("APP_PORT", 8080)
API_MAX_BODY_BYTES = _get_int("API_MAX_BODY_BYTES", 1_048_576)
API_AUTH_TOKEN = os.getenv("API_AUTH_TOKEN")
ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("ALLOWED_HOSTS", "127.0.0.1,localhost,testserver").split(",")
    if host.strip()
]

# 路径配置
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")

# 确保输出目录存在
os.makedirs(OUTPUT_DIR, exist_ok=True)
