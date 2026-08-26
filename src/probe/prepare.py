"""Prepare tool-call-depth slices from accepted trajectory JSONL."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from grafting.utils import create_progress, logger, setup_logging

_TOOL_RESPONSE_RE = re.compile(
    r"<tool_response>(.*?)</tool_response>",
    re.DOTALL,
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dataset_fingerprint(
    source_sha256: str,
    depth_hashes: dict[str, str],
) -> str:
    payload = {
        "source_sha256": source_sha256,
        "depths": dict(sorted(depth_hashes.items())),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def verify_prepared_data(data_dir: Path, manifest: dict[str, Any]) -> str:
    """Verify slice checksums and return the prepared dataset fingerprint."""
    source_sha256 = manifest.get("source_sha256")
    expected_fingerprint = manifest.get("fingerprint")
    depths = manifest.get("depths")
    if (
        not isinstance(source_sha256, str)
        or not isinstance(expected_fingerprint, str)
        or not isinstance(depths, dict)
    ):
        raise ValueError("manifest.json is missing fingerprint metadata")

    depth_hashes: dict[str, str] = {}
    for depth, info in depths.items():
        if not isinstance(info, dict):
            raise ValueError(f"Invalid manifest entry for depth {depth}")
        expected_hash = info.get("sha256")
        relative_path = info.get("path")
        if not isinstance(expected_hash, str) or not isinstance(relative_path, str):
            raise ValueError(f"Invalid checksum metadata for depth {depth}")
        depth_path = data_dir / relative_path
        if not depth_path.is_file():
            raise FileNotFoundError(depth_path)
        actual_hash = _sha256_file(depth_path)
        if actual_hash != expected_hash:
            raise ValueError(f"Slice checksum mismatch: {depth_path}")
        depth_hashes[str(depth)] = actual_hash

    actual_fingerprint = _dataset_fingerprint(source_sha256, depth_hashes)
    if actual_fingerprint != expected_fingerprint:
        raise ValueError("Prepared dataset fingerprint mismatch")
    return actual_fingerprint


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc


def _is_tool_call(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return (
        message.get("role") == "assistant"
        and isinstance(content, str)
        and "<tool_call>" in content
    )


def _tool_call_indices(messages: list[dict[str, Any]]) -> list[int]:
    return [index for index, message in enumerate(messages) if _is_tool_call(message)]


def _immediate_response_failed(
    messages: list[dict[str, Any]],
    tool_call_index: int,
) -> bool:
    for message in messages[tool_call_index + 1 :]:
        content = message.get("content")
        responses = (
            _TOOL_RESPONSE_RE.findall(content) if isinstance(content, str) else []
        )
        if responses:
            for response in responses:
                try:
                    result = json.loads(response)
                except json.JSONDecodeError:
                    continue
                if isinstance(result, dict) and result.get("success") is False:
                    return True
            continue
        if message.get("role") in {"user", "assistant"}:
            return False
    return False


def _slice_record(
    record: dict[str, Any],
    *,
    depth_from_end: int,
) -> dict[str, Any] | None:
    messages = record.get("messages", [])
    if len(messages) < 3:
        return None
    indices = _tool_call_indices(messages)
    if depth_from_end > len(indices):
        return None
    tool_call_index = indices[-depth_from_end]
    if _immediate_response_failed(messages, tool_call_index):
        return None
    return {"messages": messages[: tool_call_index + 1]}


def count_tool_calls(record: dict[str, Any]) -> int:
    """Count assistant tool-call messages in one trajectory."""
    return len(_tool_call_indices(record.get("messages", [])))


def prepare_all_depths(input_path: Path, output_dir: Path) -> dict[str, Any]:
    """Write one JSONL slice file for every tool-call depth."""
    records = list(_read_jsonl(input_path))
    source_sha256 = _sha256_file(input_path)
    max_depth = max((count_tool_calls(record) for record in records), default=0)
    output_dir.mkdir(parents=True, exist_ok=True)

    depths: dict[str, dict[str, Any]] = {}
    with create_progress() as progress:
        task = progress.add_task("Slicing", total=max_depth)
        for depth in range(1, max_depth + 1):
            depth_path = output_dir / f"depth_{depth}.jsonl"
            kept = 0
            with depth_path.open("w", encoding="utf-8") as handle:
                for sample_id, record in enumerate(records):
                    sliced = _slice_record(record, depth_from_end=depth)
                    if sliced is None:
                        continue
                    sliced["sample_id"] = sample_id
                    handle.write(json.dumps(sliced, ensure_ascii=False) + "\n")
                    kept += 1
            depths[str(depth)] = {
                "kept": kept,
                "path": depth_path.name,
                "sha256": _sha256_file(depth_path),
            }
            progress.advance(task)

    fingerprint = _dataset_fingerprint(
        source_sha256,
        {depth: info["sha256"] for depth, info in depths.items()},
    )
    manifest: dict[str, Any] = {
        "input": str(input_path),
        "source_sha256": source_sha256,
        "fingerprint": fingerprint,
        "total_samples": len(records),
        "max_depth": max_depth,
        "depths": depths,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Accepted trajectory JSONL")
    parser.add_argument("output_dir", type=Path, help="Probe slice directory")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    setup_logging(args.log_level)
    if not args.input.is_file():
        logger.error("Input file does not exist: {}", args.input)
        return 1
    manifest = prepare_all_depths(args.input, args.output_dir)
    logger.info(
        "Prepared {} samples across {} depths in {}",
        manifest["total_samples"],
        manifest["max_depth"],
        args.output_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
