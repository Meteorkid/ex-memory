# Build stage
FROM python:3.11-slim AS builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Runtime stage
FROM python:3.11-slim

LABEL org.opencontainers.image.title="ex-memory"
LABEL org.opencontainers.image.description="前任记忆智能体"

RUN addgroup --system app && adduser --system --ingroup app app

WORKDIR /app

COPY --from=builder /root/.local /home/app/.local
ENV PATH=/home/app/.local/bin:$PATH

COPY . .

RUN mkdir -p /app/data/exes /app/data/logs && chown -R app:app /app/data
VOLUME /app/data

USER app

EXPOSE 8000 7860

# 默认启动 FastAPI 服务器
CMD ["python", "-m", "uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8000"]
