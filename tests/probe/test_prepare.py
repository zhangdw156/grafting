from __future__ import annotations

import json

from probe.prepare import count_tool_calls, prepare_all_depths


def _record(tool_count: int, *, fail_last: bool = False) -> dict:
    messages = [
        {"role": "system", "content": "System"},
        {"role": "user", "content": "Task"},
    ]
    for index in range(tool_count):
        messages.append(
            {
                "role": "assistant",
                "content": (
                    f'<tool_call>{{"name": "tool_{index}", '
                    '"arguments": {}}</tool_call>'
                ),
            }
        )
        failed = fail_last and index == tool_count - 1
        messages.append(
            {
                "role": "user",
                "content": (
                    f'<tool_response>{{"success": {str(not failed).lower()}}}'
                    "</tool_response>"
                ),
            }
        )
    messages.append({"role": "assistant", "content": "Done"})
    return {"messages": messages}


def test_prepare_all_depths_and_stable_sample_ids(tmp_path) -> None:
    input_path = tmp_path / "input.jsonl"
    records = [_record(3), _record(0), _record(2)]
    input_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    output_dir = tmp_path / "probe"
    manifest = prepare_all_depths(input_path, output_dir)

    assert manifest["total_samples"] == 3
    assert manifest["max_depth"] == 3
    assert len(manifest["fingerprint"]) == 64
    assert len(manifest["source_sha256"]) == 64
    depth_one = [
        json.loads(line)
        for line in (output_dir / "depth_1.jsonl").read_text().splitlines()
    ]
    assert {record["sample_id"] for record in depth_one} == {0, 2}
    assert len((output_dir / "depth_3.jsonl").read_text().splitlines()) == 1
    assert json.loads((output_dir / "manifest.json").read_text()) == manifest


def test_prepare_filters_failed_tool_call(tmp_path) -> None:
    input_path = tmp_path / "input.jsonl"
    input_path.write_text(json.dumps(_record(1, fail_last=True)) + "\n")

    output_dir = tmp_path / "probe"
    manifest = prepare_all_depths(input_path, output_dir)

    assert count_tool_calls(_record(1, fail_last=True)) == 1
    assert manifest["depths"]["1"]["kept"] == 0
    assert len(manifest["depths"]["1"]["sha256"]) == 64
    assert (output_dir / "depth_1.jsonl").read_text() == ""


def test_prepare_filters_parallel_calls_when_any_response_fails(tmp_path) -> None:
    record = {
        "messages": [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Task"},
            {
                "role": "assistant",
                "content": (
                    '<tool_call>{"name": "a", "arguments": {}}</tool_call>'
                    '<tool_call>{"name": "b", "arguments": {}}</tool_call>'
                ),
            },
            {
                "role": "user",
                "content": (
                    '<tool_response>{"success": true}</tool_response>'
                    '<tool_response>{"success": false}</tool_response>'
                ),
            },
        ]
    }
    input_path = tmp_path / "input.jsonl"
    input_path.write_text(json.dumps(record) + "\n")
    output_dir = tmp_path / "probe"

    manifest = prepare_all_depths(input_path, output_dir)

    assert manifest["depths"]["1"]["kept"] == 0
