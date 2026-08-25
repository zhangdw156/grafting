"""Simulation runner.

Consumes task files produced by TaskGeneratorRunner, loads the
corresponding packaged environments, and orchestrates multi-turn
conversations between a UserAgent and an AssistantAgent to produce
trajectory files.

Typical latency depends on the configured chat-completions endpoint.

CLI::

    uv run grafting.sim env_dir=<dir> task_dir=<dir>

Parameters:
    env_dir (required): Root directory containing packaged environments.
    task_dir (required): Directory with task files from grafting.task.generate.
    output_dir: Output directory for trajectory files (default: results/trajectories).
    workers: Concurrent simulation workers (default: $WORKERS or 1).
    max_turns: Maximum conversation turns per simulation (default: 30).

Examples::

    uv run grafting.sim \
        env_dir=artifacts/env_top30_skills \
        task_dir=results/tasks

    uv run grafting.sim \
        env_dir=artifacts/env_top30_skills \
        task_dir=results/tasks \
        output_dir=results/trajectories \
        workers=4 max_turns=20
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import hydra
from omegaconf import DictConfig

from grafting.agent import (
    AssistantAgent,
    AssistantAgentConfig,
    AssistantTurnResult,
    ChatUserAgent,
    UserAgentConfig,
)
from grafting.environment import load_module_from_path, resolve_env_contract
from grafting.runner._db_migrate import add_columns_if_missing
from grafting.runner._tracker_db import BaseTrackerDB
from grafting.runner.base import BaseRunner
from grafting.utils import (
    classify_error,
    create_progress,
    exit_with_code,
    load_settings,
    logger,
    now_iso,
    setup_logging,
)
from grafting.utils.token_usage import TokenUsage

_config_path = Path(__file__).resolve().parent.parent / "configs"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_tools_from_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    tools: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped:
            tools.append(json.loads(stripped))
    return tools


def _extract_assistant_response(result: AssistantTurnResult) -> str:
    return result.assistant_message or ""


# ---------------------------------------------------------------------------
# SimulationsDB (inline)
# ---------------------------------------------------------------------------

class SimulationsDB(BaseTrackerDB):


    def _init_table(self, conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS simulations (
                simulation_id   TEXT PRIMARY KEY,
                task_id         TEXT NOT NULL,
                env_name        TEXT NOT NULL,
                traj_file       TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'succeeded',
                message_count   INTEGER,
                turn_count      INTEGER,
                tool_call_count INTEGER,
                ended_normally  INTEGER,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                error           TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sim_env ON simulations(env_name)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sim_task ON simulations(task_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sim_status ON simulations(status)"
        )
        conn.commit()
        add_columns_if_missing(conn, "simulations", [
            ("user_model", "TEXT"),
            ("user_provider", "TEXT"),
            ("assistant_model", "TEXT"),
            ("assistant_provider", "TEXT"),
            ("error_type", "TEXT"),
            ("input_tokens", "INTEGER"),
            ("output_tokens", "INTEGER"),
            ("total_tokens", "INTEGER"),
        ])

    def insert_succeeded(
        self,
        simulation_id: str,
        task_id: str,
        env_name: str,
        traj_file: str,
        message_count: int,
        turn_count: int,
        tool_call_count: int,
        ended_normally: bool,
        user_model: str = "",
        user_provider: str = "",
        assistant_model: str = "",
        assistant_provider: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO simulations "
            "(simulation_id, task_id, env_name, traj_file, status, "
            "message_count, turn_count, tool_call_count, ended_normally, "
            "created_at, user_model, user_provider, "
            "assistant_model, assistant_provider, "
            "input_tokens, output_tokens, total_tokens) "
            "VALUES (?, ?, ?, ?, 'succeeded', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                simulation_id, task_id, env_name, traj_file,
                message_count, turn_count, tool_call_count,
                int(ended_normally), now_iso(),
                user_model, user_provider,
                assistant_model, assistant_provider,
                input_tokens, output_tokens, total_tokens,
            ),
        )
        conn.commit()

    def insert_failed(
        self,
        simulation_id: str,
        task_id: str,
        env_name: str,
        error: str,
        user_model: str = "",
        user_provider: str = "",
        assistant_model: str = "",
        assistant_provider: str = "",
        error_type: str = "permanent",
    ) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO simulations "
            "(simulation_id, task_id, env_name, traj_file, status, error, "
            "created_at, user_model, user_provider, "
            "assistant_model, assistant_provider, error_type) "
            "VALUES (?, ?, ?, '', 'failed', ?, ?, ?, ?, ?, ?, ?)",
            (
                simulation_id, task_id, env_name, error, now_iso(),
                user_model, user_provider,
                assistant_model, assistant_provider, error_type,
            ),
        )
        conn.commit()

    def has(self, simulation_id: str) -> bool:
        """Return True if the simulation already succeeded (skip it)."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM simulations WHERE simulation_id = ? "
            "AND status = 'succeeded'",
            (simulation_id,),
        ).fetchone()
        return row is not None

    def clean_failures(self) -> int:
        """Delete all failures and stale running records so items
        can be retried."""
        conn = self._get_conn()
        cur = conn.execute(
            "DELETE FROM simulations WHERE status = 'failed'"
        )
        cleaned = cur.rowcount
        cur2 = conn.execute(
            "DELETE FROM simulations WHERE status = 'running'"
        )
        cleaned += cur2.rowcount
        conn.commit()
        return cleaned

    def try_claim(self, simulation_id: str) -> bool:
        """Atomically claim a simulation slot. Returns True if claimed."""
        conn = self._get_conn()
        try:
            conn.execute(
                "INSERT INTO simulations "
                "(simulation_id, task_id, env_name, traj_file, status, "
                "created_at) "
                "VALUES (?, '', '', '', 'running', ?)",
                (simulation_id, now_iso()),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def completed_ids(self) -> set[str]:
        """Return simulation_ids that are claimed (running) or succeeded."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT simulation_id FROM simulations "
            "WHERE status = 'succeeded' "
            "OR status = 'running'"
        ).fetchall()
        return {row["simulation_id"] for row in rows}


# ---------------------------------------------------------------------------
# SimulationRunner
# ---------------------------------------------------------------------------


class SimulationRunner(BaseRunner):

    def __init__(
        self,
        *,
        env_dir: Path,
        task_dir: Path,
        output_root: Path,
        workers: int = 1,
        max_turns: int = 30,
    ) -> None:
        super().__init__(output_root=output_root, workers=workers)
        self.env_dir = env_dir.resolve()
        self.task_dir = task_dir.resolve()
        self.max_turns = max_turns

        settings = load_settings()
        if settings.simulation is None:
            raise RuntimeError(
                "settings.yaml: 'simulation' section is required. "
                "See settings.yaml.example for the expected format."
            )
        sim = settings.simulation

        self._user_endpoint = sim.user_agent.endpoint
        self._assistant_endpoint = sim.assistant_agent.endpoint

        self._tasks_db_path = self.task_dir / "tasks.sqlite3"
        self._sim_db = SimulationsDB(self.output_root / "simulations.sqlite3")

    def run(self) -> dict[str, Any]:
        stats = {"succeeded": 0, "failed": 0}

        cleaned = self._sim_db.clean_failures()
        if cleaned:
            logger.info("Cleaned {} failure(s) for retry", cleaned)

        tasks = self._discover_tasks()

        if not tasks:
            logger.info("No pending tasks found.")
            return stats

        logger.info("Found {} pending tasks", len(tasks))

        with create_progress() as progress:
            ptask = progress.add_task("Simulating", total=len(tasks))
            if self.workers <= 1:
                for task_row in tasks:
                    ok = self._simulate_one(task_row)
                    stats["succeeded" if ok else "failed"] += 1
                    progress.advance(ptask)
            else:
                with ThreadPoolExecutor(max_workers=self.workers) as pool:
                    futures = {
                        pool.submit(self._simulate_one, row): row
                        for row in tasks
                    }
                    for future in as_completed(futures):
                        try:
                            ok = future.result()
                            stats["succeeded" if ok else "failed"] += 1
                        except Exception as exc:
                            row = futures[future]
                            logger.error("Simulation crashed for {}: {}", row["task_id"], exc)
                            stats["failed"] += 1
                        progress.advance(ptask)

        logger.info(
            "Simulation complete: {} succeeded, {} failed",
            stats["succeeded"], stats["failed"],
        )
        return stats

    def _discover_tasks(self) -> list[dict[str, Any]]:
        if not self._tasks_db_path.exists():
            return []
        conn = sqlite3.connect(str(self._tasks_db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT task_id, env_name, task_file FROM tasks "
            "WHERE status = 'succeeded'"
        ).fetchall()
        conn.close()

        done = self._sim_db.completed_ids()

        result: list[dict[str, Any]] = []
        for r in rows:
            task_id = r["task_id"]
            seq_num = int(task_id.rsplit("/", 1)[-1].replace("task_", ""))
            simulation_id = f"{r['env_name']}/traj_{seq_num:03d}"
            if simulation_id not in done:
                result.append(dict(r))
        return result

    def _simulate_one(self, task_row: dict[str, Any]) -> bool:
        task_id = task_row["task_id"]
        env_name = task_row["env_name"]
        task_file = task_row["task_file"]

        task_seq = task_id.rsplit("/", 1)[-1]
        seq_num = int(task_seq.replace("task_", ""))
        simulation_id = f"{env_name}/traj_{seq_num:03d}"
        traj_file = f"{env_name}/traj_{seq_num:03d}.json"

        if self._sim_db.has(simulation_id):
            logger.info("Skipping already-simulated: {}", simulation_id)
            return True

        if not self._sim_db.try_claim(simulation_id):
            logger.debug("Skipping claimed simulation: {}", simulation_id)
            return True

        logger.info("Simulating: {} (env={})", task_id, env_name)

        try:
            task_path = self.task_dir / task_file
            task = json.loads(task_path.read_text(encoding="utf-8"))

            backend, tools, session_db = self._setup_session(
                env_name, task, simulation_id,
            )

            try:
                trajectory, provenance = self._run_conversation(
                    task_id, env_name, task, backend, tools,
                )
            finally:
                for suffix in ("", "-wal", "-shm"):
                    p = session_db.parent / (session_db.name + suffix)
                    if p.exists():
                        p.unlink(missing_ok=True)
                session_dir = session_db.parent
                if session_dir.exists() and not any(session_dir.iterdir()):
                    session_dir.rmdir()

            traj_dest = self.output_root / traj_file
            traj_dest.parent.mkdir(parents=True, exist_ok=True)
            traj_dest.write_text(
                json.dumps(trajectory, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            summary = trajectory["summary"]
            self._sim_db.insert_succeeded(
                simulation_id=simulation_id,
                task_id=task_id,
                env_name=env_name,
                traj_file=traj_file,
                message_count=summary["message_count"],
                turn_count=summary["turn_count"],
                tool_call_count=summary["tool_call_count"],
                ended_normally=summary["ended_normally"],
                user_model=provenance["user_model"],
                user_provider=provenance["user_provider"],
                assistant_model=provenance["assistant_model"],
                assistant_provider=provenance["assistant_provider"],
                input_tokens=summary.get("input_tokens", 0),
                output_tokens=summary.get("output_tokens", 0),
                total_tokens=summary.get("total_tokens", 0),
            )

            logger.info(
                "Completed: {} — {} turns, {} tool calls, ended_normally={}",
                simulation_id, summary["turn_count"],
                summary["tool_call_count"], summary["ended_normally"],
            )
            return True

        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.error("Failed: {} — {}", task_id, error_msg)
            self._sim_db.insert_failed(
                simulation_id=simulation_id,
                task_id=task_id,
                env_name=env_name,
                error=error_msg,
                error_type=classify_error(exc),
            )
            return False

    def _setup_session(
        self, env_name: str, task: dict[str, Any],
        simulation_id: str,
    ) -> tuple[Any, list[dict[str, Any]], Path]:
        env_path = self.env_dir / env_name
        contract = resolve_env_contract(env_path)

        if not contract.world_db_path.exists():
            raise FileNotFoundError(f"world.db not found: {contract.world_db_path}")
        if not contract.backend_path.exists():
            raise FileNotFoundError(f"backend.py not found: {contract.backend_path}")

        safe_id = simulation_id.replace("/", "_")
        session_dir = self.output_root / ".sessions" / safe_id
        session_dir.mkdir(parents=True, exist_ok=True)
        session_db = session_dir / "session.db"
        shutil.copy2(contract.world_db_path, session_db)

        world_setup = task.get("world_setup") or []
        if world_setup:
            conn = sqlite3.connect(str(session_db))
            for stmt in world_setup:
                conn.execute(stmt)
            conn.commit()
            conn.close()

        module = load_module_from_path(
            module_name=f"grafting_env_{env_name}",
            file_path=contract.backend_path,
        )
        backend = module.EnvironmentBackend(
            db_path=session_db,
        )

        tools = _load_tools_from_jsonl(contract.tools_jsonl_path)

        return backend, tools, session_db

    def _run_conversation(
        self,
        task_id: str,
        env_name: str,
        task: dict[str, Any],
        backend: Any,
        tools: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        user_agent = ChatUserAgent(UserAgentConfig(
            endpoint=self._user_endpoint,
            max_turns=self.max_turns,
        ))
        assistant_agent = AssistantAgent(AssistantAgentConfig(
            endpoint=self._assistant_endpoint,
        ))
        assistant_agent.create(
            tools=tools,
            tool_executor=lambda tool_name, arguments, **_: backend.call(
                tool_name, arguments,
            ),
        )

        messages: list[dict[str, Any]] = []
        turn_count = 0
        tool_call_count = 0
        ended_normally = False
        total_usage = TokenUsage()

        user_result = user_agent.start(task)
        if user_result.usage:
            total_usage = total_usage + user_result.usage
        if user_result.is_done:
            ended_normally = True
        else:
            messages.append({"role": "user", "content": user_result.message})
            assistant_result = assistant_agent.run_turn(messages=messages)
            if assistant_result.usage:
                total_usage = total_usage + assistant_result.usage
            messages.extend(assistant_result.messages)
            tool_call_count += len(assistant_result.tool_calls)
            turn_count += 1

        while not ended_normally and turn_count < self.max_turns:
            assistant_response = _extract_assistant_response(assistant_result)
            user_result = user_agent.respond(assistant_response)
            if user_result.usage:
                total_usage = total_usage + user_result.usage
            if user_result.is_done:
                ended_normally = True
                break
            messages.append({"role": "user", "content": user_result.message})

            assistant_result = assistant_agent.run_turn(messages=messages)
            if assistant_result.usage:
                total_usage = total_usage + assistant_result.usage
            messages.extend(assistant_result.messages)
            tool_call_count += len(assistant_result.tool_calls)
            turn_count += 1

        provenance = {
            "user_model": self._user_endpoint.model,
            "user_provider": self._user_endpoint.provider,
            "assistant_model": self._assistant_endpoint.model,
            "assistant_provider": self._assistant_endpoint.provider,
        }

        trajectory = {
            "task_id": task_id,
            "env_name": env_name,
            "task": task,
            "messages": messages,
            "demand": task.get("demand", ""),
            "tools": tools,
            "summary": {
                "message_count": len(messages),
                "turn_count": turn_count,
                "tool_call_count": tool_call_count,
                "ended_normally": ended_normally,
                "input_tokens": total_usage.input_tokens,
                "output_tokens": total_usage.output_tokens,
                "total_tokens": total_usage.total_tokens,
            },
        }
        return trajectory, provenance

    def shutdown(self) -> None:
        self._sim_db.close()
        sessions_dir = self.output_root / ".sessions"
        if sessions_dir.exists():
            shutil.rmtree(sessions_dir, ignore_errors=True)


@hydra.main(
    config_path=str(_config_path),
    config_name="simulation",
    version_base=None,
)
@exit_with_code
def main(cfg: DictConfig) -> int:
    setup_logging(cfg.log_level)
    env_dir = Path(cfg.env_dir).resolve()
    task_dir = Path(cfg.task_dir).resolve()
    output_dir = Path(cfg.output_dir).resolve()

    if not env_dir.exists():
        logger.error("env_dir does not exist: {}", env_dir)
        return 1
    if not task_dir.exists():
        logger.error("task_dir does not exist: {}", task_dir)
        return 1

    runner = SimulationRunner(
        env_dir=env_dir,
        task_dir=task_dir,
        output_root=output_dir,
        workers=cfg.get("workers", 1),
        max_turns=cfg.get("max_turns", 30),
    )

    try:
        stats = runner.run()
        logger.info("Stats: {}", stats)
        return 1 if stats.get("failed", 0) else 0
    except KeyboardInterrupt:
        logger.info("Interrupted by user. Progress is saved — safe to restart.")
        return 130
    finally:
        runner.shutdown()


if __name__ == "__main__":
    import sys
    sys.exit(main())
