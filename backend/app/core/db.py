"""数据库连接与会话管理（MySQL / SQLite 双兼容）。

对外只暴露 4 个东西：
    Base          —— ORM 基类
    engine        —— 全局引擎
    SessionLocal  —— 会话工厂
    get_db()      —— FastAPI 依赖
    init_db()     —— 建库 + 建表（幂等）
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

logger = logging.getLogger("triptide.db")


class Base(DeclarativeBase):
    pass


def _engine_kwargs() -> dict:
    if settings.is_sqlite:
        return {"connect_args": {"check_same_thread": False}}
    # MySQL：连接前 ping，避免 8 小时断连后第一个请求报错
    return {"pool_pre_ping": True, "pool_recycle": 3600, "pool_size": 5, "max_overflow": 10}


engine = create_engine(
    settings.resolved_db_url,
    echo=settings.db_echo,
    future=True,
    **_engine_kwargs(),
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def ensure_database() -> None:
    """MySQL 下若目标库不存在则自动创建。SQLite 无需处理。"""
    if settings.is_sqlite:
        return

    parts = urlsplit(settings.resolved_db_url)
    db_name = parts.path.lstrip("/")
    if not db_name:
        return

    # 连到服务器级（不带库名），检查并建库
    server_url = urlunsplit((parts.scheme, parts.netloc, "/", parts.query, ""))
    server_engine = create_engine(server_url, future=True, pool_pre_ping=True)
    try:
        with server_engine.connect() as conn:
            exists = conn.execute(
                text(
                    "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
                    "WHERE SCHEMA_NAME = :name"
                ),
                {"name": db_name},
            ).first()
            if not exists:
                # 库名来自配置文件，不接受外部输入；仍用反引号包裹防关键字冲突
                conn.execute(
                    text(
                        f"CREATE DATABASE `{db_name}` "
                        "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                    )
                )
                conn.commit()
                logger.info("已自动创建数据库 %s", db_name)
    finally:
        server_engine.dispose()


def init_db() -> None:
    """建库 + 建表。可重复调用。"""
    ensure_database()
    # 让 models 完成注册
    from app.trip import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    logger.info("数据表已就绪：%s", ", ".join(sorted(Base.metadata.tables)))


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
