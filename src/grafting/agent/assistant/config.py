from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grafting.utils import Endpoint


@dataclass(frozen=True, slots=True)
class AssistantAgentConfig:
    mcp_url: str = ""
    verbose: bool = False
    request_timeout_sec: float = 300.0
    max_retries: int = 2
    max_llm_calls_per_run: int = 12
    endpoint: Endpoint | None = None
    extra_system_prompt: str = ""

    @property
    def provider(self) -> str:
        if self.endpoint is None:
            return "openai_chat_completions"
        return self.endpoint.provider

    def validate_basic(self) -> list[str]:
        errors: list[str] = []
        if self.request_timeout_sec <= 0:
            errors.append("request_timeout_sec must be positive")
        if self.max_retries < 0:
            errors.append("max_retries must not be negative")
        if self.max_llm_calls_per_run <= 0:
            errors.append("max_llm_calls_per_run must be positive")
        if self.provider not in {"anthropic", "openai_chat_completions"}:
            errors.append(
                "provider must be anthropic or openai_chat_completions: "
                f"{self.provider}"
            )
        return errors
