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

if grep -q "replace_with_32_random_hex_chars" .env; then
    echo -e "${YELLOW}请先在 .env 中设置 SEARXNG_SECRET，例如：openssl rand -hex 32${NC}"
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
