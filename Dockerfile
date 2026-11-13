# syntax=docker/dockerfile:1
FROM python:3.12-slim

# 可选增强：WD14 ONNX 本地反推。默认 0 = 镜像零 pip 依赖，构建不需要联网装包。
ARG WITH_ONNX=0
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data \
    MODEL_DIR=/app/models/wd14 \
    PORT=8199 \
    COMFYUI_URL=http://127.0.0.1:8188 \
    PUID=99 \
    PGID=100

WORKDIR /app
COPY app/ /app/app/
COPY data/ /app/seed/
COPY entrypoint.sh /app/entrypoint.sh
COPY requirements-optional.txt /app/requirements-optional.txt

RUN set -eux; \
    if [ "$WITH_ONNX" = "1" ]; then \
      pip install --no-cache-dir -i "$PIP_INDEX_URL" -r /app/requirements-optional.txt; \
    fi; \
    sed -i 's/\r$//' /app/entrypoint.sh; \
    chmod +x /app/entrypoint.sh; \
    mkdir -p /app/data /app/models/wd14

EXPOSE 8199
VOLUME ["/app/data", "/app/models/wd14"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT','8199'), timeout=3)"

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "-m", "app.server"]
