# Research Agent Dockerfile
FROM python:3.13-slim

LABEL maintainer="research-agent@example.com"
LABEL description="AI-powered Competitive Research Agent with PRD Generation"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    APP_ENV=production \
    APP_HOST=0.0.0.0 \
    APP_PORT=8080

# 设置工作目录
WORKDIR /app

# 安装系统依赖（Playwright和健康检查需要）
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 安装 uv
RUN pip install uv

# 复制依赖元数据
COPY pyproject.toml uv.lock README.md ./

# 使用 uv 安装依赖（使用锁定版本）
RUN uv sync --frozen --no-dev --no-install-project

# 安装Playwright浏览器
RUN uv run playwright install chromium --with-deps \
    && chmod -R 755 /ms-playwright

# 复制项目文件
COPY . .

# 创建非root用户和输出目录
# appuser 固定 uid=1000：与常见部署用户/GitHub Actions runner 对齐，
# bind mount 的 output 目录（属主来自宿主机）容器内才有写权限。
RUN useradd --create-home --shell /usr/sbin/nologin --uid 1000 appuser \
    && mkdir -p /app/output \
    && chown -R appuser:appuser /app /ms-playwright

USER appuser

# 暴露端口
EXPOSE 8080

# 健康检查。用**就绪**探针：缺 LLM 密钥时 /health 仍返 200，容器会被判为健康，
# 直到用户点"开始调研"才 401。/health/ready 在依赖不可用时返回 503，
# 让编排系统能据此摘流量或直接暴露配置问题。
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health/ready || exit 1

# 启动命令
CMD ["uv", "run", "python", "server.py"]
