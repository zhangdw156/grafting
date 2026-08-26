from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from probe.db import ProbeDB
from probe.prepare import prepare_all_depths
from probe.report import generate_report
from probe.rollout import do_rollout, main, run_rollouts


class _Completions:
    def create(self, **kwargs):
        del kwargs
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            '<tool_call>{"name": "search", '
                            '"arguments": {"q": "x"}}</tool_call>'
                        )
                    )
                )
            ]
        )


class _Client:
    chat = SimpleNamespace(completions=_Completions())


def test_do_rollout_with_fake_client() -> None:
    ground_truth = (
        '<tool_call>{"name": "search", "arguments": {"q": "x"}}</tool_call>'
    )
    result = do_rollout(
        _Client(),
        model="model",
        prompt=[{"role": "user", "content": "Search"}],
        ground_truth=ground_truth,
        temperature=1.0,
        max_tokens=128,
    )
    assert result["status"] == "success"
    assert result["format_score"] == 0.1
    assert result["correctness_score"] == 1.0


def test_generate_report(tmp_path) -> None:
    db = ProbeDB(tmp_path / "probe.db")
    db.bind_dataset("dataset")
    db.bind_expected_groups({(0, 1)})
    for rollout_index in range(8):
        db.insert_rollout(
            sample_id=0,
            depth=1,
            rollout_index=rollout_index,
            format_score=0.1,
            correctness_score=1.0,
            total_score=1.1,
            response="response",
            status="success",
            error_msg=None,
        )
    report = generate_report(db)
    db.close()
    assert "# Probe Report" in report
    assert "Per-Depth Difficulty Distribution" in report
    assert "| 1 | 1 | 100.0%" in report


def test_rollout_cli_returns_nonzero_when_requests_fail(monkeypatch) -> None:
    monkeypatch.setattr(
        "probe.rollout.run_rollouts",
        lambda *args, **kwargs: (7, 1),
    )
    assert main(["data", "probe.db"]) == 1


def test_run_rollouts_retries_failed_indices(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    input_path = tmp_path / "input.jsonl"
    input_path.write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "system", "content": "System"},
                    {"role": "user", "content": "Search"},
                    {
                        "role": "assistant",
                        "content": (
                            '<tool_call>{"name": "search", '
                            '"arguments": {"q": "x"}}</tool_call>'
                        ),
                    },
                ]
            }
        )
        + "\n"
    )
    manifest = prepare_all_depths(input_path, data_dir)

    db_path = tmp_path / "probe.db"
    db = ProbeDB(db_path)
    db.bind_dataset(manifest["fingerprint"])
    db.insert_rollout(
        sample_id=0,
        depth=1,
        rollout_index=0,
        format_score=0.1,
        correctness_score=1.0,
        total_score=1.1,
        response="ok",
        status="success",
        error_msg=None,
    )
    for rollout_index in range(1, 8):
        db.insert_rollout(
            sample_id=0,
            depth=1,
            rollout_index=rollout_index,
            format_score=None,
            correctness_score=None,
            total_score=None,
            response=None,
            status="timeout",
            error_msg="timeout",
        )
    db.close()

    calls = 0

    def fake_rollout(*args, **kwargs):
        nonlocal calls
        del args, kwargs
        calls += 1
        return {
            "format_score": 0.1,
            "correctness_score": 1.0,
            "total_score": 1.1,
            "response": "ok",
            "status": "success",
            "error_msg": None,
        }

    monkeypatch.setattr("probe.rollout.OpenAI", lambda **kwargs: object())
    monkeypatch.setattr("probe.rollout.do_rollout", fake_rollout)

    succeeded, failed = run_rollouts(
        data_dir,
        db_path,
        base_url="http://localhost:8000/v1",
        model="model",
        temperature=1.0,
        max_tokens=128,
        workers=2,
        timeout=1,
    )

    assert (succeeded, failed) == (7, 0)
    assert calls == 7
    db = ProbeDB(db_path)
    assert len(db.get_completed()) == 8
    assert not db.has_incomplete_groups()
    db.close()


def test_run_rollouts_rejects_changed_prepared_dataset(
    tmp_path,
    monkeypatch,
) -> None:
    data_dir = tmp_path / "data"
    input_path = tmp_path / "new.jsonl"
    input_path.write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "system", "content": "System"},
                    {"role": "user", "content": "No tool call"},
                    {"role": "assistant", "content": "Done"},
                ]
            }
        )
        + "\n"
    )
    prepare_all_depths(input_path, data_dir)
    db_path = tmp_path / "probe.db"
    db = ProbeDB(db_path)
    db.bind_dataset("old-dataset")
    db.close()
    monkeypatch.setattr("probe.rollout.OpenAI", lambda **kwargs: object())

    with pytest.raises(ValueError, match="different prepared dataset"):
        run_rollouts(
            data_dir,
            db_path,
            base_url="http://localhost:8000/v1",
            model="model",
            temperature=1.0,
            max_tokens=128,
            workers=1,
            timeout=1,
        )


def test_run_rollouts_rejects_tampered_slice_file(tmp_path, monkeypatch) -> None:
    input_path = tmp_path / "input.jsonl"
    input_path.write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "system", "content": "System"},
                    {"role": "user", "content": "Task"},
                    {
                        "role": "assistant",
                        "content": (
                            '<tool_call>{"name": "a", "arguments": {}}</tool_call>'
                        ),
                    },
                ]
            }
        )
        + "\n"
    )
    data_dir = tmp_path / "probe"
    prepare_all_depths(input_path, data_dir)
    (data_dir / "depth_1.jsonl").write_text("tampered\n")
    monkeypatch.setattr("probe.rollout.OpenAI", lambda **kwargs: object())

    with pytest.raises(ValueError, match="checksum"):
        run_rollouts(
            data_dir,
            tmp_path / "probe.db",
            base_url="http://localhost:8000/v1",
            model="model",
            temperature=1.0,
            max_tokens=128,
            workers=1,
            timeout=1,
        )
