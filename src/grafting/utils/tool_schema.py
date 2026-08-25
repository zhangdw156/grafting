from __future__ import annotations

import keyword
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

_DEFAULT_RESERVED_NAMES = {"__state_key"}


@dataclass(frozen=True, slots=True)
class ToolParameterMapping:
    public_name: str
    original_name: str
    schema: Any
    required: bool


def build_parameter_name_mappings(
    *,
    tool_params: dict[str, Any],
    required_names: set[str],
    reserved_names: Iterable[str] = _DEFAULT_RESERVED_NAMES,
) -> list[ToolParameterMapping]:
    reserved = set(reserved_names)
    used_public_names: set[str] = set()
    mappings: list[ToolParameterMapping] = []

    for original_name, schema in tool_params.items():
        if not isinstance(original_name, str):
            continue

        public_name = make_safe_parameter_name(
            original_name=original_name,
            reserved_names=reserved,
            used_names=used_public_names,
        )
        used_public_names.add(public_name)
        mappings.append(
            ToolParameterMapping(
                public_name=public_name,
                original_name=original_name,
                schema=schema,
                required=original_name in required_names,
            )
        )

    return mappings


def restore_original_argument_names(
    *,
    arguments: dict[str, Any],
    parameter_mappings: list[ToolParameterMapping],
) -> dict[str, Any]:
    alias_to_original = {
        mapping.public_name: mapping.original_name for mapping in parameter_mappings
    }
    restored: dict[str, Any] = {}

    for key, value in arguments.items():
        target_key = alias_to_original.get(key, key)
        if target_key in restored and key != target_key:
            continue
        restored[target_key] = value

    return restored


def make_safe_parameter_name(
    *,
    original_name: str,
    reserved_names: set[str],
    used_names: set[str],
) -> str:
    candidate = original_name
    if is_safe_parameter_name(candidate, reserved_names=reserved_names):
        public_name = candidate
    else:
        public_name = sanitize_parameter_name(candidate)

    if public_name in reserved_names:
        public_name = f"{public_name}_"
    if keyword.iskeyword(public_name):
        public_name = f"{public_name}_"
    if not public_name.isidentifier():
        public_name = sanitize_parameter_name(public_name)

    base_name = public_name or "arg"
    suffix = 2
    while (
        public_name in used_names
        or public_name in reserved_names
        or keyword.iskeyword(public_name)
        or not public_name.isidentifier()
    ):
        public_name = f"{base_name}_{suffix}"
        suffix += 1

    return public_name


def is_safe_parameter_name(name: str, *, reserved_names: set[str]) -> bool:
    return (
        bool(name)
        and name.isidentifier()
        and not keyword.iskeyword(name)
        and name not in reserved_names
    )


def sanitize_parameter_name(name: str) -> str:
    sanitized = re.sub(r"[^0-9A-Za-z_]", "_", (name or "").strip())
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    if not sanitized:
        sanitized = "arg"
    if sanitized[0].isdigit():
        sanitized = f"arg_{sanitized}"
    return sanitized
