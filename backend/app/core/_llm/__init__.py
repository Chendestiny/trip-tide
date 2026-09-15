"""LLM 统一调用入口。

对外只暴露 LlmClient + llm 单例。调用实现由 plugin 决定，部署源由 deployment 决定：
- plugin="native"：OpenAI SDK 直调 OpenAI-compatible API（未装 openai 包时自动降级 httpx）
- plugin="httpx" ：裸 HTTP，零依赖
- deployment="default"：线上模型服务
- deployment="private"：私有化 / 代理部署的模型服务

三种调用形态：
- chat()                 普通对话，返回 LLMResponse（含 tool_calls）
- with_structured_output()  结构化输出，把 schema 实例返回
- run_tools()            function calling 编排循环：模型调工具 → 执行 → 结果喂回 → 直到收敛
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from ._native import dispatch_chat, has_openai_sdk
from ._registry import list_models, resolve_model_name
from ._schemas import LLMFormatError, LLMResponse, ProviderKind, ToolCall

logger = logging.getLogger("triptide.llm")

__all__ = [
    "LlmClient",
    "llm",
    "LLMResponse",
    "LLMFormatError",
    "ToolCall",
    "ProviderKind",
    "extract_json",
    "resolve_model_name",
    "list_models",
]

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _ensure_json_hint(messages):
    """结构化输出兜底：确保 messages 中出现 'json' 一词。

    部分 provider 的 json 模式强制要求 messages 含 'json' 字样，否则直接 400。
    业务 prompt 漏写时这里自动补一条，避免整类调用批量报错。
    """
    try:
        for m in messages:
            content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
            if "json" in str(content or "").lower():
                return messages
    except Exception:  # noqa: BLE001
        return messages

    hint = "输出要求：结果必须是合法的 JSON。"
    if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
        merged = dict(messages[0])
        merged["content"] = f"{str(messages[0].get('content', '')).rstrip()}\n{hint}"
        return [merged, *list(messages[1:])]
    return [{"role": "system", "content": hint}, *list(messages)]


def extract_json(text: str) -> dict:
    """从模型输出里尽可能稳地抠出一个 JSON 对象。"""
    if not text or not text.strip():
        raise ValueError("模型返回内容为空")

    cleaned = _FENCE_RE.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"返回内容中找不到 JSON 对象：{text[:120]}")

    snippet = cleaned[start : end + 1]
    try:
        return json.loads(snippet)
    except json.JSONDecodeError:
        # 最常见的两种小毛病：尾逗号、中文全角引号
        repaired = _TRAILING_COMMA_RE.sub(r"\1", snippet)
        repaired = repaired.replace("“", '"').replace("”", '"')
        try:
            return json.loads(repaired)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 解析失败（{exc.msg} @ {exc.pos}）") from exc


class LlmClient:
    """统一 LLM 调用入口。"""

    def chat(
        self,
        messages,
        *,
        model: str = "",
        plugin: str = "native",
        deployment: str = "default",
        **kwargs,
    ) -> LLMResponse:
        """发送 messages，返回统一的 LLMResponse（可能带 tool_calls）。"""
        return dispatch_chat(
            plugin,
            model=model,
            messages=messages,
            deployment=deployment,
            **kwargs,
        )

    def with_structured_output(
        self,
        messages,
        schema,
        *,
        model: str = "",
        plugin: str = "native",
        deployment: str = "default",
        return_raw: bool = False,
        **kwargs,
    ):
        """结构化输出：内部强制 json_object 模式 + 容错抽取 + schema 校验。

        校验失败抛 LLMFormatError（携带原文），由调用方决定是否回灌重试。
        """
        messages = _ensure_json_hint(messages)
        resp = self.chat(
            messages,
            model=model,
            plugin=plugin,
            deployment=deployment,
            response_format={"type": "json_object"},
            **kwargs,
        )
        try:
            data = extract_json(resp.content)
            obj = (
                schema.model_validate(data)
                if hasattr(schema, "model_validate")
                else schema(**data)
            )
        except Exception as exc:  # noqa: BLE001
            raise LLMFormatError(str(exc), raw=resp.content) from exc
        return (obj, resp) if return_raw else obj

    def run_tools(
        self,
        messages,
        tools: list[dict],
        dispatch: Callable[[str, dict], Any],
        *,
        model: str = "",
        plugin: str = "native",
        deployment: str = "default",
        max_rounds: int = 12,
        stop_when: Callable[[], bool] | None = None,
        on_tool: Callable[[str, dict, Any], None] | None = None,
        **kwargs,
    ) -> tuple[LLMResponse | None, list[dict]]:
        """function calling 编排循环。

        模型调工具 → 本地执行 → 结果塞回对话 → 继续，直到：
          · 模型不再调工具（返回最后一次响应），或
          · stop_when() 命中（通常表示已调用终止工具），或
          · 达到 max_rounds（防止死循环）

        返回 (最后一次响应, 完整对话记录)。对话记录可直接落盘用于排查。
        """
        convo: list[dict] = list(messages)
        last: LLMResponse | None = None

        for round_no in range(1, max_rounds + 1):
            last = self.chat(
                convo, model=model, plugin=plugin, deployment=deployment,
                tools=tools, tool_choice="auto", **kwargs,
            )
            convo.append(last.assistant_message())

            if not last.tool_calls:
                logger.info("第 %d 轮：模型不再调用工具，编排结束", round_no)
                break

            logger.info(
                "第 %d 轮：调用 %d 个工具 → %s",
                round_no,
                len(last.tool_calls),
                ", ".join(tc.name for tc in last.tool_calls),
            )
            for tc in last.tool_calls:
                args = tc.parsed()
                try:
                    out = dispatch(tc.name, args)
                except Exception as exc:  # noqa: BLE001 —— 工具报错要如实回给模型，别让它崩掉整条链路
                    logger.warning("工具 %s 执行失败：%s", tc.name, exc)
                    out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                if on_tool is not None:
                    on_tool(tc.name, args, out)
                convo.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(out, ensure_ascii=False, default=str),
                    }
                )

            if stop_when is not None and stop_when():
                logger.info("第 %d 轮：终止工具已调用，编排结束", round_no)
                break
        else:
            logger.warning("达到 max_rounds=%d，强制结束编排", max_rounds)

        return last, convo


llm = LlmClient()
