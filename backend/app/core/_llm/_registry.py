"""模型注册表（对齐 BI_Agent `core/_llm/_registry.py`）。

业务代码只写别名，真实模型名在这里统一映射，换模型不用改业务。
私有化部署的模型名由 PRIVATE_LLM_MODEL 覆盖，避免把部署信息写死在代码里。
"""

from __future__ import annotations

from app.core.config import settings

_MODEL_CATALOG: dict[str, str] = {
    # 默认模型：flash 够快够用，规划这种结构化编排不需要 reasoner
    "": "deepseek-flash",
    "default": "deepseek-flash",
    # DeepSeek 官方
    "deepseek": "deepseek-flash",
    "deepseek-flash": "deepseek-flash",
    "deepseek-chat": "deepseek-chat",
    "deepseek-v3": "deepseek-chat",
    "deepseek-reasoner": "deepseek-reasoner",
    "deepseek-r1": "deepseek-reasoner",
    # 私有化部署占位（真实名由 PRIVATE_LLM_MODEL 覆盖）
    "private": "deepseek-flash",
}


def resolve_model_name(model_key: str, deployment: str = "default") -> str:
    """解析模型别名 → 真实模型名。"""
    if deployment == "private":
        if model_key in ("", "private"):
            return settings.private_llm_model or _MODEL_CATALOG["private"]
        return settings.private_llm_model or model_key
    return _MODEL_CATALOG.get(model_key, model_key)


def list_models() -> dict[str, str]:
    """给运维看的别名总表。"""
    return dict(_MODEL_CATALOG)
