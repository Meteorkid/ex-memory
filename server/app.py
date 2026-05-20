"""FastAPI 应用入口。"""

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from starlette.exceptions import HTTPException as StarletteHTTPException

from server.middleware import setup_cors, RateLimiter, RequestLoggingMiddleware, SecurityHeadersMiddleware
from server.routes import router

STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"


class PublicStaticFiles(StaticFiles):
    """公开静态资源，但拒绝历史自定义贴纸目录。"""

    async def get_response(self, path: str, scope):
        parts = Path(path).parts
        if len(parts) >= 2 and parts[0] == "stickers" and parts[1] == "custom":
            raise StarletteHTTPException(status_code=404)
        return await super().get_response(path, scope)


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

    # 基础安全响应头
    app.middleware("http")(SecurityHeadersMiddleware())

    # 限流中间件
    app.middleware("http")(RateLimiter(max_requests=120, window_seconds=60))

    app.include_router(router)

    # 静态文件
    if STATIC_DIR.exists():
        app.mount("/static", PublicStaticFiles(directory=str(STATIC_DIR)), name="static")

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

        # 数据目录可写检查
        try:
            from pathlib import Path as _Path
            test_dir = _Path("data/health_check")
            test_dir.mkdir(parents=True, exist_ok=True)
            write_test = test_dir / ".write_test"
            write_test.write_text("ok", encoding="utf-8")
            write_test.unlink(missing_ok=True)
            checks["data_dir"] = "ok"
        except Exception as e:
            checks["data_dir"] = f"error: {e}"

        # 认证数据库检查
        try:
            import server.auth as auth
            with auth._get_conn() as conn:
                conn.execute("SELECT 1").fetchone()
            checks["database"] = "ok"
        except Exception as e:
            checks["database"] = f"error: {e}"

        # 磁盘空间检查 (>100MB)
        try:
            usage = shutil.disk_usage(".")
            free_mb = usage.free // (1024 * 1024)
            checks["disk"] = "ok" if free_mb > 100 else f"low: {free_mb}MB free"
        except Exception:
            checks["disk"] = "unknown"

        all_ok = all(v == "ok" for v in checks.values())
        payload = {
            "status": "ok" if all_ok else "degraded",
            "version": "1.0.0",
            "checks": checks,
        }
        return JSONResponse(status_code=200 if all_ok else 503, content=payload)

    return app


app = create_app()


def run_server(host: str = "0.0.0.0", port: int = 8000):
    import uvicorn
    uvicorn.run("server.app:app", host=host, port=port, reload=True)


if __name__ == "__main__":
    run_server()
