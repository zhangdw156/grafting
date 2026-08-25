"""Export accepted trajectories as Qwen3 SFT JSONL."""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omegaconf import DictConfig, OmegaConf

from grafting.export._filter import (
    EvalFilterConfig,
    iter_accepted_trajectories,
    truncate_after_last_tool_call,
    truncate_at_each_tool_call,
)
from grafting.export.qwen3.core import build_output_record
from grafting.utils import config as grafting_config
from grafting.utils import create_progress, logger

__all__ = [
    "DatasetExportConfig",
    "config_from_cfg",
    "config_from_mapping",
    "export_dataset_sft",
    "run_dataset_export",
]

_config_path = Path(__file__).resolve().parent.parent / "configs"


_TRUNCATE_KEYWORDS = frozenset({"no", "last", "half", "all"})


def _parse_truncate(value: str) -> str | float:
    """Return a keyword or a float ratio in (0, 1)."""
    if value in _TRUNCATE_KEYWORDS:
        return value
    try:
        ratio = float(value)
    except ValueError:
        return value
    return ratio


def _validate_truncate(value: str) -> str | None:
    """Return an error message if *value* is not a valid truncate spec."""
    parsed = _parse_truncate(value)
    if isinstance(parsed, str):
        if parsed in _TRUNCATE_KEYWORDS:
            return None
        return (
            f"truncate must be one of {sorted(_TRUNCATE_KEYWORDS)} "
            f"or a decimal in (0, 1): {value!r}"
        )
    if not (0 < parsed < 1):
        return f"truncate ratio must be in (0, 1): {value!r}"
    return None


@dataclass(frozen=True, slots=True)
class DatasetExportConfig:
    traj_dir: Path | None = None
    eval_dir: Path | None = None
    output_path: Path | None = None
    limit: int | None = None
    dry_run: bool = False
    truncate: str = "no"

    def normalized(self) -> DatasetExportConfig:
        return DatasetExportConfig(
            traj_dir=self.traj_dir.resolve() if self.traj_dir else None,
            eval_dir=self.eval_dir.resolve() if self.eval_dir else None,
            output_path=self.output_path.resolve() if self.output_path else None,
            limit=self.limit,
            dry_run=self.dry_run,
            truncate=self.truncate,
        )

    def build_filter_config(self) -> EvalFilterConfig:
        return EvalFilterConfig(limit=self.limit)

    def validate_basic(self) -> list[str]:
        errors: list[str] = []
        if self.traj_dir is None:
            errors.append("traj_dir is required")
        if self.eval_dir is None:
            errors.append("eval_dir is required")
        if not self.dry_run and self.output_path is None:
            errors.append("output is required when dry_run is false")
        if self.limit is not None and self.limit <= 0:
            errors.append(f"limit must be positive when provided: {self.limit}")
        if self.truncate not in _TRUNCATE_KEYWORDS:
            err = _validate_truncate(self.truncate)
            if err:
                errors.append(err)
        return errors


def export_dataset_sft(
    *,
    traj_dir: Path,
    eval_dir: Path,
    output_path: Path | None,
    filter_config: EvalFilterConfig,
    truncate: str = "no",
    dry_run: bool = False,
) -> dict[str, Any]:
    if not eval_dir.exists():
        raise FileNotFoundError(f"eval_dir does not exist: {eval_dir}")
    if not traj_dir.exists():
        raise FileNotFoundError(f"traj_dir does not exist: {traj_dir}")
    if not dry_run and output_path is None:
        raise ValueError("output is required when dry_run is false")

    if dry_run:
        return _export_dry_run(
            traj_dir=traj_dir, eval_dir=eval_dir, filter_config=filter_config,
        )

    return _export_streaming(
        traj_dir=traj_dir,
        eval_dir=eval_dir,
        output_path=output_path,  # type: ignore[arg-type]
        filter_config=filter_config,
        truncate=truncate,
    )


def _export_dry_run(
    *, traj_dir: Path, eval_dir: Path, filter_config: EvalFilterConfig,
) -> dict[str, Any]:
    total = 0
    rows: list[tuple[str, Path]] = []
    for traj_id, traj_path, _env in iter_accepted_trajectories(
        eval_dir=eval_dir, traj_dir=traj_dir, cfg=filter_config,
    ):
        total += 1
        rows.append((traj_id, traj_path))
    return {"total": total, "rows": rows}


def _export_streaming(
    *,
    traj_dir: Path,
    eval_dir: Path,
    output_path: Path,
    filter_config: EvalFilterConfig,
    truncate: str = "no",
) -> dict[str, Any]:
    trajectories = iter_accepted_trajectories(
        eval_dir=eval_dir, traj_dir=traj_dir, cfg=filter_config,
    )
    return _stream_from_iter(
        trajectories=trajectories,
        output_path=output_path,
        truncate=truncate,
    )


def _stream_from_iter(
    *,
    trajectories: Iterable[tuple[str, Path, str]],
    output_path: Path,
    truncate: str = "no",
) -> dict[str, Any]:
    root = grafting_config.get_project_root()
    total = 0
    written = 0
    skipped_missing_traj = 0
    skipped_bad_json = 0
    skipped_convert_error = 0
    skipped_no_tool_call = 0

    out_path = output_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with create_progress() as progress, out_path.open("w", encoding="utf-8") as out_f:
        ptask = progress.add_task("Exporting", total=None)
        for _traj_id, traj_path, _env in trajectories:
            total += 1
            progress.advance(ptask)
            if not traj_path.exists():
                skipped_missing_traj += 1
                continue
            try:
                trajectory = json.loads(traj_path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Bad JSON in {}: {}", traj_path, exc)
                skipped_bad_json += 1
                continue
            try:
                rel_path = traj_path.resolve().relative_to(root)
                source_path_str = str(rel_path)
            except ValueError:
                source_path_str = str(traj_path)
            try:
                record = build_output_record(trajectory, source_path_str)
            except Exception as exc:
                logger.warning("Converter error for {}: {}", traj_path, exc)
                skipped_convert_error += 1
                continue
            messages = record.get("messages")
            if not isinstance(messages, list):
                skipped_missing_traj += 1
                continue
            parsed = _parse_truncate(truncate)
            if parsed == "no":
                out_f.write(json.dumps({"messages": messages}, ensure_ascii=False) + "\n")
                written += 1
            elif parsed == "last":
                messages = truncate_after_last_tool_call(messages)
                if messages is None:
                    skipped_no_tool_call += 1
                    continue
                out_f.write(json.dumps({"messages": messages}, ensure_ascii=False) + "\n")
                written += 1
            else:
                if parsed == "half":
                    keep_ratio = 0.5
                elif parsed == "all":
                    keep_ratio = 1.0
                else:
                    keep_ratio = float(parsed)
                truncations = truncate_at_each_tool_call(
                    messages, keep_ratio=keep_ratio,
                )
                if not truncations:
                    skipped_no_tool_call += 1
                    continue
                for truncated in truncations:
                    out_f.write(
                        json.dumps({"messages": truncated}, ensure_ascii=False) + "\n"
                    )
                    written += 1

    return {
        "total": total,
        "written": written,
        "skipped_missing_traj": skipped_missing_traj,
        "skipped_bad_json": skipped_bad_json,
        "skipped_convert_error": skipped_convert_error,
        "skipped_no_tool_call": skipped_no_tool_call,
        "output_path": out_path,
    }


def config_from_mapping(raw: Mapping[str, object]) -> DatasetExportConfig:
    return DatasetExportConfig(
        traj_dir=_coerce_optional_path(raw.get("traj_dir")),
        eval_dir=_coerce_optional_path(raw.get("eval_dir")),
        output_path=_coerce_optional_path(raw.get("output")),
        limit=_coerce_optional_positive_int(raw.get("limit")),
        dry_run=bool(raw.get("dry_run", False)),
        truncate=str(raw.get("truncate", "no") or "no"),
    )


def config_from_cfg(cfg: DictConfig) -> DatasetExportConfig:
    raw = OmegaConf.to_container(cfg, resolve=True)
    if not isinstance(raw, dict):
        raise TypeError("dataset config must resolve to a mapping")
    return config_from_mapping({str(k): v for k, v in raw.items()})


def run_dataset_export(config: DatasetExportConfig) -> int:
    config = config.normalized()
    errors = config.validate_basic()
    if errors:
        raise ValueError("invalid dataset export config:\n- " + "\n- ".join(errors))

    result = export_dataset_sft(
        traj_dir=config.traj_dir,  # type: ignore[arg-type]
        eval_dir=config.eval_dir,  # type: ignore[arg-type]
        output_path=config.output_path,
        filter_config=config.build_filter_config(),
        truncate=config.truncate,
        dry_run=config.dry_run,
    )

    if config.dry_run:
        print(f"[dry-run] accepted trajectories: {result['total']}")
        for traj_id, traj_path in result["rows"]:
            print(f"  {traj_id}\t{traj_path}")
        return 0

    print(f"Accepted trajectories: {result['total']}")
    print(f"Written JSONL lines: {result['written']}")
    if result.get("skipped_missing_traj", 0):
        print(f"Missing trajectories: {result['skipped_missing_traj']}")
    if result.get("skipped_bad_json", 0):
        print(f"Bad JSON: {result['skipped_bad_json']}")
    if result.get("skipped_convert_error", 0):
        print(f"Converter errors: {result['skipped_convert_error']}")
    if config.truncate != "no" and result.get("skipped_no_tool_call", 0):
        print(f"Skipped (no tool call): {result['skipped_no_tool_call']}")
    print(f"Output: {result['output_path']}")
    return 0


def _coerce_optional_path(value: object) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    return Path(text) if text else None


def _coerce_optional_positive_int(value: object) -> int | None:
    if value is None:
        return None
    number = int(str(value))
    return number if number > 0 else None
