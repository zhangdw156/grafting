"""Build and search a full-text index over synthesized environments.

Used to detect duplicates before synthesizing a new environment.
Indexes each environment's ENVIRONMENT.md and tool names.

Usage:
    # Build/rebuild index from all environments in a directory
    python search_environments.py <env_root> build

    # Search for similar environments
    python search_environments.py <env_root> search --query "jira issue tracking sprint"

    # Check if a new skill would duplicate an existing env
    python search_environments.py <env_root> check-dup \
        --skill-description "Manage Jira boards and sprints"
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def _read_env_text(env_dir: Path) -> str:
    """Read ENVIRONMENT.md + tool names for indexing."""
    parts = []

    env_md = env_dir / "spec" / "ENVIRONMENT.md"
    if env_md.exists():
        try:
            parts.append(env_md.read_text(encoding="utf-8")[:5000])
        except UnicodeDecodeError:
            pass

    tools_jsonl = env_dir / "spec" / "tools.jsonl"
    if tools_jsonl.exists():
        try:
            for line in tools_jsonl.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    tool = json.loads(line)
                    parts.append(f"{tool.get('name', '')} {tool.get('description', '')[:200]}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    return "\n".join(parts)


def cmd_build(env_root: Path) -> None:
    index_db = env_root / "environment_index.db"
    if index_db.exists():
        index_db.unlink()

    db = sqlite3.connect(str(index_db))
    db.executescript("""
        CREATE TABLE env_index (
            rowid        INTEGER PRIMARY KEY AUTOINCREMENT,
            environment_id TEXT NOT NULL UNIQUE,
            tool_count   INTEGER NOT NULL,
            content      TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE env_fts USING fts5(
            environment_id, content,
            content='env_index', content_rowid='rowid'
        );
    """)

    count = 0
    for env_dir in sorted(env_root.iterdir()):
        if not env_dir.is_dir():
            continue
        if not (env_dir / "runtime" / "backend.py").exists():
            continue

        content = _read_env_text(env_dir)
        tools_jsonl = env_dir / "spec" / "tools.jsonl"
        tool_count = 0
        if tools_jsonl.exists():
            tool_count = sum(1 for line in tools_jsonl.read_text().splitlines() if line.strip())

        db.execute(
            "INSERT INTO env_index (environment_id, tool_count, content) VALUES (?, ?, ?)",
            (env_dir.name, tool_count, content),
        )
        count += 1

    db.commit()
    db.execute("INSERT INTO env_fts(env_fts) VALUES('rebuild')")
    db.commit()
    db.close()

    print(f"Indexed {count} environments → {index_db}")


def cmd_search(env_root: Path, query: str, limit: int) -> None:
    index_db = env_root / "environment_index.db"
    if not index_db.exists():
        print("No index. Run 'build' first.")
        return

    db = sqlite3.connect(str(index_db))
    db.row_factory = sqlite3.Row

    rows = db.execute(
        "SELECT ei.environment_id, ei.tool_count, "
        "snippet(env_fts, 1, '>>>', '<<<', '...', 30) as snippet "
        "FROM env_fts ft JOIN env_index ei ON ft.rowid = ei.rowid "
        "WHERE env_fts MATCH ? ORDER BY rank LIMIT ?",
        (query, limit),
    ).fetchall()

    if not rows:
        print("No similar environments found.")
        return

    print(f"{'Environment':<30} {'Tools':>5}  Matching context")
    print(f"{'-'*30} {'-'*5}  {'-'*60}")
    for r in rows:
        snippet = r["snippet"][:60].replace("\n", " ")
        print(f"{r['environment_id']:<30} {r['tool_count']:>5}  {snippet}")

    db.close()


def cmd_check_dup(env_root: Path, description: str) -> None:
    index_db = env_root / "environment_index.db"
    if not index_db.exists():
        print("No index. Run 'build' first.")
        return

    db = sqlite3.connect(str(index_db))
    db.row_factory = sqlite3.Row

    rows = db.execute(
        "SELECT ei.environment_id, ei.tool_count "
        "FROM env_fts ft JOIN env_index ei ON ft.rowid = ei.rowid "
        "WHERE env_fts MATCH ? ORDER BY rank LIMIT 5",
        (description,),
    ).fetchall()

    if not rows:
        print("NO_DUPLICATE: No similar environments found.")
        return

    print("POTENTIAL_DUPLICATES:")
    for r in rows:
        print(f"  {r['environment_id']} ({r['tool_count']} tools)")

    db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Environment similarity search")
    parser.add_argument("env_root", help="Path to environments directory")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("build", help="Build/rebuild the environment index")

    p_search = sub.add_parser("search", help="Search for similar environments")
    p_search.add_argument("--query", required=True)
    p_search.add_argument("--limit", type=int, default=5)

    p_dup = sub.add_parser("check-dup", help="Check if a skill would duplicate existing")
    p_dup.add_argument("--skill-description", required=True)

    args = parser.parse_args()
    env_root = Path(args.env_root)

    if args.cmd == "build":
        cmd_build(env_root)
    elif args.cmd == "search":
        cmd_search(env_root, args.query, args.limit)
    elif args.cmd == "check-dup":
        cmd_check_dup(env_root, args.skill_description)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
