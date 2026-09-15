"""LLM 类型定义。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ProviderKind = Literal["openai_native", "openai_private", "httpx_fallback"]


@dataclass
class ToolCall:
    """模型发起的一次工具调用。arguments 保留原始 JSON 字符串，不做预解析。"""

    id: str = ""
    name: str = ""
    arguments: str = "{}"

    def parsed(self) -> dict[str, Any]:
        import json

        try:
            data = json.loads(self.arguments or "{}")
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}


@dataclass
class LLMResponse:
    """统一的 LLM 调用响应。"""

    content: str = ""
    reasoning: str = ""
    model: str = ""
    provider: ProviderKind = "openai_native"
    usage: dict = field(default_factory=dict)
    finish_reason: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def assistant_message(self) -> dict[str, Any]:
        """转回 OpenAI messages 格式，供下一轮对话使用（必须原样带回 tool_calls）。"""
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments or "{}"},
                }
                for tc in self.tool_calls
            ]
        return msg


class LLMFormatError(RuntimeError):
    """模型输出能拿到，但解析 / 校验不通过。携带原文，便于回灌重试。"""

    def __init__(self, message: str, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw or ""
