from __future__ import annotations

from dataclasses import dataclass, field

from grafting.utils.token_usage import TokenUsage

__all__ = ["HarnessRunResult", "MCPConfig", "TokenUsage"]


@dataclass(frozen=True, slots=True)
class HarnessRunResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    harness_backend: str = ""
    model: str = ""
    provider: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    @property
    def success(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True, slots=True)
class MCPConfig:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
