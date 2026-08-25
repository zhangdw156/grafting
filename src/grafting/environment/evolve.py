"""Environment evolution — assess, diagnose, fix, and re-assess environments.

Runs assess on packaged environments, classifies results, dispatches
harness agents to diagnose and fix issues, then re-assesses to verify.

CLI::

    uv run grafting.env.evolve env_dir=<dir>

Parameters:
    env_dir (required): Directory containing packaged environments to evolve.
    inner_skills_root: Path to evolution skills
        (default: src/grafting/inner_skills/environment_evolution).
    output_root: Run log output directory (default: results/env_evolution).
    concurrency: Number of environments to evolve concurrently (default: 1).
    workers: Harness workers per environment (default: $WORKERS or 1).
    initial_assess_count: Tasks for initial assessment (default: 5).
    reassess_count: Tasks for re-assessment after fix (default: 3).
    max_turns: Maximum conversation turns (default: 30).
    sandbox_path: Sandbox base directory. null = auto (default: null).

Examples::

    uv run grafting.env.evolve env_dir=artifacts/environments
    uv run grafting.env.evolve env_dir=artifacts/environments workers=4 concurrency=2
"""

from __future__ import annotations

import sys
from pathlib import Path

import hydra
from omegaconf import DictConfig

from grafting.driver import HarnessBackend
from grafting.runner import EnvironmentEvolutionRunner
from grafting.utils import (
    exit_with_code,
    load_settings,
    logger,
    resolve_package_path,
    setup_logging,
)

_config_path = Path(__file__).resolve().parent.parent / "configs"


@hydra.main(
    config_path=str(_config_path),
    config_name="env_evolution",
    version_base=None,
)
@exit_with_code
def main(cfg: DictConfig) -> int:
    setup_logging(cfg.log_level)
    env_dir = Path(cfg.env_dir).resolve()
    inner_skills_root = resolve_package_path(cfg.inner_skills_root)
    output_root = Path(cfg.output_root)
    sandbox_path = Path(cfg.sandbox_path) if cfg.get("sandbox_path") else None

    if not env_dir.exists():
        logger.error("env_dir does not exist: {}", env_dir)
        return 1
    if not inner_skills_root.exists():
        logger.error("inner_skills_root does not exist: {}", inner_skills_root)
        return 1

    settings = load_settings()

    for section_name in ("task", "simulation", "evaluation", "evolution"):
        if getattr(settings, section_name) is None:
            logger.error(
                "settings.yaml: '{}' section is required for evolution. "
                "See settings.yaml.example for the expected format.",
                section_name,
            )
            return 1

    evo_settings = settings.evolution
    assert evo_settings is not None
    runner = EnvironmentEvolutionRunner(
        env_dir=env_dir,
        inner_skills_root=inner_skills_root,
        initial_assess_count=cfg.initial_assess_count,
        reassess_count=cfg.reassess_count,
        max_turns=cfg.max_turns,
        concurrency=cfg.concurrency,
        output_root=output_root,
        workers=cfg.workers,
        backend=HarnessBackend(evo_settings.backend),
        project_root=Path.cwd(),
        sandbox_path=sandbox_path,
        endpoint=evo_settings.endpoint,
        timeout=evo_settings.timeout,
    )

    try:
        stats = runner.run()
        logger.info("Evolution complete: {}", stats)
        return 1 if stats.get("failed", 0) else 0
    except KeyboardInterrupt:
        logger.info("Interrupted. Progress saved to evolution.sqlite3.")
        return 130
    finally:
        runner.shutdown()


if __name__ == "__main__":
    sys.exit(main())
