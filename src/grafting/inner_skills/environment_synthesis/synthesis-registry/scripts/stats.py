"""Print summary statistics of the synthesis registry."""
from __future__ import annotations

import argparse
import sqlite3

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
    parser = argparse.ArgumentParser(description="Registry statistics")
    parser.add_argument("registry_db", help="Path to registry.db")
    args = parser.parse_args()

    db = sqlite3.connect(args.registry_db)
    db.executescript(SCHEMA)

    total_skills = db.execute(
        "SELECT COUNT(*) FROM processed_skills"
    ).fetchone()[0]
    synthesized = db.execute(
        "SELECT COUNT(*) FROM processed_skills "
        "WHERE status='synthesized'"
    ).fetchone()[0]
    rejected = db.execute(
        "SELECT COUNT(*) FROM processed_skills "
        "WHERE status='rejected'"
    ).fetchone()[0]
    env_count = db.execute("SELECT COUNT(*) FROM environments").fetchone()[0]

    env_stats = db.execute(
        "SELECT COALESCE(SUM(tool_count),0), COALESCE(SUM(test_count),0), "
        "COALESCE(SUM(total_rows),0), COALESCE(SUM(db_size_bytes),0) FROM environments"
    ).fetchone()

    print("=== Synthesis Registry ===")
    print(f"Skills processed:  {total_skills}")
    print(f"  synthesized:     {synthesized}")
    print(f"  rejected:        {rejected}")
    if total_skills > 0:
        print(f"  acceptance rate: {synthesized / total_skills * 100:.1f}%")
    print()
    print(f"Environments:      {env_count}")
    print(f"  total tools:     {env_stats[0]}")
    print(f"  total tests:     {env_stats[1]}")
    print(f"  total rows:      {env_stats[2]}")
    print(f"  total DB size:   {env_stats[3] // 1024}KB")

    if rejected > 0:
        print()
        print("Top rejection reasons:")
        for row in db.execute(
            "SELECT reason, COUNT(*) as cnt "
            "FROM rejections GROUP BY reason "
            "ORDER BY cnt DESC LIMIT 5"
        ).fetchall():
            print(f"  {row[1]}x  {row[0][:80]}")

    db.close()


if __name__ == "__main__":
    main()
