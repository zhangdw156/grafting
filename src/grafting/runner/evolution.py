"""Environment evolution runner.

Assesses packaged environments via a smoke test (task generation →
simulation → evaluation), classifies results, dispatches harness agents
to diagnose and fix issues, then re-assesses to verify fixes.

Typical latency: ~30 min per environment (5-task assess + evolution agent + 3-task re-assess).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from grafting.driver import HarnessBackend
from grafting.environment import is_valid_env_dir
from grafting.runner._db_migrate import add_columns_if_missing
from grafting.runner.evaluation import EvaluationRunner
from grafting.runner.harness import BaseHarnessRunner
from grafting.runner.simulation import SimulationRunner
from grafting.runner.task_generator import TaskGeneratorRunner
from grafting.utils import get_package_root, load_settings, logger, now_iso, truncate_stderr

_TASK_GEN_SKILLS = get_package_root() / "inner_skills" / "task_generator"
_EVAL_SKILLS = get_package_root() / "inner_skills" / "evaluation"


# ---------------------------------------------------------------------------
# EvolutionDB — state tracking in {env_dir}/evolution.sqlite3
# ---------------------------------------------------------------------------

class EvolutionDB:

    _ALLOWED_UPDATE_COLS = frozenset({
        "reassess_accepted", "reassess_rejected",
        "input_tokens", "output_tokens", "total_tokens",
    })

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._local = threading.local()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_table(self._get_conn())

    def _get_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _init_table(self, conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS evolutions (
                env_name            TEXT PRIMARY KEY,
                classification      TEXT NOT NULL,
                action              TEXT NOT NULL,
                reason              TEXT NOT NULL,
                initial_accepted    INTEGER,
                initial_rejected    INTEGER,
                has_internal_error  INTEGER DEFAULT 0,
                evolution_status    TEXT NOT NULL DEFAULT 'pending',
                reassess_accepted   INTEGER,
                reassess_rejected   INTEGER,
                created_at          TEXT NOT NULL,
                completed_at        TEXT
            )
        """)
        conn.commit()
        add_columns_if_missing(conn, "evolutions", [
            ("harness_backend", "TEXT"),
            ("model", "TEXT"),
            ("provider", "TEXT"),
            ("input_tokens", "INTEGER"),
            ("output_tokens", "INTEGER"),
            ("total_tokens", "INTEGER"),
        ])

    def get(self, env_name: str) -> dict | None:
        row = self._get_conn().execute(
            "SELECT * FROM evolutions WHERE env_name = ?", (env_name,),
        ).fetchone()
        return dict(row) if row else None

    def is_done(self, env_name: str) -> bool:
        row = self.get(env_name)
        if row is None:
            return False
        return row["evolution_status"] in ("passed", "evolved", "skipped")

    def insert(
        self, env_name: str, classification: str, action: str, reason: str,
        initial_accepted: int, initial_rejected: int, has_internal_error: bool,
        harness_backend: str = "", model: str = "", provider: str = "",
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO evolutions "
            "(env_name, classification, action, reason, initial_accepted, "
            "initial_rejected, has_internal_error, evolution_status, created_at, "
            "harness_backend, model, provider) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
            (env_name, classification, action, reason,
             initial_accepted, initial_rejected, int(has_internal_error), now_iso(),
             harness_backend, model, provider),
        )
        conn.commit()

    def update_status(self, env_name: str, status: str, **kwargs: Any) -> None:
        conn = self._get_conn()
        sets = ["evolution_status = ?"]
        vals: list[Any] = [status]
        if status in ("evolved", "passed", "skipped", "failed"):
            sets.append("completed_at = ?")
            vals.append(now_iso())
        for k, v in kwargs.items():
            if k not in self._ALLOWED_UPDATE_COLS:
                raise ValueError(f"Unknown update column: {k!r}")
            sets.append(f"{k} = ?")
            vals.append(v)
        vals.append(env_name)
        conn.execute(
            f"UPDATE evolutions SET {', '.join(sets)} WHERE env_name = ?", vals,
        )
        conn.commit()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


# ---------------------------------------------------------------------------
# EnvironmentEvolutionRunner
# ---------------------------------------------------------------------------

class EnvironmentEvolutionRunner(BaseHarnessRunner):

    def __init__(
        self,
        *,
        env_dir: Path,
        inner_skills_root: Path,
        initial_assess_count: int = 5,
        reassess_count: int = 3,
        max_turns: int = 30,
        concurrency: int = 1,
        **kwargs: Any,
    ) -> None:
        self.env_dir = env_dir.resolve()
        self.initial_assess_count = initial_assess_count
        self.reassess_count = reassess_count
        self.max_turns = max_turns
        self.concurrency = concurrency
        self.assess_workers = kwargs.get("workers", 1)

        self._pre_clean_sandbox(
            kwargs.get("sandbox_path"), kwargs.get("output_root"),
        )
        # BaseHarnessRunner uses `workers` to create agents — we need
        # `concurrency` agents (one per concurrent environment for evolve).
        kwargs["workers"] = concurrency
        super().__init__(inner_skills_root=inner_skills_root, **kwargs)

        for i in range(self.concurrency):
            sb = self._setup_sandbox(i)
            self._sandboxes.append(sb)
        self._repoint_agents_to_sandboxes()

        self._evo_db = EvolutionDB(self.env_dir / "evolution.sqlite3")

    # ------------------------------------------------------------------
    # Sandbox
    # ------------------------------------------------------------------

    def _setup_sandbox(self, worker_index: int) -> Path:
        agent_dir = self._agent_xdg_path(worker_index)
        project = agent_dir / "project"

        for d in (project, project / "output"):
            d.mkdir(parents=True, exist_ok=True)

        self._setup_harness_skills(project)
        return project

    # ------------------------------------------------------------------
    # Main loop (per-environment concurrent dispatch)
    # ------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        stats: dict[str, int] = {
            "total": 0, "passed": 0, "evolved": 0,
            "enriched": 0, "failed": 0, "skipped": 0,
        }

        env_names = sorted(
            d.name for d in self.env_dir.iterdir()
            if d.is_dir() and is_valid_env_dir(d)
        )
        stats["total"] = len(env_names)

        if not env_names:
            logger.info("No valid environments in {}", self.env_dir)
            return stats

        logger.info("Found {} environments in {}", len(env_names), self.env_dir)

        pending = []
        for env_name in env_names:
            if self._evo_db.is_done(env_name):
                logger.info("{}: already processed, skipping", env_name)
                stats["skipped"] += 1
            else:
                pending.append(env_name)

        if not pending:
            logger.info("All environments already processed.")
            return stats

        logger.info(
            "{} environments to process (concurrency={}, assess_workers={})",
            len(pending), self.concurrency, self.assess_workers,
        )

        env_iter = iter(pending)
        lock = threading.Lock()
        processed = 0

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures: dict[Any, tuple[int, str]] = {}

            for wi in range(min(self.concurrency, len(pending))):
                env_name = next(env_iter, None)
                if env_name is None:
                    break
                futures[pool.submit(self._process_one, wi, env_name)] = (wi, env_name)

            while futures:
                done_set, _ = wait(futures, return_when=FIRST_COMPLETED)

                for future in done_set:
                    wi, env_name = futures.pop(future)
                    try:
                        outcome = future.result()
                    except Exception as exc:
                        logger.error("Worker {}: {} crashed: {}", wi, env_name, exc)
                        outcome = "failed"

                    with lock:
                        stats[outcome] = stats.get(outcome, 0) + 1
                        processed += 1

                    if processed % 5 == 0:
                        logger.info(
                            "Progress: {}/{} processed — {} passed, {} evolved, "
                            "{} enriched, {} failed",
                            processed, len(pending),
                            stats["passed"], stats["evolved"],
                            stats["enriched"], stats["failed"],
                        )

                    next_env = next(env_iter, None)
                    if next_env is not None:
                        futures[pool.submit(self._process_one, wi, next_env)] = (wi, next_env)

        logger.info(
            "Evolution complete: {} passed, {} evolved, {} enriched, {} failed",
            stats["passed"], stats["evolved"], stats["enriched"], stats["failed"],
        )
        return stats

    def _process_one(self, worker_index: int, env_name: str) -> str:
        """Process one environment through the full lifecycle.

        Returns outcome string: "passed", "evolved", "enriched", or "failed".
        """
        logger.info("Worker {}: assessing {}", worker_index, env_name)
        report = self._run_assess(worker_index, env_name, target_count=self.initial_assess_count)

        cls = self._classify(env_name, report)
        logger.info(
            "Worker {}: {} → {} — {} (accepted={}, rejected={})",
            worker_index, env_name, cls["action"], cls["reason"],
            cls.get("accepted", 0), cls.get("rejected", 0),
        )

        if cls["action"] == "pass":
            self._evo_db.update_status(env_name, "passed")
            return "passed"

        evo_result = self._evolve_one(worker_index, env_name, cls, report)

        if not evo_result.get("success"):
            self._evo_db.update_status(env_name, "failed")
            return "failed"

        logger.info("Worker {}: re-assessing {}", worker_index, env_name)
        reassess_dir = self.output_root / "assess" / env_name
        if reassess_dir.exists():
            shutil.rmtree(reassess_dir)
        reassess = self._run_assess(worker_index, env_name, target_count=self.reassess_count)
        verdicts = reassess.get("verdicts", {})
        accepted = verdicts.get("accepted", 0)
        rejected = verdicts.get("rejected", 0)
        has_ie = self._check_internal_errors(reassess)

        # Fail if zero accepted OR any INTERNAL_ERROR survives post-evolution.
        # The has_ie veto is intentional: INTERNAL_ERROR is always a backend
        # defect (wrong execution order, missing validation, etc.).  Even when
        # some tasks pass, the bug means certain tool paths are broken.  The
        # eval layer already rejects affected trajectories, so accepted count
        # alone would let a buggy environment through — has_ie catches that.
        if accepted == 0 or has_ie:
            logger.warning(
                "Worker {}: {} re-assess failed (accepted={}, has_internal_error={}), "
                "restoring backup",
                worker_index, env_name, accepted, has_ie,
            )
            backup = self.output_root / "backups" / env_name
            dest = self.env_dir / env_name
            if backup.exists():
                shutil.rmtree(dest)
                shutil.copytree(backup, dest)
            self._evo_db.update_status(
                env_name, "failed",
                reassess_accepted=accepted, reassess_rejected=rejected,
            )
            return "failed"

        self._evo_db.update_status(
            env_name, "evolved",
            reassess_accepted=accepted, reassess_rejected=rejected,
        )

        outcome = "enriched" if cls["action"] == "enrich" else "evolved"
        logger.info(
            "Worker {}: {} {} — re-assess {}/{} accepted",
            worker_index, env_name, outcome, accepted, accepted + rejected,
        )
        return outcome

    # ------------------------------------------------------------------
    # Assess (inline)
    # ------------------------------------------------------------------

    def _run_assess(self, worker_index: int, env_name: str, *, target_count: int) -> dict[str, Any]:
        env_path = self.env_dir / env_name
        assess_dir = self.output_root / "assess" / env_name

        env_root = assess_dir / ".env_root"
        env_root.mkdir(parents=True, exist_ok=True)
        env_link = env_root / env_name
        if env_link.exists() or env_link.is_symlink():
            env_link.unlink()
        env_link.symlink_to(env_path)

        tasks_dir = assess_dir / "tasks"
        traj_dir = assess_dir / "trajectories"
        eval_dir = assess_dir / "evaluations"

        settings = load_settings()

        task_settings = settings.task
        assert task_settings is not None
        task_runner = TaskGeneratorRunner(
            env_dir=env_root,
            inner_skills_root=_TASK_GEN_SKILLS.resolve(),
            target_count=target_count,
            output_root=tasks_dir,
            workers=self.assess_workers,
            backend=HarnessBackend(task_settings.backend),
            project_root=Path.cwd(),
            endpoint=task_settings.endpoint,
            timeout=task_settings.timeout,
        )
        try:
            task_stats = task_runner.run()
        finally:
            task_runner.shutdown()

        if task_stats.get("succeeded", 0) == 0:
            return {
                "verdicts": {"accepted": 0, "rejected": 0},
                "task_stats": task_stats,
                "assess_dir": str(assess_dir),
            }

        # Phase 2: Simulation
        sim_runner = SimulationRunner(
            env_dir=env_root,
            task_dir=tasks_dir,
            output_root=traj_dir,
            workers=self.assess_workers,
            max_turns=self.max_turns,
        )
        try:
            sim_stats = sim_runner.run()
        finally:
            sim_runner.shutdown()

        if sim_stats.get("succeeded", 0) == 0:
            return {
                "verdicts": {"accepted": 0, "rejected": 0},
                "task_stats": task_stats,
                "sim_stats": sim_stats,
                "assess_dir": str(assess_dir),
            }

        # Phase 3: Evaluation
        eval_settings = settings.evaluation
        assert eval_settings is not None
        eval_runner = EvaluationRunner(
            env_dir=env_root,
            traj_dir=traj_dir,
            inner_skills_root=_EVAL_SKILLS.resolve(),
            output_root=eval_dir,
            workers=self.assess_workers,
            backend=HarnessBackend(eval_settings.backend),
            project_root=Path.cwd(),
            endpoint=eval_settings.endpoint,
            timeout=eval_settings.timeout,
        )
        try:
            eval_stats = eval_runner.run()
        finally:
            eval_runner.shutdown()

        # Build report
        verdicts: dict[str, int] = {"accepted": 0, "rejected": 0}
        eval_db_path = eval_dir / "evaluations.sqlite3"
        if eval_db_path.exists():
            conn = sqlite3.connect(str(eval_db_path))
            conn.row_factory = sqlite3.Row
            for row in conn.execute(
                "SELECT verdict FROM evaluations WHERE status = 'succeeded'"
            ):
                v = row["verdict"]
                verdicts[v] = verdicts.get(v, 0) + 1
            conn.close()

        # Clean up env_root symlink
        shutil.rmtree(env_root, ignore_errors=True)

        return {
            "verdicts": verdicts,
            "task_stats": task_stats,
            "sim_stats": sim_stats,
            "eval_stats": eval_stats,
            "assess_dir": str(assess_dir),
        }

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def _classify(self, env_name: str, report: dict[str, Any]) -> dict[str, Any]:
        """Classify an environment based on initial assessment results.

        Note on INTERNAL_ERROR and sampling coverage: the initial assessment
        runs a limited number of tasks (default 5), which may not exercise
        every tool path.  An environment can score 5/5 accepted yet still
        contain latent backend bugs in untested code paths (e.g. delete
        operations with incorrect FK cleanup order).  When all tasks pass,
        the environment is classified as "too_simple" and sent to enrich.
        The reassess phase after enrichment may then generate tasks that
        expose the latent bug — this is working as intended.
        """
        verdicts = report.get("verdicts", {})
        accepted = verdicts.get("accepted", 0)
        rejected = verdicts.get("rejected", 0)
        total = accepted + rejected

        has_ie = self._check_internal_errors(report)

        if total == 0:
            cls: dict[str, str | int] = {
                "action": "fix", "reason": "no_trajectories", "category": "likely_buggy",
            }
        elif accepted == total:
            cls = {"action": "enrich", "reason": "all_accepted", "category": "too_simple"}
        elif accepted == 0:
            cls = {"action": "fix", "reason": "zero_accepted", "category": "likely_buggy"}
        elif has_ie:
            cls = {"action": "fix", "reason": "internal_errors", "category": "has_bugs"}
        else:
            cls = {"action": "pass", "reason": "acceptable_quality", "category": "ok"}

        cls["accepted"] = accepted
        cls["rejected"] = rejected

        self._evo_db.insert(
            env_name, str(cls["category"]), str(cls["action"]), str(cls["reason"]),
            accepted, rejected, has_ie,
            harness_backend=self.backend.value,
            model=self._agents[0].model if self._agents else "",
            provider=self._agents[0].provider if self._agents else "",
        )
        return cls

    def _check_internal_errors(self, report: dict[str, Any]) -> bool:
        """Scan trajectories for INTERNAL_ERROR responses from the backend.

        INTERNAL_ERROR means the backend hit an unhandled exception (per the
        environment contract, ``call()`` wraps all exceptions as INTERNAL_ERROR).
        This always indicates an environment quality issue — either a real code
        bug or missing input validation — never an assistant mistake.

        However, INTERNAL_ERROR may lurk in code paths that the initial
        assessment never exercises.  A backend can pass 5/5 tasks and still
        harbour latent bugs (e.g. a delete tool whose FK cleanup runs in the
        wrong order).  The evaluation layer independently rejects trajectories
        that hit these errors, so even environments with latent bugs can still
        produce high-quality accepted trajectories on the paths that work.

        This flag is used in two places with different severity:
        - _classify (initial): routes to "fix" action — appropriate, gives the
          evolution agent a chance to repair the backend.
        - reassess (post-evolution): vetoes the result alongside accepted==0,
          restoring the backup.  This is deliberately strict: if INTERNAL_ERROR
          survives after evolution, the environment is not ready.
        """
        assess_dir = Path(report.get("assess_dir", ""))
        traj_dir = assess_dir / "trajectories"
        if not traj_dir.exists():
            return False

        for env_dir in traj_dir.iterdir():
            if not env_dir.is_dir():
                continue
            for traj_file in env_dir.glob("traj_*.json"):
                try:
                    traj = json.loads(traj_file.read_text(encoding="utf-8"))
                    for msg in traj.get("messages", []):
                        if msg.get("role") == "function":
                            content = str(msg.get("content", ""))
                            if "INTERNAL_ERROR" in content:
                                return True
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
        return False

    def _evolve_one(
        self,
        worker_index: int,
        env_name: str,
        classification: dict[str, Any],
        assess_report: dict[str, Any],
    ) -> dict[str, Any]:
        sandbox = self._sandboxes[worker_index]
        self._clean_sandbox_output(sandbox)

        # Copy environment into sandbox (full copy for modification)
        env_sandbox = sandbox / "env"
        if env_sandbox.exists():
            shutil.rmtree(env_sandbox)
        shutil.copytree(self.env_dir / env_name, env_sandbox)

        # Copy assess artifacts into sandbox
        assess_sandbox = sandbox / "assess_artifacts"
        if assess_sandbox.exists():
            shutil.rmtree(assess_sandbox)
        self._copy_assess_artifacts(assess_report, assess_sandbox)

        # Build prompt
        prompt = self._build_prompt(env_name, classification, assess_report)

        logger.info(
            "Worker {}: evolving {} (action={})",
            worker_index, env_name, classification["action"],
        )
        self._evo_db.update_status(env_name, "in_progress")

        result = self.run_agent(worker_index, prompt)

        self._evo_db.update_status(
            env_name, "in_progress",
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            total_tokens=result.total_tokens,
        )

        if not result.success:
            error = truncate_stderr(result.stderr) if result.stderr else "harness failed"
            logger.warning(
                "Worker {}: evolution failed for {}: {}",
                worker_index, env_name, error[:300],
            )
            return {"success": False, "error": error}

        return self._post_validate(worker_index, env_name)

    def _copy_assess_artifacts(
        self, report: dict[str, Any], dest: Path,
    ) -> None:
        dest.mkdir(parents=True, exist_ok=True)

        assess_dir = Path(report.get("assess_dir", ""))
        if not assess_dir.exists():
            return

        # Copy report summary
        verdicts_file = dest / "report.json"
        verdicts_file.write_text(
            json.dumps(report, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

        # Copy trajectories
        traj_src = assess_dir / "trajectories"
        if traj_src.exists():
            traj_dest = dest / "trajectories"
            shutil.copytree(traj_src, traj_dest, dirs_exist_ok=True)

        # Copy evaluations
        eval_src = assess_dir / "evaluations"
        if eval_src.exists():
            eval_dest = dest / "evaluations"
            shutil.copytree(eval_src, eval_dest, dirs_exist_ok=True)

    def _post_validate(
        self, worker_index: int, env_name: str,
    ) -> dict[str, Any]:
        sandbox = self._sandboxes[worker_index]
        env_sandbox = sandbox / "env"

        if not is_valid_env_dir(env_sandbox):
            return {"success": False, "error": "Environment structure invalid after evolution"}

        # Copy evolved environment back, with backup
        dest = self.env_dir / env_name
        backup = self.output_root / "backups" / env_name
        if backup.exists():
            shutil.rmtree(backup)
        shutil.copytree(dest, backup)
        shutil.rmtree(dest)
        shutil.copytree(env_sandbox, dest)

        logger.info("Worker {}: {} evolved and copied back", worker_index, env_name)
        return {"success": True}

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        env_name: str,
        classification: dict[str, Any],
        assess_report: dict[str, Any],
    ) -> str:
        action = classification["action"]
        if action == "enrich":
            return self._build_enrich_prompt(env_name, classification, assess_report)
        return self._build_fix_prompt(env_name, classification, assess_report)

    def _build_fix_prompt(
        self,
        env_name: str,
        classification: dict[str, Any],
        assess_report: dict[str, Any],
    ) -> str:
        verdicts = assess_report.get("verdicts", {})
        return "\n\n".join([
            "## Role\n\n"
            "You are an environment evolution agent. Your job is to diagnose "
            "and fix quality issues in a packaged environment based on "
            "assessment results. The environment was assessed with a smoke "
            "test (task generation → simulation → evaluation) and found to "
            "have issues.",

            f"## Assessment Context\n\n"
            f"- Environment: `{env_name}`\n"
            f"- Classification: **{classification['category']}**"
            f" — {classification['reason']}\n"
            f"- Accepted: {verdicts.get('accepted', 0)},"
            f" Rejected: {verdicts.get('rejected', 0)}\n\n"
            f"The assess artifacts are in `assess_artifacts/`. The environment "
            f"files are in `env/`. You will modify files in `env/` to fix issues.",

            "## Skills\n\n"
            "Read your skills before starting. Execute them in this order:\n\n"
            "1. **assess-diagnosis** — Read assess artifacts, classify root cause\n"
            "2. **backend-bugfix** and/or **contract-alignment** — Apply targeted fixes\n"
            "3. **runtime-verification** — Verify fixes work\n\n"
            "If runtime-verification fails, go back to step 2 and fix the remaining issues.",

            "## Resources\n\n"
            "- Environment files: `env/` (spec/, runtime/, world/, tests/)\n"
            "- Assess artifacts: `assess_artifacts/` (report.json, trajectories/, evaluations/)\n"
            f"- Inner skills: `{self._native_skills_prefix()}/`\n"
            "- Output: write results to `output/`",

            "## Constraints\n\n"
            "- Apply minimal targeted fixes — do not rewrite working code\n"
            "- All changes must follow `references/contract.md`\n"
            "- Do not modify tools outside the diagnosed issue scope\n"
            "- Do not add third-party imports to backend.py\n"
            "- All seed data must be English-only",

            "## Output\n\n"
            "Write `output/evolution_result.json` with a summary of changes made.",
        ])

    def _build_enrich_prompt(
        self,
        env_name: str,
        classification: dict[str, Any],
        assess_report: dict[str, Any],
    ) -> str:
        verdicts = assess_report.get("verdicts", {})
        return "\n\n".join([
            "## Role\n\n"
            "You are an environment evolution agent. This environment passed "
            "all assessment checks (all trajectories accepted), which suggests "
            "it may be too simple to generate diverse training data. Your job "
            "is to enrich the environment with more complex seed data.",

            f"## Assessment Context\n\n"
            f"- Environment: `{env_name}`\n"
            f"- Classification: **too_simple** — all "
            f"{verdicts.get('accepted', 0)} trajectories accepted\n"
            f"- The environment works correctly but may lack data diversity.\n\n"
            f"The assess artifacts are in `assess_artifacts/`. The environment "
            f"files are in `env/`.",

            "## Skills\n\n"
            "Read your skills before starting. Execute them in this order:\n\n"
            "1. **assess-diagnosis** — Confirm the environment is indeed too simple\n"
            "2. **seed-data-enrichment** — Add rows, diversity, edge cases to world.db\n"
            "3. **runtime-verification** — Verify the enriched environment still works",

            "## Resources\n\n"
            "- Environment files: `env/` (spec/, runtime/, world/, tests/)\n"
            "- Assess artifacts: `assess_artifacts/` (report.json, trajectories/, evaluations/)\n"
            f"- Inner skills: `{self._native_skills_prefix()}/`\n"
            "- Output: write results to `output/`",

            "## Constraints\n\n"
            "- Only modify `env/world/world.db` — do not change backend.py or tools.jsonl\n"
            "- Follow the data richness rules in `references/contract.md`\n"
            "- All seed data must be English-only\n"
            "- Respect all CHECK constraints and foreign keys in schema.sql",

            "## Output\n\n"
            "Write `output/evolution_result.json` with a summary of enrichment changes.",
        ])

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        self._evo_db.close()
        super().shutdown()
