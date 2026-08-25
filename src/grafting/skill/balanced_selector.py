"""Balanced skill selection — select skills with domain-balanced sampling.

CLI (not registered in pyproject.toml)::

    uv run python -m grafting.skill.balanced_selector \
        input_manifest=<path> output_selected=<path>

Parameters:
    input_manifest: Input skill manifest JSONL (default: artifacts/skill_manifest.jsonl).
    output_selected: Output selected skills JSONL
        (default: artifacts/skill_manifest_balanced.jsonl).
    output_summary: Optional summary output path (default: null).
    target_count: Target number of skills to select (default: 300).
    balance_strength: Domain balancing strength, 0-1 (default: 1.0).
    eligible_only: Only select eligible skills (default: true).

Examples::

    uv run python -m grafting.skill.balanced_selector \
        input_manifest=artifacts/skill_manifest.jsonl \
        output_selected=artifacts/balanced_300.jsonl \
        target_count=300
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import hydra
from hydra.utils import get_original_cwd
from omegaconf import DictConfig, OmegaConf


def _coerce_optional_path(value: object) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    return Path(text) if text else None


@dataclass(frozen=True, slots=True)
class SkillBalancedSelectorConfig:
    input_manifest: Path | None = Path("artifacts/skill_manifest.jsonl")
    output_selected: Path | None = Path("artifacts/skill_manifest_balanced.jsonl")
    output_summary: Path | None = None
    target_count: int = 300
    balance_strength: float = 1.0
    eligible_only: bool = True

    def normalized(self) -> SkillBalancedSelectorConfig:
        return SkillBalancedSelectorConfig(
            input_manifest=self.input_manifest,
            output_selected=self.output_selected,
            output_summary=self.output_summary,
            target_count=self.target_count,
            balance_strength=float(self.balance_strength),
            eligible_only=bool(self.eligible_only),
        )

    def validate_basic(self) -> list[str]:
        errors: list[str] = []
        if self.input_manifest is None:
            errors.append("input_manifest path is required")
        if self.output_selected is None:
            errors.append("output_selected path is required")
        if self.target_count < 0:
            errors.append(f"target_count cannot be negative: {self.target_count}")
        if self.balance_strength < 0:
            errors.append(f"balance_strength cannot be negative: {self.balance_strength}")
        return errors


def config_from_mapping(raw: Mapping[str, object]) -> SkillBalancedSelectorConfig:
    return SkillBalancedSelectorConfig(
        input_manifest=_coerce_optional_path(
            raw.get("input_manifest", "artifacts/skill_manifest.jsonl")
        ),
        output_selected=_coerce_optional_path(
            raw.get("output_selected", "artifacts/skill_manifest_balanced.jsonl")
        ),
        output_summary=_coerce_optional_path(raw.get("output_summary")),
        target_count=int(str(raw.get("target_count") or 300)),
        balance_strength=float(str(raw.get("balance_strength") or 1.0)),
        eligible_only=bool(raw.get("eligible_only", True)),
    )


def config_from_cfg(cfg: DictConfig) -> SkillBalancedSelectorConfig:
    raw = OmegaConf.to_container(cfg, resolve=True)
    if not isinstance(raw, dict):
        raise TypeError("skill balanced selector config must resolve to a mapping")
    return config_from_mapping({str(k): v for k, v in raw.items()})


def read_manifest_jsonl(path: Path) -> list[dict[str, Any]]:
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
            rows.append(obj)
    return rows


def write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _sort_key(row: dict[str, Any]) -> tuple[float, float, float, str]:
    return (
        float(row.get("selection_score", 0.0)),
        float(row.get("bfcl_relevance_score", 0.0)),
        float(row.get("mockability_score", 0.0)),
        str(row.get("dir_name", "")),
    )


def select_balanced_manifest_rows(
    rows: Sequence[dict[str, Any]],
    *,
    target_count: int,
    balance_strength: float,
    eligible_only: bool,
) -> list[dict[str, Any]]:
    if target_count <= 0:
        return []

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if eligible_only and not row.get("eligible"):
            continue
        domain = str(row.get("primary_domain", "")).strip()
        if not domain:
            continue
        grouped[domain].append(dict(row))

    for group in grouped.values():
        group.sort(key=_sort_key, reverse=True)

    selected: list[dict[str, Any]] = []
    selected_counts: Counter[str] = Counter()

    while len(selected) < target_count:
        best_domain: str | None = None
        best_candidate: dict[str, Any] | None = None
        best_effective_score: float | None = None

        for domain, group in grouped.items():
            if not group:
                continue
            candidate = group[0]
            raw_score = float(candidate.get("selection_score", 0.0))
            effective_score = raw_score / (1.0 + balance_strength * selected_counts[domain])
            if best_candidate is None:
                best_domain = domain
                best_candidate = candidate
                best_effective_score = effective_score
                continue
            assert best_effective_score is not None
            if effective_score > best_effective_score:
                best_domain = domain
                best_candidate = candidate
                best_effective_score = effective_score
                continue
            if effective_score == best_effective_score:
                current_count = selected_counts[domain]
                best_count = selected_counts[str(best_domain)]
                candidate_key = (current_count, *_sort_key(candidate))
                best_key = (best_count, *_sort_key(best_candidate))
                if candidate_key > best_key:
                    best_domain = domain
                    best_candidate = candidate
                    best_effective_score = effective_score

        if best_domain is None or best_candidate is None:
            break

        grouped[best_domain].pop(0)
        selected_counts[best_domain] += 1

        chosen = dict(best_candidate)
        chosen["balanced_selector_domain_pick_index"] = selected_counts[best_domain]
        chosen["balanced_selector_effective_score"] = round(
            float(best_effective_score or 0.0),
            6,
        )
        chosen["balanced_selector_order"] = len(selected) + 1
        selected.append(chosen)

    return selected


def build_selection_summary(
    source_rows: Sequence[dict[str, Any]],
    selected_rows: Sequence[dict[str, Any]],
    *,
    target_count: int,
    balance_strength: float,
    eligible_only: bool,
) -> dict[str, Any]:
    source_domain_counts: Counter[str] = Counter()
    eligible_pool = 0
    for row in source_rows:
        if eligible_only and not row.get("eligible"):
            continue
        domain = str(row.get("primary_domain", "")).strip()
        if not domain:
            continue
        eligible_pool += 1
        source_domain_counts[domain] += 1

    selected_domain_counts: Counter[str] = Counter(
        str(row.get("primary_domain", "")).strip()
        for row in selected_rows
        if row.get("primary_domain")
    )

    top_selected = [
        {
            "dir_name": row.get("dir_name", ""),
            "skill_relpath": row.get("skill_relpath", ""),
            "primary_domain": row.get("primary_domain", ""),
            "selection_score": row.get("selection_score", 0.0),
            "balanced_selector_effective_score": row.get(
                "balanced_selector_effective_score",
                0.0,
            ),
            "balanced_selector_order": row.get("balanced_selector_order"),
        }
        for row in selected_rows[:20]
    ]

    return {
        "target_count": target_count,
        "selected_count": len(selected_rows),
        "eligible_pool": eligible_pool,
        "balance_strength": balance_strength,
        "eligible_only": eligible_only,
        "domains_in_pool": len(source_domain_counts),
        "domains_selected": len(selected_domain_counts),
        "selected_by_primary_domain": dict(sorted(selected_domain_counts.items())),
        "pool_by_primary_domain": dict(sorted(source_domain_counts.items())),
        "top_selected": top_selected,
    }


def run_skill_balanced_selector(config: SkillBalancedSelectorConfig) -> int:
    config = config.normalized()
    errors = config.validate_basic()
    if errors:
        raise ValueError("invalid skill balanced selector config:\n- " + "\n- ".join(errors))

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

    input_manifest = resolve_path(config.input_manifest)
    output_selected = resolve_path(config.output_selected)
    summary_path = resolve_path(config.output_summary)

    assert input_manifest is not None
    assert output_selected is not None

    manifest_rows = read_manifest_jsonl(input_manifest)
    selected_rows = select_balanced_manifest_rows(
        manifest_rows,
        target_count=config.target_count,
        balance_strength=config.balance_strength,
        eligible_only=config.eligible_only,
    )
    selected_rows.sort(key=lambda row: int(row.get("balanced_selector_order", 10**9)))
    write_jsonl(output_selected, selected_rows)

    if summary_path is None:
        summary_path = output_selected.with_name("skill_manifest_balanced_summary.json")
    summary = build_selection_summary(
        manifest_rows,
        selected_rows,
        target_count=config.target_count,
        balance_strength=config.balance_strength,
        eligible_only=config.eligible_only,
    )
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Wrote balanced selection: {output_selected}")
    print(f"Wrote summary:            {summary_path}")
    return 0


@hydra.main(
    config_path=str(Path(__file__).resolve().parent.parent / "configs"),
    config_name="skill_select_balanced",
    version_base=None,
)
def main(cfg: DictConfig) -> None:
    raise SystemExit(run_skill_balanced_selector(config_from_cfg(cfg)))


if __name__ == "__main__":
    main()
