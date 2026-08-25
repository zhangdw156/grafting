from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MCP_TOOL_PREFIX = "skill-tools-"


def canonicalize_tool_name(name: Any) -> str:
    text = str(name or "").strip()
    if text.startswith(MCP_TOOL_PREFIX):
        return text[len(MCP_TOOL_PREFIX) :]
    return text


def canonicalize_mcp_register_name(*, register_name: Any, tool_name: Any) -> str:
    raw_register_name = str(register_name or "").strip()
    canonical_tool = canonicalize_tool_name(tool_name)
    if not canonical_tool:
        return raw_register_name
    if raw_register_name == canonical_tool:
        return canonical_tool
    if raw_register_name.endswith(f"-{canonical_tool}"):
        return canonical_tool
    return raw_register_name


def resolve_registered_tool_name_from_map(function_map: Any, tool_name: Any) -> str:
    requested_name = str(tool_name or "").strip()
    if not requested_name or not isinstance(function_map, Mapping):
        return requested_name
    if requested_name in function_map:
        return requested_name

    canonical_requested_name = canonicalize_tool_name(requested_name)
    if not canonical_requested_name:
        return requested_name
    if canonical_requested_name in function_map:
        return canonical_requested_name

    matches = [
        candidate
        for candidate in function_map
        if isinstance(candidate, str)
        and canonicalize_tool_name(candidate) == canonical_requested_name
    ]
    if len(matches) == 1:
        return matches[0]
    return requested_name
