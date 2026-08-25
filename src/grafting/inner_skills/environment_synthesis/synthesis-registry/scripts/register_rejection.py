"""Register a rejected raw skill in the registry."""
from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_skills (
    skill_id       TEXT PRIMARY KEY,
    skill_name     TEXT NOT NULL,
    repo           TEXT,
    status         TEXT NOT NULL CHECK(status IN ('synthesized', 'rejected')),
    environment_id TEXT,
    processed_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS environments (
    environment_id  TEXT PRIMARY KEY,
    tool_count      INTEGER NOT NULL,
    test_count      INTEGER NOT NULL,
    table_count     INTEGER NOT NULL,
    db_size_bytes   INTEGER NOT NULL,
    total_rows      INTEGER NOT NULL,
    source_skills   TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    harness_backend TEXT,
    model           TEXT,
    provider        TEXT
);

CREATE TABLE IF NOT EXISTS rejections (
    skill_id           TEXT PRIMARY KEY REFERENCES processed_skills(skill_id),
    reason             TEXT NOT NULL,
    estimated_tools    INTEGER,
    mockable           INTEGER,
    data_constructable INTEGER,
    rejected_at        TEXT NOT NULL,
    harness_backend    TEXT,
    model              TEXT,
    provider           TEXT
);
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Register a rejected skill")
    parser.add_argument("registry_db", help="Path to registry.db")
    parser.add_argument("--skill-id", required=True, help="Skill directory name")
    parser.add_argument("--skill-name", required=True, help="Skill name from _meta.json")
    parser.add_argument("--repo", default="", help="Source repo name")
    parser.add_argument("--reason", required=True, help="Human-readable rejection reason")
    parser.add_argument("--estimated-tools", type=int, default=0)
    parser.add_argument("--mockable", type=int, choices=[0, 1], default=0)
    parser.add_argument("--data-constructable", type=int, choices=[0, 1], default=0)
    parser.add_argument("--harness-backend", default="", help="Harness backend used")
    parser.add_argument("--model", default="", help="Model used")
    parser.add_argument("--provider", default="", help="Provider used")
    parser.add_argument("--input-tokens", type=int, default=0, help="Input tokens consumed")
    parser.add_argument("--output-tokens", type=int, default=0, help="Output tokens consumed")
    parser.add_argument("--total-tokens", type=int, default=0, help="Total tokens consumed")
    args = parser.parse_args()

    db = sqlite3.connect(args.registry_db)
    db.executescript(SCHEMA)
    for table in ("environments", "rejections"):
        for col in ("harness_backend", "model", "provider"):
            try:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass
        for col in ("input_tokens", "output_tokens", "total_tokens"):
            try:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col} INTEGER")
            except sqlite3.OperationalError:
                pass
    db.commit()

    now = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S")

    db.execute(
        "INSERT OR REPLACE INTO processed_skills (skill_id, skill_name, repo, status, "
        "environment_id, processed_at) VALUES (?, ?, ?, 'rejected', NULL, ?)",
        (args.skill_id, args.skill_name, args.repo, now),
    )
    db.execute(
        "INSERT OR REPLACE INTO rejections (skill_id, reason, estimated_tools, mockable, "
        "data_constructable, rejected_at, harness_backend, model, provider, "
        "input_tokens, output_tokens, total_tokens) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (args.skill_id, args.reason, args.estimated_tools, args.mockable,
         args.data_constructable, now,
         args.harness_backend, args.model, args.provider,
         args.input_tokens, args.output_tokens, args.total_tokens),
    )

    db.commit()
    db.close()

    print(f"Rejected: {args.skill_id} ({args.skill_name})")
    print(f"  reason: {args.reason}")


if __name__ == "__main__":
    main()
