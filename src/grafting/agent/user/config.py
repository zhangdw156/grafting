"""User agent configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grafting.utils import Endpoint


@dataclass(frozen=True, slots=True)
class UserAgentConfig:
    endpoint: Endpoint | None = None
    max_turns: int = 30

    @property
    def provider(self) -> str:
        if self.endpoint is None:
            return "openai_chat_completions"
        return self.endpoint.provider
