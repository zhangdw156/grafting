"""Shared progress bar factory for CLI commands.

All bars use the :data:`~grafting.utils.logging.console` instance so that
loguru messages appear above the bar instead of corrupting it.
"""

from __future__ import annotations

from typing import Any

from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from grafting.utils.logging import console


def create_progress(**kwargs: Any) -> Progress:
    """Return a pre-configured :class:`~rich.progress.Progress` bar."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TextColumn("eta"),
        TimeRemainingColumn(),
        console=console,
        **kwargs,
    )
