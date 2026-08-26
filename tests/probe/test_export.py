from __future__ import annotations

import json

from probe.export import export_selected, select_pairs


def test_select_pairs_keeps_effective_and_samples_hard() -> None:
    pairs = {
        "too_easy": [(999, 1)],
        "effective": [(0, 1), (1, 1)],
        "hard": [(index, 1) for index in range(2, 12)],
        "too_hard": [(998, 1)],
    }
    selected = select_pairs(pairs, hard_ratio=0.2, seed=42)
    assert {(0, 1), (1, 1)} <= selected
    assert len(selected) == 4
    assert (999, 1) not in selected
    assert (998, 1) not in selected


def test_export_selected(tmp_path) -> None:
    data_dir = tmp_path / "probe"
    data_dir.mkdir()
    depth_path = data_dir / "depth_1.jsonl"
    depth_path.write_text(
        "".join(
            json.dumps(
                {
                    "sample_id": sample_id,
                    "messages": [{"role": "assistant", "content": str(sample_id)}],
                }
            )
            + "\n"
            for sample_id in range(3)
        )
    )
    manifest = {
        "input": "input.jsonl",
        "total_samples": 3,
        "max_depth": 1,
        "depths": {"1": {"kept": 3, "path": "depth_1.jsonl"}},
    }
    output = tmp_path / "train.jsonl"

    written = export_selected(data_dir, manifest, {(1, 1), (2, 1)}, output)

    assert written == 2
    records = [json.loads(line) for line in output.read_text().splitlines()]
    assert [record["messages"][-1]["content"] for record in records] == ["1", "2"]
    assert all(list(record) == ["messages"] for record in records)
