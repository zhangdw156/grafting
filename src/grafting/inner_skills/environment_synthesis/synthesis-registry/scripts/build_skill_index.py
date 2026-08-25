"""Build a full-text search index over all raw skills for retrieval-based synthesis.

Scans all raw skills, extracts metadata + descriptions, and builds a
SQLite FTS5 index. The synthesis agent queries this index to find
related skills when a single skill doesn't have enough tools.

Usage:
    python build_skill_index.py <skills_root> <output_db>

    # Then query:
    python search_skills.py <index_db> --query "jira sprint board" [--repo X] [--limit 20]
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


def _read_meta(skill_dir: Path) -> dict:
    meta_file = skill_dir / "_meta.json"
    if meta_file.exists():
        try:
            return json.loads(meta_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    return {}


def _read_description(skill_dir: Path) -> str:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return ""
    try:
        text = skill_md.read_text(encoding="utf-8")[:3000]
    except UnicodeDecodeError:
        return ""

    if text.startswith("---"):
        end = text.find("---", 3)
        if end > 0:
            frontmatter = text[3:end]
            for line in frontmatter.splitlines():
                if line.strip().startswith("description:"):
                    desc = line.split(":", 1)[1].strip().strip("'\"")
                    if desc and desc != ">":
                        return desc[:500]
                    idx = frontmatter.find("description:")
                    remaining = frontmatter[idx:].splitlines()[1:]
                    multi = []
                    for rl in remaining:
                        stripped = rl.strip()
                        if stripped and not stripped.startswith(("name:", "---")):
                            multi.append(stripped)
                        elif stripped.startswith(("name:", "---")):
                            break
                    if multi:
                        return " ".join(multi)[:500]

    in_frontmatter = False
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter:
            continue
        if stripped and not stripped.startswith("#"):
            lines.append(stripped)
            if len(lines) >= 3:
                break
    return " ".join(lines)[:500]


def _list_scripts(skill_dir: Path) -> str:
    scripts_dir = skill_dir / "scripts"
    if not scripts_dir.exists():
        return ""
    return " ".join(sorted(f.stem for f in scripts_dir.iterdir() if f.is_file()))


def main() -> None:
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <skills_root> <output_db>")
        sys.exit(1)

    skills_root = Path(sys.argv[1])
    output_db = Path(sys.argv[2])

    if output_db.exists():
        output_db.unlink()

    db = sqlite3.connect(str(output_db))
    db.executescript("""
        CREATE TABLE skill_index (
            rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_id    TEXT NOT NULL UNIQUE,
            skill_name  TEXT NOT NULL,
            repo        TEXT NOT NULL,
            description TEXT NOT NULL,
            script_names TEXT NOT NULL,
            script_count INTEGER NOT NULL,
            consumed    INTEGER NOT NULL DEFAULT 0,
            consumed_by TEXT
        );

        CREATE VIRTUAL TABLE skill_fts USING fts5(
            skill_name, description, script_names,
            content='skill_index', content_rowid='rowid'
        );

        CREATE INDEX idx_skill_repo ON skill_index(repo);
    """)

    total = 0
    indexed = 0

    for skill_dir in sorted(skills_root.iterdir()):
        if not skill_dir.is_dir():
            continue
        total += 1

        scripts_dir = skill_dir / "scripts"
        if not scripts_dir.exists():
            continue

        script_files = [f for f in scripts_dir.iterdir() if f.is_file()]
        if not script_files:
            continue

        meta = _read_meta(skill_dir)
        description = _read_description(skill_dir)
        script_names = " ".join(sorted(f.stem for f in script_files))

        db.execute(
            "INSERT INTO skill_index "
            "(skill_id, skill_name, repo, description, "
            "script_names, script_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (skill_dir.name, meta.get("skill_name", skill_dir.name),
             meta.get("repo", "unknown"), description, script_names, len(script_files)),
        )
        indexed += 1

    db.commit()

    # Populate FTS index
    db.execute("INSERT INTO skill_fts(skill_fts) VALUES('rebuild')")
    db.commit()

    print(f"Scanned: {total}, Indexed: {indexed}")
    print(f"Index: {output_db} ({output_db.stat().st_size // 1024}KB)")

    db.close()


if __name__ == "__main__":
    main()
