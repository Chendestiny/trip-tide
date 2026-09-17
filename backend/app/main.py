"""AI 旅行搭子 后端应用入口。

职责：
  1. 创建 FastAPI 实例、注册 CORS（默认放行 Vite dev server 5173）
  2. 挂载 /api/trip 路由
  3. 启动时确保数据表存在

前端是独立的 Vite 工程（frontend/），dev 下由 vite 把 /api 代理到 8000，
所以这里不做静态托管；生产由 nginx 分别伺服前端静态文件与 /api。

启动：cd backend && python run.py
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core import list_models, settings
from app.core import init_db
from app.trip.routers import router as trip_router

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("triptide")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="AI 行程规划 —— 选城市 / 勾景点 / 出按天方案",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(trip_router, prefix="/api/trip", tags=["trip"])

    @app.get("/api/health", tags=["meta"])
    def health() -> dict:
        return {
            "ok": True,
            "app": settings.app_name,
            "version": settings.app_version,
            "llm_ready": settings.has_llm,
            "amap_ready": settings.has_amap,
            "db": "sqlite" if settings.is_sqlite else "mysql",
            "model": settings.model_name,
        }

    @app.get("/api/health/models", tags=["meta"])
    def model_catalog() -> dict:
        """模型别名总表，排查「配置的模型名到底解析成了什么」时用。"""
        return list_models()

    @app.on_event("startup")
    def _startup() -> None:
        init_db()
        if not settings.has_llm:
            logger.warning("未配置 DEEPSEEK_API_KEY —— 规划接口将走本地规则引擎兜底")
        if not settings.has_amap:
            logger.warning("未配置 AMAP_KEY —— 数据初始化将使用离线种子坐标")

    @app.exception_handler(Exception)
    async def _unhandled(_request, exc: Exception) -> JSONResponse:
        logger.exception("未捕获异常: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": "服务内部错误", "detail": str(exc)},
        )

    return app


app = create_app()
