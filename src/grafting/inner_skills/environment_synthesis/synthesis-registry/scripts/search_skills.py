"""Search the skill index for related raw skills.

Used by the synthesis agent when a single skill doesn't have enough
tools and needs to find related skills to combine.

Usage:
    # Full-text search (excludes consumed skills by default)
    python search_skills.py <index_db> --query "jira sprint board agile"

    # Include consumed skills
    python search_skills.py <index_db> --query "jira" --include-consumed

    # Find all skills from same repo
    python search_skills.py <index_db> --same-repo-as 00001_jira-agile

    # Mark skills as consumed after synthesis
    python search_skills.py <index_db> --consume 00001_jira-agile,00002_jira-auth --by 00001_jira
"""
from __future__ import annotations

import argparse
import json
import sqlite3


def main() -> None:
    parser = argparse.ArgumentParser(description="Search skill index")
    parser.add_argument("index_db", help="Path to skill_index.db")
    parser.add_argument("--query", default="", help="FTS5 search query")
    parser.add_argument("--repo", default="", help="Filter by repo name")
    parser.add_argument("--same-repo-as", default="", help="Find skills from same repo")
    parser.add_argument("--exclude", default="", help="Comma-separated skill_ids to exclude")
    parser.add_argument(
        "--include-consumed", action="store_true",
        help="Include already-consumed skills",
    )
    parser.add_argument("--limit", type=int, default=20, help="Max results")
    parser.add_argument("--format", choices=["table", "json"], default="table")
    parser.add_argument(
        "--consume", default="",
        help="Mark skill_ids as consumed (comma-separated)",
    )
    parser.add_argument(
        "--by", default="",
        help="Environment ID that consumed the skills",
    )
    args = parser.parse_args()

    db = sqlite3.connect(args.index_db)
    db.row_factory = sqlite3.Row

    # Handle --consume command
    if args.consume:
        skill_ids = [s.strip() for s in args.consume.split(",") if s.strip()]
        for sid in skill_ids:
            db.execute(
                "UPDATE skill_index SET consumed = 1, consumed_by = ? WHERE skill_id = ?",
                (args.by or None, sid),
            )
        db.commit()
        print(f"Marked {len(skill_ids)} skills as consumed" + (f" by {args.by}" if args.by else ""))
        db.close()
        return

    exclude_ids = {s.strip() for s in args.exclude.split(",") if s.strip()}
    consumed_filter = "" if args.include_consumed else " AND si.consumed = 0"

    if args.same_repo_as:
        row = db.execute(
            "SELECT repo FROM skill_index WHERE skill_id = ?", (args.same_repo_as,)
        ).fetchone()
        if not row:
            print(f"Skill not found: {args.same_repo_as}")
            return
        rows = db.execute(
            f"SELECT skill_id, skill_name, repo, description, script_count, consumed "
            f"FROM skill_index si WHERE repo = ? AND skill_id != ?{consumed_filter} "
            f"ORDER BY script_count DESC LIMIT ?",
            (row["repo"], args.same_repo_as, args.limit),
        ).fetchall()

    elif args.query:
        repo_filter = " AND si.repo = ?" if args.repo else ""
        params: list = [args.query]
        if args.repo:
            params.append(args.repo)
        params.append(args.limit)

        rows = db.execute(
            "SELECT si.skill_id, si.skill_name, si.repo, "
            "si.description, si.script_count, si.consumed "
            "FROM skill_fts ft JOIN skill_index si "
            "ON ft.rowid = si.rowid "
            f"WHERE skill_fts MATCH ?{repo_filter}"
            f"{consumed_filter} "
            "ORDER BY rank LIMIT ?",
            params,
        ).fetchall()

    elif args.repo:
        rows = db.execute(
            f"SELECT skill_id, skill_name, repo, description, script_count, consumed "
            f"FROM skill_index si WHERE repo = ?{consumed_filter} "
            f"ORDER BY script_count DESC LIMIT ?",
            (args.repo, args.limit),
        ).fetchall()
    else:
        print("Provide --query, --repo, --same-repo-as, or --consume")
        return

    results = [r for r in rows if r["skill_id"] not in exclude_ids]

    if not results:
        print("No results found.")
        return

    if args.format == "json":
        print(json.dumps([dict(r) for r in results], indent=2, ensure_ascii=False))
        return

    print(f"{'skill_id':<40} {'repo':<30} {'scripts':>7} description")
    print(f"{'-'*40} {'-'*30} {'-'*7} {'-'*50}")
    for r in results:
        desc = r["description"][:50] + "..." if len(r["description"]) > 50 else r["description"]
        consumed_mark = " [consumed]" if r["consumed"] else ""
        print(f"{r['skill_id']:<40} {r['repo']:<30} {r['script_count']:>7} {desc}{consumed_mark}")

    db.close()


if __name__ == "__main__":
    main()
