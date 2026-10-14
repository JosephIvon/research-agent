#!/bin/bash
set -euo pipefail

echo "=== Research Agent 部署脚本 ==="

# 颜色定义
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# 检查Docker
if ! command -v docker &> /dev/null; then
    echo -e "${YELLOW}Docker未安装，请先手动安装Docker后重试。${NC}"
    exit 1
fi

# 检查Docker Compose
if ! docker compose version &> /dev/null; then
    echo -e "${YELLOW}Docker Compose不可用，请先安装Docker Compose插件后重试。${NC}"
    exit 1
fi

# 可选拉取最新代码（默认不自动改动工作区）
if [ "${DEPLOY_PULL:-0}" = "1" ] && [ -d ".git" ]; then
    echo -e "${GREEN}拉取最新代码...${NC}"
    git pull --ff-only origin main
fi

if [ ! -f ".env" ]; then
    echo -e "${YELLOW}缺少 .env，请先 cp .env.example .env 并填入真实配置。${NC}"
    exit 1
fi

# ===========================================
# 密钥与口令校验（表驱动）
# ===========================================
# 占位符特征：CHANGE_ME / YOUR_ / replace_with / xxxxx（大小写不敏感）。
# 必须与 src/config/settings.py 的 SECRET_PLACEHOLDER_PATTERN 保持一致，
# 否则会出现"部署脚本放行、应用启动报错"或反过来的错位。
PLACEHOLDER_RE='CHANGE_ME|YOUR_|REPLACE_WITH|X{5,}'

# 读取 .env 中某个变量的值：跳过注释行，剥掉行尾注释与引号
read_env_value() {
    local key="$1" line
    line="$(grep -E "^[[:space:]]*${key}=" .env | tail -n 1 || true)"
    line="${line#*=}"
    line="${line%%[[:space:]]#*}"
    # 去掉首尾空白
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    line="${line%\"}"
    line="${line#\"}"
    line="${line%\'}"
    line="${line#\'}"
    printf '%s' "$line"
}

# 校验单个变量：未设置 / 命中占位符 / 长度不足，任一成立即 exit 1
check_secret() {
    local var_name="$1" min_len="$2" hint="$3" reason="" value
    value="$(read_env_value "$var_name")"
    if [ -z "$value" ]; then
        echo -e "${YELLOW}.env 中的 ${var_name} 未设置或为空，拒绝部署。${NC}"
        echo -e "  修复方式：${hint}"
        exit 1
    fi
    if printf '%s' "$value" | grep -Eiq "$PLACEHOLDER_RE"; then
        reason="仍是占位符"
    elif [ "${#value}" -lt "$min_len" ]; then
        reason="长度不足 ${min_len} 位（当前 ${#value} 位）"
    fi
    if [ -n "$reason" ]; then
        echo -e "${YELLOW}.env 中的 ${var_name} ${reason}，拒绝部署。${NC}"
        echo -e "  修复方式：${hint}"
        exit 1
    fi
}

# 当前启用的 LLM 提供商对应哪个 API key 变量（minimax -> MINIMAX_API_KEY）
LLM_PROVIDER_NAME="$(read_env_value LLM_PROVIDER)"
LLM_PROVIDER_NAME="${LLM_PROVIDER_NAME:-minimax}"
LLM_API_KEY_VAR="$(printf '%s' "$LLM_PROVIDER_NAME" | tr '[:lower:]' '[:upper:]')_API_KEY"

# ---- 必检：JWT 密钥（任何部署形态都需要）----
check_secret "JWT_SECRET_KEY" 32 "openssl rand -hex 32"

# ---- 条件必检：初始管理员（留空 = 刻意不建管理员，合法形态）----
ADMIN_PW="$(read_env_value INITIAL_ADMIN_PASSWORD)"
if [ -n "$ADMIN_PW" ]; then
    check_secret "INITIAL_ADMIN_PASSWORD" 12 "openssl rand -base64 18，或留空以跳过初始管理员创建"
else
    echo -e "${GREEN}INITIAL_ADMIN_PASSWORD 为空：将不创建初始管理员。${NC}"
fi

# ---- 条件必检：SearXNG 密钥（仅作为搜索后端时才是安全必检项）----
SEARCH_PROVIDER_NAME="$(read_env_value SEARCH_PROVIDER)"
SEARCH_PROVIDER_NAME="${SEARCH_PROVIDER_NAME:-searxng}"
if [ "$SEARCH_PROVIDER_NAME" = "searxng" ]; then
    check_secret "SEARXNG_SECRET" 32 "openssl rand -hex 32"
elif [ -z "$(read_env_value SEARXNG_SECRET)" ]; then
    # 用 bing/serpapi 等外部搜索时不需要真实 SearXNG 密钥，但
    # docker-compose.yml 的 ${SEARXNG_SECRET:?} 插值仍要求非空值；
    # 导出随机占位值即可（shell 环境优先级高于 .env 文件）。
    export SEARXNG_SECRET="$(openssl rand -hex 32)"
    echo -e "${GREEN}SEARCH_PROVIDER=${SEARCH_PROVIDER_NAME}：SEARXNG_SECRET 仅为 compose 插值生成随机占位值。${NC}"
fi

# ---- 必检：当前 LLM 提供商的 API Key ----
check_secret "${LLM_API_KEY_VAR}" 32 "到 ${LLM_API_KEY_VAR%%_API_KEY} 提供商控制台创建真实 API Key，填入 .env 的 ${LLM_API_KEY_VAR}"

# ---- 必检：ALLOWED_HOSTS 必须显式声明 ----
# compose 的默认值只含 localhost；未配置时用域名访问会被
# TrustedHostMiddleware 拒成 400，且报错对运维不可读。强制显式选择。
ALLOWED_HOSTS_VALUE="$(read_env_value ALLOWED_HOSTS)"
if [ -z "$ALLOWED_HOSTS_VALUE" ]; then
    echo -e "${YELLOW}.env 未设置 ALLOWED_HOSTS：域名/外部 IP 访问将得到 400。请显式配置（纯本机部署可写 localhost,127.0.0.1,research-agent）。${NC}"
    exit 1
fi
if printf '%s' "$ALLOWED_HOSTS_VALUE" | grep -Eiq 'example\.(com|org|net)|your[-_.]?domain'; then
    echo -e "${YELLOW}.env 的 ALLOWED_HOSTS 仍是示例域名（example.com 等），拒绝部署。${NC}"
    exit 1
fi

# 构建并启动后端、前端、SearXNG
echo -e "${GREEN}构建并启动 Docker Compose 服务...${NC}"
docker compose build
docker compose up -d

# 检查健康状态
echo -e "${GREEN}检查服务健康状态...${NC}"
sleep 5
curl -f http://localhost:8080/health >/dev/null
curl -f http://localhost:3000/ >/dev/null
echo -e "${GREEN}部署成功：后端 http://localhost:8080，前端 http://localhost:3000，SearXNG http://localhost:8888${NC}"
