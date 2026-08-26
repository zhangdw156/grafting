"""Format and exact tool-call scoring for capability probing."""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

_TOOL_CALL_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)


def _hashable(value: Any) -> Any:
    if isinstance(value, dict):
        return frozenset((key, _hashable(item)) for key, item in value.items())
    if isinstance(value, list):
        return tuple(sorted((_hashable(item) for item in value), key=repr))
    return value


def _parse_tool_calls(text: str) -> tuple[list[dict[str, Any]], bool]:
    matches = _TOOL_CALL_RE.findall(text)
    if (
        not matches
        or text.count("<tool_call>") != len(matches)
        or text.count("</tool_call>") != len(matches)
    ):
        return [], False

    result: list[dict[str, Any]] = []
    for match in matches:
        try:
            parsed = json.loads(match)
        except json.JSONDecodeError:
            return [], False
        if (
            not isinstance(parsed, dict)
            or not isinstance(parsed.get("name"), str)
            or not isinstance(parsed.get("arguments"), dict)
        ):
            return [], False
        result.append(parsed)
    return result, True


def extract_tool_calls(text: str) -> list[dict[str, Any]]:
    """Extract valid JSON tool calls, returning an empty list if malformed."""
    calls, valid = _parse_tool_calls(text)
    return calls if valid else []


def compare_tool_calls(ground_truth: list[Any], prediction: list[Any]) -> bool:
    """Compare tool-call collections without relying on call order."""
    expected = Counter(_hashable(item) for item in ground_truth)
    actual = Counter(_hashable(item) for item in prediction)
    return expected == actual


def score_response(response: str, ground_truth: str) -> tuple[float, float]:
    """Return ``(format_score, correctness_score)`` for one response."""
    expected, expected_valid = _parse_tool_calls(ground_truth)
    prediction, prediction_valid = _parse_tool_calls(response)
    format_score = 0.1 if prediction_valid else 0.0
    correctness_score = (
        1.0
        if expected_valid
        and prediction_valid
        and compare_tool_calls(expected, prediction)
        else 0.0
    )
    return format_score, correctness_score
