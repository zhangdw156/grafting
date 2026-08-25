"""Environment synthesis runner.

Takes raw skills from an external skills directory and synthesizes
packaged environments. Uses a coordinator pattern: the runner picks
non-conflicting raw skills serially, then dispatches them to harness
agents in parallel.

Each agent autonomously reads the raw skill, assesses viability,
searches for related skills if needed, and produces a complete
environment package (or rejects the skill).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from grafting.runner.harness import BaseHarnessRunner
from grafting.utils import (
    classify_error as _classify_error_str,
)
from grafting.utils import (
    create_progress,
    logger,
    truncate_stderr,
)

ENV_REQUIRED_FILES = [
    "spec/tools.jsonl",
    "spec/tool_graph.json",
    "spec/ENVIRONMENT.md",
    "runtime/backend.py",
    "world/schema.sql",
    "world/world.db",
    "tests/test_runtime.py",
]


class EnvironmentSynthesisRunner(BaseHarnessRunner):

    def __init__(
        self,
        *,
        raw_skills_root: Path,
        env_output: Path,
        inner_skills_root: Path,
        **kwargs: Any,
    ) -> None:
        self.raw_skills_root = raw_skills_root.resolve()
        self.env_output = env_output.resolve()

        self.env_output.mkdir(parents=True, exist_ok=True)
        self._ensure_skill_index()

        # Clean stale sandboxes BEFORE super().__init__ creates agents
        self._pre_clean_sandbox(kwargs.get("sandbox_path"), kwargs.get("output_root"))

        super().__init__(inner_skills_root=inner_skills_root, **kwargs)

        # Create sandboxes and re-point agents to sandbox project_root
        for i in range(self.workers):
            sb = self._setup_sandbox(i)
            self._sandboxes.append(sb)
        self._repoint_agents_to_sandboxes()

    # ------------------------------------------------------------------
    # Sandbox
    # ------------------------------------------------------------------

    def _setup_sandbox(self, worker_index: int) -> Path:
        """Create a sandbox directory with symlinks for one worker."""
        agent_dir = self._agent_xdg_path(worker_index)
        project = agent_dir / "project"

        for d in (project, project / "output"):
            d.mkdir(parents=True, exist_ok=True)

        symlinks = {
            "skill_index.db": self.env_output / "skill_index.db",
            "registry.db": self.env_output / "registry.db",
            "environment_index.db": self.env_output / "environment_index.db",
        }
        # raw_skills/ is a real directory; individual skills are symlinked
        # per-task by _link_raw_skill() to avoid exposing 28K entries.
        (project / "raw_skills").mkdir(parents=True, exist_ok=True)
        for name, target in symlinks.items():
            link = project / name
            if link.exists() or link.is_symlink():
                link.unlink()
            if target.exists():
                link.symlink_to(target)

        self._setup_harness_skills(project)

        return project

    def _link_raw_skill(self, sandbox: Path, raw_skill_id: str) -> None:
        """Symlink only the target raw skill into the sandbox."""
        raw_skills_dir = sandbox / "raw_skills"
        # Remove any previous skill links
        for child in raw_skills_dir.iterdir():
            if child.is_symlink():
                child.unlink()
        target = self.raw_skills_root / raw_skill_id
        if target.exists():
            (raw_skills_dir / raw_skill_id).symlink_to(target)

    # ------------------------------------------------------------------
    # Skill index
    # ------------------------------------------------------------------

    def _ensure_skill_index(self) -> None:
        """Build skill_index.db from raw_skills_root if it does not exist."""
        index_db = self.env_output / "skill_index.db"
        if index_db.exists():
            try:
                db = sqlite3.connect(str(index_db))
                db.execute("SELECT COUNT(*) FROM skill_index")
                db.close()
                return
            except sqlite3.DatabaseError:
                logger.warning("Corrupted skill index, rebuilding")
                index_db.unlink()

        logger.info("Building skill index from {}", self.raw_skills_root)

        db = sqlite3.connect(str(index_db))
        db.executescript("""
            CREATE TABLE skill_index (
                rowid        INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_id     TEXT NOT NULL UNIQUE,
                skill_name   TEXT NOT NULL,
                repo         TEXT NOT NULL,
                description  TEXT NOT NULL,
                script_names TEXT NOT NULL,
                script_count INTEGER NOT NULL,
                consumed     INTEGER NOT NULL DEFAULT 0,
                consumed_by  TEXT
            );
            CREATE VIRTUAL TABLE skill_fts USING fts5(
                skill_name, description, script_names,
                content='skill_index', content_rowid='rowid'
            );
            CREATE INDEX idx_skill_repo ON skill_index(repo);
        """)

        total = 0
        indexed = 0
        for skill_dir in sorted(self.raw_skills_root.iterdir()):
            if not skill_dir.is_dir():
                continue
            total += 1
            scripts_dir = skill_dir / "scripts"
            if not scripts_dir.exists():
                continue
            script_files = [f for f in scripts_dir.iterdir() if f.is_file()]
            if not script_files:
                continue

            meta = self._read_skill_meta(skill_dir)
            description = self._read_skill_description(skill_dir)
            script_names = " ".join(sorted(f.stem for f in script_files))

            db.execute(
                "INSERT INTO skill_index "
                "(skill_id, skill_name, repo, description, script_names, script_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    skill_dir.name,
                    meta.get("skill_name", skill_dir.name),
                    meta.get("repo", "unknown"),
                    description,
                    script_names,
                    len(script_files),
                ),
            )
            indexed += 1

        db.commit()
        db.execute("INSERT INTO skill_fts(skill_fts) VALUES('rebuild')")
        db.commit()
        db.close()

        logger.info(
            "Skill index built: scanned {}, indexed {} ({}KB)",
            total, indexed, index_db.stat().st_size // 1024,
        )

    @staticmethod
    def _read_skill_meta(skill_dir: Path) -> dict:
        meta_file = skill_dir / "_meta.json"
        if meta_file.exists():
            try:
                return json.loads(meta_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        return {}

    @staticmethod
    def _read_skill_description(skill_dir: Path) -> str:
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
        in_frontmatter = False
        lines: list[str] = []
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

    # ------------------------------------------------------------------
    # Main loop (streaming dispatch)
    # ------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        """Run the synthesis loop with streaming dispatch.

        Each worker immediately picks the next skill upon completion,
        keeping all workers busy at all times.
        """
        stats = {"synthesized": 0, "rejected": 0, "failed": 0, "total": 0}
        self._pick_lock = threading.Lock()
        self._env_index_counter = 0
        self._env_index_interval = self.workers

        skill_iter = self._skill_iterator()

        with create_progress() as progress, \
             ThreadPoolExecutor(max_workers=self.workers) as pool:
            ptask = progress.add_task("Synthesizing", total=None)
            futures: dict[Any, tuple[int, str]] = {}

            for worker_index in range(self.workers):
                skill_id = next(skill_iter, None)
                if skill_id is None:
                    break
                futures[pool.submit(self._run_one, worker_index, skill_id)] = (
                    worker_index,
                    skill_id,
                )

            while futures:
                done_set, _ = wait(futures, return_when=FIRST_COMPLETED)

                for future in done_set:
                    worker_index, skill_id = futures.pop(future)
                    try:
                        outcome = future.result()
                    except Exception as exc:
                        logger.error("Worker {} failed for {}: {}", worker_index, skill_id, exc)
                        outcome = "failed"

                    stats[outcome] = stats.get(outcome, 0) + 1
                    stats["total"] += 1
                    progress.advance(ptask)

                    self._env_index_counter += 1
                    if self._env_index_counter >= self._env_index_interval:
                        self._rebuild_env_index()
                        self._env_index_counter = 0

                    next_skill = next(skill_iter, None)
                    if next_skill is not None:
                        futures[pool.submit(self._run_one, worker_index, next_skill)] = (
                            worker_index,
                            next_skill,
                        )

        self._rebuild_env_index()
        logger.info(
            "Synthesis complete: {} synthesized, {} rejected, {} failed out of {} processed",
            stats["synthesized"], stats["rejected"], stats["failed"], stats["total"],
        )
        return stats

    # ------------------------------------------------------------------
    # Coordinator: skill iterator
    # ------------------------------------------------------------------

    def _skill_iterator(self):
        """Yield unprocessed skill IDs one at a time."""
        index_db = self.env_output / "skill_index.db"
        if not index_db.exists():
            return

        db = sqlite3.connect(str(index_db))
        db.row_factory = sqlite3.Row
        # NOTE: script_count DESC means small skills (e.g. the 8 Jira skills
        # with 2-6 scripts each) are processed last.  These are exactly the
        # ones most likely to need same-repo merging to reach the 12-tool
        # threshold.  Consider a second pass or mixed ordering strategy.
        candidates = db.execute(
            "SELECT skill_id, repo FROM skill_index "
            "WHERE consumed = 0 "
            "ORDER BY script_count DESC"
        ).fetchall()
        db.close()

        registry_db = self.env_output / "registry.db"
        processed = set()
        if registry_db.exists():
            rdb = sqlite3.connect(str(registry_db))
            processed = {
                r[0] for r in rdb.execute(
                    "SELECT skill_id FROM processed_skills"
                ).fetchall()
            }
            rdb.close()

        for row in candidates:
            skill_id = row["skill_id"]
            if skill_id in processed:
                continue
            yield skill_id

    def _run_one(self, worker_index: int, raw_skill_id: str) -> str:
        """Run synthesis for one skill on one agent. Returns outcome string."""
        sandbox = self._sandboxes[worker_index]
        self._clean_sandbox_output(sandbox)
        self._link_raw_skill(sandbox, raw_skill_id)

        prompt = self._build_prompt(raw_skill_id)

        logger.info("Worker {}: starting {}", worker_index, raw_skill_id)
        result = self.run_agent(worker_index, prompt)

        if not result.success:
            error = f"Harness failed: {truncate_stderr(result.stderr, 300)}"
            logger.warning(
                "Worker {}: harness failed for {} (exit={}) stderr={}",
                worker_index, raw_skill_id, result.exit_code, truncate_stderr(result.stderr, 500),
            )
            if _classify_error_str(error) == "transient":
                logger.info(
                    "Worker {}: transient failure for {}, will retry",
                    worker_index, raw_skill_id,
                )
            else:
                self._register_rejection(
                    raw_skill_id, error,
                    harness_backend=result.harness_backend,
                    model=result.model, provider=result.provider,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    total_tokens=result.total_tokens,
                )
            return "failed"

        return self._post_validate(
            worker_index, raw_skill_id,
            harness_backend=result.harness_backend,
            model=result.model, provider=result.provider,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            total_tokens=result.total_tokens,
        )

    # ------------------------------------------------------------------
    # Prompt assembly
    # ------------------------------------------------------------------

    def _build_prompt(self, raw_skill_id: str) -> str:
        return "\n\n".join([
            self._section_role(),
            self._section_task(raw_skill_id),
            self._section_skills(),
            self._section_resources(),
            self._section_workflow(raw_skill_id),
            self._section_constraints(),
            self._section_test_environment(),
            self._section_output(raw_skill_id),
        ])

    @staticmethod
    def _section_role() -> str:
        return (
            "You are an environment synthesis agent. You read a raw MCP skill "
            "and produce a complete executable environment package, or reject "
            "the skill if it cannot form a viable environment."
        )

    @staticmethod
    def _section_task(raw_skill_id: str) -> str:
        return (
            f"Your input raw skill is at: raw_skills/{raw_skill_id}/\n"
            "Read ALL files in this directory: SKILL.md, scripts/, _meta.json, "
            "and any other files present. Understand what this skill does, "
            "what API it targets, and what operations it supports."
        )

    def _section_skills(self) -> str:
        p = self._native_skills_prefix()
        return (
            "The following skills provide detailed guidance for each synthesis step. "
            "Read the SKILL.md in each directory before starting that step:\n\n"
            f"- {p}/skill-viability-assessment/ — assess if this skill can "
            "become an environment (tool count, mockability, data constructability)\n"
            f"- {p}/tool-design/ — extract and expand tools, write tools.jsonl\n"
            f"- {p}/schema-design/ — design SQLite schema, create world.db\n"
            f"- {p}/tool-graph-design/ — build tool relationship graph\n"
            f"- {p}/backend-implementation/ — implement EnvironmentBackend class\n"
            f"- {p}/environment-brief/ — write ENVIRONMENT.md (7 sections)\n"
            f"- {p}/runtime-tests/ — write test_runtime.py\n"
            f"- {p}/quality-validation/ — validate all artifacts for consistency\n"
            f"- {p}/synthesis-registry/ — search related skills, check duplicates"
        )

    def _section_resources(self) -> str:
        p = self._native_skills_prefix()
        return (
            "Available resources (all paths relative to project root):\n\n"
            "- skill_index.db — FTS5 index over all raw skills. Query with:\n"
            f"  python {p}/synthesis-registry/scripts/search_skills.py "
            "skill_index.db --query \"<keywords>\"\n"
            f"  python {p}/synthesis-registry/scripts/search_skills.py "
            "skill_index.db --same-repo-as <skill_id>\n\n"
            "- environment_index.db — index of existing environments. Check duplicates:\n"
            f"  python {p}/synthesis-registry/scripts/search_environments.py "
            ". check-dup --skill-description \"<description>\"\n\n"
            "- registry.db — synthesis history. Check if already processed:\n"
            f"  python {p}/synthesis-registry/scripts/check_processed.py "
            "registry.db --skill-id <skill_id>"
        )

    def _section_workflow(self, raw_skill_id: str) -> str:
        p = self._native_skills_prefix()
        return (
            "Follow this sequence:\n\n"
            f"1. READ raw_skills/{raw_skill_id}/ — understand the skill thoroughly\n\n"
            f"2. ASSESS viability (read {p}/skill-viability-assessment/):\n"
            "   You MUST write output/VIABILITY.json BEFORE any other output.\n"
            "   - Check Hard Rejection Criteria (R1-R7) first — if any apply, reject immediately\n"
            "   - Phase 1: count tools extractable from scripts/\n"
            "   - Phase 2: estimate tools after expansion\n"
            "   - If total >= 12: proceed to step 3\n"
            "   - If < 12: search for related skills:\n"
            f"     python {p}/synthesis-registry/scripts/search_skills.py "
            f"skill_index.db --query \"<keywords>\" --same-repo-as {raw_skill_id}\n"
            "     If combining with found skills reaches 12+: read those skills too.\n"
            "     If still < 12: set verdict to \"reject\"\n\n"
            "   Write output/VIABILITY.json with the assessment result:\n"
            "   {\"skill_id\": \"...\", \"verdict\": \"pass\"|\"reject\", "
            "\"reasoning\": \"...\", ...}\n\n"
            "   If verdict is \"reject\":\n"
            "     Write output/REJECTED.json and STOP immediately.\n"
            "     Do NOT proceed to synthesis steps.\n\n"
            "3. CHECK for duplicate environments:\n"
            f"   python {p}/synthesis-registry/scripts/search_environments.py "
            ". check-dup --skill-description \"<description>\"\n"
            "   If POTENTIAL_DUPLICATES: REJECT with reason \"duplicate\".\n\n"
            "4. DESIGN tools → write output/{env_name}/spec/tools.jsonl\n"
            "5. DESIGN schema → write output/{env_name}/world/schema.sql + world.db\n"
            "6. BUILD tool graph → write output/{env_name}/spec/tool_graph.json\n"
            "7. IMPLEMENT backend → write output/{env_name}/runtime/backend.py\n"
            "8. WRITE brief → write output/{env_name}/spec/ENVIRONMENT.md\n"
            "9. WRITE tests → write output/{env_name}/tests/test_runtime.py\n"
            f"10. VALIDATE all artifacts (read {p}/quality-validation/)\n"
            "11. RUN tests: uv run pytest output/{env_name}/tests/test_runtime.py -x\n"
            "    Fix any failures before finishing."
        )

    @staticmethod
    def _section_constraints() -> str:
        return (
            "Constraints:\n"
            "- All state in SQLite — no in-memory-only state\n"
            "- EnvironmentBackend class: __init__(db_path), "
            "call() never raises, visible_state()\n"
            "- Unified return format: {\"success\": true, \"data\": ...} / "
            "{\"success\": false, \"error\": {\"code\": \"...\", \"message\": \"...\"}}\n"
            "- At least 12 tools. Category: only \"query\" or \"mutation\"\n"
            "- Deterministic: no randomness, no external calls, no network\n"
            "- Token expiry: TTL-based (ISO 8601 datetime), not call-count\n"
            "- Sensitive data: require auth, return masked (last 4 digits, no CVV)\n"
            "- Use datetime.now(tz=UTC) instead of datetime.utcnow() — "
            "the latter is deprecated in Python 3.12+\n"
            "- CRUD completeness: every entity must have create, get, list, "
            "update, and delete tools unless explicitly immutable\n"
            "- Seed data: at least 30 rows total, 3+ records per entity table, "
            "2+ top-level projects/scenarios for cross-entity task diversity\n"
            "- Every non-trivial parameter must have a concrete example in its "
            "description (e.g., '2024-06-15', 'SFO', 'CC0000001')\n"
            "- Conversational accessibility: environments are used in user-assistant "
            "conversations, not programmatic API clients. Apply these rules:\n"
            "  - Every mutation tool that requires an internal ID must have a "
            "corresponding query/search tool that discovers that ID from "
            "natural user information (names, descriptions, dates, etc.)\n"
            "  - Add search/lookup tools for any entity with internal IDs — "
            "users cannot guess IDs like 'BK0000042'\n"
            "  - Authentication tools must accept natural credentials "
            "(username, email, password), not OAuth client secrets or API keys\n"
            "  - Every column written to the sessions table must be readable "
            "by at least one tool — no dead writes\n"
            "- English only: all generated content must be in English — "
            "tool descriptions, parameter examples, schema comments, "
            "seed data, docstrings, error messages, ENVIRONMENT.md, "
            "and test assertions. Even when the raw skill is in another "
            "language, translate and produce English output"
        )

    @staticmethod
    def _section_test_environment() -> str:
        return (
            "Test execution environment (CRITICAL — read before writing backend.py):\n\n"
            "Tests run as: uv run pytest output/<env>/tests/test_runtime.py -x -q\n"
            "Working directory during tests: parent of the environment directory\n"
            "Timeout: 120 seconds\n\n"
            "Your backend.py MUST comply with these constraints:\n"
            "- No subprocess calls (subprocess.run, os.system, os.popen)\n"
            "- No network calls (requests, urllib, http.client, socket)\n"
            "- No external tool invocations (latexmk, ffmpeg, node, docker)\n"
            "- No environment variable requirements (API keys, tokens)\n"
            "- Only Python stdlib imports (sqlite3, json, datetime, pathlib, re, etc.)\n"
            "- All file paths computed from db_path parameter, never relative\n"
            "- All state in the SQLite database passed via db_path\n\n"
            "If the raw skill requires any of the above (external binaries, API keys, "
            "network calls), you MUST reject it in the viability assessment, not attempt "
            "to synthesize a broken environment."
        )

    @staticmethod
    def _section_output(raw_skill_id: str) -> str:
        env_name = raw_skill_id
        return (
            f"Write ALL environment files to: output/{env_name}/\n"
            f"  output/{env_name}/spec/tools.jsonl\n"
            f"  output/{env_name}/spec/tool_graph.json\n"
            f"  output/{env_name}/spec/ENVIRONMENT.md\n"
            f"  output/{env_name}/runtime/backend.py\n"
            f"  output/{env_name}/world/schema.sql\n"
            f"  output/{env_name}/world/world.db\n"
            f"  output/{env_name}/tests/test_runtime.py\n\n"
            "If rejecting, write: output/REJECTED.json\n"
            "{\"skill_id\": \"...\", \"reason\": \"...\", \"estimated_tools\": N, "
            "\"additional_skills_searched\": true/false}"
        )

    # ------------------------------------------------------------------
    # Post-validation
    # ------------------------------------------------------------------

    def _post_validate(
        self, worker_index: int, raw_skill_id: str,
        harness_backend: str = "", model: str = "", provider: str = "",
        input_tokens: int = 0, output_tokens: int = 0, total_tokens: int = 0,
    ) -> str:
        sandbox = self._sandboxes[worker_index]
        output_dir = sandbox / "output"

        # 1. Check for explicit rejection via REJECTED.json
        rejected = output_dir / "REJECTED.json"
        if rejected.exists():
            try:
                info = json.loads(rejected.read_text(encoding="utf-8"))
                reason = info.get("reason", "Unknown")
            except (json.JSONDecodeError, UnicodeDecodeError):
                reason = "Agent rejected (unparseable)"
            logger.info("Worker {}: {} rejected — {}", worker_index, raw_skill_id, reason)
            self._register_rejection(
                raw_skill_id, reason,
                harness_backend=harness_backend, model=model, provider=provider,
                input_tokens=input_tokens, output_tokens=output_tokens,
                total_tokens=total_tokens,
            )
            return "rejected"

        # 2. Check VIABILITY.json — if verdict is "reject", treat as rejection
        viability = output_dir / "VIABILITY.json"
        if viability.exists():
            try:
                vinfo = json.loads(viability.read_text(encoding="utf-8"))
                if vinfo.get("verdict") == "reject":
                    reason = vinfo.get("reasoning", "Viability assessment rejected")
                    logger.info(
                        "Worker {}: {} rejected via viability assessment — {}",
                        worker_index, raw_skill_id, reason,
                    )
                    self._register_rejection(
                        raw_skill_id, reason,
                        harness_backend=harness_backend, model=model, provider=provider,
                        input_tokens=input_tokens, output_tokens=output_tokens,
                        total_tokens=total_tokens,
                    )
                    return "rejected"
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger.warning(
                    "Worker {}: VIABILITY.json unparseable for {}",
                    worker_index, raw_skill_id,
                )

        # 3. Look for environment directories with runtime/ subdirectory
        env_dirs = [
            d for d in output_dir.iterdir()
            if d.is_dir() and (d / "runtime").exists()
        ]
        if not env_dirs:
            logger.warning("Worker {}: no output produced for {}", worker_index, raw_skill_id)
            self._register_rejection(
                raw_skill_id, "No output produced",
                harness_backend=harness_backend, model=model, provider=provider,
                input_tokens=input_tokens, output_tokens=output_tokens,
                total_tokens=total_tokens,
            )
            return "failed"

        env_dir = env_dirs[0]
        env_name = env_dir.name

        missing = [f for f in ENV_REQUIRED_FILES if not (env_dir / f).exists()]
        if missing:
            logger.warning(
                "Worker {}: {} missing files: {}",
                worker_index, raw_skill_id, missing,
            )
            self._register_rejection(
                raw_skill_id, f"Missing files: {missing}",
                harness_backend=harness_backend, model=model, provider=provider,
                input_tokens=input_tokens, output_tokens=output_tokens,
                total_tokens=total_tokens,
            )
            return "failed"

        test_ok = self._run_tests(env_dir)
        if not test_ok:
            logger.warning("Worker {}: tests failed for {}", worker_index, raw_skill_id)
            self._save_failed_output(env_dir, raw_skill_id)
            self._register_rejection(
                raw_skill_id, "Tests failed",
                harness_backend=harness_backend, model=model, provider=provider,
                input_tokens=input_tokens, output_tokens=output_tokens,
                total_tokens=total_tokens,
            )
            return "failed"

        self._finalize_success(
            env_dir, env_name, raw_skill_id,
            harness_backend=harness_backend, model=model, provider=provider,
            input_tokens=input_tokens, output_tokens=output_tokens,
            total_tokens=total_tokens,
        )
        return "synthesized"

    def _run_tests(self, env_dir: Path) -> bool:
        test_file = (env_dir / "tests" / "test_runtime.py").resolve()
        try:
            result = subprocess.run(
                ["uv", "run", "pytest", str(test_file), "-x", "-q", "--tb=short"],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(env_dir.resolve().parent),
            )
            if result.returncode != 0:
                output = result.stdout[-1000:] if result.stdout else result.stderr[-1000:]
                logger.warning("Test output:\n{}", output)
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------

    def _save_failed_output(self, env_dir: Path, raw_skill_id: str) -> None:
        """Copy failed environment to output_root/failed/ for debugging."""
        failed_dir = self.output_root / "failed" / raw_skill_id
        if failed_dir.exists():
            shutil.rmtree(failed_dir)
        try:
            shutil.copytree(env_dir, failed_dir)
            logger.info("Saved failed output to: {}", failed_dir)
        except Exception as exc:
            logger.warning("Could not save failed output: {}", exc)

    def _finalize_success(
        self, env_dir: Path, env_name: str, raw_skill_id: str,
        harness_backend: str = "", model: str = "", provider: str = "",
        input_tokens: int = 0, output_tokens: int = 0, total_tokens: int = 0,
    ) -> None:
        dest = self.env_output / env_name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(env_dir, dest)

        scripts = self.inner_skills_root / "synthesis-registry" / "scripts"

        subprocess.run(
            [
                "uv", "run", "python", str(scripts / "register_env.py"),
                str(self.env_output / "registry.db"),
                "--env-id", env_name,
                "--source-skills", raw_skill_id,
                "--env-root", str(self.env_output),
                "--harness-backend", harness_backend,
                "--model", model,
                "--provider", provider,
                "--input-tokens", str(input_tokens),
                "--output-tokens", str(output_tokens),
                "--total-tokens", str(total_tokens),
            ],
            capture_output=True, text=True, check=False,
        )

        subprocess.run(
            [
                "uv", "run", "python", str(scripts / "search_skills.py"),
                str(self.env_output / "skill_index.db"),
                "--consume", raw_skill_id,
                "--by", env_name,
            ],
            capture_output=True, text=True, check=False,
        )

        logger.info("Synthesized environment: {} from {}", env_name, raw_skill_id)

    def _register_rejection(
        self, raw_skill_id: str, reason: str,
        harness_backend: str = "", model: str = "", provider: str = "",
        input_tokens: int = 0, output_tokens: int = 0, total_tokens: int = 0,
    ) -> None:
        scripts = self.inner_skills_root / "synthesis-registry" / "scripts"

        subprocess.run(
            [
                "uv", "run", "python", str(scripts / "register_rejection.py"),
                str(self.env_output / "registry.db"),
                "--skill-id", raw_skill_id,
                "--skill-name", raw_skill_id,
                "--reason", reason,
                "--harness-backend", harness_backend,
                "--model", model,
                "--provider", provider,
                "--input-tokens", str(input_tokens),
                "--output-tokens", str(output_tokens),
                "--total-tokens", str(total_tokens),
            ],
            capture_output=True, text=True, check=False,
        )

    def _rebuild_env_index(self) -> None:
        scripts = self.inner_skills_root / "synthesis-registry" / "scripts"
        subprocess.run(
            [
                "uv", "run", "python", str(scripts / "search_environments.py"),
                str(self.env_output), "build",
            ],
            capture_output=True, text=True, check=False,
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        super().shutdown()
        for sb in self._sandboxes:
            output = sb / "output"
            if output.exists():
                shutil.rmtree(output, ignore_errors=True)
