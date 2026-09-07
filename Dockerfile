# 国内加速源；若可用官方 Hub 可改回：FROM python:3.11-slim
# 由 docker-compose 的 platform 决定架构，避免 exec format error
FROM docker.m.daocloud.io/library/python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    curl \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Shanghai
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
# 使用国内 PyPI 镜像，避免 files.pythonhosted.org 超时
RUN pip install --no-cache-dir \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com \
    --default-timeout=120 \
    -r requirements.txt

COPY app/ ./app/

RUN mkdir -p /app/data /media/videos

EXPOSE 8080

# NAS 挂载目录权限各异，默认以 root 运行以保证可写；健康检查保留
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1

# TRUST_PROXY_HEADERS=true 时开启 uvicorn 的 proxy-headers（信任 XFF），否则关闭防伪造
CMD ["sh", "-c", "if [ \"$TRUST_PROXY_HEADERS\" = \"true\" ] || [ \"$TRUST_PROXY_HEADERS\" = \"1\" ]; then PH=--proxy-headers; else PH=--no-proxy-headers; fi; exec uvicorn app.main:app --host 0.0.0.0 --port 8080 $PH"]
