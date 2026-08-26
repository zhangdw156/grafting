"""SQLite storage and difficulty classification for probe rollouts."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS rollouts (
    sample_id          INTEGER NOT NULL,
    depth              INTEGER NOT NULL,
    rollout_index      INTEGER NOT NULL,
    format_score       REAL,
    correctness_score  REAL,
    total_score        REAL,
    response           TEXT,
    status             TEXT NOT NULL,
    error_msg          TEXT,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (sample_id, depth, rollout_index)
);
CREATE TABLE IF NOT EXISTS metadata (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expected_groups (
    sample_id  INTEGER NOT NULL,
    depth      INTEGER NOT NULL,
    PRIMARY KEY (sample_id, depth)
);
"""

THRESHOLD_TOO_EASY = 0.875
EXPECTED_ROLLOUTS = 8


def classify_difficulty(pass_rate: float, format_rate: float) -> str:
    """Classify a sample-depth pair from rollout pass and format rates."""
    if pass_rate >= THRESHOLD_TOO_EASY:
        return "too_easy"
    if pass_rate > 0:
        return "effective"
    if format_rate > 0:
        return "hard"
    return "too_hard"


class ProbeDB:
    """Thread-local SQLite access for resumable probe rollouts."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_table(self._conn())

    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    @staticmethod
    def _init_table(conn: sqlite3.Connection) -> None:
        conn.executescript(_SCHEMA)

    def get_completed(self) -> set[tuple[int, int, int]]:
        rows = self._conn().execute(
            "SELECT sample_id, depth, rollout_index FROM rollouts "
            "WHERE status = 'success'"
        )
        return {(row[0], row[1], row[2]) for row in rows.fetchall()}

    def bind_dataset(self, fingerprint: str) -> None:
        """Bind this database to one exact prepared probe dataset."""
        conn = self._conn()
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = 'dataset_fingerprint'"
        ).fetchone()
        if row is None:
            if conn.execute("SELECT 1 FROM rollouts LIMIT 1").fetchone() is not None:
                raise ValueError(
                    "Probe database already contains rollouts but has no "
                    "dataset fingerprint"
                )
            conn.execute(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                ("dataset_fingerprint", fingerprint),
            )
            conn.commit()
            return
        if row["value"] != fingerprint:
            raise ValueError(
                "Probe database belongs to a different prepared dataset"
            )

    def bind_expected_groups(self, groups: set[tuple[int, int]]) -> None:
        """Persist the exact sample-depth groups required for completion."""
        conn = self._conn()
        existing = {
            (row["sample_id"], row["depth"])
            for row in conn.execute(
                "SELECT sample_id, depth FROM expected_groups"
            ).fetchall()
        }
        marker = conn.execute(
            "SELECT 1 FROM metadata WHERE key = 'expected_groups_bound'"
        ).fetchone()
        if marker is not None:
            if existing != groups:
                raise ValueError(
                    "Probe database has a different expected group set"
                )
            return
        if existing:
            raise ValueError(
                "Probe database has expected groups without binding metadata"
            )
        conn.executemany(
            "INSERT INTO expected_groups (sample_id, depth) VALUES (?, ?)",
            sorted(groups),
        )
        conn.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            ("expected_groups_bound", "1"),
        )
        conn.commit()

    def has_incomplete_groups(self) -> bool:
        """Return whether any expected group lacks exactly 8 successes."""
        if (
            self._conn()
            .execute(
                "SELECT 1 FROM metadata WHERE key = 'expected_groups_bound'"
            )
            .fetchone()
            is None
        ):
            return True
        row = self._conn().execute(
            """\
            SELECT 1
            FROM expected_groups AS expected
            LEFT JOIN rollouts AS rollout
              ON rollout.sample_id = expected.sample_id
             AND rollout.depth = expected.depth
            GROUP BY expected.sample_id, expected.depth
            HAVING SUM(
                CASE WHEN rollout.status='success' THEN 1 ELSE 0 END
            ) != ?
            LIMIT 1
            """,
            (EXPECTED_ROLLOUTS,),
        ).fetchone()
        return row is not None

    def insert_rollout(
        self,
        *,
        sample_id: int,
        depth: int,
        rollout_index: int,
        format_score: float | None,
        correctness_score: float | None,
        total_score: float | None,
        response: str | None,
        status: str,
        error_msg: str | None,
    ) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO rollouts "
            "(sample_id, depth, rollout_index, format_score, correctness_score, "
            "total_score, response, status, error_msg) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                sample_id,
                depth,
                rollout_index,
                format_score,
                correctness_score,
                total_score,
                response,
                status,
                error_msg,
            ),
        )
        conn.commit()

    def depth_summary(self) -> list[dict[str, Any]]:
        by_depth: dict[int, list[dict[str, Any]]] = {}
        for profile in self.trajectory_profiles():
            by_depth.setdefault(profile["depth"], []).append(profile)

        result: list[dict[str, Any]] = []
        for depth, profiles in sorted(by_depth.items()):
            counts = {
                "too_easy": 0,
                "effective": 0,
                "hard": 0,
                "too_hard": 0,
            }
            for profile in profiles:
                counts[profile["difficulty"]] += 1
            total = len(profiles)
            result.append(
                {
                    "depth": depth,
                    "total_samples": total,
                    "mean_score": sum(
                        profile["mean_score"] for profile in profiles
                    )
                    / total,
                    "mean_format": sum(
                        profile["mean_format"] for profile in profiles
                    )
                    / total,
                    "mean_correctness": sum(
                        profile["mean_correctness"] for profile in profiles
                    )
                    / total,
                    **counts,
                    "mean_pass_rate": sum(
                        profile["pass_rate"] for profile in profiles
                    )
                    / total,
                }
            )
        return result

    def trajectory_profiles(self) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            """\
            SELECT sample_id, depth,
                SUM(
                    CASE WHEN status='success' AND correctness_score >= 1.0
                    THEN 1 ELSE 0 END
                ) AS pass_count,
                SUM(
                    CASE WHEN status='success' AND format_score > 0
                    THEN 1 ELSE 0 END
                ) AS fmt_count,
                SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count,
                AVG(CASE WHEN status='success' THEN total_score END) AS mean_score,
                AVG(CASE WHEN status='success' THEN format_score END) AS mean_format,
                AVG(
                    CASE WHEN status='success' THEN correctness_score END
                ) AS mean_correctness
            FROM rollouts
            GROUP BY sample_id, depth
            HAVING success_count = ?
            ORDER BY sample_id, depth
            """,
            (EXPECTED_ROLLOUTS,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            success_count = row["success_count"]
            pass_rate = row["pass_count"] / success_count if success_count else 0.0
            format_rate = row["fmt_count"] / success_count if success_count else 0.0
            result.append(
                {
                    "sample_id": row["sample_id"],
                    "depth": row["depth"],
                    "pass_rate": pass_rate,
                    "format_rate": format_rate,
                    "difficulty": classify_difficulty(pass_rate, format_rate),
                    "mean_score": row["mean_score"],
                    "mean_format": row["mean_format"],
                    "mean_correctness": row["mean_correctness"],
                }
            )
        return result

    def capability_boundaries(self) -> list[dict[str, Any]]:
        by_sample: dict[int, list[dict[str, Any]]] = {}
        for profile in self.trajectory_profiles():
            by_sample.setdefault(profile["sample_id"], []).append(profile)

        result: list[dict[str, Any]] = []
        for sample_id, depths in sorted(by_sample.items()):
            depths.sort(key=lambda item: item["depth"])
            boundary: str | int = "always_easy"
            for depth in depths:
                if depth["pass_rate"] < THRESHOLD_TOO_EASY:
                    boundary = depth["depth"]
                    break
            if all(depth["pass_rate"] == 0 for depth in depths):
                boundary = "always_hard"
            result.append({"sample_id": sample_id, "boundary": boundary})
        return result

    def difficulty_pairs(self) -> dict[str, list[tuple[int, int]]]:
        result: dict[str, list[tuple[int, int]]] = {
            "too_easy": [],
            "effective": [],
            "hard": [],
            "too_hard": [],
        }
        for profile in self.trajectory_profiles():
            result[profile["difficulty"]].append(
                (profile["sample_id"], profile["depth"])
            )
        return result

    def close(self) -> None:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
