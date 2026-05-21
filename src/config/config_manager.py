"""运行时配置管理器 - 支持热更新"""
import os
import json
import threading
from typing import Any, Dict, Optional, Callable
from datetime import datetime


class ConfigManager:
    """
    配置管理器 - 支持运行时更新和变更回调

    使用方式:
    - get(key) - 读取配置
    - set(key, value) - 更新配置（触发回调）
    - watch(key, callback) - 监听配置变更
    """

    # 敏感配置项 - 不会通过API暴露
    SENSITIVE_KEYS = {
        "API_KEY", "SECRET", "PASSWORD", "TOKEN", "PRIVATE_KEY",
        "MINIMAX_API_KEY", "DEEPSEEK_API_KEY", "DOUBAO_API_KEY",
        "GLM_API_KEY", "OPENAI_API_KEY", "FEISHU_APP_SECRET",
        "TENCENT_APP_SECRET", "SERPAPI_KEY",
    }

    # 可热更新的配置项（运行时可通过API修改）
    HOT_RELOADABLE_KEYS = {
        "LLM_READ_TIMEOUT",
        "LLM_CIRCUIT_BREAKER_THRESHOLD",
        "SEARCH_MAX_RESULTS",
        "RATE_LIMIT_PER_IP",
        "RATE_LIMIT_PER_TOKEN",
        "VERIFICATION_PASS_THRESHOLD",
        "VERIFICATION_WARN_THRESHOLD",
        "SEARCH_TIMEOUT",
        "CRAWL_MAX_CONCURRENCY",
        "CRAWL_MIN_CONCURRENCY",
        "CRAWL_REQUEST_INTERVAL",
    }

    def __init__(self):
        self._config: Dict[str, Any] = {}
        self._lock = threading.RLock()
        self._watchers: Dict[str, list[Callable]] = {}
        self._load_from_env()

    def _load_from_env(self):
        """从环境变量加载所有已知配置"""
        # LLM settings
        self._config["LLM_PROVIDER"] = os.getenv("LLM_PROVIDER", "minimax")
        self._config["LLM_FALLBACK_PROVIDER"] = os.getenv("LLM_FALLBACK_PROVIDER", "")
        self._config["LLM_READ_TIMEOUT"] = int(os.getenv("LLM_READ_TIMEOUT", "120"))
        self._config["LLM_CIRCUIT_BREAKER_THRESHOLD"] = int(os.getenv("LLM_CIRCUIT_BREAKER_THRESHOLD", "5"))

        # Search settings
        self._config["SEARCH_PROVIDER"] = os.getenv("SEARCH_PROVIDER", "searxng")
        self._config["SEARCH_MAX_RESULTS"] = int(os.getenv("SEARCH_MAX_RESULTS", "10"))
        self._config["SEARCH_TIMEOUT"] = int(os.getenv("SEARCH_TIMEOUT", "15"))

        # Rate limit settings
        self._config["RATE_LIMIT_PER_IP"] = os.getenv("RATE_LIMIT_PER_IP", "60/minute")
        self._config["RATE_LIMIT_PER_TOKEN"] = os.getenv("RATE_LIMIT_PER_TOKEN", "120/minute")

        # Threshold settings
        self._config["VERIFICATION_PASS_THRESHOLD"] = float(os.getenv("VERIFICATION_PASS_THRESHOLD", "7.0"))
        self._config["VERIFICATION_WARN_THRESHOLD"] = float(os.getenv("VERIFICATION_WARN_THRESHOLD", "5.0"))

        # Sync settings
        self._config["FEISHU_SYNC_ENABLED"] = bool(os.getenv("FEISHU_APP_ID") and os.getenv("FEISHU_APP_SECRET"))
        self._config["TENCENT_SYNC_ENABLED"] = bool(os.getenv("TENCENT_CLIENT_ID") and os.getenv("TENCENT_APP_SECRET"))

        # Crawl settings
        self._config["CRAWL_MAX_CONCURRENCY"] = int(os.getenv("CRAWL_MAX_CONCURRENCY", "5"))
        self._config["CRAWL_MIN_CONCURRENCY"] = int(os.getenv("CRAWL_MIN_CONCURRENCY", "1"))
        self._config["CRAWL_REQUEST_INTERVAL"] = float(os.getenv("CRAWL_REQUEST_INTERVAL", "1.0"))

        # App settings
        self._config["APP_ENV"] = os.getenv("APP_ENV", "development")
        self._config["APP_HOST"] = os.getenv("APP_HOST", "127.0.0.1")
        self._config["APP_PORT"] = int(os.getenv("APP_PORT", "8080"))

        # Feature flags
        self._config["CONFIG_HOT_RELOAD_ENABLED"] = True

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._config.get(key, default)

    def set(self, key: str, value: Any) -> bool:
        with self._lock:
            old_value = self._config.get(key)
            if old_value == value:
                return False  # No change

            self._config[key] = value

            # Trigger watchers
            if key in self._watchers:
                for callback in self._watchers[key]:
                    try:
                        callback(key, old_value, value)
                    except Exception:
                        pass  # Don't let watcher errors break the config update

            return True

    def watch(self, key: str, callback: Callable[[str, Any, Any], None]):
        """注册配置变更监听器"""
        with self._lock:
            if key not in self._watchers:
                self._watchers[key] = []
            self._watchers[key].append(callback)

    def get_all(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._config)

    def get_public_config(self) -> Dict[str, Any]:
        """获取公开配置（排除敏感项）"""
        with self._lock:
            result = {}
            for key, value in self._config.items():
                # Skip sensitive keys
                if any(sensitive in key.upper() for sensitive in self.SENSITIVE_KEYS):
                    result[key] = "[REDACTED]"
                else:
                    result[key] = value
            return result

    def is_hot_reloadable(self, key: str) -> bool:
        """检查配置项是否支持热更新"""
        return key in self.HOT_RELOADABLE_KEYS

    def reload(self):
        """重新从环境变量加载"""
        with self._lock:
            self._load_from_env()


_config_manager: Optional[ConfigManager] = None


def get_config_manager() -> ConfigManager:
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager()
    return _config_manager