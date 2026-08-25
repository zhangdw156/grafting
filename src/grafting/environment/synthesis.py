"""Environment synthesis — convert raw skills into packaged environments.

CLI::

    uv run grafting.env.synthesis raw_skills_root=<dir> env_output=<dir>

Parameters:
    raw_skills_root (required): Root directory containing raw skill scripts.
    env_output (required): Output directory for packaged environments.
    inner_skills_root: Path to synthesis skills
        (default: src/grafting/inner_skills/environment_synthesis).
    output_root: Run log output directory (default: results/environment_synthesis).
    workers: Concurrent harness workers (default: $WORKERS or 1).
    sandbox_path: Sandbox base directory. null = auto (default: null).

Examples::

    uv run grafting.env.synthesis \
        raw_skills_root=/path/to/grafting-skills-scripts/github \
        env_output=/path/to/grafting-environments

    uv run grafting.env.synthesis \
        raw_skills_root=artifacts/skills \
        env_output=artifacts/environments \
        workers=4
"""
from __future__ import annotations

import sys
from pathlib import Path

import hydra
from omegaconf import DictConfig

from grafting.driver import HarnessBackend
from grafting.runner import EnvironmentSynthesisRunner
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
    config_name="environment_synthesis",
    version_base=None,
)
@exit_with_code
def main(cfg: DictConfig) -> int:
    """Hydra entry point for environment synthesis."""
    setup_logging(cfg.log_level)
    raw_skills_root = Path(cfg.raw_skills_root).resolve()
    env_output = Path(cfg.env_output).resolve()
    inner_skills_root = resolve_package_path(cfg.inner_skills_root)
    output_root = Path(cfg.output_root)
    sandbox_path = Path(cfg.sandbox_path) if cfg.get("sandbox_path") else None

    if not raw_skills_root.exists():
        logger.error("raw_skills_root does not exist: {}", raw_skills_root)
        return 1
    if not inner_skills_root.exists():
        logger.error("inner_skills_root does not exist: {}", inner_skills_root)
        return 1

    env_output.mkdir(parents=True, exist_ok=True)

    settings = load_settings()
    if settings.environment is None:
        logger.error(
            "settings.yaml: 'environment' section is required. "
            "See settings.yaml.example for the expected format."
        )
        return 1

    env_settings = settings.environment
    backend = HarnessBackend(env_settings.backend)

    logger.info(
        "Starting environment synthesis: raw_skills={} env_output={} "
        "workers={} backend={}",
        raw_skills_root, env_output, cfg.workers, backend.value,
    )

    runner = EnvironmentSynthesisRunner(
        raw_skills_root=raw_skills_root,
        env_output=env_output,
        inner_skills_root=inner_skills_root,
        output_root=output_root,
        workers=cfg.workers,
        backend=backend,
        project_root=Path.cwd(),
        sandbox_path=sandbox_path,
        endpoint=env_settings.endpoint,
        timeout=env_settings.timeout,
    )

    try:
        stats = runner.run()
        logger.info(
            "Synthesis complete: {} synthesized, {} rejected, {} failed "
            "in {} batches",
            stats.get("synthesized", 0),
            stats.get("rejected", 0),
            stats.get("failed", 0),
            stats.get("batches", 0),
        )
        return 1 if stats.get("failed", 0) else 0
    except KeyboardInterrupt:
        logger.info("Interrupted by user. Progress is saved — safe to restart.")
        return 130
    finally:
        runner.shutdown()


if __name__ == "__main__":
    sys.exit(main())
