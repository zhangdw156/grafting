from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from grafting.utils.token_usage import TokenUsage


@dataclass(slots=True)
class AssistantToolCall:
    """
    Assistant 一轮中的单次工具调用。
    """

    name: str
    arguments: str
    result: str = ""


@dataclass(slots=True)
class AssistantTurnResult:
    """
    Assistant 一轮执行结果。
    """

    messages: list[dict[str, Any]]
    assistant_message: str
    tool_calls: list[AssistantToolCall]
    raw_response: list[Any]
    reasoning_content: str = ""
    usage: TokenUsage | None = None