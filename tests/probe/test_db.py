from __future__ import annotations

import pytest

from probe.db import ProbeDB, classify_difficulty


def _insert(
    db: ProbeDB,
    *,
    sample_id: int,
    depth: int,
    rollout_index: int,
    format_score: float,
    correctness_score: float,
) -> None:
    db.insert_rollout(
        sample_id=sample_id,
        depth=depth,
        rollout_index=rollout_index,
        format_score=format_score,
        correctness_score=correctness_score,
        total_score=format_score + correctness_score,
        response="response",
        status="success",
        error_msg=None,
    )


def test_classify_difficulty() -> None:
    assert classify_difficulty(1.0, 1.0) == "too_easy"
    assert classify_difficulty(0.875, 0.5) == "too_easy"
    assert classify_difficulty(0.5, 0.8) == "effective"
    assert classify_difficulty(0.0, 0.5) == "hard"
    assert classify_difficulty(0.0, 0.0) == "too_hard"


def test_completed_summary_and_boundary(tmp_path) -> None:
    db = ProbeDB(tmp_path / "probe.db")
    for rollout_index in range(8):
        _insert(
            db,
            sample_id=0,
            depth=1,
            rollout_index=rollout_index,
            format_score=0.1,
            correctness_score=1.0,
        )
        _insert(
            db,
            sample_id=0,
            depth=2,
            rollout_index=rollout_index,
            format_score=0.1,
            correctness_score=1.0 if rollout_index < 3 else 0.0,
        )

    assert len(db.get_completed()) == 16
    summary = db.depth_summary()
    assert summary[0]["too_easy"] == 1
    assert summary[1]["effective"] == 1
    assert db.capability_boundaries() == [{"sample_id": 0, "boundary": 2}]
    db.close()


def test_difficulty_pairs(tmp_path) -> None:
    db = ProbeDB(tmp_path / "probe.db")
    for sample_id, correct_count, format_score in (
        (0, 8, 0.1),
        (1, 4, 0.1),
        (2, 0, 0.1),
        (3, 0, 0.0),
    ):
        for rollout_index in range(8):
            _insert(
                db,
                sample_id=sample_id,
                depth=1,
                rollout_index=rollout_index,
                format_score=format_score,
                correctness_score=1.0 if rollout_index < correct_count else 0.0,
            )

    pairs = db.difficulty_pairs()
    assert pairs == {
        "too_easy": [(0, 1)],
        "effective": [(1, 1)],
        "hard": [(2, 1)],
        "too_hard": [(3, 1)],
    }
    db.close()


def test_incomplete_or_failed_rollouts_are_not_completed_or_classified(
    tmp_path,
) -> None:
    db = ProbeDB(tmp_path / "probe.db")
    _insert(
        db,
        sample_id=0,
        depth=1,
        rollout_index=0,
        format_score=0.1,
        correctness_score=1.0,
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

    assert db.get_completed() == {(0, 1, 0)}
    assert db.has_incomplete_groups()
    assert db.depth_summary() == []
    assert db.difficulty_pairs() == {
        "too_easy": [],
        "effective": [],
        "hard": [],
        "too_hard": [],
    }
    db.close()


def test_populated_legacy_db_cannot_bind_new_dataset(tmp_path) -> None:
    db = ProbeDB(tmp_path / "probe.db")
    _insert(
        db,
        sample_id=0,
        depth=1,
        rollout_index=0,
        format_score=0.1,
        correctness_score=1.0,
    )
    with pytest.raises(ValueError, match="already contains rollouts"):
        db.bind_dataset("new-dataset")
    db.close()


def test_entirely_missing_expected_group_is_incomplete(tmp_path) -> None:
    db = ProbeDB(tmp_path / "probe.db")
    db.bind_dataset("dataset")
    db.bind_expected_groups({(0, 1), (1, 1)})
    for rollout_index in range(8):
        _insert(
            db,
            sample_id=0,
            depth=1,
            rollout_index=rollout_index,
            format_score=0.1,
            correctness_score=1.0,
        )

    assert db.has_incomplete_groups()
    db.close()
