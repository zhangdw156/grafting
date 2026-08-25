"""Loguru logging with Hydra integration and rich console output.

Console output is routed through a shared :class:`rich.console.Console`
so that log messages coordinate with ``rich.progress`` bars (messages
appear above the bar instead of corrupting it).  File output uses plain
text.  Standard-library ``logging`` is intercepted so that third-party
libraries (httpx, OpenAI SDK, Anthropic SDK, …) flow through the same pipeline.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from loguru import logger
from rich.console import Console

console = Console(stderr=True, highlight=False)

_FMT = (
    "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | "
    "{name}:{function}:{line} - {message}"
)


class _InterceptHandler(logging.Handler):
    """Route stdlib ``logging`` records into loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = sys._getframe(6), 6
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back  # type: ignore[assignment]
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage(),
        )


def setup_logging(log_level: str = "INFO") -> None:
    """Configure loguru for console + file output.

    Args:
        log_level: Console log level (default ``"INFO"``).
            File sink always uses ``"DEBUG"``.
    """
    logger.remove()

    logger.add(
        lambda msg: console.print(msg, end="", highlight=False, markup=False),
        format=_FMT,
        level=log_level,
        colorize=False,
    )

    try:
        from hydra.core.hydra_config import HydraConfig
        hcfg = HydraConfig.get()
        output_dir = Path(hcfg.runtime.output_dir)
        job_name = hcfg.job.name
    except Exception:
        output_dir = None
        job_name = None

    if output_dir is not None:
        log_path = output_dir / f"{job_name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(log_path),
            format=_FMT,
            level="DEBUG",
            rotation="10 MB",
        )

    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
