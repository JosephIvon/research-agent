"""本机管理员凭据存储（文档同步平台 App 凭据）。

背景：飞书 / 腾讯文档的 App 凭据此前只能来自**进程启动时的环境变量**——
界面上既没有输入框，也没有写入通路。于是同步页的「连接」按钮把人指向一个
**无法完成配置**的页面：同步功能对任何人都是不可用的。

为什么单独存文件、而不是放进 `research_agent.db`：
- 那个库是**租户共享的业务库**，凭据进去就跟着备份、迁移、导出一路走；
- 仓库里没有任何加解密实现，明文落库就是明文存储。
独立文件 + 仅本机 + 仅管理员可写，是当前风险最小的落点。
每用户凭据隔离留待二期——那需要先有密钥加密方案。

落点：`DOC_SYNC_CREDENTIALS_FILE` 环境变量优先，否则 `<仓库根>/.local/`。
该目录已在 .gitignore 中，密钥不会进版本库。
"""
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# 平台 → (app_id 环境变量候选, app_secret 环境变量候选)
_ENV_KEYS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "feishu": (("FEISHU_APP_ID",), ("FEISHU_APP_SECRET",)),
    "tencent": (("TENCENT_APP_ID", "TENCENT_CLIENT_ID"), ("TENCENT_APP_SECRET",)),
}

PLATFORMS = tuple(_ENV_KEYS.keys())

_REPO_ROOT = Path(__file__).resolve().parents[2]


def credentials_path() -> Path:
    """凭据文件路径。环境变量优先，其次仓库根下的 .local/。"""
    override = os.getenv("DOC_SYNC_CREDENTIALS_FILE")
    if override:
        return Path(override)
    return _REPO_ROOT / ".local" / "doc_sync_credentials.json"


def _read_env(platform: str) -> Tuple[Optional[str], Optional[str]]:
    id_keys, secret_keys = _ENV_KEYS.get(platform, ((), ()))
    app_id = next((os.environ[k] for k in id_keys if os.environ.get(k)), None)
    app_secret = next((os.environ[k] for k in secret_keys if os.environ.get(k)), None)
    return app_id, app_secret


def load_credentials() -> Dict[str, Dict[str, str]]:
    """读全部已存凭据。文件缺失或损坏都返回空——不能让一个坏文件挡住同步。"""
    path = credentials_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: {"app_id": str(v.get("app_id") or ""), "app_secret": str(v.get("app_secret") or "")}
        for k, v in data.items()
        if isinstance(v, dict)
    }


def save_credentials(platform: str, app_id: str, app_secret: str) -> Dict[str, str]:
    """写入一个平台的凭据。返回落盘后的记录（不含路径之外的敏感信息）。"""
    if platform not in _ENV_KEYS:
        raise ValueError(f"不支持的平台：{platform}")
    data = load_credentials()
    data[platform] = {"app_id": app_id, "app_secret": app_secret}
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows 上没有 POSIX 权限位，忽略即可
    return data[platform]


def resolve_credentials(platform: str) -> Tuple[Optional[str], Optional[str]]:
    """**调用时**解析凭据：先看已存的，再回落环境变量。

    调用时现读是刻意的——管理员在界面上改完立刻生效，不必重启进程。
    此前 `FeishuSync` 在构造时读一次 env、又被进程级单例缓存住，运行期改环境
    变量是不生效的。
    """
    stored = load_credentials().get(platform) or {}
    app_id = stored.get("app_id") or None
    app_secret = stored.get("app_secret") or None
    if app_id and app_secret:
        return app_id, app_secret
    env_id, env_secret = _read_env(platform)
    return (
        app_id or env_id,
        app_secret or env_secret,
    )


def _mask(value: Optional[str]) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:4]}…{value[-2:]}"


def credential_status(platform: str) -> Dict[str, Any]:
    """给界面看的状态：**只回显是否已配置与掩码后的 app_id**，secret 不出服务端。"""
    app_id, app_secret = resolve_credentials(platform)
    stored = platform in load_credentials()
    return {
        "platform": platform,
        "configured": bool(app_id and app_secret),
        "app_id_masked": _mask(app_id),
        "source": "stored" if stored else ("env" if app_id and app_secret else "none"),
    }
