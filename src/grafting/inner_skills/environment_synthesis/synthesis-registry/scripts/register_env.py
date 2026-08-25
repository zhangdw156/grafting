"""Register a synthesized environment in the registry."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

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


def _ensure_schema(db: sqlite3.Connection) -> None:
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


def _inspect_env(env_dir: Path) -> dict:
    """Auto-detect metrics from the environment directory."""
    tools_jsonl = env_dir / "spec" / "tools.jsonl"
    tool_count = sum(1 for line in tools_jsonl.read_text().splitlines() if line.strip())

    test_file = env_dir / "tests" / "test_runtime.py"
    test_lines = test_file.read_text().splitlines()
    test_count = sum(
        1 for line in test_lines if line.startswith("def test_")
    )

    world_db = env_dir / "world" / "world.db"
    db_size = world_db.stat().st_size

    conn = sqlite3.connect(str(world_db))
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    table_count = len(tables)
    total_rows = sum(
        conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]  # noqa: S608
        for t in tables
    )
    conn.close()

    return {
        "tool_count": tool_count,
        "test_count": test_count,
        "table_count": table_count,
        "db_size_bytes": db_size,
        "total_rows": total_rows,
    }


def _read_meta(skill_dir: Path) -> dict:
    meta_file = skill_dir / "_meta.json"
    if meta_file.exists():
        return json.loads(meta_file.read_text())
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Register a synthesized environment")
    parser.add_argument("registry_db", help="Path to registry.db")
    parser.add_argument("--env-id", required=True, help="Environment directory name")
    parser.add_argument(
        "--source-skills", required=True,
        help="Comma-separated skill directory names",
    )
    parser.add_argument(
        "--env-root", required=True,
        help="Path to environments root directory",
    )
    parser.add_argument(
        "--skills-root", default="",
        help="Path to raw skills root (for reading _meta.json)",
    )
    parser.add_argument("--harness-backend", default="", help="Harness backend used for synthesis")
    parser.add_argument("--model", default="", help="Model used for synthesis")
    parser.add_argument("--provider", default="", help="Provider used for synthesis")
    parser.add_argument("--input-tokens", type=int, default=0, help="Input tokens consumed")
    parser.add_argument("--output-tokens", type=int, default=0, help="Output tokens consumed")
    parser.add_argument("--total-tokens", type=int, default=0, help="Total tokens consumed")
    args = parser.parse_args()

    db = sqlite3.connect(args.registry_db)
    _ensure_schema(db)

    env_dir = Path(args.env_root) / args.env_id
    if not env_dir.exists():
        print(f"Error: environment directory not found: {env_dir}", file=sys.stderr)
        sys.exit(1)

    metrics = _inspect_env(env_dir)
    now = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S")
    skill_ids = [s.strip() for s in args.source_skills.split(",")]

    db.execute(
        "INSERT OR REPLACE INTO environments (environment_id, tool_count, test_count, "
        "table_count, db_size_bytes, total_rows, source_skills, created_at, "
        "harness_backend, model, provider, "
        "input_tokens, output_tokens, total_tokens) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (args.env_id, metrics["tool_count"], metrics["test_count"],
         metrics["table_count"], metrics["db_size_bytes"], metrics["total_rows"],
         ",".join(skill_ids), now,
         args.harness_backend, args.model, args.provider,
         args.input_tokens, args.output_tokens, args.total_tokens),
    )

    skills_root = Path(args.skills_root) if args.skills_root else None
    for skill_id in skill_ids:
        meta = _read_meta(skills_root / skill_id) if skills_root else {}
        db.execute(
            "INSERT OR REPLACE INTO processed_skills (skill_id, skill_name, repo, status, "
            "environment_id, processed_at) VALUES (?, ?, ?, 'synthesized', ?, ?)",
            (skill_id, meta.get("skill_name", skill_id), meta.get("repo", ""), args.env_id, now),
        )

    db.commit()
    db.close()

    print(f"Registered environment: {args.env_id}")
    print(f"  tools: {metrics['tool_count']}, tests: {metrics['test_count']}, "
          f"tables: {metrics['table_count']}, rows: {metrics['total_rows']}, "
          f"db: {metrics['db_size_bytes']} bytes")
    print(f"  source skills: {', '.join(skill_ids)}")


if __name__ == "__main__":
    main()
