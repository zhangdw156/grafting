"""Export effective and sampled hard probe slices for GRPO training."""

from __future__ import annotations

import argparse
import json
import random
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from grafting.utils import logger, setup_logging
from probe.db import ProbeDB
from probe.prepare import verify_prepared_data


def select_pairs(
    pairs: dict[str, list[tuple[int, int]]],
    *,
    hard_ratio: float,
    seed: int,
) -> set[tuple[int, int]]:
    """Keep all effective pairs and a deterministic sample of hard pairs."""
    if not 0.0 <= hard_ratio <= 1.0:
        raise ValueError("hard_ratio must be between 0 and 1")
    selected = set(pairs.get("effective", []))
    hard = pairs.get("hard", [])
    if hard_ratio >= 1.0:
        selected.update(hard)
    elif hard_ratio > 0 and hard:
        count = max(1, int(len(hard) * hard_ratio))
        selected.update(random.Random(seed).sample(hard, count))
    return selected


def export_selected(
    data_dir: Path,
    manifest: dict[str, Any],
    selected: set[tuple[int, int]],
    output: Path,
) -> int:
    """Write selected slices as message-only JSONL records."""
    output.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with output.open("w", encoding="utf-8") as destination:
        for depth_text, info in manifest["depths"].items():
            depth = int(depth_text)
            depth_path = data_dir / info["path"]
            if not depth_path.is_file():
                raise FileNotFoundError(depth_path)
            with depth_path.open(encoding="utf-8") as source:
                for line in source:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    if (record["sample_id"], depth) not in selected:
                        continue
                    destination.write(
                        json.dumps(
                            {"messages": record["messages"]},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    written += 1
    return written


def export_training_data(
    db_path: Path,
    data_dir: Path,
    output: Path,
    *,
    hard_ratio: float,
    seed: int,
) -> int:
    manifest_path = data_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fingerprint = verify_prepared_data(data_dir, manifest)
    db = ProbeDB(db_path)
    try:
        db.bind_dataset(fingerprint)
        if db.has_incomplete_groups():
            raise ValueError("Probe database has incomplete rollout groups")
        selected = select_pairs(
            db.difficulty_pairs(),
            hard_ratio=hard_ratio,
            seed=seed,
        )
    finally:
        db.close()
    return export_selected(data_dir, manifest, selected, output)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path", type=Path)
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--hard", type=float, default=0.1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    setup_logging(args.log_level)
    if not args.db_path.is_file():
        logger.error("Database not found: {}", args.db_path)
        return 1
    try:
        written = export_training_data(
            args.db_path,
            args.data_dir,
            args.output,
            hard_ratio=args.hard,
            seed=args.seed,
        )
    except (FileNotFoundError, ValueError) as exc:
        logger.error("{}", exc)
        return 1
    logger.info("Wrote {} training records to {}", written, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
