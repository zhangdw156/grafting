"""Qwen3 SFT dataset export — batch export accepted trajectories to JSONL.

CLI::

    uv run grafting.export.qwen3 traj_dir=<dir> eval_dir=<dir> output=<path>

Parameters:
    traj_dir (required): Directory with trajectory JSON files.
    eval_dir (required): Directory with evaluation SQLite databases.
    output (required): Output JSONL file path.
    limit: Maximum number of trajectories to export. 0 = no limit (default: 0).
    dry_run: Only count accepted trajectories without writing (default: false).
    truncate: Truncation mode — "no", "last", "half", "all", or a decimal
        ratio in (0, 1) (default: no).

Examples::

    uv run grafting.export.qwen3 \
        traj_dir=results/trajectories \
        eval_dir=results/evaluations \
        output=datasets/qwen3_export.jsonl

    uv run grafting.export.qwen3 \
        traj_dir=results/trajectories \
        eval_dir=results/evaluations \
        output=datasets/qwen3_tc.jsonl \
        truncate=last
"""
from __future__ import annotations

import sys
from pathlib import Path

import hydra
from omegaconf import DictConfig

from grafting.export.qwen3.core import build_output_record
from grafting.utils import exit_with_code, setup_logging

__all__ = [
    "build_output_record",
    "main",
]

_config_path = Path(__file__).resolve().parent.parent.parent / "configs"


@hydra.main(
    config_path=str(_config_path),
    config_name="dataset",
    version_base=None,
)
@exit_with_code
def main(cfg: DictConfig) -> int:
    setup_logging(cfg.log_level)
    from grafting.export.dataset import config_from_cfg, run_dataset_export

    try:
        return run_dataset_export(config_from_cfg(cfg))
    except (FileNotFoundError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
