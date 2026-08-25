"""Evaluation runner.

Consumes trajectory files produced by SimulationRunner and packaged
environments, then launches harness agents that replay tool calls,
verify demand fulfillment, check assistant grounding, and detect
user drift.  Produces evaluation JSON files with accept/reject
verdicts.

Typical latency: ~2.7 min per evaluation (claude_code harness, Claude Sonnet 4.6).

CLI::

    uv run grafting.eval env_dir=<dir> traj_dir=<dir>

Parameters:
    env_dir (required): Root directory containing packaged environments.
    traj_dir (required): Directory with trajectory files from grafting.sim.
    output_dir: Output directory for evaluation results (default: results/evaluations).
    inner_skills_root: Path to evaluation skills (default: src/grafting/inner_skills/evaluation).
    workers: Concurrent harness workers (default: $WORKERS or 1).
    sandbox_path: Sandbox base directory. null = auto (default: null).

Examples::

    uv run grafting.eval \
        env_dir=artifacts/env_top30_skills \
        traj_dir=results/trajectories

    uv run grafting.eval \
        env_dir=artifacts/env_top30_skills \
        traj_dir=results/trajectories \
        output_dir=results/evaluations \
        workers=12
"""

from __future__ import annotations

import json
import queue
import re
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import hydra
from omegaconf import DictConfig

from grafting.driver import HarnessBackend
from grafting.environment import is_valid_env_dir
from grafting.runner._db_migrate import add_columns_if_missing
from grafting.runner._tracker_db import BaseTrackerDB
from grafting.runner.harness import BaseHarnessRunner
from grafting.utils import (
    classify_error as _classify_error_str,
)
from grafting.utils import (
    create_progress,
    exit_with_code,
    load_settings,
    logger,
    now_iso,
    resolve_package_path,
    setup_logging,
    truncate_stderr,
)

_config_path = Path(__file__).resolve().parent.parent / "configs"

_TRAJ_PATTERN = re.compile(r"^traj_(\d+)\.json$")


# ---------------------------------------------------------------------------
# EvaluationsDB (inline)
# ---------------------------------------------------------------------------

class EvaluationsDB(BaseTrackerDB):


    def _init_table(self, conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS evaluations (
                evaluation_id      TEXT PRIMARY KEY,
                trajectory_id      TEXT NOT NULL,
                env_name           TEXT NOT NULL,
                eval_file          TEXT NOT NULL,
                status             TEXT NOT NULL DEFAULT 'succeeded',
                demand_fulfilled   INTEGER,
                assistant_grounded INTEGER,
                user_consistent    INTEGER,
                verdict            TEXT,
                created_at         TEXT NOT NULL DEFAULT (datetime('now')),
                error              TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_eval_env "
            "ON evaluations(env_name)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_eval_traj "
            "ON evaluations(trajectory_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_eval_status "
            "ON evaluations(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_eval_verdict "
            "ON evaluations(verdict)"
        )
        conn.commit()
        add_columns_if_missing(conn, "evaluations", [
            ("harness_backend", "TEXT"),
            ("model", "TEXT"),
            ("provider", "TEXT"),
            ("error_type", "TEXT"),
            ("input_tokens", "INTEGER"),
            ("output_tokens", "INTEGER"),
            ("total_tokens", "INTEGER"),
        ])

    def has(self, evaluation_id: str) -> bool:
        """Return True if the evaluation already succeeded (skip it)."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM evaluations WHERE evaluation_id = ? "
            "AND status = 'succeeded'",
            (evaluation_id,),
        ).fetchone()
        return row is not None

    def clean_failures(self) -> int:
        """Delete all failures so items can be retried on next run."""
        conn = self._get_conn()
        cur = conn.execute(
            "DELETE FROM evaluations WHERE status = 'failed'"
        )
        conn.commit()
        return cur.rowcount

    def insert_succeeded(
        self,
        evaluation_id: str,
        trajectory_id: str,
        env_name: str,
        eval_file: str,
        demand_fulfilled: bool,
        assistant_grounded: bool,
        user_consistent: bool,
        verdict: str,
        harness_backend: str = "",
        model: str = "",
        provider: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO evaluations "
            "(evaluation_id, trajectory_id, env_name, eval_file, status, "
            "demand_fulfilled, assistant_grounded, user_consistent, "
            "verdict, created_at, harness_backend, model, provider, "
            "input_tokens, output_tokens, total_tokens) "
            "VALUES (?, ?, ?, ?, 'succeeded', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                evaluation_id, trajectory_id, env_name, eval_file,
                int(demand_fulfilled), int(assistant_grounded),
                int(user_consistent), verdict, now_iso(),
                harness_backend, model, provider,
                input_tokens, output_tokens, total_tokens,
            ),
        )
        conn.commit()

    def insert_failed(
        self,
        evaluation_id: str,
        trajectory_id: str,
        env_name: str,
        error: str,
        harness_backend: str = "",
        model: str = "",
        provider: str = "",
        error_type: str = "permanent",
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO evaluations "
            "(evaluation_id, trajectory_id, env_name, eval_file, "
            "status, error, created_at, harness_backend, model, provider, "
            "error_type, input_tokens, output_tokens, total_tokens) "
            "VALUES (?, ?, ?, '', 'failed', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                evaluation_id, trajectory_id, env_name, error, now_iso(),
                harness_backend, model, provider, error_type,
                input_tokens, output_tokens, total_tokens,
            ),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# EvaluationRunner
# ---------------------------------------------------------------------------

class EvaluationRunner(BaseHarnessRunner):

    def __init__(
        self,
        *,
        env_dir: Path,
        traj_dir: Path,
        inner_skills_root: Path,
        **kwargs: Any,
    ) -> None:
        self.env_dir = env_dir.resolve()
        self.traj_dir = traj_dir.resolve()

        self._pre_clean_sandbox(
            kwargs.get("sandbox_path"), kwargs.get("output_root"),
        )

        super().__init__(inner_skills_root=inner_skills_root, **kwargs)

        for i in range(self.workers):
            sb = self._setup_sandbox(i)
            self._sandboxes.append(sb)
        self._repoint_agents_to_sandboxes()

        self._eval_db = EvaluationsDB(self.output_root / "evaluations.sqlite3")

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

    def _copy_trajectory(self, sandbox: Path, traj_path: Path) -> None:
        dest = sandbox / "trajectory.json"
        if dest.exists():
            dest.unlink()
        shutil.copy2(traj_path, dest)

    # ------------------------------------------------------------------
    # Trajectory discovery
    # ------------------------------------------------------------------

    def _discover_trajectories(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []

        if not self.traj_dir.exists():
            return items

        for env_dir in sorted(self.traj_dir.iterdir()):
            if not env_dir.is_dir():
                continue
            env_name = env_dir.name

            for traj_file in sorted(env_dir.iterdir()):
                match = _TRAJ_PATTERN.match(traj_file.name)
                if not match:
                    continue
                seq = int(match.group(1))
                trajectory_id = f"{env_name}/traj_{seq:03d}"
                evaluation_id = f"{env_name}/eval_{seq:03d}"

                if self._eval_db.has(evaluation_id):
                    continue

                items.append({
                    "env_name": env_name,
                    "seq": seq,
                    "trajectory_id": trajectory_id,
                    "evaluation_id": evaluation_id,
                    "traj_path": str(traj_file),
                })

        return items

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        stats: dict[str, int] = {"succeeded": 0, "failed": 0}

        cleaned = self._eval_db.clean_failures()
        if cleaned:
            logger.info("Cleaned {} failure(s) for retry", cleaned)

        items = self._discover_trajectories()

        if not items:
            logger.info("No unevaluated trajectories found.")
            return stats

        logger.info("Found {} unevaluated trajectories", len(items))

        with create_progress() as progress:
            ptask = progress.add_task("Evaluating", total=len(items))
            if self.workers <= 1:
                for i, item in enumerate(items):
                    ok = self._run_one(
                        worker_index=0,
                        env_name=item["env_name"],
                        seq=item["seq"],
                        trajectory_id=item["trajectory_id"],
                        evaluation_id=item["evaluation_id"],
                        traj_path=Path(item["traj_path"]),
                    )
                    stats["succeeded" if ok else "failed"] += 1
                    progress.advance(ptask)
            else:
                sandbox_pool: queue.Queue[int] = queue.Queue()
                for i in range(self.workers):
                    sandbox_pool.put(i)

                def _run_with_sandbox(item: dict[str, Any]) -> bool:
                    wi = sandbox_pool.get()
                    try:
                        return self._run_one(
                            worker_index=wi,
                            env_name=item["env_name"],
                            seq=item["seq"],
                            trajectory_id=item["trajectory_id"],
                            evaluation_id=item["evaluation_id"],
                            traj_path=Path(item["traj_path"]),
                        )
                    finally:
                        sandbox_pool.put(wi)

                with ThreadPoolExecutor(max_workers=self.workers) as pool:
                    futures = {
                        pool.submit(_run_with_sandbox, item): item
                        for item in items
                    }
                    for future in as_completed(futures):
                        item = futures[future]
                        try:
                            ok = future.result()
                            stats["succeeded" if ok else "failed"] += 1
                        except Exception as exc:
                            logger.error(
                                "Eval crashed for {}: {}",
                                item["evaluation_id"], exc,
                            )
                            stats["failed"] += 1
                        progress.advance(ptask)

        logger.info(
            "Evaluation complete: {} succeeded, {} failed",
            stats["succeeded"], stats["failed"],
        )
        return stats

    def _run_one(
        self,
        *,
        worker_index: int,
        env_name: str,
        seq: int,
        trajectory_id: str,
        evaluation_id: str,
        traj_path: Path,
    ) -> bool:
        env_path = self.env_dir / env_name
        if not env_path.exists() or not is_valid_env_dir(env_path):
            error = f"Environment not found or invalid: {env_name}"
            logger.warning("{}", error)
            self._eval_db.insert_failed(
                evaluation_id, trajectory_id, env_name, error,
            )
            return False

        sandbox = self._sandboxes[worker_index]
        self._clean_sandbox_output(sandbox)
        self._link_env(sandbox, env_name)
        self._copy_trajectory(sandbox, traj_path)

        try:
            traj = json.loads(traj_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            error = f"Invalid trajectory JSON: {exc}"
            logger.warning("Worker {}: {}", worker_index, error)
            self._eval_db.insert_failed(
                evaluation_id, trajectory_id, env_name, error,
            )
            return False

        task = traj.get("task", {})
        prompt = self._build_prompt(task)

        logger.info(
            "Worker {}: evaluating {} (env={})",
            worker_index, trajectory_id, env_name,
        )

        result = self.run_agent(worker_index, prompt)

        if not result.success:
            error = truncate_stderr(result.stderr) if result.stderr else "harness failed"
            if result.stdout:
                logger.debug(
                    "Worker {}: stdout for {}: {}",
                    worker_index, evaluation_id, result.stdout[-500:],
                )
            logger.warning(
                "Worker {}: harness failed for {} (exit={}): {}",
                worker_index, evaluation_id, result.exit_code, error,
            )
            self._eval_db.insert_failed(
                evaluation_id, trajectory_id, env_name, error,
                harness_backend=result.harness_backend,
                model=result.model, provider=result.provider,
                error_type=_classify_error_str(error),
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                total_tokens=result.total_tokens,
            )
            return False

        return self._post_validate(
            worker_index, env_name, seq, trajectory_id, evaluation_id,
            harness_backend=result.harness_backend,
            model=result.model, provider=result.provider,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            total_tokens=result.total_tokens,
        )

    # ------------------------------------------------------------------
    # Prompt assembly
    # ------------------------------------------------------------------

    def _build_prompt(self, task: dict[str, Any]) -> str:
        return "\n\n".join([
            self._section_role(),
            self._section_task(task),
            self._section_resources(),
            self._section_workflow(),
            self._section_output(),
        ])

    @staticmethod
    def _section_role() -> str:
        return (
            "## Role\n\n"
            "You are a trajectory evaluation agent. You evaluate the "
            "quality of a simulated multi-turn conversation between a "
            "user agent and an assistant agent.\n\n"
            "Your job is to determine three things:\n"
            "1. Was the user's demand actually fulfilled? (verified via "
            "database state after replaying tool calls)\n"
            "2. Is the assistant grounded? (no hallucinations in text, "
            "claimed operations, or tool call arguments)\n"
            "3. Is the user consistent? (no knowledge boundary violations, "
            "personality drift, AI-like language, or goal drift)"
        )

    @staticmethod
    def _section_task(task: dict[str, Any]) -> str:
        demand = task.get("demand", "")
        world_setup = task.get("world_setup", [])
        uac = task.get("user_agent_config", {})
        rule = task.get("rule", "")

        parts = ["## Task\n"]
        parts.append(f"**Demand:** {demand}\n")

        if rule:
            parts.append(f"**Rule:** {rule}\n")
            if rule == "miss_func":
                parts.append(
                    "*This task intentionally includes sub-goals that "
                    "require capabilities absent from the environment. "
                    "If the assistant correctly identified and communicated "
                    "the limitation to the user, those sub-goals should be "
                    "considered handled (not a demand_not_fulfilled failure).*\n"
                )
            elif rule == "error_recovery":
                parts.append(
                    "*This task intentionally triggers errors via world "
                    "setup. The assistant is expected to recover from "
                    "these errors. Evaluate the final outcome after "
                    "recovery, not intermediate error states. If the user "
                    "explicitly agrees to a modified approach during "
                    "recovery, the modified outcome satisfies the demand.*\n"
                )
            elif rule == "miss_param":
                parts.append(
                    "*This task intentionally omits information the "
                    "assistant needs. The assistant is expected to ask "
                    "the user or make reasonable inferences. Evaluate "
                    "whether the assistant handled the gap appropriately.*\n"
                )

        if world_setup:
            parts.append("**World Setup:**")
            for stmt in world_setup:
                parts.append(f"```sql\n{stmt}\n```")

        parts.append(f"**User Role:** {uac.get('role', '')}")
        parts.append(f"**User Personality:** {uac.get('personality', '')}")
        parts.append(
            f"**User Knowledge Boundary:** "
            f"{uac.get('knowledge_boundary', '')}"
        )

        return "\n".join(parts)

    @staticmethod
    def _section_resources() -> str:
        return (
            "## Resources\n\n"
            "All paths are relative to the project root:\n\n"
            "- Trajectory to evaluate: trajectory.json\n"
            "- Environment directory: env/\n"
            "  - Environment description: env/spec/ENVIRONMENT.md\n"
            "  - Tool definitions: env/spec/tools.jsonl\n"
            "  - Database schema: env/world/schema.sql\n"
            "  - World data: env/world/world.db\n"
            "  - Backend: env/runtime/backend.py"
        )

    @staticmethod
    def _section_workflow() -> str:
        return (
            "## Workflow\n\n"
            "Read your skills, then execute in order:\n\n"
            "1. **trajectory-replay** — run the replay script to replay "
            "all tool calls from the trajectory against a fresh "
            "environment copy. This produces output/replay_result.json "
            "and output/eval.db\n"
            "2. **demand-verification** — using the replay results and "
            "eval.db, verify whether each sub-goal in the demand was "
            "fulfilled\n"
            "3. **assistant-grounding** — check each assistant message "
            "and function_call for hallucinations (text, operation, "
            "parameter)\n"
            "4. **user-consistency** — check each user message against "
            "the user_agent_config for knowledge boundary violations, "
            "personality drift, AI-like language, and goal drift"
        )

    @staticmethod
    def _section_output() -> str:
        return (
            "## Output\n\n"
            "Write the evaluation result to: output/evaluation.json\n\n"
            "The file must contain exactly these fields:\n"
            '- "demand_fulfilled": boolean\n'
            '- "assistant_grounded": boolean\n'
            '- "user_consistent": boolean\n'
            '- "issues": array of issue objects (may be empty)\n'
            '- "verdict": "accepted" if all three booleans are true, '
            '"rejected" otherwise\n\n'
            "Each issue object must contain:\n"
            '- "type": one of "demand_not_fulfilled", '
            '"text_hallucination", "operation_hallucination", '
            '"parameter_hallucination", "knowledge_boundary_violation", '
            '"personality_drift", "ai_language", "goal_drift"\n'
            '- "turn": conversation turn number (0-indexed)\n'
            '- "message_index": index in the messages array\n'
            '- "detail": human-readable explanation'
        )

    # ------------------------------------------------------------------
    # Post-validation
    # ------------------------------------------------------------------

    def _post_validate(
        self,
        worker_index: int,
        env_name: str,
        seq: int,
        trajectory_id: str,
        evaluation_id: str,
        harness_backend: str = "",
        model: str = "",
        provider: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
    ) -> bool:
        sandbox = self._sandboxes[worker_index]
        eval_file_path = sandbox / "output" / "evaluation.json"

        if not eval_file_path.exists():
            logger.warning(
                "Worker {}: no output/evaluation.json for {}",
                worker_index, evaluation_id,
            )
            self._eval_db.insert_failed(
                evaluation_id, trajectory_id, env_name, "No output file",
                harness_backend=harness_backend, model=model, provider=provider,
                input_tokens=input_tokens, output_tokens=output_tokens,
                total_tokens=total_tokens,
            )
            return False

        try:
            evaluation = json.loads(
                eval_file_path.read_text(encoding="utf-8"),
            )
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning(
                "Worker {}: invalid JSON for {}: {}",
                worker_index, evaluation_id, exc,
            )
            self._eval_db.insert_failed(
                evaluation_id, trajectory_id, env_name,
                f"Invalid JSON: {exc}",
                harness_backend=harness_backend, model=model, provider=provider,
                input_tokens=input_tokens, output_tokens=output_tokens,
                total_tokens=total_tokens,
            )
            return False

        errors = self._validate_evaluation_schema(evaluation)
        if errors:
            error_str = "; ".join(errors)
            logger.warning(
                "Worker {}: validation failed for {}: {}",
                worker_index, evaluation_id, error_str,
            )
            self._eval_db.insert_failed(
                evaluation_id, trajectory_id, env_name, error_str,
                harness_backend=harness_backend, model=model, provider=provider,
                input_tokens=input_tokens, output_tokens=output_tokens,
                total_tokens=total_tokens,
            )
            return False

        eval_rel = f"{env_name}/eval_{seq:03d}.json"
        dest = self.output_root / eval_rel
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Recompute verdict from boolean fields — do not trust LLM output.
        computed_verdict = (
            "accepted"
            if (
                evaluation["demand_fulfilled"]
                and evaluation["assistant_grounded"]
                and evaluation["user_consistent"]
                and not evaluation.get("issues")
            )
            else "rejected"
        )
        llm_verdict = evaluation.get("verdict")
        if llm_verdict != computed_verdict:
            logger.warning(
                "Worker {}: {} — LLM verdict '{}' overridden to '{}' "
                "(df={}, ag={}, uc={}, issues={})",
                worker_index, evaluation_id, llm_verdict, computed_verdict,
                evaluation["demand_fulfilled"],
                evaluation["assistant_grounded"],
                evaluation["user_consistent"],
                len(evaluation.get("issues", [])),
            )
            evaluation["verdict"] = computed_verdict

        dest.write_text(
            json.dumps(evaluation, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        self._eval_db.insert_succeeded(
            evaluation_id=evaluation_id,
            trajectory_id=trajectory_id,
            env_name=env_name,
            eval_file=eval_rel,
            demand_fulfilled=evaluation["demand_fulfilled"],
            assistant_grounded=evaluation["assistant_grounded"],
            user_consistent=evaluation["user_consistent"],
            verdict=computed_verdict,
            harness_backend=harness_backend,
            model=model,
            provider=provider,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )

        logger.info(
            "Worker {}: {} — verdict={}",
            worker_index, evaluation_id, computed_verdict,
        )
        return True

    @staticmethod
    def _validate_evaluation_schema(
        evaluation: dict[str, Any],
    ) -> list[str]:
        errors: list[str] = []

        for field in ("demand_fulfilled", "assistant_grounded", "user_consistent"):
            if not isinstance(evaluation.get(field), bool):
                errors.append(f"{field} must be a boolean")

        verdict = evaluation.get("verdict")
        if verdict not in ("accepted", "rejected"):
            errors.append('verdict must be "accepted" or "rejected"')

        issues = evaluation.get("issues")
        if not isinstance(issues, list):
            errors.append("issues must be a list")
        else:
            for i, issue in enumerate(issues):
                if not isinstance(issue, dict):
                    errors.append(f"issues[{i}] must be an object")
                    continue
                if not isinstance(issue.get("type"), str):
                    errors.append(f"issues[{i}].type must be a string")
                if not isinstance(issue.get("detail"), str):
                    errors.append(f"issues[{i}].detail must be a string")

        return errors

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        super().shutdown()
        self._eval_db.close()
        for sb in self._sandboxes:
            output = sb / "output"
            if output.exists():
                shutil.rmtree(output, ignore_errors=True)


# ---------------------------------------------------------------------------
# Hydra entry point
# ---------------------------------------------------------------------------

@hydra.main(
    config_path=str(_config_path),
    config_name="evaluation",
    version_base=None,
)
@exit_with_code
def main(cfg: DictConfig) -> int:
    setup_logging(cfg.log_level)
    env_dir = Path(cfg.env_dir).resolve()
    traj_dir = Path(cfg.traj_dir).resolve()
    inner_skills_root = resolve_package_path(cfg.inner_skills_root)
    output_dir = Path(cfg.output_dir)
    sandbox_path = Path(cfg.sandbox_path) if cfg.get("sandbox_path") else None

    if not env_dir.exists():
        logger.error("env_dir does not exist: {}", env_dir)
        return 1
    if not traj_dir.exists():
        logger.error("traj_dir does not exist: {}", traj_dir)
        return 1
    if not inner_skills_root.exists():
        logger.error("inner_skills_root does not exist: {}", inner_skills_root)
        return 1

    settings = load_settings()
    if settings.evaluation is None:
        logger.error("settings.yaml: 'evaluation' section is required")
        return 1

    backend = HarnessBackend(settings.evaluation.backend)
    endpoint = settings.evaluation.endpoint
    timeout = settings.evaluation.timeout

    logger.info(
        "Using endpoint: model={} provider={}",
        endpoint.model,
        endpoint.provider,
    )

    logger.info(
        "Starting evaluation: env_dir={} traj_dir={} output={} "
        "workers={} backend={}",
        env_dir, traj_dir, output_dir, cfg.workers, backend.value,
    )

    runner = EvaluationRunner(
        env_dir=env_dir,
        traj_dir=traj_dir,
        inner_skills_root=inner_skills_root,
        output_root=output_dir,
        workers=cfg.workers,
        backend=backend,
        project_root=Path.cwd(),
        sandbox_path=sandbox_path,
        endpoint=endpoint,
        timeout=timeout,
    )

    try:
        stats = runner.run()
        logger.info(
            "Evaluation complete: {} succeeded, {} failed",
            stats.get("succeeded", 0),
            stats.get("failed", 0),
        )
        return 1 if stats.get("failed", 0) else 0
    except KeyboardInterrupt:
        logger.info("Interrupted by user. Progress is saved — safe to restart.")
        return 130
    finally:
        runner.shutdown()


if __name__ == "__main__":
    import sys
    sys.exit(main())
