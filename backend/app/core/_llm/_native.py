"""Provider: OpenAI SDK 直调 + httpx 裸协议兜底（对齐 BI_Agent `_native.py`）。

deployment 决定模型部署源：
- default：线上模型服务（默认 DeepSeek 官方 OpenAI-compatible 端点）
- private：私有化 / 代理部署的 OpenAI-compatible 服务

plugin 决定用哪种实现：
- native：openai SDK（推荐，自动重试与超时管理）
- httpx ：裸 HTTP，仅在未安装 openai 包时自动降级使用

错误归一：所有底层异常统一转成 RuntimeError，文案面向使用者而非开发者。
"""

from __future__ import annotations

import json
import logging

import httpx

from app.core.config import settings
from app.core._llm._registry import resolve_model_name
from app.core._llm._schemas import LLMResponse, ToolCall

logger = logging.getLogger("triptide.llm.native")

_CLIENTS: dict[str, object] = {}


# ---------------------------------------------------------------- 凭据与客户端
def _credentials(deployment: str) -> tuple[str, str]:
    """按部署源取 (api_key, base_url)。"""
    if deployment == "private":
        key = settings.private_llm_api_key or settings.model_api_key
        base = settings.private_llm_base_url
        if not base:
            raise RuntimeError("PRIVATE_LLM_BASE_URL 未配置，请检查 backend/.env")
    else:
        key = settings.model_api_key
        base = settings.model_base_url

    if not key:
        raise RuntimeError("MODEL_API_KEY 未配置，请检查 backend/.env")
    return key, base.rstrip("/")


def _get_openai_client(deployment: str = "default"):
    """按部署源创建并缓存 OpenAI-compatible 客户端。"""
    if deployment in _CLIENTS:
        return _CLIENTS[deployment]

    from openai import OpenAI  # 惰性导入：没装 openai 时自动走 httpx

    api_key, base_url = _credentials(deployment)
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=settings.llm_timeout)
    _CLIENTS[deployment] = client
    return client


# ---------------------------------------------------------------- 请求体组装
def _build_body(
    model: str,
    messages,
    *,
    temperature,
    max_tokens,
    response_format,
    enable_thinking: bool,
    deployment: str,
    **kwargs,
) -> dict:
    body: dict = {"model": model, "messages": messages, "stream": False}
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if response_format is not None:
        body["response_format"] = response_format
    # 私有化端点不一定支持 enable_thinking 扩展参数，只在线上端点注入
    if deployment != "private":
        body["extra_body"] = {"enable_thinking": 8192 if enable_thinking else False}
    body.update(kwargs)
    return body


def _to_response(content: str, reasoning: str, model: str, usage: dict, finish: str,
                 deployment: str, tool_calls: list[ToolCall] | None = None) -> LLMResponse:
    return LLMResponse(
        content=(content or "").strip(),
        reasoning=(reasoning or "").strip(),
        model=model,
        provider="openai_private" if deployment == "private" else "openai_native",
        usage=usage,
        finish_reason=finish or "",
        tool_calls=tool_calls or [],
    )


def _parse_tool_calls(raw) -> list[ToolCall]:
    """把 OpenAI 形态的 tool_calls 归一成我们自己的 ToolCall。"""
    out: list[ToolCall] = []
    for tc in raw or []:
        fn = getattr(tc, "function", None)
        if fn is not None:  # SDK 对象
            out.append(ToolCall(id=getattr(tc, "id", "") or "", name=fn.name or "",
                                arguments=fn.arguments or "{}"))
        elif isinstance(tc, dict):  # 裸 JSON
            fn = tc.get("function") or {}
            out.append(ToolCall(id=tc.get("id", "") or "", name=fn.get("name", "") or "",
                                arguments=fn.get("arguments") or "{}"))
    return out


# ---------------------------------------------------------------- openai SDK
def _native_chat(
    model,
    messages,
    *,
    enable_thinking: bool = False,
    temperature=None,
    max_tokens=None,
    response_format=None,
    deployment: str = "default",
    **kwargs,
) -> LLMResponse:
    """OpenAI SDK 非流式调用，返回统一 LLMResponse。"""
    from openai import APIError, APITimeoutError, OpenAIError, RateLimitError

    model_name = resolve_model_name(model, deployment)
    client = _get_openai_client(deployment)
    body = _build_body(
        model_name,
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format=response_format,
        enable_thinking=enable_thinking,
        deployment=deployment,
        **kwargs,
    )

    try:
        resp = client.chat.completions.create(**body)
    except RateLimitError as exc:
        raise RuntimeError("API 被限流，请稍后重试") from exc
    except APITimeoutError as exc:
        raise RuntimeError(f"API 超时（{settings.llm_timeout:.0f}s）") from exc
    except APIError as exc:
        raise RuntimeError(f"API 失败 (HTTP {exc.status_code}): {exc.message}") from exc
    except OpenAIError as exc:
        raise RuntimeError(f"API 异常: {exc}") from exc

    if not resp.choices:
        raise RuntimeError("模型返回空结果")

    choice = resp.choices[0]
    msg = choice.message
    usage = {}
    if resp.usage:
        usage = {
            "prompt_tokens": resp.usage.prompt_tokens or 0,
            "completion_tokens": resp.usage.completion_tokens or 0,
            "total_tokens": resp.usage.total_tokens or 0,
        }

    return _to_response(
        msg.content,
        getattr(msg, "reasoning_content", ""),
        model_name,
        usage,
        choice.finish_reason,
        deployment,
        tool_calls=_parse_tool_calls(getattr(msg, "tool_calls", None)),
    )


# ---------------------------------------------------------------- httpx 兜底
_STATUS_HINT = {
    401: "API Key 无效（401）",
    402: "账户余额不足（402）",
    403: "无访问权限（403）",
    404: "模型或接口路径不存在（404）",
    429: "API 被限流（429），请稍后重试",
}


def _httpx_chat(
    model,
    messages,
    *,
    enable_thinking: bool = False,
    temperature=None,
    max_tokens=None,
    response_format=None,
    deployment: str = "default",
    **kwargs,
) -> LLMResponse:
    """未安装 openai SDK 时的裸 HTTP 实现，返回同样的 LLMResponse。"""
    api_key, base_url = _credentials(deployment)
    model_name = resolve_model_name(model, deployment)
    body = _build_body(
        model_name,
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format=response_format,
        enable_thinking=enable_thinking,
        deployment=deployment,
        **kwargs,
    )
    # 裸 HTTP 下 extra_body 要平铺到顶层
    body.update(body.pop("extra_body", {}))

    try:
        resp = httpx.post(
            f"{base_url}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=settings.llm_timeout,
        )
    except httpx.TimeoutException as exc:
        raise RuntimeError(f"API 超时（{settings.llm_timeout:.0f}s）") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"网络异常: {exc}") from exc

    if resp.status_code >= 400:
        hint = _STATUS_HINT.get(resp.status_code)
        if hint:
            raise RuntimeError(hint)
        raise RuntimeError(f"API 失败 (HTTP {resp.status_code}): {resp.text[:200]}")

    try:
        data = resp.json()
        choice = data["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, ValueError) as exc:
        raise RuntimeError(f"无法解析模型响应结构: {resp.text[:200]}") from exc

    raw_usage = data.get("usage") or {}
    usage = {
        "prompt_tokens": raw_usage.get("prompt_tokens", 0),
        "completion_tokens": raw_usage.get("completion_tokens", 0),
        "total_tokens": raw_usage.get("total_tokens", 0),
    }

    return _to_response(
        message.get("content", ""),
        message.get("reasoning_content", ""),
        model_name,
        usage,
        choice.get("finish_reason", ""),
        deployment,
        tool_calls=_parse_tool_calls(message.get("tool_calls")),
    )


# ---------------------------------------------------------------- 分发
def has_openai_sdk() -> bool:
    try:
        import openai  # noqa: F401
    except ImportError:
        return False
    return True


def dispatch_chat(plugin: str, **kwargs) -> LLMResponse:
    """按 plugin 选择实现。openai SDK 缺失时 native 自动降级为 httpx。"""
    if plugin == "httpx":
        return _httpx_chat(**kwargs)
    if plugin == "native":
        if not has_openai_sdk():
            logger.warning("未安装 openai 包，plugin=native 自动降级为 httpx 实现")
            return _httpx_chat(**kwargs)
        return _native_chat(**kwargs)
    raise ValueError(f"未知 plugin: {plugin!r}，可选: native, httpx")
