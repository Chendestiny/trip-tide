"""核心能力层：LLM 统一入口 + 配置 + 数据库。

对外用法（对齐 BI_Agent 的「只暴露两个入口」思路，这里只暴露 llm 一族 + 基础设施）：

    from app.core import llm          # LLM 调用
    from app.core import settings     # 配置
    from app.core import get_db       # FastAPI 依赖

内部实现用 PEP 562 惰性导入，避免 config ←→ _llm 的循环依赖。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "llm",
    "LlmClient",
    "LLMResponse",
    "LLMFormatError",
    "ToolCall",
    "resolve_model_name",
    "list_models",
    "extract_json",
    "settings",
    "Settings",
    "get_settings",
    "Base",
    "engine",
    "SessionLocal",
    "get_db",
    "init_db",
]

if TYPE_CHECKING:  # 仅供类型检查与 IDE 补全，运行时不导入
    from app.core._llm import (  # noqa: F401
        LLMFormatError,
        LLMResponse,
        LlmClient,
        ToolCall,
        extract_json,
        list_models,
        llm,
        resolve_model_name,
    )
    from app.core.config import Settings, get_settings, settings  # noqa: F401
    from app.core.db import Base, SessionLocal, engine, get_db, init_db  # noqa: F401


# 名字 → (模块, 属性) 的惰性映射
_LAZY: dict[str, tuple[str, str]] = {
    "llm": ("app.core._llm", "llm"),
    "LlmClient": ("app.core._llm", "LlmClient"),
    "LLMResponse": ("app.core._llm", "LLMResponse"),
    "LLMFormatError": ("app.core._llm", "LLMFormatError"),
    "ToolCall": ("app.core._llm", "ToolCall"),
    "resolve_model_name": ("app.core._llm", "resolve_model_name"),
    "list_models": ("app.core._llm", "list_models"),
    "extract_json": ("app.core._llm", "extract_json"),
    "settings": ("app.core.config", "settings"),
    "Settings": ("app.core.config", "Settings"),
    "get_settings": ("app.core.config", "get_settings"),
    "Base": ("app.core.db", "Base"),
    "engine": ("app.core.db", "engine"),
    "SessionLocal": ("app.core.db", "SessionLocal"),
    "get_db": ("app.core.db", "get_db"),
    "init_db": ("app.core.db", "init_db"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(target[0]), target[1])
    globals()[name] = value  # 缓存，后续访问不再走 __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
