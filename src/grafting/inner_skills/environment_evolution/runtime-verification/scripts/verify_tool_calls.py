"""Verify environment backend by calling each tool with minimal arguments.

Usage:
    python verify_tool_calls.py --env-dir env --output output/verification_result.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path


def _load_backend(env_dir: Path, db_path: Path):
    backend_path = env_dir / "runtime" / "backend.py"
    spec = importlib.util.spec_from_file_location("_env_backend", str(backend_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load backend from {backend_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.EnvironmentBackend(db_path=db_path)


def _minimal_value(schema_type: str):
    return {
        "string": "",
        "integer": 0,
        "number": 0.0,
        "boolean": False,
        "object": {},
        "array": [],
    }.get(schema_type, "")


def _build_minimal_args(tool_def: dict) -> dict:
    schema = tool_def.get("inputSchema", {})
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    args = {}
    for name, prop in props.items():
        if name in required:
            args[name] = _minimal_value(prop.get("type", "string"))
    return args


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-dir", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    env_dir = Path(args.env_dir)
    tools_path = env_dir / "spec" / "tools.jsonl"
    world_db = env_dir / "world" / "world.db"

    tools = []
    with tools_path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                tools.append(json.loads(line))

    with tempfile.TemporaryDirectory() as tmp:
        test_db = Path(tmp) / "test.db"
        shutil.copy2(world_db, test_db)

        try:
            backend = _load_backend(env_dir, test_db)
        except Exception as exc:
            result = {
                "total_tools": len(tools),
                "tested": 0,
                "passed": 0,
                "failed": len(tools),
                "visible_state_ok": False,
                "errors": [{
                    "tool_name": "_load",
                    "error_code": "LOAD_ERROR",
                    "error_message": str(exc),
                    "arguments": {},
                }],
            }
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(json.dumps(result, indent=2, ensure_ascii=False))
            return 1

        passed = 0
        failed = 0
        errors = []

        for tool in tools:
            name = tool["name"]
            minimal_args = _build_minimal_args(tool)
            try:
                r = backend.call(name, minimal_args)
            except Exception as exc:
                failed += 1
                errors.append({
                    "tool_name": name,
                    "error_code": "EXCEPTION",
                    "error_message": str(exc),
                    "arguments": minimal_args,
                })
                continue

            error_code = ""
            if isinstance(r, dict) and not r.get("success", True):
                err = r.get("error", {})
                if isinstance(err, dict):
                    error_code = err.get("code", "")

            if error_code in ("INTERNAL_ERROR", "UNKNOWN_TOOL"):
                failed += 1
                errors.append({
                    "tool_name": name,
                    "error_code": error_code,
                    "error_message": r.get("error", {}).get("message", ""),
                    "arguments": minimal_args,
                })
            else:
                passed += 1

        visible_state_ok = True
        try:
            vs = backend.visible_state()
            if not isinstance(vs, dict):
                visible_state_ok = False
        except Exception:
            visible_state_ok = False

    result = {
        "total_tools": len(tools),
        "tested": len(tools),
        "passed": passed,
        "failed": failed,
        "visible_state_ok": visible_state_ok,
        "errors": errors,
    }

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, indent=2, ensure_ascii=False))

    if failed > 0:
        print(f"FAIL: {failed}/{len(tools)} tools have errors", file=sys.stderr)
        return 1
    print(f"PASS: {passed}/{len(tools)} tools OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
