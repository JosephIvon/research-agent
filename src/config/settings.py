"""配置模块 - 加载环境变量和全局设置"""
import logging
import os
import re
from typing import Any, Dict, Optional
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


# ---------------------------------------------------------------------------
# LLM provider 配置体检
# ---------------------------------------------------------------------------
# provider -> (密钥环境变量名, 最小长度)。
# 只检查**被选中的那个** provider：其余没配是正常的（多 provider 部署本来就只用一个），
# 把没配的全判成错误只会制造噪声。
LLM_PROVIDER_KEYS = {
    "minimax": ("MINIMAX_API_KEY", 32),
    "deepseek": ("DEEPSEEK_API_KEY", 32),
    "doubao": ("DOUBAO_API_KEY", 32),
    "glm": ("GLM_API_KEY", 32),
    "openai": ("OPENAI_API_KEY", 32),
}

# 不需要密钥的 provider：离线脚本化替身（M1 的 ScriptedLLMClient）与测试用 mock。
# 加新 provider 时若既不在上面两张表里，会被 test_llm_config.py 判红——
# 这是刻意的：漏登记等于让"缺密钥"这类错误静默通过。
LLM_PROVIDERS_WITHOUT_KEY = {"fake", "scripted", "mock", "offline"}

# 体检结论
LLM_CREDENTIAL_OK = "ok"
LLM_CREDENTIAL_MISSING = "missing"
LLM_CREDENTIAL_PLACEHOLDER = "placeholder"
LLM_CREDENTIAL_TOO_SHORT = "too_short"
LLM_CREDENTIAL_NOT_REQUIRED = "not_required"
LLM_CREDENTIAL_UNKNOWN_PROVIDER = "unknown_provider"


def _llm_credential_value(env_key: str) -> str:
    """读取 provider 密钥的实际取值。

    **优先读模块常量**——`src/llm` 里的客户端用的是 `settings.MINIMAX_API_KEY` 这类
    模块常量，而不是每次 `os.getenv`。若体检走 os.getenv 而客户端走常量，
    两者一旦不一致（测试注入、运行期改 env），健康检查就会对真实使用的密钥说谎。
    回落到 os.getenv 只是为了让函数在常量尚未定义时也能安全工作。
    """
    value = globals().get(env_key)
    if value:
        return str(value)
    return os.getenv(env_key) or ""


def assess_llm_credential(provider: Optional[str] = None) -> Dict[str, Any]:
    """检查所选 LLM provider 的密钥配置是否可用。

    **只做配置层检查，绝不发起网络请求**——它会被 `/health` 调用，
    而健康检查不该有副作用、烧配额或在 provider 故障时把探针一起拖死。

    返回的 dict 不含密钥值，只含判定结论，供日志与 `/health` 使用。
    """
    selected = (provider or LLM_PROVIDER or "").lower()
    if not selected:
        return {"provider": "", "credential": LLM_CREDENTIAL_MISSING, "message": "未指定 LLM_PROVIDER"}
    if selected in LLM_PROVIDERS_WITHOUT_KEY:
        return {
            "provider": selected,
            "credential": LLM_CREDENTIAL_NOT_REQUIRED,
            "message": "该 provider 使用脚本化响应，无需密钥",
        }
    spec = LLM_PROVIDER_KEYS.get(selected)
    if spec is None:
        return {
            "provider": selected,
            "credential": LLM_CREDENTIAL_UNKNOWN_PROVIDER,
            "message": f"未登记的 provider：{selected}",
        }

    env_key, min_length = spec
    value = _llm_credential_value(env_key)
    if not value.strip():
        return {
            "provider": selected,
            "env_key": env_key,
            "credential": LLM_CREDENTIAL_MISSING,
            "message": f"{env_key} 未设置",
        }
    if is_placeholder_secret(value):
        return {
            "provider": selected,
            "env_key": env_key,
            "credential": LLM_CREDENTIAL_PLACEHOLDER,
            "message": describe_weak_secret(env_key, value, min_length),
        }
    if len(value.strip()) < min_length:
        return {
            "provider": selected,
            "env_key": env_key,
            "credential": LLM_CREDENTIAL_TOO_SHORT,
            "message": describe_weak_secret(env_key, value, min_length),
        }
    return {
        "provider": selected,
        "env_key": env_key,
        "credential": LLM_CREDENTIAL_OK,
        "message": "",
    }


def llm_credential_usable(provider: Optional[str] = None) -> bool:
    """所选 provider 的密钥是否真的能用。"""
    return assess_llm_credential(provider)["credential"] in (
        LLM_CREDENTIAL_OK,
        LLM_CREDENTIAL_NOT_REQUIRED,
    )


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

# 抽取质量门禁（可通过环境变量动态调整）
#
# 原先这里是 VERIFICATION_PASS/WARN_THRESHOLD，对 LLM 自报 confidence 求平均后比较。
# 该口径已废弃：confidence 是模型给自己的评分（缺省时由 extract_key_info 硬编码 0.8），
# 实测对"全部 confidence=0.5"的脏数据依然放行，对"全部 0.8"的空壳数据给 8.0 分——
# 它不是质量门禁，只是恒真的存在性检查。换成两项可确定性验证的属性：
#   - 来源覆盖率：抽取出的数据里有多少条带得上可追溯的 source_url
#   - 证据条数：够不够写出一份有依据的报告
EXTRACTION_SOURCE_COVERAGE_MIN = _get_float("EXTRACTION_SOURCE_COVERAGE_MIN", 0.8)
EXTRACTION_MIN_EVIDENCE_ITEMS = _get_int("EXTRACTION_MIN_EVIDENCE_ITEMS", 3)

# 缺口定向补抓（gap refetch）：引用校验仍不过时，判定层允许的**单轮**总页数额度。
# 这是补抓的成本刹车——只做一轮、每站页数受 per_candidate_pages 约束，
# 不递归、不循环。legacy 整段没有 token 预算，补抓的护栏就是这个小额度。
GAP_REFETCH_MAX_PAGES = _get_int("GAP_REFETCH_MAX_PAGES", 3)

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

# ---------------------------------------------------------------------------
# LLM 密钥可用性体检（放在 APP_ENV 定义之后，才能区分生产与非生产）
# ---------------------------------------------------------------------------
# 为什么必须在启动期拦：实测 `MINIMAX_API_KEY=YOUR_KEY_HERE` 时客户端**照常构造成功**，
# 只在真正发请求时才 401——配置错误此前要等用户点"开始调研"才暴露。
# 非生产只警告：开发与测试经常没有真实密钥，硬失败会挡住离线流程。
_llm_credential = assess_llm_credential()
if APP_ENV == "production":
    if not llm_credential_usable():
        raise ValueError(
            f"LLM provider '{LLM_PROVIDER}' is not usable in production: "
            f"{_llm_credential['message']}"
        )
elif not llm_credential_usable():
    logger.warning(
        "LLM provider '%s' 的密钥不可用：%s（仅生产环境会拒绝启动）",
        LLM_PROVIDER,
        _llm_credential["message"],
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
