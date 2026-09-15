"""全局配置。所有可调参数集中在这里，其它模块只读不写。

配置来源优先级：环境变量 > backend/.env > 本文件默认值。

环境变量命名同时兼容 my-website 的既有写法（DATABASE_URL / DEEPSEEK_API_KEY），
这样 TripTide 未来并入 my-website 时 .env 可以直接复用，不用改一行配置。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> backend/
BACKEND_DIR = Path(__file__).resolve().parents[2]
# 仓库根目录
PROJECT_DIR = BACKEND_DIR.parent
FRONTEND_DIR = PROJECT_DIR / "frontend"
DATA_DIR = BACKEND_DIR / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "TripTide API"
    app_version: str = "0.1.0"
    debug: bool = True

    # ---------- 数据库 ----------
    # DATABASE_URL 为 my-website 既有名，DB_URL 为本项目原名，两者都认
    db_url: str = Field(
        "",
        validation_alias=AliasChoices("DATABASE_URL", "DB_URL"),
        description="留空则退化为 SQLite 单文件，零配置也能跑",
    )
    db_echo: bool = False

    # ---------- 模型服务 ----------
    # DEEPSEEK_API_KEY 为 my-website 既有名，MODEL_API_KEY 为通用名，两者都认
    model_api_key: str = Field(
        "",
        validation_alias=AliasChoices("MODEL_API_KEY", "DEEPSEEK_API_KEY"),
    )
    model_base_url: str = Field(
        "https://api.deepseek.com/v1",
        validation_alias=AliasChoices("MODEL_BASE_URL", "DEEPSEEK_BASE_URL"),
    )
    model_name: str = Field(
        "deepseek-flash",
        validation_alias=AliasChoices("MODEL_NAME", "DEEPSEEK_MODEL"),
        description="模型别名，真实名由 core/_llm/_registry.py 解析",
    )
    # 私有化 / 代理部署（deployment="private" 时生效）
    private_llm_api_key: str = ""
    private_llm_base_url: str = ""
    private_llm_model: str = ""

    llm_timeout: float = 180.0
    llm_max_retry: int = 1
    llm_temperature: float = 0.7
    llm_max_tokens: int = 8192

    # ---------- 高德 Web 服务（POI 搜索补 GCJ-02 坐标） ----------
    amap_key: str = ""
    amap_base_url: str = "https://restapi.amap.com"
    amap_timeout: float = 15.0

    # ---------- 前端 / CORS ----------
    cors_origins: str = "http://localhost:5176,http://127.0.0.1:5176"

    # ---------- 业务默认值 ----------
    default_start_time: str = "09:00"
    default_return_time: str = "19:30"
    max_days: int = 7

    # ------------------------------------------------------------------
    @property
    def resolved_db_url(self) -> str:
        """DB_URL 为空时退化为 SQLite，保证零配置也能起服务。"""
        if self.db_url.strip():
            return self.db_url.strip()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(DATA_DIR / 'triptide.db').as_posix()}"

    @property
    def is_sqlite(self) -> bool:
        return self.resolved_db_url.startswith("sqlite")

    @property
    def has_llm(self) -> bool:
        return bool(self.model_api_key.strip())

    @property
    def has_amap(self) -> bool:
        return bool(self.amap_key.strip())

    @property
    def cors_origin_list(self) -> list[str]:
        raw = (self.cors_origins or "*").strip()
        if raw == "*":
            return ["*"]
        return [item.strip() for item in raw.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
