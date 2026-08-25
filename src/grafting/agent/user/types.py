"""User agent types."""

from __future__ import annotations

from dataclasses import dataclass

from grafting.utils.token_usage import TokenUsage


@dataclass(slots=True)
class UserTurnResult:
    message: str
    is_done: bool
    usage: TokenUsage | None = None
