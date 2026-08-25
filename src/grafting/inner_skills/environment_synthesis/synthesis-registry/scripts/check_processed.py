"""Check whether a skill has already been processed."""
from __future__ import annotations

import argparse
import sqlite3
import sys

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
    environment_id TEXT PRIMARY KEY,
    tool_count     INTEGER NOT NULL,
    test_count     INTEGER NOT NULL,
    table_count    INTEGER NOT NULL,
    db_size_bytes  INTEGER NOT NULL,
    total_rows     INTEGER NOT NULL,
    source_skills  TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rejections (
    skill_id           TEXT PRIMARY KEY REFERENCES processed_skills(skill_id),
    reason             TEXT NOT NULL,
    estimated_tools    INTEGER,
    mockable           INTEGER,
    data_constructable INTEGER,
    rejected_at        TEXT NOT NULL
);
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Check if a skill has been processed")
    parser.add_argument("registry_db", help="Path to registry.db")
    parser.add_argument("--skill-id", required=True, help="Skill directory name")
    args = parser.parse_args()

    db = sqlite3.connect(args.registry_db)
    db.executescript(SCHEMA)

    row = db.execute(
        "SELECT status, environment_id, processed_at FROM processed_skills WHERE skill_id = ?",
        (args.skill_id,),
    ).fetchone()
    db.close()

    if row is None:
        print(f"Not processed: {args.skill_id}")
        sys.exit(1)

    status, env_id, processed_at = row
    print(f"Already processed: {args.skill_id}")
    print(f"  status: {status}")
    if env_id:
        print(f"  environment: {env_id}")
    print(f"  processed_at: {processed_at}")
    sys.exit(0)


if __name__ == "__main__":
    main()
