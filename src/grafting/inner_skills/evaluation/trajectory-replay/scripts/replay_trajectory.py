"""Replay trajectory tool calls against a fresh environment copy.

Usage:
    python replay_trajectory.py \\
        --trajectory trajectory.json \\
        --env-dir env \\
        --output output/replay_result.json
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_backend(backend_path: Path, db_path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("_env_backend", backend_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from: {backend_path}")
    module: ModuleType = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EnvironmentBackend(db_path=db_path)


def _extract_tool_calls(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        for fc in msg.get("function_calls", []):
            tool_use_id = fc.get("tool_use_id", "")
            original_result = None
            for j in range(i + 1, len(messages)):
                candidate = messages[j]
                if (
                    candidate.get("role") == "function"
                    and candidate.get("tool_use_id") == tool_use_id
                ):
                    try:
                        original_result = json.loads(candidate["content"])
                    except (json.JSONDecodeError, KeyError):
                        original_result = candidate.get("content")
                    break
            calls.append({
                "tool_name": fc["name"],
                "arguments": fc.get("arguments", {}),
                "tool_use_id": tool_use_id,
                "original_result": original_result,
                "message_index": i,
            })
    return calls


def _extract_token(result: Any) -> str | None:
    """Extract access_token from a tool result if present."""
    if not isinstance(result, dict):
        return None
    data = result.get("data")
    if isinstance(data, dict):
        tok = data.get("access_token")
        if isinstance(tok, str) and tok:
            return tok
    return None


def _substitute_tokens(
    args: dict[str, Any],
    token_map: dict[str, str],
) -> dict[str, Any]:
    """Deep-copy args and replace any old token values with new ones."""
    if not token_map:
        return args
    return _substitute_recursive(copy.deepcopy(args), token_map)


def _substitute_recursive(obj: Any, token_map: dict[str, str]) -> Any:
    if isinstance(obj, str):
        return token_map.get(obj, obj)
    if isinstance(obj, dict):
        return {k: _substitute_recursive(v, token_map) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_recursive(item, token_map) for item in obj]
    return obj


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay trajectory tool calls")
    parser.add_argument("--trajectory", required=True, help="Path to trajectory.json")
    parser.add_argument("--env-dir", required=True, help="Path to environment directory")
    parser.add_argument("--output", required=True, help="Path to write replay_result.json")
    args = parser.parse_args()

    traj_path = Path(args.trajectory)
    env_dir = Path(args.env_dir)
    output_path = Path(args.output)

    trajectory = json.loads(traj_path.read_text(encoding="utf-8"))
    task = trajectory.get("task", {})
    messages = trajectory.get("messages", [])

    world_db = env_dir / "world" / "world.db"
    backend_py = env_dir / "runtime" / "backend.py"

    if not world_db.exists():
        print(f"ERROR: world.db not found: {world_db}", file=sys.stderr)
        return 1
    if not backend_py.exists():
        print(f"ERROR: backend.py not found: {backend_py}", file=sys.stderr)
        return 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    eval_db = output_path.parent / "eval.db"
    shutil.copy2(world_db, eval_db)

    world_setup = task.get("world_setup") or []
    if world_setup:
        conn = sqlite3.connect(str(eval_db))
        for stmt in world_setup:
            conn.execute(stmt)
        conn.commit()
        conn.close()

    backend = _load_backend(backend_py, eval_db)

    tool_calls = _extract_tool_calls(messages)

    results: list[dict[str, Any]] = []
    matched = 0
    mismatched = 0
    token_map: dict[str, str] = {}
    substitutions = 0

    for idx, tc in enumerate(tool_calls):
        patched_args = _substitute_tokens(tc["arguments"], token_map)
        was_substituted = patched_args != tc["arguments"]
        if was_substituted:
            substitutions += 1

        replay_result = backend.call(tc["tool_name"], patched_args)

        orig_token = _extract_token(tc["original_result"])
        replay_token = _extract_token(replay_result)
        if orig_token and replay_token and orig_token != replay_token:
            token_map[orig_token] = replay_token

        original = tc["original_result"]
        is_match = json.dumps(original, sort_keys=True) == json.dumps(
            replay_result, sort_keys=True,
        )
        if is_match:
            matched += 1
        else:
            mismatched += 1

        results.append({
            "index": idx,
            "tool_name": tc["tool_name"],
            "arguments": patched_args,
            "original_result": original,
            "replay_result": replay_result,
            "match": is_match,
        })

    output = {
        "calls": results,
        "total": len(results),
        "matched": matched,
        "mismatched": mismatched,
        "token_substitutions": substitutions,
        "eval_db": str(eval_db),
    }

    output_path.write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(
        f"Replay complete: {len(results)} calls, {matched} matched,"
        f" {mismatched} mismatched, {substitutions} token substitutions"
    )
    print(f"Eval DB: {eval_db}")
    print(f"Result: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
