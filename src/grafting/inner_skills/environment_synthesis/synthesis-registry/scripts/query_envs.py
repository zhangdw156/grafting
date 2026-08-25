"""Query registered environments."""
from __future__ import annotations

import argparse
import json
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
    parser = argparse.ArgumentParser(description="Query environments")
    parser.add_argument("registry_db", help="Path to registry.db")
    parser.add_argument("--min-tools", type=int, default=0, help="Minimum tool count")
    parser.add_argument("--format", choices=["table", "json"], default="table")
    args = parser.parse_args()

    db = sqlite3.connect(args.registry_db)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)

    query = "SELECT * FROM environments WHERE 1=1"
    params: list = []
    if args.min_tools:
        query += " AND tool_count >= ?"
        params.append(args.min_tools)
    query += " ORDER BY environment_id"

    rows = db.execute(query, params).fetchall()
    db.close()

    if not rows:
        print("No environments found.")
        return

    if args.format == "json":
        print(json.dumps([dict(r) for r in rows], indent=2))
        return

    header = (
        f"{'Environment':<30} {'Tools':>5} {'Tests':>5}"
        f" {'Tables':>6} {'Rows':>6} {'DB Size':>8}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        size = f"{r['db_size_bytes'] // 1024}KB"
        print(f"{r['environment_id']:<30} {r['tool_count']:>5} "
              f"{r['test_count']:>5} {r['table_count']:>6} {r['total_rows']:>6} {size:>8}")


if __name__ == "__main__":
    main()
