"""Run OpenAI-compatible model rollouts over prepared probe slices."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from openai import OpenAI

from grafting.utils import create_progress, logger, setup_logging
from probe.db import EXPECTED_ROLLOUTS, ProbeDB
from probe.prepare import verify_prepared_data
from probe.scorer import score_response


def _detect_model(client: OpenAI) -> str | None:
    try:
        models = client.models.list()
    except Exception:
        return None
    return models.data[0].id if models.data else None


def _load_tasks(
    data_dir: Path,
    manifest: dict[str, Any],
) -> list[tuple[int, int, list[dict[str, Any]], str]]:
    tasks: list[tuple[int, int, list[dict[str, Any]], str]] = []
    for depth_text, info in manifest["depths"].items():
        depth = int(depth_text)
        depth_path = data_dir / info["path"]
        if not depth_path.is_file():
            raise FileNotFoundError(depth_path)
        with depth_path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                messages = record["messages"]
                tasks.append(
                    (
                        record["sample_id"],
                        depth,
                        messages[:-1],
                        messages[-1]["content"],
                    )
                )
    return tasks


def do_rollout(
    client: Any,
    *,
    model: str,
    prompt: list[dict[str, Any]],
    ground_truth: str,
    temperature: float,
    max_tokens: int,
) -> dict[str, Any]:
    """Run and score one completion without raising request errors."""
    try:
        response = client.chat.completions.create(
            model=model,
            messages=prompt,
            n=1,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        text = response.choices[0].message.content or ""
        format_score, correctness_score = score_response(text, ground_truth)
        return {
            "format_score": format_score,
            "correctness_score": correctness_score,
            "total_score": format_score + correctness_score,
            "response": text,
            "status": "success",
            "error_msg": None,
        }
    except Exception as exc:
        is_timeout = "timeout" in type(exc).__name__.lower()
        return {
            "format_score": None,
            "correctness_score": None,
            "total_score": None,
            "response": None,
            "status": "timeout" if is_timeout else "error",
            "error_msg": str(exc)[:500],
        }


def run_rollouts(
    data_dir: Path,
    db_path: Path,
    *,
    base_url: str,
    model: str | None,
    temperature: float,
    max_tokens: int,
    workers: int,
    timeout: float,
) -> tuple[int, int]:
    """Run pending rollouts and return ``(succeeded, failed)``."""
    manifest_path = data_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fingerprint = verify_prepared_data(data_dir, manifest)

    client = OpenAI(
        base_url=base_url,
        api_key=os.getenv("OPENAI_API_KEY", "EMPTY"),
        timeout=timeout,
    )
    model_name = model or _detect_model(client)
    if model_name is None:
        raise ValueError("Could not detect a model; pass --model explicitly")

    tasks = _load_tasks(data_dir, manifest)
    db = ProbeDB(db_path)
    try:
        db.bind_dataset(fingerprint)
        db.bind_expected_groups(
            {(sample_id, depth) for sample_id, depth, _, _ in tasks}
        )
        completed = db.get_completed()
        pending: list[
            tuple[int, int, int, list[dict[str, Any]], str]
        ] = []
        for sample_id, depth, prompt, ground_truth in tasks:
            for rollout_index in range(EXPECTED_ROLLOUTS):
                if (sample_id, depth, rollout_index) not in completed:
                    pending.append(
                        (
                            sample_id,
                            depth,
                            rollout_index,
                            prompt,
                            ground_truth,
                        )
                    )

        if not pending:
            return 0, 0

        succeeded = 0
        failed = 0

        def run_one(
            item: tuple[int, int, int, list[dict[str, Any]], str],
        ) -> tuple[int, int, int, dict[str, Any]]:
            sample_id, depth, rollout_index, prompt, ground_truth = item
            result = do_rollout(
                client,
                model=model_name,
                prompt=prompt,
                ground_truth=ground_truth,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return sample_id, depth, rollout_index, result

        with (
            create_progress() as progress,
            ThreadPoolExecutor(max_workers=workers) as pool,
        ):
            futures = [pool.submit(run_one, item) for item in pending]
            task = progress.add_task("Rolling out", total=len(futures))
            for future in as_completed(futures):
                sample_id, depth, rollout_index, result = future.result()
                db.insert_rollout(
                    sample_id=sample_id,
                    depth=depth,
                    rollout_index=rollout_index,
                    format_score=result["format_score"],
                    correctness_score=result["correctness_score"],
                    total_score=result["total_score"],
                    response=result["response"],
                    status=result["status"],
                    error_msg=result["error_msg"],
                )
                if result["status"] == "success":
                    succeeded += 1
                else:
                    failed += 1
                progress.advance(task)
        return succeeded, failed
    finally:
        db.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("db_path", type=Path)
    parser.add_argument("--base-url", default="http://localhost:8000/v1")
    parser.add_argument("--model")
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    setup_logging(args.log_level)
    if args.workers < 1:
        logger.error("--workers must be positive")
        return 1
    try:
        succeeded, failed = run_rollouts(
            args.data_dir,
            args.db_path,
            base_url=args.base_url,
            model=args.model,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            workers=args.workers,
            timeout=args.timeout,
        )
    except (FileNotFoundError, ValueError) as exc:
        logger.error("{}", exc)
        return 1
    logger.info("Probe rollout complete: {} succeeded, {} failed", succeeded, failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
