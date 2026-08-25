#!/usr/bin/env python3
"""Build the ranked skill manifest from unified skill-filter results.

CLI (not registered in pyproject.toml)::

    uv run python -m grafting.skill.manifest \
        filter_result=<path> output_manifest=<path>

Parameters:
    filter_result: Input skill filter result JSON (default: artifacts/skill_filter_result.json).
    output_manifest: Output manifest JSONL (default: artifacts/skill_manifest.jsonl).
    output_summary: Optional summary output path (default: null).
    output_selected: Optional pre-selected output path (default: null).
    top_k_per_domain: Select top K skills per domain. 0 = no limit (default: 0).
    blocked_risk_flags: List of risk flags to exclude (default: []).

Examples::

    uv run python -m grafting.skill.manifest \
        filter_result=artifacts/skill_filter_result.json \
        output_manifest=artifacts/skill_manifest.jsonl
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import hydra
from hydra.utils import get_original_cwd
from omegaconf import DictConfig, OmegaConf

from grafting.skill._schema import (
    DEFAULT_BLOCKED_RISK_FLAGS,
    assign_ranks,
    build_manifest_record,
    select_top_k_per_domain,
    validate_filter_judgement,
    write_jsonl,
    write_summary,
)

__all__ = [
    "SkillManifestConfig",
    "config_from_cfg",
    "config_from_mapping",
    "read_filter_jsonl",
    "run_skill_manifest",
]


@dataclass(frozen=True, slots=True)
class SkillManifestConfig:
    filter_result: Path | None = Path("artifacts/skill_filter_result.json")
    output_manifest: Path | None = Path("artifacts/skill_manifest.jsonl")
    output_summary: Path | None = None
    output_selected: Path | None = None
    top_k_per_domain: int = 0
    blocked_risk_flags: tuple[str, ...] = ()

    def normalized(self) -> SkillManifestConfig:
        return SkillManifestConfig(
            filter_result=self.filter_result,
            output_manifest=self.output_manifest,
            output_summary=self.output_summary,
            output_selected=self.output_selected,
            top_k_per_domain=self.top_k_per_domain,
            blocked_risk_flags=tuple(
                flag.strip() for flag in self.blocked_risk_flags if flag.strip()
            ),
        )

    def validate_basic(self) -> list[str]:
        errors: list[str] = []
        if self.filter_result is None:
            errors.append("filter_result path is required")
        if self.output_manifest is None:
            errors.append("output_manifest path is required")
        if self.top_k_per_domain < 0:
            errors.append(f"top_k_per_domain cannot be negative: {self.top_k_per_domain}")
        blank_flags = [flag for flag in self.blocked_risk_flags if not flag.strip()]
        if blank_flags:
            errors.append("blocked_risk_flags entries must be non-empty strings")
        return errors


def read_filter_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        raise FileNotFoundError(f"file does not exist: {path}")
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_no}: {exc}") from exc
            if not isinstance(obj, dict):
                raise ValueError(f"JSONL line must be an object: {path}:{line_no}")
            validate_filter_judgement(obj)
            rows.append(obj)
    return rows


def _coerce_optional_path(value: object) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    return Path(text) if text else None


def _coerce_blocked_risk_flags(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, Sequence) and not isinstance(value, str):
        return tuple(str(item).strip() for item in value if str(item).strip())
    text = str(value).strip()
    return (text,) if text else ()


def config_from_mapping(raw: Mapping[str, object]) -> SkillManifestConfig:
    return SkillManifestConfig(
        filter_result=_coerce_optional_path(
            raw.get("filter_result", "artifacts/skill_filter_result.json")
        ),
        output_manifest=_coerce_optional_path(
            raw.get("output_manifest", "artifacts/skill_manifest.jsonl")
        ),
        output_summary=_coerce_optional_path(raw.get("output_summary")),
        output_selected=_coerce_optional_path(raw.get("output_selected")),
        top_k_per_domain=int(str(raw.get("top_k_per_domain") or 0)),
        blocked_risk_flags=_coerce_blocked_risk_flags(raw.get("blocked_risk_flags", ())),
    )


def config_from_cfg(cfg: DictConfig) -> SkillManifestConfig:
    raw = OmegaConf.to_container(cfg, resolve=True)
    if not isinstance(raw, dict):
        raise TypeError("skill manifest config must resolve to a mapping")
    return config_from_mapping({str(k): v for k, v in raw.items()})


def run_skill_manifest(config: SkillManifestConfig) -> int:
    config = config.normalized()
    errors = config.validate_basic()
    if errors:
        raise ValueError("invalid skill manifest config:\n- " + "\n- ".join(errors))

    try:
        base = Path(get_original_cwd())
    except ValueError:
        base = Path.cwd()

    def resolve_path(raw: Path | None) -> Path | None:
        if raw is None:
            return None
        path = raw
        if not path.is_absolute():
            path = (base / path).resolve()
        return path

    filter_path = resolve_path(config.filter_result)
    output_manifest = resolve_path(config.output_manifest)
    summary_path = resolve_path(config.output_summary)
    output_selected = resolve_path(config.output_selected)

    assert filter_path is not None
    assert output_manifest is not None

    filter_rows = read_filter_jsonl(filter_path)

    blocked_risk_flags = set(DEFAULT_BLOCKED_RISK_FLAGS)
    blocked_risk_flags.update(config.blocked_risk_flags)

    manifest_rows = [
        build_manifest_record(filter_row, blocked_risk_flags=blocked_risk_flags)
        for filter_row in filter_rows
    ]

    assign_ranks(manifest_rows)
    manifest_rows.sort(
        key=lambda row: (
            0 if row.get("eligible") else 1,
            row.get("primary_domain", ""),
            -(float(row.get("selection_score", 0.0))),
            row.get("dir_name", ""),
        )
    )

    write_jsonl(output_manifest, manifest_rows)

    if summary_path is None:
        summary_path = output_manifest.with_name("skill_manifest_summary.json")
    write_summary(summary_path, manifest_rows)

    if config.top_k_per_domain > 0 and output_selected is not None:
        selected = select_top_k_per_domain(
            manifest_rows,
            top_k_per_domain=config.top_k_per_domain,
        )
        selected.sort(
            key=lambda row: (
                row.get("primary_domain", ""),
                row.get("rank_within_primary_domain") or 10**9,
                row.get("dir_name", ""),
            )
        )
        write_jsonl(output_selected, selected)

    print(f"Wrote manifest: {output_manifest}")
    print(f"Wrote summary:  {summary_path}")
    if config.top_k_per_domain > 0 and output_selected is not None:
        print(f"Wrote selected: {output_selected}")
    return 0


@hydra.main(
    config_path=str(Path(__file__).resolve().parent.parent / "configs"),
    config_name="skill_manifest",
    version_base=None,
)
def main(cfg: DictConfig) -> None:
    raise SystemExit(run_skill_manifest(config_from_cfg(cfg)))


if __name__ == "__main__":
    main()
