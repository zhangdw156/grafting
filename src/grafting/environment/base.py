"""EnvironmentBackendProtocol — the duck-typing interface for backends."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EnvironmentBackendProtocol(Protocol):
    """Environment backend protocol (duck typing).

    Backends do not need to inherit this class — any class that implements
    these three methods is a valid backend.
    """

    def __init__(self, *, db_path: Path) -> None: ...
    def call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...
    def visible_state(self) -> dict[str, Any]: ...
