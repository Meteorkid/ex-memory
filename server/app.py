"""FastAPI 应用入口。"""

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from server.middleware import setup_cors, RateLimiter, RequestLoggingMiddleware
from server.routes import router

STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"


def create_app() -> FastAPI:
    app = FastAPI(
        title="ex-memory API",
        description="前任记忆智能体 REST API",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
    )

    setup_cors(app)

    # 请求日志中间件（在限流之前记录）
    app.middleware("http")(RequestLoggingMiddleware())

    # 限流中间件
    app.middleware("http")(RateLimiter(max_requests=120, window_seconds=60))

    app.include_router(router)

    # 静态文件
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index():
        """Web 客户端首页。"""
        html = STATIC_DIR / "index.html"
        if html.exists():
            return FileResponse(str(html), media_type="text/html")
        return {"message": "ex-memory API", "docs": "/api/docs"}

    @app.get("/health")
    def health():
        return {"status": "ok", "version": "1.0.0"}

    @app.get("/health/ready")
    def health_ready():
        """就绪检查：验证依赖是否可用。"""
        import shutil
        checks = {}

        # ChromaDB 可写检查
        try:
            from pathlib import Path as _Path
            test_dir = _Path("data/health_check_chroma")
            test_dir.mkdir(parents=True, exist_ok=True)
            (test_dir / ".write_test").write_text("ok")
            checks["chromadb"] = "ok"
        except Exception as e:
            checks["chromadb"] = f"error: {e}"

        # 磁盘空间检查 (>100MB)
        try:
            usage = shutil.disk_usage(".")
            free_mb = usage.free // (1024 * 1024)
            checks["disk"] = "ok" if free_mb > 100 else f"low: {free_mb}MB free"
        except Exception:
            checks["disk"] = "unknown"

        all_ok = all(v == "ok" or v.startswith("low:") for v in checks.values())
        return {
            "status": "ok" if all_ok else "degraded",
            "version": "1.0.0",
            "checks": checks,
        }

    return app


app = create_app()


def run_server(host: str = "0.0.0.0", port: int = 8000):
    import uvicorn
    uvicorn.run("server.app:app", host=host, port=port, reload=True)


if __name__ == "__main__":
    run_server()
