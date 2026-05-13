"""FastAPI 应用入口。"""

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from server.middleware import setup_cors, RateLimiter
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

    return app


app = create_app()


def run_server(host: str = "0.0.0.0", port: int = 8000):
    import uvicorn
    uvicorn.run("server.app:app", host=host, port=port, reload=True)
