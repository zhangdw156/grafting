"""Task generator runner.

Consumes packaged environments and generates task files by launching
harness agents with task generation skills. Each harness run produces
one task.json file.

The program layer samples a subgraph from the tool graph and a rule,
sets up the sandbox with environment files and skills, and launches
the harness. The harness agent creates a task constrained to the
given subgraph. Tool-coverage diversity is owned by the program layer
via SubgraphSampler.

Typical latency: ~5 min per task (codex harness, gpt-5.4).

CLI::

    uv run grafting.task.generate env_dir=<dir>

Parameters:
    env_dir (required): Root directory containing packaged environments.
    output_dir: Output directory for generated task files (default: results/tasks).
    inner_skills_root: Path to task generation skills
        (default: src/grafting/inner_skills/task_generator).
    target_count: Number of tasks to generate per environment (default: 30).
    workers: Concurrent harness workers (default: $WORKERS or 1).
    sandbox_path: Sandbox base directory. null = auto (default: null).
    high_volume: Enable high-volume mode for large-scale generation (default: false).

Examples::

    uv run grafting.task.generate \
        env_dir=artifacts/env_top30_skills/my_skill \
        target_count=30

    uv run grafting.task.generate \
        env_dir=artifacts/env_top30_skills \
        output_dir=results/tasks \
        workers=8 target_count=50
"""

from __future__ import annotations

import json
import queue
import random
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
from grafting.runner.subgraph import SamplerGraph, SubgraphSample, SubgraphSampler
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


# ---------------------------------------------------------------------------
# Rule definitions
# ---------------------------------------------------------------------------

RULE_DISTRIBUTION: dict[str, float] = {
    "normal": 0.50,
    "miss_func": 0.15,
    "miss_param": 0.15,
    "error_recovery": 0.10,
    "long_context": 0.10,
}

RULE_INSTRUCTIONS: dict[str, str] = {
    "normal": (
        "Standard task. The demand must be achievable with the "
        "available tools."
    ),
    "miss_func": (
        "The demand must ask for a capability the environment does "
        "NOT have. Verify the capability is genuinely absent from "
        "tools.jsonl. The assistant should gracefully explain the "
        "limitation."
    ),
    "miss_param": (
        "The demand should NOT directly provide key parameter values "
        "needed for tool calls. The user describes needs vaguely, "
        "forcing the assistant to ask clarifying questions or use "
        "discovery tools."
    ),
    "error_recovery": (
        "The demand should lead to a situation where the assistant's "
        "initial attempt fails (e.g., constraint violation, "
        "insufficient balance, unavailable resource). Use world_setup "
        "to create the error condition. The user then adjusts and "
        "the assistant retries successfully."
    ),
    "long_context": (
        "A complex demand that requires the assistant to perform 6+ "
        "distinct actions spanning multiple workflows across the "
        "environment."
    ),
}

_RULE_MODES = list(RULE_DISTRIBUTION.keys())
_RULE_WEIGHTS = list(RULE_DISTRIBUTION.values())


def _sample_rule() -> dict[str, str]:
    mode = random.choices(_RULE_MODES, weights=_RULE_WEIGHTS, k=1)[0]
    return {"mode": mode, "instructions": RULE_INSTRUCTIONS[mode]}


# ---------------------------------------------------------------------------
# TasksDB (inline)
# ---------------------------------------------------------------------------

class TasksDB(BaseTrackerDB):


    def _init_table(self, conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id      TEXT PRIMARY KEY,
                env_name     TEXT NOT NULL,
                task_file    TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'succeeded',
                created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                consumed     INTEGER NOT NULL DEFAULT 0,
                consumed_at  TEXT,
                error        TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_env ON tasks(env_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_consumed ON tasks(consumed)")
        conn.commit()
        add_columns_if_missing(conn, "tasks", [
            ("harness_backend", "TEXT"),
            ("model", "TEXT"),
            ("provider", "TEXT"),
            ("error_type", "TEXT"),
            ("rule", "TEXT"),
            ("subgraph_fingerprint", "TEXT"),
            ("input_tokens", "INTEGER"),
            ("output_tokens", "INTEGER"),
            ("total_tokens", "INTEGER"),
        ])

    def count_by_env(self, env_name: str) -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM tasks "
            "WHERE env_name = ? AND status = 'succeeded'",
            (env_name,),
        ).fetchone()
        return row["cnt"]

    def succeeded_seqs_for_env(self, env_name: str) -> set[int]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT task_id FROM tasks "
            "WHERE env_name = ? AND status = 'succeeded'",
            (env_name,),
        ).fetchall()
        seqs: set[int] = set()
        for row in rows:
            parts = row["task_id"].rsplit("/task_", 1)
            if len(parts) == 2:
                try:
                    seqs.add(int(parts[1]))
                except ValueError:
                    pass
        return seqs

    def delete_failed_for_env(self, env_name: str) -> int:
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM tasks WHERE env_name = ? AND status = 'failed'",
            (env_name,),
        )
        conn.commit()
        return cursor.rowcount

    def insert_succeeded(
        self, task_id: str, env_name: str, task_file: str,
        harness_backend: str = "", model: str = "", provider: str = "",
        rule: str = "", subgraph_fingerprint: str = "",
        input_tokens: int = 0, output_tokens: int = 0, total_tokens: int = 0,
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO tasks "
            "(task_id, env_name, task_file, status, created_at, "
            "harness_backend, model, provider, rule, subgraph_fingerprint, "
            "input_tokens, output_tokens, total_tokens) "
            "VALUES (?, ?, ?, 'succeeded', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, env_name, task_file, now_iso(),
             harness_backend, model, provider, rule, subgraph_fingerprint,
             input_tokens, output_tokens, total_tokens),
        )
        conn.commit()

    def insert_failed(
        self, task_id: str, env_name: str, error: str,
        harness_backend: str = "", model: str = "", provider: str = "",
        error_type: str = "permanent",
        input_tokens: int = 0, output_tokens: int = 0, total_tokens: int = 0,
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO tasks "
            "(task_id, env_name, task_file, status, error, created_at, "
            "harness_backend, model, provider, error_type, "
            "input_tokens, output_tokens, total_tokens) "
            "VALUES (?, ?, '', 'failed', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, env_name, error, now_iso(),
             harness_backend, model, provider, error_type,
             input_tokens, output_tokens, total_tokens),
        )
        conn.commit()

    def succeeded_fingerprints_for_env(
        self, env_name: str,
    ) -> list[tuple[frozenset[str], str]]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT subgraph_fingerprint, rule FROM tasks "
            "WHERE env_name = ? AND status = 'succeeded' "
            "AND subgraph_fingerprint IS NOT NULL "
            "AND subgraph_fingerprint != ''",
            (env_name,),
        ).fetchall()
        result: list[tuple[frozenset[str], str]] = []
        for row in rows:
            fp = frozenset(json.loads(row["subgraph_fingerprint"]))
            rule_mode = row["rule"] or ""
            result.append((fp, rule_mode))
        return result


# ---------------------------------------------------------------------------
# TaskGeneratorRunner
# ---------------------------------------------------------------------------

class TaskGeneratorRunner(BaseHarnessRunner):

    def __init__(
        self,
        *,
        env_dir: Path,
        inner_skills_root: Path,
        target_count: int = 30,
        high_volume: bool = False,  # when True, set target_count to 300+ for full coverage
        **kwargs: Any,
    ) -> None:
        self.env_dir = env_dir.resolve()
        self.target_count = target_count
        self.high_volume = high_volume

        self._pre_clean_sandbox(
            kwargs.get("sandbox_path"), kwargs.get("output_root"),
        )

        super().__init__(inner_skills_root=inner_skills_root, **kwargs)

        for i in range(self.workers):
            sb = self._setup_sandbox(i)
            self._sandboxes.append(sb)
        self._repoint_agents_to_sandboxes()

        self._tasks_db = TasksDB(self.output_root / "tasks.sqlite3")

    # ------------------------------------------------------------------
    # Sandbox
    # ------------------------------------------------------------------

    def _setup_sandbox(self, worker_index: int) -> Path:
        agent_dir = self._agent_xdg_path(worker_index)
        project = agent_dir / "project"

        for d in (project, project / "output"):
            d.mkdir(parents=True, exist_ok=True)

        symlinks = {
            "tasks.sqlite3": self.output_root / "tasks.sqlite3",
        }
        for name, target in symlinks.items():
            link = project / name
            if link.exists() or link.is_symlink():
                link.unlink()
            if target.exists():
                link.symlink_to(target)

        self._setup_harness_skills(project)

        return project

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        stats: dict[str, int] = {"succeeded": 0, "failed": 0, "envs": 0}

        env_names = sorted(
            d.name for d in self.env_dir.iterdir()
            if d.is_dir() and is_valid_env_dir(d)
        )

        if not env_names:
            logger.info("No valid environments found in {}", self.env_dir)
            return stats

        logger.info("Found {} environments", len(env_names))

        all_items: list[dict[str, Any]] = []

        for env_name in env_names:
            succeeded = self._tasks_db.count_by_env(env_name)
            if succeeded >= self.target_count:
                logger.info(
                    "{}: already has {}/{} tasks, skipping",
                    env_name, succeeded, self.target_count,
                )
                continue

            cleared = self._tasks_db.delete_failed_for_env(env_name)
            if cleared:
                logger.info(
                    "{}: cleared {} failed records for retry",
                    env_name, cleared,
                )

            stats["envs"] += 1
            items = self._plan_for_env(env_name)
            all_items.extend(items)

        if not all_items:
            logger.info("No tasks to generate.")
            return stats

        logger.info("Planned {} tasks across {} envs", len(all_items), stats["envs"])

        with create_progress() as progress:
            ptask = progress.add_task("Generating tasks", total=len(all_items))
            if self.workers <= 1:
                for item in all_items:
                    ok = self._run_one_item(item, worker_index=0)
                    stats["succeeded" if ok else "failed"] += 1
                    progress.advance(ptask)
            else:
                sandbox_pool: queue.Queue[int] = queue.Queue()
                for i in range(self.workers):
                    sandbox_pool.put(i)

                def _run_with_sandbox(item: dict[str, Any]) -> bool:
                    wi = sandbox_pool.get()
                    try:
                        return self._run_one_item(item, worker_index=wi)
                    finally:
                        sandbox_pool.put(wi)

                with ThreadPoolExecutor(max_workers=self.workers) as pool:
                    futures = {
                        pool.submit(_run_with_sandbox, item): item
                        for item in all_items
                    }
                    for future in as_completed(futures):
                        item = futures[future]
                        try:
                            ok = future.result()
                            stats["succeeded" if ok else "failed"] += 1
                        except Exception as exc:
                            logger.error(
                                "Task gen crashed for {}/task_{:03d}: {}",
                                item["env_name"], item["seq"], exc,
                            )
                            stats["failed"] += 1
                        progress.advance(ptask)

        logger.info(
            "Task generation complete: {} succeeded, {} failed across {} envs",
            stats["succeeded"], stats["failed"], stats["envs"],
        )
        return stats

    def _plan_for_env(
        self, env_name: str,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []

        succeeded_seqs = self._tasks_db.succeeded_seqs_for_env(env_name)
        needed_seqs = [
            s for s in range(1, self.target_count + 1)
            if s not in succeeded_seqs
        ]

        graph_path = self.env_dir / env_name / "spec" / "tool_graph.json"
        sampler: SubgraphSampler | None = None
        if graph_path.exists():
            graph = SamplerGraph.load(graph_path)
            max_anchors = 3 if self.high_volume else 2
            sampler = SubgraphSampler(graph, max_anchors=max_anchors)
            logger.info(
                "{}: loaded tool graph ({} mutations, {} edges)",
                env_name, len(graph.mutation_nodes()), len(graph.edges),
            )

        fp_round: dict[tuple[frozenset[str], str], int] = {}

        if sampler is not None:
            existing_fps = (
                self._tasks_db.succeeded_fingerprints_for_env(env_name)
            )
            for fp, rule_mode in existing_fps:
                sampler.mark_used_fingerprint(fp, rule_mode)
            if self.high_volume:
                for fp, rule_mode in existing_fps:
                    key = (fp, rule_mode)
                    fp_round[key] = fp_round.get(key, 0) + 1

        for seq in needed_seqs:
            rule = _sample_rule()

            subgraph: SubgraphSample | None = None
            if sampler is not None:
                subgraph = sampler.sample(rule_mode=rule["mode"])
                if subgraph is None:
                    sampler.reset()
                    subgraph = sampler.sample(rule_mode=rule["mode"])

            variation_round = 0
            if self.high_volume and subgraph is not None:
                key = (subgraph.fingerprint, rule["mode"])
                variation_round = fp_round.get(key, 0)
                fp_round[key] = variation_round + 1

            item: dict[str, Any] = {
                "env_name": env_name,
                "seq": seq,
                "rule": rule,
                "subgraph": subgraph,
            }
            if variation_round > 0:
                item["variation_round"] = variation_round
            items.append(item)

            if sampler is not None and subgraph is not None:
                sampler.mark_used(subgraph, rule_mode=rule["mode"])

        return items

    def _run_one_item(self, item: dict[str, Any], worker_index: int) -> bool:
        env_name = item["env_name"]
        seq = item["seq"]
        rule = item["rule"]
        subgraph = item["subgraph"]
        variation_round = item.get("variation_round", 0)

        outcome = self._run_one(worker_index, env_name, seq, rule, subgraph, variation_round)
        return outcome == "succeeded"

    def _run_one(
        self,
        worker_index: int,
        env_name: str,
        seq: int,
        rule: dict[str, str],
        subgraph: SubgraphSample | None,
        variation_round: int = 0,
    ) -> str:
        sandbox = self._sandboxes[worker_index]
        self._clean_sandbox_output(sandbox)
        self._link_env(sandbox, env_name)

        prompt = self._build_prompt(env_name, rule, subgraph, variation_round)

        anchors = (
            ",".join(subgraph.mutation_anchors) if subgraph else "none"
        )
        logger.info(
            "Worker {}: generating task {}/task_{:03d} (rule={}, anchors={})",
            worker_index, env_name, seq, rule["mode"], anchors,
        )

        result = self.run_agent(worker_index, prompt)

        if not result.success:
            error = truncate_stderr(result.stderr) if result.stderr else "harness failed"
            logger.warning(
                "Worker {}: harness failed for {}/task_{:03d}: {}",
                worker_index, env_name, seq, error,
            )
            task_id = f"{env_name}/task_{seq:03d}"
            self._tasks_db.insert_failed(
                task_id, env_name, error,
                harness_backend=result.harness_backend,
                model=result.model, provider=result.provider,
                error_type=_classify_error_str(error),
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                total_tokens=result.total_tokens,
            )
            return "failed"

        return self._post_validate(
            worker_index, env_name, seq,
            harness_backend=result.harness_backend,
            model=result.model, provider=result.provider,
            subgraph=subgraph,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            total_tokens=result.total_tokens,
        )

    # ------------------------------------------------------------------
    # Prompt assembly
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        env_name: str,
        rule: dict[str, str],
        subgraph: SubgraphSample | None,
        variation_round: int = 0,
    ) -> str:
        sections = [
            self._section_role(),
            self._section_rule(rule),
        ]
        if subgraph is not None:
            sections.append(subgraph.to_prompt_text())
        if variation_round > 0:
            sections.append(self._section_variation_round(variation_round))
        sections.extend([
            self._section_resources(env_name),
            self._section_workflow(),
        ])
        if self.high_volume:
            sections.append(self._section_conflict_relaxation())
        sections.append(self._section_output())
        return "\n\n".join(sections)

    @staticmethod
    def _section_role() -> str:
        return (
            "## Role\n\n"
            "You are a task generator. You create task.json files that "
            "drive multi-turn trajectory simulation between a user agent "
            "and an assistant agent operating in the environment described "
            "in the resources below.\n\n"
            "A task.json contains:\n"
            "- demand: a single goal description (one complex goal that "
            "may require multiple tool calls)\n"
            "- world_setup: SQL statements to initialize the environment\n"
            "- user_agent_config: persona with role, personality, and "
            "knowledge_boundary"
        )

    @staticmethod
    def _section_rule(rule: dict[str, str]) -> str:
        return (
            f"## Rule: {rule['mode']}\n\n"
            f"{rule['instructions']}"
        )

    @staticmethod
    def _section_resources(env_name: str) -> str:
        return (
            "## Environment Resources\n\n"
            "All paths are relative to the project root:\n\n"
            "- Environment description: env/spec/ENVIRONMENT.md\n"
            "- Tool definitions: env/spec/tools.jsonl\n"
            "- Tool relationship graph: env/spec/tool_graph.json\n"
            "- Database schema: env/world/schema.sql\n"
            "- World data (query via sqlite3): env/world/world.db\n"
            f"- Task pool (query via sqlite3): tasks.sqlite3 "
            f"(filter by env_name='{env_name}')"
        )

    @staticmethod
    def _section_workflow() -> str:
        return (
            "## Workflow\n\n"
            "Read your skills, then execute in order:\n\n"
            "1. **tool-graph-exploration** — analyze the tool graph, "
            "check existing task coverage, sample subgraphs "
            "(one per demand)\n"
            "2. **task-creation** — construct demands, world_setup, "
            "and user_agent_config\n"
            "3. **task-grounding-check** — verify all referenced "
            "entities exist\n"
            "4. **task-solvability-check** — verify conversation "
            "reachability\n"
            "5. **task-demand-clarity-check** — verify demand "
            "boundaries\n"
            "6. **world-setup-validation** — validate world_setup SQL\n"
            "7. **task-pool-conflict-check** — check for duplicates\n"
            "8. If conflict: **task-mutation**, then re-run steps 3-7"
        )

    @staticmethod
    def _section_variation_round(round_num: int) -> str:
        return (
            "## Variation Round\n\n"
            f"This is variation round **{round_num}** for this subgraph + rule "
            "combination. The task pool already contains tasks exercising the "
            "same mutation tools. You MUST create a meaningfully different "
            "scenario by varying at least two of:\n\n"
            "1. **Target entities** — operate on different users, resources, "
            "or records from world.db\n"
            "2. **User persona** — different role, personality, or "
            "knowledge_boundary\n"
            "3. **Business scenario** — different real-world motivation "
            "(e.g., cancellation vs. upgrade vs. correction)\n"
            "4. **World setup** — different initial state via world_setup SQL\n\n"
            "The resulting trajectory must be clearly distinguishable from "
            "earlier tasks using the same tools."
        )

    @staticmethod
    def _section_conflict_relaxation() -> str:
        return (
            "## Conflict Check Guidance (High-Volume Mode)\n\n"
            "In high-volume mode, the conflict check should be MORE "
            "PERMISSIVE than the default. Specifically:\n\n"
            "- Same mutation tool set + **different target entities** "
            "(different users, reservations, resources) → **No conflict**\n"
            "- Same mutation tool set + **different user persona** "
            "(different role or personality) → **No conflict**\n"
            "- Same mutation tool set + **different business scenario** "
            "(different real-world motivation) → **No conflict**\n"
            "- Same mutation tool set + **different world_setup** "
            "(different initial DB state) → **No conflict**\n\n"
            "Only flag a conflict when both the mutation tool set AND "
            "the core scenario (same entities, same actions, same outcome) "
            "are essentially identical."
        )

    @staticmethod
    def _section_output() -> str:
        return (
            "## Output\n\n"
            "Write the final task.json to: output/task.json\n\n"
            "The file must contain exactly these fields:\n"
            '- "demand": a single goal description string\n'
            '- "world_setup": array of SQL strings (may be empty)\n'
            '- "user_agent_config": object with "role", "personality", '
            '"knowledge_boundary"\n'
            '- "rule": the rule mode string — copy the exact value '
            'from the Rule section above (e.g. "normal", "miss_func")\n\n'
            "Do NOT include demands (plural), init_context, or any other fields."
        )

    # ------------------------------------------------------------------
    # Post-validation
    # ------------------------------------------------------------------

    def _post_validate(
        self, worker_index: int, env_name: str, seq: int,
        harness_backend: str = "", model: str = "", provider: str = "",
        subgraph: SubgraphSample | None = None,
        input_tokens: int = 0, output_tokens: int = 0, total_tokens: int = 0,
    ) -> str:
        sandbox = self._sandboxes[worker_index]
        task_file_path = sandbox / "output" / "task.json"
        task_id = f"{env_name}/task_{seq:03d}"

        if not task_file_path.exists():
            logger.warning("Worker {}: no output/task.json for {}", worker_index, task_id)
            self._tasks_db.insert_failed(
                task_id, env_name, "No output file",
                harness_backend=harness_backend, model=model, provider=provider,
                input_tokens=input_tokens, output_tokens=output_tokens,
                total_tokens=total_tokens,
            )
            return "failed"

        try:
            task = json.loads(task_file_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("Worker {}: invalid JSON for {}: {}", worker_index, task_id, exc)
            self._tasks_db.insert_failed(
                task_id, env_name, f"Invalid JSON: {exc}",
                harness_backend=harness_backend, model=model, provider=provider,
                input_tokens=input_tokens, output_tokens=output_tokens,
                total_tokens=total_tokens,
            )
            return "failed"

        errors = self._validate_task_schema(task)
        if errors:
            error_str = "; ".join(errors)
            logger.warning(
                "Worker {}: validation failed for {}: {}",
                worker_index, task_id, error_str,
            )
            self._tasks_db.insert_failed(
                task_id, env_name, error_str,
                harness_backend=harness_backend, model=model, provider=provider,
                input_tokens=input_tokens, output_tokens=output_tokens,
                total_tokens=total_tokens,
            )
            return "failed"

        task_rel = f"{env_name}/task_{seq:03d}.json"
        dest = self.output_root / task_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(task_file_path, dest)

        fp_json = ""
        if subgraph is not None:
            fp_json = json.dumps(sorted(subgraph.fingerprint))

        self._tasks_db.insert_succeeded(
            task_id, env_name, task_rel,
            harness_backend=harness_backend, model=model, provider=provider,
            rule=task.get("rule", ""),
            subgraph_fingerprint=fp_json,
            input_tokens=input_tokens, output_tokens=output_tokens,
            total_tokens=total_tokens,
        )
        logger.info(
            "Worker {}: task {} succeeded",
            worker_index, task_id,
        )
        return "succeeded"

    @staticmethod
    def _validate_task_schema(task: dict[str, Any]) -> list[str]:
        errors: list[str] = []

        demand = task.get("demand")
        if not isinstance(demand, str) or not demand.strip():
            errors.append("demand must be a non-empty string")

        if "init_context" in task:
            errors.append("init_context is deprecated and must not be present")

        rule = task.get("rule")
        if not isinstance(rule, str) or rule not in RULE_DISTRIBUTION:
            errors.append(
                f"rule must be one of {list(RULE_DISTRIBUTION.keys())}"
            )

        world_setup = task.get("world_setup")
        if world_setup is not None:
            if not isinstance(world_setup, list):
                errors.append("world_setup must be a list")
            elif not all(isinstance(s, str) for s in world_setup):
                errors.append("world_setup items must be strings")

        uac = task.get("user_agent_config")
        if not isinstance(uac, dict):
            errors.append("user_agent_config must be an object")
        else:
            for key in ("role", "personality", "knowledge_boundary"):
                if not isinstance(uac.get(key), str) or not uac[key].strip():
                    errors.append(f"user_agent_config.{key} is required")

        return errors

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        super().shutdown()
        self._tasks_db.close()
        for sb in self._sandboxes:
            output = sb / "output"
            if output.exists():
                shutil.rmtree(output, ignore_errors=True)


# ---------------------------------------------------------------------------
# Hydra entry point
# ---------------------------------------------------------------------------

@hydra.main(
    config_path=str(_config_path),
    config_name="task_generation",
    version_base=None,
)
@exit_with_code
def main(cfg: DictConfig) -> int:
    setup_logging(cfg.log_level)
    env_dir = Path(cfg.env_dir).resolve()
    inner_skills_root = resolve_package_path(cfg.inner_skills_root)
    output_dir = Path(cfg.output_dir)
    sandbox_path = Path(cfg.sandbox_path) if cfg.get("sandbox_path") else None

    if not env_dir.exists():
        logger.error("env_dir does not exist: {}", env_dir)
        return 1
    if not inner_skills_root.exists():
        logger.error("inner_skills_root does not exist: {}", inner_skills_root)
        return 1

    settings = load_settings()
    if settings.task is None:
        logger.error("settings.yaml: 'task' section is required")
        return 1

    backend = HarnessBackend(settings.task.backend)
    endpoint = settings.task.endpoint
    timeout = settings.task.timeout

    logger.info(
        "Using endpoint: model={} provider={}",
        endpoint.model,
        endpoint.provider,
    )

    logger.info(
        "Starting task generation: env_dir={} output={} target={} workers={} backend={}",
        env_dir, output_dir, cfg.target_count, cfg.workers, backend.value,
    )

    runner = TaskGeneratorRunner(
        env_dir=env_dir,
        inner_skills_root=inner_skills_root,
        target_count=cfg.target_count,
        high_volume=cfg.get("high_volume", False),
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
            "Task generation complete: {} succeeded, {} failed across {} envs",
            stats.get("succeeded", 0),
            stats.get("failed", 0),
            stats.get("envs", 0),
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
