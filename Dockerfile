# Build stage
FROM python:3.11-slim AS builder

WORKDIR /app
COPY requirements.lock .
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
RUN pip install --no-cache-dir --user --index-url "$PIP_INDEX_URL" -r requirements.lock

# Runtime stage
FROM python:3.11-slim

LABEL org.opencontainers.image.title="ex-memory"
LABEL org.opencontainers.image.description="前任记忆智能体"

RUN addgroup --system app && adduser --system --ingroup app app

WORKDIR /app

COPY --from=builder /root/.local /home/app/.local
ENV PATH=/home/app/.local/bin:$PATH
ENV PYTHONPATH=/home/app/.local/lib/python3.11/site-packages

COPY . .

RUN mkdir -p /app/data/exes /app/data/logs /app/logs && chown -R app:app /app/data /app/logs
VOLUME /app/data

USER app

EXPOSE 8000 7860

# 默认启动 FastAPI 服务器
CMD ["python", "-m", "uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8000"]
