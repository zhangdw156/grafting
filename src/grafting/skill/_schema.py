"""Manifest record schema utilities.

Pure data-processing helpers used by the manifest builder: validation,
scoring, ranking, and JSONL I/O.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

ALLOWED_EXEC_RISK_FLAGS = frozenset(
    {
        "external_http",
        "oauth",
        "browser_only",
        "hardware_io",
        "high_state_complexity",
        "safety_sensitive",
        "streaming",
        "long_running_job",
        "non_deterministic",
        "filesystem_heavy",
        "gui_only",
        "requires_real_credentials",
        "requires_live_account",
    }
)
_ALLOWED_DOMAIN_FRAMEWORKS = frozenset({"bfcl_v4", "tau2", "tau3", "none"})
DEFAULT_BLOCKED_RISK_FLAGS = frozenset({"oauth", "browser_only", "requires_live_account"})


def _skill_name_from_dirname(dirname: str) -> str:
    return re.sub(r"^\d+_", "", dirname).strip() or dirname


def clamp_float(value: Any, *, low: float, high: float, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def normalize_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _require_mapping(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"'{field}' must be an object")
    return value


def validate_filter_judgement(parsed: dict[str, Any]) -> None:
    errors: list[str] = []
    dir_name = parsed.get("dir_name")
    if not isinstance(dir_name, str) or not dir_name.strip():
        errors.append("'dir_name' must be a non-empty string")

    skill_relpath = parsed.get("skill_relpath")
    if not isinstance(skill_relpath, str) or not skill_relpath.strip():
        errors.append("'skill_relpath' must be a non-empty string")

    approved_for_copy = parsed.get("approved_for_copy")
    if not isinstance(approved_for_copy, bool):
        errors.append("'approved_for_copy' must be a bool")

    copy_reason = parsed.get("copy_reason")
    if not isinstance(copy_reason, str) or not copy_reason.strip():
        errors.append("'copy_reason' must be a non-empty string")

    skill_name = parsed.get("skill_name")
    if skill_name is not None and (not isinstance(skill_name, str) or not skill_name.strip()):
        errors.append("'skill_name' must be a non-empty string when provided")

    try:
        domain = _require_mapping(parsed.get("domain"), field="domain")
        executability = _require_mapping(parsed.get("executability"), field="executability")
    except ValueError as exc:
        errors.append(str(exc))
        domain = {}
        executability = {}

    if domain:
        framework = domain.get("framework")
        if framework not in _ALLOWED_DOMAIN_FRAMEWORKS:
            errors.append(f"'domain.framework' must be one of {sorted(_ALLOWED_DOMAIN_FRAMEWORKS)}")
        if not isinstance(domain.get("match"), bool):
            errors.append("'domain.match' must be a bool")
        elif framework == "none" and domain.get("match") is True:
            errors.append("'domain.match' must be false when 'domain.framework' is 'none'")
        reason = domain.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            errors.append("'domain.reason' must be a non-empty string")
        matched_domains = domain.get("matched_domains")
        if not isinstance(matched_domains, list) or not all(
            isinstance(item, str) for item in matched_domains
        ):
            errors.append("'domain.matched_domains' must be a list of strings")
        primary_domain = domain.get("primary_domain")
        if not isinstance(primary_domain, str):
            errors.append("'domain.primary_domain' must be a string")
        for field, low, high in (
            ("relevance_score", 0.0, 5.0),
            ("confidence", 0.0, 1.0),
            ("tool_call_intensity_score", 0.0, 5.0),
            ("multi_turn_potential_score", 0.0, 5.0),
        ):
            value = domain.get(field)
            if not isinstance(value, (int, float)):
                errors.append(f"'domain.{field}' must be a number")
            elif not (low <= float(value) <= high):
                errors.append(f"'domain.{field}' must be between {low} and {high}, got {value}")

    if executability:
        if not isinstance(executability.get("match"), bool):
            errors.append("'executability.match' must be a bool")
        reason = executability.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            errors.append("'executability.reason' must be a non-empty string")
        for field in (
            "mockability_score",
            "determinism_score",
            "schema_clarity_score",
            "state_complexity_score",
            "multi_turn_fitness_score",
            "expected_data_yield_score",
        ):
            value = executability.get(field)
            if not isinstance(value, (int, float)):
                errors.append(f"'executability.{field}' must be a number")
            elif not (0.0 <= float(value) <= 5.0):
                errors.append(f"'executability.{field}' must be between 0.0 and 5.0, got {value}")
        risk_flags = executability.get("risk_flags")
        if not isinstance(risk_flags, list) or not all(
            isinstance(item, str) for item in risk_flags
        ):
            errors.append("'executability.risk_flags' must be a list of strings")
        else:
            invalid_flags = sorted(
                {flag for flag in risk_flags if flag not in ALLOWED_EXEC_RISK_FLAGS}
            )
            if invalid_flags:
                errors.append(
                    "'executability.risk_flags' contains unsupported values: "
                    + ", ".join(invalid_flags)
                )

    if errors:
        raise ValueError("invalid unified skill filter judgement: " + "; ".join(errors))


def build_manifest_record(
    filter_row: dict[str, Any],
    *,
    blocked_risk_flags: set[str],
) -> dict[str, Any]:
    domain = dict(filter_row.get("domain") or {})
    executability = dict(filter_row.get("executability") or {})
    dir_name = str(filter_row.get("dir_name", "")).strip()
    skill_name = str(
        filter_row.get("skill_name") or _skill_name_from_dirname(dir_name)
    ).strip()
    skill_relpath = str(filter_row.get("skill_relpath", dir_name)).strip() or dir_name

    matched_domains = normalize_str_list(domain.get("matched_domains", []))
    primary_domain = str(domain.get("primary_domain", "")).strip()
    risk_flags = normalize_str_list(executability.get("risk_flags", []))
    approved_for_copy = bool(filter_row.get("approved_for_copy", False))
    copy_reason = str(filter_row.get("copy_reason", "")).strip()

    record: dict[str, Any] = {
        "dir_name": dir_name,
        "skill_relpath": skill_relpath,
        "skill_name": skill_name,
        "approved_for_copy": approved_for_copy,
        "copy_reason": copy_reason,
        "domain_framework": str(domain.get("framework", "")).strip(),
        "domain_match": bool(domain.get("match", False)),
        "domain_reason": str(domain.get("reason", "")).strip(),
        "matched_domains": matched_domains,
        "primary_domain": primary_domain,
        "bfcl_relevance_score": clamp_float(domain.get("relevance_score"), low=0.0, high=5.0),
        "domain_confidence": clamp_float(domain.get("confidence"), low=0.0, high=1.0),
        "tool_call_intensity_score": clamp_float(
            domain.get("tool_call_intensity_score"),
            low=0.0,
            high=5.0,
        ),
        "multi_turn_potential_score": clamp_float(
            domain.get("multi_turn_potential_score"),
            low=0.0,
            high=5.0,
        ),
        "executability_match": bool(executability.get("match", False)),
        "executability_reason": str(executability.get("reason", "")).strip(),
        "mockability_score": clamp_float(
            executability.get("mockability_score"),
            low=0.0,
            high=5.0,
        ),
        "determinism_score": clamp_float(
            executability.get("determinism_score"),
            low=0.0,
            high=5.0,
        ),
        "schema_clarity_score": clamp_float(
            executability.get("schema_clarity_score"),
            low=0.0,
            high=5.0,
        ),
        "state_complexity_score": clamp_float(
            executability.get("state_complexity_score"),
            low=0.0,
            high=5.0,
        ),
        "multi_turn_fitness_score": clamp_float(
            executability.get("multi_turn_fitness_score"),
            low=0.0,
            high=5.0,
        ),
        "expected_data_yield_score": clamp_float(
            executability.get("expected_data_yield_score"),
            low=0.0,
            high=5.0,
        ),
        "risk_flags": risk_flags,
    }

    blocked_hits = [flag for flag in risk_flags if flag in blocked_risk_flags]
    eligible = all(
        [
            approved_for_copy,
            record["domain_match"],
            record["executability_match"],
            record["bfcl_relevance_score"] >= 3.5,
            record["mockability_score"] >= 3.5,
            record["determinism_score"] >= 3.0,
            record["schema_clarity_score"] >= 3.5,
            record["state_complexity_score"] <= 3.5,
            bool(record["primary_domain"]),
            len(blocked_hits) == 0,
        ]
    )

    selection_score = (
        0.30 * record["bfcl_relevance_score"]
        + 0.20 * record["mockability_score"]
        + 0.15 * record["determinism_score"]
        + 0.15 * record["schema_clarity_score"]
        + 0.10 * record["tool_call_intensity_score"]
        + 0.10 * record["expected_data_yield_score"]
        + 0.05 * record["multi_turn_potential_score"]
        + 0.05 * record["multi_turn_fitness_score"]
        - 0.15 * record["state_complexity_score"]
    )

    notes: list[str] = []
    if record["approved_for_copy"]:
        notes.append("approved for copy")
    if record["bfcl_relevance_score"] >= 4.0:
        notes.append("high domain relevance")
    if record["mockability_score"] >= 4.0:
        notes.append("high mockability")
    if record["schema_clarity_score"] >= 4.0:
        notes.append("clear schema potential")
    if record["state_complexity_score"] <= 2.0:
        notes.append("low state complexity")
    if blocked_hits:
        notes.append(f"blocked by risk flags: {', '.join(blocked_hits)}")
    if not notes:
        notes.append("mixed profile")

    record.update(
        {
            "eligible": eligible,
            "selection_score": round(selection_score, 6),
            "selection_notes": "; ".join(notes),
            "rank_within_primary_domain": None,
            "rank_global": None,
        }
    )
    return record


def assign_ranks(records: list[dict[str, Any]]) -> None:
    eligible_records = [record for record in records if record.get("eligible")]
    eligible_records.sort(
        key=lambda record: (
            float(record.get("selection_score", 0.0)),
            float(record.get("bfcl_relevance_score", 0.0)),
            float(record.get("mockability_score", 0.0)),
            float(record.get("schema_clarity_score", 0.0)),
        ),
        reverse=True,
    )
    for index, record in enumerate(eligible_records, 1):
        record["rank_global"] = index

    domain_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in eligible_records:
        primary_domain = str(record.get("primary_domain", "")).strip()
        if primary_domain:
            domain_groups[primary_domain].append(record)

    for group in domain_groups.values():
        group.sort(
            key=lambda record: (
                float(record.get("selection_score", 0.0)),
                float(record.get("bfcl_relevance_score", 0.0)),
                float(record.get("mockability_score", 0.0)),
            ),
            reverse=True,
        )
        for index, record in enumerate(group, 1):
            record["rank_within_primary_domain"] = index


def select_top_k_per_domain(
    rows: list[dict[str, Any]],
    *,
    top_k_per_domain: int,
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not row.get("eligible"):
            continue
        domain = str(row.get("primary_domain", "")).strip()
        if not domain:
            continue
        groups[domain].append(row)

    selected: list[dict[str, Any]] = []
    for _, group in sorted(groups.items()):
        group.sort(
            key=lambda row: (
                float(row.get("selection_score", 0.0)),
                float(row.get("bfcl_relevance_score", 0.0)),
                float(row.get("mockability_score", 0.0)),
            ),
            reverse=True,
        )
        selected.extend(group[:top_k_per_domain])
    return selected


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_summary(path: Path, rows: list[dict[str, Any]]) -> None:
    domain_buckets: dict[str, int] = defaultdict(int)
    eligible_count = 0
    approved_count = 0
    for row in rows:
        if row.get("approved_for_copy"):
            approved_count += 1
        if row.get("eligible"):
            eligible_count += 1
            domain = str(row.get("primary_domain", "")).strip()
            if domain:
                domain_buckets[domain] += 1

    top_global = [
        {
            "dir_name": row.get("dir_name", ""),
            "skill_relpath": row.get("skill_relpath", ""),
            "primary_domain": row.get("primary_domain", ""),
            "selection_score": row.get("selection_score", 0.0),
            "rank_global": row.get("rank_global"),
        }
        for row in rows
        if row.get("eligible") and row.get("rank_global") is not None and row["rank_global"] <= 20
    ]

    summary = {
        "total": len(rows),
        "approved_for_copy": approved_count,
        "eligible": eligible_count,
        "ineligible": len(rows) - eligible_count,
        "eligible_by_primary_domain": dict(sorted(domain_buckets.items())),
        "top_global": top_global,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
