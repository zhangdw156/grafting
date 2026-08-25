"""批量导出用：基于 evaluations.sqlite3 过滤已通过的轨迹。"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class EvalFilterConfig:
    verdict: str = "accepted"
    limit: int | None = None


def iter_accepted_trajectories(
    *,
    eval_dir: Path,
    traj_dir: Path,
    cfg: EvalFilterConfig,
) -> Iterable[tuple[str, Path, str]]:
    """Yield (trajectory_id, traj_path, env_name) for accepted evaluations."""
    db_path = eval_dir / "evaluations.sqlite3"
    if not db_path.exists():
        raise FileNotFoundError(f"evaluations.sqlite3 not found in {eval_dir}")
    conn = sqlite3.connect(str(db_path))
    try:
        sql = (
            "SELECT trajectory_id, env_name FROM evaluations"
            " WHERE verdict = ? AND status = 'succeeded'"
            " ORDER BY trajectory_id"
        )
        params: list[object] = [cfg.verdict]
        if cfg.limit is not None and cfg.limit > 0:
            sql += " LIMIT ?"
            params.append(cfg.limit)
        for trajectory_id, env_name in conn.execute(sql, params):
            traj_path = traj_dir / (str(trajectory_id) + ".json")
            yield str(trajectory_id), traj_path, str(env_name)
    finally:
        conn.close()


def truncate_after_last_tool_call(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """保留到最后一轮 assistant tool call 为止；若无或 tool response 失败则返回 None。"""
    last_tool_call_index: int | None = None
    for i, msg in enumerate(messages):
        role = (msg.get("role") or "").strip()
        content = msg.get("content")
        if role == "assistant" and content and "<tool_call>" in str(content):
            last_tool_call_index = i
    if last_tool_call_index is None:
        return None
    if _has_failed_response(messages, last_tool_call_index):
        return None
    return messages[: last_tool_call_index + 1]


def truncate_at_each_tool_call(
    messages: list[dict[str, Any]],
    *,
    keep_ratio: float = 1.0,
) -> list[list[dict[str, Any]]]:
    """为每个 tool call 生成一条截断，跳过 tool response 失败的。

    *keep_ratio* (0, 1] controls what fraction of tool-call truncations to
    keep, counted from the end.  ``1.0`` keeps all (``truncate=all``),
    ``0.5`` keeps the latter half (``truncate=half``), ``0.2`` keeps the
    last 20 %, etc.
    """
    results: list[list[dict[str, Any]]] = []
    for i, msg in enumerate(messages):
        role = (msg.get("role") or "").strip()
        content = msg.get("content")
        if role != "assistant" or not content or "<tool_call>" not in str(content):
            continue
        if _has_failed_response(messages, i):
            continue
        results.append(messages[: i + 1])
    if keep_ratio < 1.0 and len(results) > 1:
        skip = len(results) - max(1, round(len(results) * keep_ratio))
        results = results[skip:]
    return results


def _has_failed_response(messages: list[dict[str, Any]], tool_call_idx: int) -> bool:
    """Check if any tool_response following a tool_call has success=false."""
    for j in range(tool_call_idx + 1, len(messages)):
        msg = messages[j]
        role = (msg.get("role") or "").strip()
        content = str(msg.get("content", ""))
        if role == "user" and "<tool_response>" in content:
            match = re.search(
                r"<tool_response>\n(.*?)\n</tool_response>", content, re.DOTALL,
            )
            if match:
                try:
                    data = json.loads(match.group(1))
                    if not data.get("success", True):
                        return True
                except (json.JSONDecodeError, ValueError):
                    pass
        elif role == "assistant":
            break
    return False
