"""Dense tool-call reward used for Fathoming GRPO training.

The reward preserves a formatting signal, gives partial credit for tool names
and arguments, and penalizes extra, malformed, or duplicate calls. The public
``compute_score`` entry point follows veRL's custom reward-function contract.
"""

import json
import math
import re
from typing import Any

_TOOL_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)


def _normalize_scalar(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.strip().lower().split())
    return value


def _canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _canonicalize(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        canon = [_canonicalize(v) for v in value]
        try:
            return sorted(
                canon,
                key=lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True),
            )
        except Exception:
            return canon
    return _normalize_scalar(value)


def _extract_tool_calls(text: str) -> tuple[list[Any], int]:
    parsed = []
    malformed = 0
    for match in _TOOL_RE.findall(text or ""):
        try:
            parsed.append(json.loads(match))
        except Exception:
            malformed += 1
    return parsed, malformed


def _tool_name(tool: Any) -> str | None:
    if not isinstance(tool, dict):
        return None
    name = tool.get("name") or tool.get("tool") or tool.get("function")
    if name is None:
        return None
    return str(name)


def _tool_args(tool: Any) -> dict[str, Any]:
    if not isinstance(tool, dict):
        return {}
    args = tool.get("arguments", {})
    if isinstance(args, dict):
        return args
    return {}


def _jaccard(a: set[Any], b: set[Any]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def _numeric_similarity(pred: float, gt: float) -> float:
    if pred == gt:
        return 1.0
    scale = max(abs(gt), 1.0)
    return max(0.0, 1.0 - abs(pred - gt) / scale)


def _value_similarity(pred: Any, gt: Any) -> float:
    pred = _canonicalize(pred)
    gt = _canonicalize(gt)

    if pred == gt:
        return 1.0

    if isinstance(pred, bool) or isinstance(gt, bool):
        return 1.0 if pred == gt else 0.0

    if isinstance(pred, (int, float)) and isinstance(gt, (int, float)):
        return _numeric_similarity(float(pred), float(gt))

    if isinstance(pred, str) and isinstance(gt, str):
        if not pred or not gt:
            return 0.0
        return 0.0

    if isinstance(pred, list) and isinstance(gt, list):
        if not pred and not gt:
            return 1.0
        pred_dump = {
            json.dumps(v, ensure_ascii=False, sort_keys=True) for v in pred
        }
        gt_dump = {json.dumps(v, ensure_ascii=False, sort_keys=True) for v in gt}
        return _jaccard(pred_dump, gt_dump)

    if isinstance(pred, dict) and isinstance(gt, dict):
        pred_keys = set(pred.keys())
        gt_keys = set(gt.keys())
        key_score = _jaccard(pred_keys, gt_keys)
        shared = pred_keys & gt_keys
        if not shared:
            return 0.4 * key_score
        val_score = sum(
            _value_similarity(pred[k], gt[k]) for k in shared
        ) / len(shared)
        return 0.4 * key_score + 0.6 * val_score

    return 0.0


def _arg_key_score(
    pred_args: dict[str, Any],
    gt_args: dict[str, Any],
) -> float:
    return _jaccard(set(pred_args.keys()), set(gt_args.keys()))


def _arg_value_score(
    pred_args: dict[str, Any],
    gt_args: dict[str, Any],
) -> float:
    shared = set(pred_args.keys()) & set(gt_args.keys())
    if not shared:
        return 0.0
    return sum(_value_similarity(pred_args[k], gt_args[k]) for k in shared) / len(
        shared
    )


def _match_score(pred_tool: Any, gt_tool: Any) -> float:
    pred_name = _tool_name(pred_tool)
    gt_name = _tool_name(gt_tool)
    if not pred_name or not gt_name:
        return 0.0
    name_score = 1.0 if pred_name == gt_name else 0.0
    pred_args = _tool_args(pred_tool)
    gt_args = _tool_args(gt_tool)
    key_score = _arg_key_score(pred_args, gt_args)
    val_score = _arg_value_score(pred_args, gt_args)
    return 0.55 * name_score + 0.20 * key_score + 0.25 * val_score


def _greedy_match(
    pred_tools: list[Any],
    gt_tools: list[Any],
) -> list[tuple[Any, Any]]:
    remaining = list(range(len(pred_tools)))
    matches: list[tuple[Any, Any]] = []
    for gt_tool in gt_tools:
        gt_name = _tool_name(gt_tool)
        best_idx = None
        best_score = -1.0
        for idx in remaining:
            pred_tool = pred_tools[idx]
            pred_name = _tool_name(pred_tool)
            score = _match_score(pred_tool, gt_tool)
            same_name_bonus = 0.01 if pred_name == gt_name else 0.0
            if score + same_name_bonus > best_score:
                best_idx = idx
                best_score = score + same_name_bonus
        if best_idx is not None:
            matches.append((pred_tools[best_idx], gt_tool))
            remaining.remove(best_idx)
    return matches


def _duplicate_penalty(pred_tools: list[Any]) -> float:
    seen: dict[str, int] = {}
    duplicates = 0
    for tool in pred_tools:
        try:
            key = json.dumps(
                _canonicalize(tool),
                ensure_ascii=False,
                sort_keys=True,
            )
        except Exception:
            key = str(tool)
        seen[key] = seen.get(key, 0) + 1
    for count in seen.values():
        if count > 1:
            duplicates += count - 1
    return min(0.15, duplicates * 0.05)


def compute_score(data_source, solution_str, ground_truth, extra_info=None):
    """Return the dense Fathoming reward for a generated tool-call response."""
    del data_source, extra_info
    if not solution_str:
        return 0.0

    pred_tools, malformed = _extract_tool_calls(solution_str)
    gt_tools, _ = _extract_tool_calls(ground_truth)

    has_tag_pair = "<tool_call>" in solution_str and "</tool_call>" in solution_str
    format_score = 1.0 if has_tag_pair and pred_tools and malformed == 0 else 0.0

    if not gt_tools:
        return 0.1 * format_score

    matches = _greedy_match(pred_tools, gt_tools) if pred_tools else []
    matched_gt = len(
        [1 for pred, gt in matches if _tool_name(pred) == _tool_name(gt)]
    )
    tool_score = matched_gt / len(gt_tools)

    key_scores = []
    value_scores = []
    for pred_tool, gt_tool in matches:
        if _tool_name(pred_tool) != _tool_name(gt_tool):
            key_scores.append(0.0)
            value_scores.append(0.0)
            continue
        pred_args = _tool_args(pred_tool)
        gt_args = _tool_args(gt_tool)
        key_scores.append(_arg_key_score(pred_args, gt_args))
        value_scores.append(_arg_value_score(pred_args, gt_args))

    arg_key_score = sum(key_scores) / len(gt_tools) if gt_tools else 0.0
    arg_val_score = sum(value_scores) / len(gt_tools) if gt_tools else 0.0

    count_gap = abs(len(pred_tools) - len(gt_tools))
    count_score = max(0.0, 1.0 - count_gap / max(len(gt_tools), 1))

    extra_calls = max(0, len(pred_tools) - matched_gt)
    extra_penalty = min(0.20, extra_calls * 0.04)
    invalid_penalty = min(0.20, malformed * 0.10)
    duplicate_penalty = _duplicate_penalty(pred_tools)

    reward = (
        0.10 * format_score
        + 0.30 * tool_score
        + 0.25 * arg_key_score
        + 0.25 * arg_val_score
        + 0.10 * count_score
        - extra_penalty
        - invalid_penalty
        - duplicate_penalty
    )

    if pred_tools and gt_tools:
        exact_match = len(pred_tools) == len(gt_tools) and all(
            _canonicalize(pred) == _canonicalize(gt)
            for pred, gt in zip(pred_tools, gt_tools, strict=True)
        )
        if exact_match:
            reward = max(reward, 1.0)
        else:
            reward *= 0.5

    if math.isnan(reward) or math.isinf(reward):
        return 0.0
    return max(0.0, min(1.0, reward))
