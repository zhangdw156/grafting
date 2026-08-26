"""Generate a Markdown report from a probe database."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from grafting.utils import logger, setup_logging
from probe.db import THRESHOLD_TOO_EASY, ProbeDB


def _percentage(count: int, total: int) -> str:
    return f"{count / total * 100:.1f}%" if total else "0.0%"


def _format(value: float | None) -> str:
    return f"{value:.4f}" if value is not None else "N/A"


def generate_report(db: ProbeDB) -> str:
    """Render per-depth difficulty and capability-boundary statistics."""
    if db.has_incomplete_groups():
        raise ValueError("Probe database has incomplete rollout groups")
    lines = ["# Probe Report", ""]
    depth_stats = db.depth_summary()
    if not depth_stats:
        return "# Probe Report\n\nNo rollout data found."

    lines.extend(
        [
            "## Per-Depth Difficulty Distribution",
            "",
            "| Depth | Samples | too_easy | effective | hard | too_hard "
            "| mean_score | mean_pass_rate |",
            "|-------|---------|----------|-----------|------|----------"
            "|------------|----------------|",
        ]
    )
    for stats in depth_stats:
        total = stats["total_samples"]
        lines.append(
            f"| {stats['depth']} | {total} "
            f"| {_percentage(stats['too_easy'], total)} "
            f"| {_percentage(stats['effective'], total)} "
            f"| {_percentage(stats['hard'], total)} "
            f"| {_percentage(stats['too_hard'], total)} "
            f"| {_format(stats['mean_score'])} "
            f"| {_format(stats['mean_pass_rate'])} |"
        )
    lines.append("")

    boundaries = db.capability_boundaries()
    if boundaries:
        counts: dict[str | int, int] = {}
        for item in boundaries:
            boundary = item["boundary"]
            counts[boundary] = counts.get(boundary, 0) + 1
        total = len(boundaries)
        lines.extend(
            [
                "## Capability Boundary Distribution",
                "",
                f"Threshold: pass_rate >= {THRESHOLD_TOO_EASY}",
                "",
                "| Boundary Depth | Count | Percentage |",
                "|----------------|-------|------------|",
            ]
        )
        numeric = sorted(key for key in counts if isinstance(key, int))
        special = sorted(key for key in counts if isinstance(key, str))
        for key in [*numeric, *special]:
            lines.append(
                f"| {key} | {counts[key]} | {_percentage(counts[key], total)} |"
            )
        lines.append("")

    if len(depth_stats) > 1:
        lines.extend(
            [
                "## Cross-Depth Pass Rate Trend",
                "",
                "| Depth | mean_pass_rate |",
                "|-------|----------------|",
            ]
        )
        for stats in depth_stats:
            lines.append(
                f"| {stats['depth']} | {_format(stats['mean_pass_rate'])} |"
            )
        lines.append("")

    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    setup_logging(args.log_level)
    if not args.db_path.is_file():
        logger.error("Database not found: {}", args.db_path)
        return 1
    db = ProbeDB(args.db_path)
    try:
        try:
            report = generate_report(db)
        except ValueError as exc:
            logger.error("{}", exc)
            return 1
    finally:
        db.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    logger.info("Report written to {}", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
